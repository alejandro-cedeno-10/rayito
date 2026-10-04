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

import fnmatch
import hashlib
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Final

from rayito._templates._instructions import CopyStep
from rayito.exceptions import BuildException

#: Nombre del fichero de exclusión, compatible con `.dockerignore` de Docker
#: (patrones glob, uno por línea, `#` comenta, `!` niega, sin soporte de
#: `**` multi-segmento: basta para los casos reales de un contexto de
#: template).
DOCKERIGNORE_FILENAME: Final = ".dockerignore"


class DockerIgnore:
    """Patrones de `.dockerignore`: `matches(relpath)` dice si excluir esa
    ruta relativa (con `/` como separador, igual que Docker)."""

    __slots__ = ("_patterns",)

    def __init__(self, patterns: Sequence[tuple[str, bool]]) -> None:
        #: `(patrón, es_negación)`, en el orden del fichero: el último que
        #: haga match gana (semántica de Docker).
        self._patterns = tuple(patterns)

    @classmethod
    def from_text(cls, text: str) -> DockerIgnore:
        patterns: list[tuple[str, bool]] = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            negated = line.startswith("!")
            pattern = line[1:] if negated else line
            patterns.append((pattern.strip("/"), negated))
        return cls(patterns)

    @classmethod
    def from_file(cls, path: Path) -> DockerIgnore:
        if not path.is_file():
            return cls(())
        return cls.from_text(path.read_text(encoding="utf-8"))

    def matches(self, relpath: str) -> bool:
        excluded = False
        for pattern, negated in self._patterns:
            if fnmatch.fnmatch(relpath, pattern) or fnmatch.fnmatch(relpath, f"{pattern}/*"):
                excluded = not negated
        return excluded


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
    o un `src` que es él mismo un enlace simbólico que escapa): sin esto,
    `Template.build()` leería y empaquetaría ficheros ajenos al contexto
    declarado. Sólo mira el `src` de primer nivel; los enlaces que haya
    dentro de un directorio copiado los omite `_walk_regular_files`."""
    try:
        source.relative_to(root)
    except ValueError as exc:
        raise BuildException(
            f"la ruta de contexto {src!r} sale del contexto de build ({root})",
            reason="context_path_outside",
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
            seen[relpath] = path.read_bytes()
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
