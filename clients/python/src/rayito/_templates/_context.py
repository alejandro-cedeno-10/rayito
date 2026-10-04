"""`ContextHasher` (dominio puro, salvo la lectura de los ficheros que el
`TemplateSpec` nombra, que es E/S local, no de red): resuelve qué ficheros
locales entran en el contexto de build y calcula un hash determinista de su
contenido, usado como componente de la clave S3 del artefacto (igual que
`cli/_publish.py` `artifact_key`: reusar una versión exige artefacto y
configuración idénticos).

Divergencia documentada: el hash es determinista y propio (sha256 sobre
`(ruta, contenido)` ordenado), **no** se ha verificado bit a bit contra el
`calculate_files_hash`/`filesHash` real de E2B (su código no está vendido
aquí); sirve para decidir reuso dentro de Rayito, no para interoperar con
un `.e2b`/`filesHash` ajeno.
"""

from __future__ import annotations

import errno
import hashlib
import os
import warnings
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Final

from rayito._templates._dockerignore import DOCKERIGNORE_FILENAME, DockerIgnore
from rayito._templates._instructions import CopyStep
from rayito.exceptions import BuildException

__all__ = [
    "DOCKERIGNORE_FILENAME",
    "DockerIgnore",
    "collect_context_files",
    "files_hash",
    "sensitive_paths",
]

CONTEXT_PATH_OUTSIDE: Final = "context_path_outside"
#: Rutas que casi siempre llevan secretos o historia que no debería acabar
#: en una imagen (credenciales de `.env`, `.git` con remotos con token,
#: `.aws`/`.ssh`, claves privadas). Sólo avisan, no excluyen: el
#: `.dockerignore` del usuario manda. Mismo texto que
#: `SENSITIVE_PATTERNS` de `templates/context.ts`.
SENSITIVE_PATTERNS: Final = "**/.env\n**/.env.*\n**/.git\n**/.aws\n**/.ssh\n**/*.pem\n**/*.key\n"
#: Cuántas rutas sensibles se nombran en el aviso (el resto sólo se cuenta).
SENSITIVE_SAMPLE_SIZE: Final = 3
SENSITIVE_CONTEXT_WARNING: Final = (
    "el contexto de build empaqueta {count} fichero(s) que suelen llevar secretos "
    "(p. ej. {sample}): exclúyelos en {dockerignore} si no deben acabar en la imagen, "
    "donde cualquier código del sandbox puede leerlos"
)
#: La profundidad hasta `Template.build` cambia entre sync, async y CLI: el
#: aviso apunta al llamador de `collect_context_files`, que es estable.
SENSITIVE_WARNING_STACKLEVEL: Final = 3
_SENSITIVE: Final = DockerIgnore.from_text(SENSITIVE_PATTERNS)
#: `O_NOFOLLOW` no existe en Windows; ahí basta con `is_symlink()`.
_OPEN_NO_FOLLOW: Final = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)


def sensitive_paths(relpaths: Sequence[str]) -> tuple[str, ...]:
    """Las rutas de `relpaths` que casan con `SENSITIVE_PATTERNS`."""
    return tuple(relpath for relpath in relpaths if _SENSITIVE.matches(relpath))


def _warn_sensitive(relpaths: Sequence[str]) -> None:
    found = sensitive_paths(relpaths)
    if not found:
        return
    warnings.warn(
        SENSITIVE_CONTEXT_WARNING.format(
            count=len(found),
            sample=", ".join(found[:SENSITIVE_SAMPLE_SIZE]),
            dockerignore=DOCKERIGNORE_FILENAME,
        ),
        UserWarning,
        stacklevel=SENSITIVE_WARNING_STACKLEVEL,
    )


def _changed_during_read(root: Path, path: Path) -> BuildException:
    return BuildException(
        f"la ruta de contexto {path.relative_to(root).as_posix()!r} cambió o sale del "
        "contexto de build durante la lectura",
        reason=CONTEXT_PATH_OUTSIDE,
    )


def _read_contained(root: Path, path: Path) -> bytes:
    """Lee `path` sin seguir un enlace en el último componente
    (`O_NOFOLLOW`, que falla con `ELOOP`) y sólo si sigue resolviendo dentro
    de `root`: defensa en profundidad por si el árbol cambia entre el
    recorrido y la lectura. Espejo de `readContained` del SDK de
    TypeScript."""
    try:
        path.resolve().relative_to(root)
    except ValueError as exc:
        raise _changed_during_read(root, path) from exc
    try:
        descriptor = os.open(path, _OPEN_NO_FOLLOW)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise _changed_during_read(root, path) from exc
        raise
    with os.fdopen(descriptor, "rb") as handle:
        return handle.read()


def _walk_regular_files(directory: Path) -> Iterator[Path]:
    """Los ficheros regulares bajo `directory`, sin seguir nunca un enlace
    simbólico (ni a fichero ni a directorio), como Docker y
    `listFilesRecursively` del SDK de TypeScript: un `config ->
    ~/.aws/credentials` dentro del contexto no se lee ni se empaqueta."""
    for entry in sorted(directory.iterdir()):
        if entry.is_symlink():
            continue
        if entry.is_dir():
            yield from _walk_regular_files(entry)
        elif entry.is_file():
            yield entry


def _iter_files(root: Path, source: Path) -> Iterator[Path]:
    if source.is_dir():
        yield from _walk_regular_files(source)
    elif source.is_file():
        yield source
    else:
        raise BuildException(
            f"la ruta de contexto no existe: {source.relative_to(root)!s}",
            reason="context_path_missing",
        )


def _ensure_contained(root: Path, source: Path, src: str) -> None:
    """Rechaza un `CopyStep.src` que resuelve fuera de `root` (un `../..`,
    o un `src` que es él mismo un enlace simbólico que apunta fuera): sin
    esto, `Template.build()` leería y empaquetaría ficheros ajenos al
    contexto declarado. Sólo mira el `src` de primer nivel, ya resuelto (un
    `src` que es un enlace a un fichero de dentro del contexto se lee, como
    el `COPY` de Docker); los enlaces que haya dentro de un directorio
    copiado los omite `_walk_regular_files`, y `_read_contained` vuelve a
    comprobar la contención de cada fichero antes de leerlo."""
    try:
        source.relative_to(root)
    except ValueError as exc:
        raise BuildException(
            f"la ruta de contexto {src!r} sale del contexto de build ({root})",
            reason=CONTEXT_PATH_OUTSIDE,
        ) from exc


def collect_context_files(
    context_dir: Path, copies: Sequence[CopyStep], *, ignore: DockerIgnore | None = None
) -> tuple[tuple[str, bytes], ...]:
    """`(ruta_relativa_al_contexto, contenido)`, ordenado y sin duplicados,
    para cada `CopyStep.src` bajo `context_dir`; filtrado por `ignore`
    (por defecto, `context_dir/.dockerignore` si existe). `context_dir` se
    resuelve una sola vez (`Path().resolve()` incluido: el valor por
    defecto de `Template.build()`) para que tanto la comprobación de
    contención como `relative_to` comparen dos rutas absolutas."""
    resolved_root = context_dir.resolve()
    resolved_ignore = (
        ignore
        if ignore is not None
        else DockerIgnore.from_file(resolved_root / DOCKERIGNORE_FILENAME)
    )
    seen: dict[str, bytes] = {}
    for step in copies:
        source = (resolved_root / step.src).resolve()
        _ensure_contained(resolved_root, source, step.src)
        for path in _iter_files(resolved_root, source):
            relpath = path.relative_to(resolved_root).as_posix()
            if resolved_ignore.matches(relpath):
                continue
            seen[relpath] = _read_contained(resolved_root, path)
    _warn_sensitive(tuple(seen))
    return tuple(sorted(seen.items()))


def files_hash(entries: Sequence[tuple[str, bytes]]) -> str:
    """sha256 hexadecimal sobre `entries`: el mismo conjunto de ficheros con
    el mismo contenido siempre da el mismo hash, sin importar en qué orden
    se pasen (se ordenan aquí por ruta antes de acumular)."""
    digest = hashlib.sha256()
    for relpath, content in sorted(entries):
        digest.update(relpath.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(content).digest())
    return digest.hexdigest()
