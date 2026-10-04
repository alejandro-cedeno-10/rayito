"""Deja el PR de release-please listo para fusionar, con un solo comando.

release-please mueve las versiones de los manifiestos, pero cuatro cosas
quedan siempre a mano (lo aprendido en 0.3.0, 0.3.1, 0.3.2 y 0.5.1):

1. **Lockfiles**: su actualizador TOML no encuentra los paquetes del
   workspace en `Cargo.lock` ("No entries modified") y no toca
   `clients/python/uv.lock`, así que `cargo test --locked` y los tests de
   versión fallan en el PR.
2. **CHANGELOG**: añade su propio bloque `## [x.y.z](compare) (fecha)` encima
   de `## [Unreleased]`, con notas generadas de los títulos de commit. Aquí
   se escriben a mano en `## [Unreleased]` durante el ciclo; el bloque
   generado se descarta, `## [Unreleased]` pasa a ser `## [x.y.z] - fecha`
   (Keep a Changelog), fusionando las secciones `### ...` repetidas, y los
   enlaces de comparación del pie (`[Unreleased]:`, `[x.y.z]:`) se ponen al
   día.
3. **Firma**: el ruleset de `main` exige commits firmados y los que crea
   release-please por la API no lo están. Se rehace el PR como un único
   commit firmado (`git commit -S -s`) y se sube con `--force-with-lease`.
4. **Rama atrasada**: release-please no rehace su rama en cada push a
   `main`, así que su commit puede colgar de un `main` anterior. Copiar el
   árbol de esa rama revierte en silencio lo fusionado después (0.5.1
   deshizo así un PR entero). El commit se construye **desde el árbol de
   `origin/main`**: de la rama de release-please sólo se toma el número de
   versión de su manifiesto, y los ficheros de versión se derivan de
   `release-please-config.json` (manifiesto, fichero propio de cada
   `release-type` y `extra-files`). Antes de commitear, una guarda exige que
   `git diff --name-only origin/main` quede dentro de ese conjunto (más
   lockfiles y CHANGELOG), y cada fichero de versión que `main` no tocó
   desde la base de release-please debe quedar idéntico al de su rama; si
   no, aborta sin tocar el remoto.

Uso (desde la raíz del repositorio, con la firma SSH configurada):

    python scripts/prepare_release_pr.py            # prepara, firma y sube
    python scripts/prepare_release_pr.py --dry-run  # sólo muestra el diff
    python scripts/prepare_release_pr.py --trailer "Co-Authored-By: ..."

Los trailers (`--trailer`, repetible, o `RELEASE_PR_TRAILERS` con uno por
línea) van al final del mensaje, antes del `Signed-off-by`.

`make release-pr` es el mismo comando. No publica nada: la release sigue
empezando al fusionar el PR (`docs/RELEASING.md` §6)."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REMOTE = "origin"
MAIN_REF = f"{REMOTE}/main"
RELEASE_BRANCH = "release-please--branches--main"
RELEASE_REF = f"{REMOTE}/{RELEASE_BRANCH}"
CONFIG = "release-please-config.json"
MANIFEST = ".release-please-manifest.json"
DEFAULT_CHANGELOG = "CHANGELOG.md"
CARGO_LOCK = "Cargo.lock"
UV_LOCK = "uv.lock"
COMPAT_TABLE = "clients/python/src/rayito/cli/_compat.py"
UNRELEASED = "## [Unreleased]\n"
UNRELEASED_LINK = re.compile(
    r"^\[Unreleased\]: (?P<base>\S+)/compare/(?P<previous>\S+)\.\.\.HEAD$",
    re.MULTILINE,
)
GENERIC_MARKER = "x-release-please-version"
SEMVER = r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?"
TRAILER = re.compile(r"^[A-Za-z][A-Za-z0-9-]*: \S.*$")
TRAILERS_ENV = "RELEASE_PR_TRAILERS"
COMMIT_FLAGS = ("-S", "-s")
EXIT_ABORT = 2

# Fichero de versión propio de cada `release-type` de release-please (los de
# `extra-files` se suman aparte). `simple` usa `version.txt` sólo si existe.
RELEASE_TYPE_FILES = {
    "python": ("pyproject.toml", "toml", "$.project.version"),
    "node": ("package.json", "json", "$.version"),
    "simple": ("version.txt", "text", ""),
}
OPTIONAL_RELEASE_TYPE_FILES = frozenset({"simple"})
# `release-type` cuyo lockfile se regenera con una herramienta externa.
LOCK_COMMANDS = {"python": (("uv", "lock", "--quiet"), UV_LOCK)}

TOML_TABLE_KEY = re.compile(
    r"^\$\.(?P<table>[A-Za-z0-9_.-]+)\.(?P<key>[A-Za-z0-9_-]+)$"
)
TOML_ARRAY_ITEM = re.compile(
    r"^\$\.(?P<array>[A-Za-z0-9_-]+)\[\?\(@\.(?P<field>[A-Za-z0-9_-]+)=='(?P<value>[^']+)'\)\]"
    r"\.(?P<key>[A-Za-z0-9_-]+)$"
)


class ReleaseError(Exception):
    """Un paso de la preparación no puede seguir; nada se ha subido."""


@dataclass(frozen=True)
class VersionFile:
    """Un fichero cuya versión hay que mover: `kind` es el actualizador de
    release-please (`generic`, `toml`, `json`, `text`) y `jsonpath`, cuando
    aplica, la clave dentro del fichero."""

    path: str
    kind: str
    jsonpath: str = ""


@dataclass(frozen=True)
class Package:
    path: str
    release_type: str
    component: str
    changelog: str


@dataclass(frozen=True)
class ReleasePlan:
    """Todo lo que la release toca, derivado de la configuración."""

    packages: tuple[Package, ...]
    version_files: tuple[VersionFile, ...]
    tag_format: str

    @property
    def changelogs(self) -> tuple[str, ...]:
        return tuple(package.changelog for package in self.packages)

    @property
    def lockfiles(self) -> tuple[str, ...]:
        return tuple(
            _join(package.path, LOCK_COMMANDS[package.release_type][1])
            for package in self.packages
            if package.release_type in LOCK_COMMANDS
        )

    @property
    def allowed_paths(self) -> frozenset[str]:
        return frozenset((MANIFEST, CARGO_LOCK, *self.changelogs, *self.lockfiles)) | {
            entry.path for entry in self.version_files
        }

    def tag(self, component: str, version: str) -> str:
        return self.tag_format.format(component=component, version=version)


def _join(package_path: str, file_path: str) -> str:
    """Ruta de un fichero de release-please: `/x` es relativo a la raíz del
    repositorio y `x`, al paquete."""
    if file_path.startswith("/"):
        return file_path.lstrip("/")
    if package_path in ("", "."):
        return file_path
    return f"{package_path}/{file_path}"


# --- plan --------------------------------------------------------------------


def release_plan(config: dict, root: Path) -> ReleasePlan:
    """Deriva paquetes, ficheros de versión y formato de tag de
    `release-please-config.json`. Un `release-type` o actualizador que no
    sabemos aplicar aborta: mejor parar que publicar una versión a medias."""
    packages, version_files = [], []
    for path, options in config["packages"].items():
        merged = {**config, **options}
        release_type = merged["release-type"]
        if release_type not in RELEASE_TYPE_FILES:
            raise ReleaseError(f"{path}: release-type '{release_type}' no soportado")
        packages.append(
            Package(
                path=path,
                release_type=release_type,
                component=merged.get("component", merged.get("package-name", path)),
                changelog=_join(path, merged.get("changelog-path", DEFAULT_CHANGELOG)),
            )
        )
        name, kind, jsonpath = RELEASE_TYPE_FILES[release_type]
        own = VersionFile(_join(path, name), kind, jsonpath)
        if (
            release_type not in OPTIONAL_RELEASE_TYPE_FILES
            or (root / own.path).exists()
        ):
            version_files.append(own)
        for extra in merged.get("extra-files", ()):
            if isinstance(extra, str):
                extra = {"type": "generic", "path": extra}
            version_files.append(
                VersionFile(
                    _join(path, extra["path"]), extra["type"], extra.get("jsonpath", "")
                )
            )
    return ReleasePlan(tuple(packages), tuple(version_files), _tag_format(config))


def _tag_format(config: dict) -> str:
    component = "{component}" + config.get("tag-separator", "-")
    if not config.get("include-component-in-tag", True):
        component = ""
    prefix = "v" if config.get("include-v-in-tag", True) else ""
    return component + prefix + "{version}"


def linked_version(manifest: dict) -> str:
    """La versión común de los tres componentes (`linked-versions`)."""
    versions = set(manifest.values())
    if len(versions) != 1:
        raise ReleaseError(f"versiones no enlazadas en {MANIFEST}: {sorted(versions)}")
    (version,) = versions
    return version


# --- actualizadores de versión ---------------------------------------------


def bump_version_file(text: str, entry: VersionFile, version: str) -> str:
    """Pone `version` en `text` como lo haría el actualizador `entry.kind`."""
    updaters = {
        "generic": _bump_generic,
        "toml": _bump_toml,
        "json": _bump_json,
        "text": _bump_text,
    }
    if entry.kind not in updaters:
        raise ReleaseError(f"{entry.path}: actualizador '{entry.kind}' no soportado")
    return updaters[entry.kind](text, entry, version)


def _bump_generic(text: str, entry: VersionFile, version: str) -> str:
    lines = text.splitlines(keepends=True)
    marked = [i for i, line in enumerate(lines) if GENERIC_MARKER in line]
    if not marked:
        raise ReleaseError(f"{entry.path}: no hay ninguna línea con '{GENERIC_MARKER}'")
    for i in marked:
        lines[i] = re.sub(SEMVER, version, lines[i], count=1)
    return "".join(lines)


def _bump_json(text: str, entry: VersionFile, version: str) -> str:
    if entry.jsonpath != "$.version":
        raise ReleaseError(f"{entry.path}: jsonpath '{entry.jsonpath}' no soportado")
    new = _replace_once(
        text,
        r'^([ \t]*"version"\s*:\s*")' + SEMVER + r'(")',
        version,
        entry.path,
        re.MULTILINE,
    )
    if json.loads(new).get("version") != version:
        raise ReleaseError(
            f"{entry.path}: la versión cambiada no es la de primer nivel"
        )
    return new


def _bump_text(text: str, entry: VersionFile, version: str) -> str:
    return _replace_once(text, r"^()" + SEMVER + r"()", version, entry.path)


def _bump_toml(text: str, entry: VersionFile, version: str) -> str:
    item = TOML_ARRAY_ITEM.match(entry.jsonpath)
    if item:
        pattern = (
            r"(\[\["
            + re.escape(item["array"])
            + r"\]\]\n"
            + re.escape(item["field"])
            + r' = "'
            + re.escape(item["value"])
            + r'"\n'
            + re.escape(item["key"])
            + r' = ")[^"]+(")'
        )
        return _replace_once(text, pattern, version, f"{entry.path} {entry.jsonpath}")
    table = TOML_TABLE_KEY.match(entry.jsonpath)
    if table:
        pattern = (
            r"(^\["
            + re.escape(table["table"])
            + r"\]\n(?:(?!\[)[^\n]*\n)*?"
            + re.escape(table["key"])
            + r' = ")[^"]+(")'
        )
        return _replace_once(
            text, pattern, version, f"{entry.path} {entry.jsonpath}", re.MULTILINE
        )
    raise ReleaseError(f"{entry.path}: jsonpath '{entry.jsonpath}' no soportado")


def _replace_once(
    text: str, pattern: str, version: str, where: str, flags: int = 0
) -> str:
    new, count = re.subn(pattern, r"\g<1>" + version + r"\g<2>", text, flags=flags)
    if count != 1:
        raise ReleaseError(f"{where}: se esperaba una versión y hay {count}")
    return new


def bump_manifest(manifest: dict, version: str) -> str:
    return json.dumps({path: version for path in manifest}, indent=2) + "\n"


# --- CHANGELOG ---------------------------------------------------------------


def normalize_changelog(text: str, version: str, date: str) -> str:
    """Descarta el bloque generado por release-please para `version`, abre la
    sección `## [version] - date` bajo un `## [Unreleased]` vacío y fusiona
    los encabezados `### ...` repetidos dentro de la nueva sección."""
    generated = re.compile(
        r"## \[" + re.escape(version) + r"\]\(.*?(?=## \[Unreleased\])", re.DOTALL
    )
    text = generated.sub("", text)
    if text.count(UNRELEASED) != 1:
        raise ValueError("el CHANGELOG debe tener exactamente un '## [Unreleased]'")
    if f"## [{version}] - " in text:
        return _squeeze(text)
    head, rest = text.split(UNRELEASED, 1)
    body, sep, tail = rest.partition("\n## [")
    section = _merge_subsections(body)
    new = (
        head
        + UNRELEASED
        + f"\n## [{version}] - {date}\n"
        + section
        + (sep + tail if sep else "")
    )
    return _squeeze(new)


def update_link_refs(text: str, version: str, tag: str) -> str:
    """Pone al día los enlaces de comparación del pie (Keep a Changelog):
    `[Unreleased]` compara desde `tag` y se añade `[version]` comparando el
    tag anterior con `tag`. Sin pie de enlaces, el texto no cambia."""
    match = UNRELEASED_LINK.search(text)
    if match is None or f"\n[{version}]: " in text:
        return text
    base = match["base"]
    refs = (
        f"[Unreleased]: {base}/compare/{tag}...HEAD\n"
        f"[{version}]: {base}/compare/{match['previous']}...{tag}"
    )
    return text[: match.start()] + refs + text[match.end() :]


def _merge_subsections(body: str) -> str:
    """Une en uno los `### Título` que aparecen más de una vez, en orden."""
    parts = re.split(r"^(### .+)$", body, flags=re.MULTILINE)
    lead, merged, order = parts[0], {}, []
    for heading, content in zip(parts[1::2], parts[2::2]):
        if heading not in merged:
            order.append(heading)
            merged[heading] = ""
        merged[heading] += "\n" + content.strip("\n") + "\n"
    out = lead
    for heading in order:
        out += "\n" + heading + "\n" + merged[heading]
    return out


def _squeeze(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text)


# --- comprobaciones ----------------------------------------------------------


def has_compat_row(compat_source: str, version: str) -> bool:
    """La tabla de `rayito doctor` tiene la fila de la serie `MAJOR.MINOR`
    de `version` (docs/RELEASING.md §6.7; sin ella `doctor` da FAIL)."""
    series = ".".join(version.split(".")[:2])
    return (
        re.search(r'CompatibilityRow\(\s*"' + re.escape(series) + r'"', compat_source)
        is not None
    )


def unexpected_paths(changed: set[str], allowed: frozenset[str]) -> list[str]:
    """Rutas cambiadas que una release no debería tocar, ordenadas."""
    return sorted(changed - allowed)


def parse_trailers(cli: list[str], env: str) -> list[str]:
    """Trailers de `--trailer` y de `RELEASE_PR_TRAILERS` (uno por línea),
    sin duplicados y en ese orden; cada uno debe ser `Clave: valor`."""
    trailers: list[str] = []
    for line in [*cli, *env.splitlines()]:
        line = line.strip()
        if not line or line in trailers:
            continue
        if not TRAILER.match(line):
            raise ReleaseError(
                f"trailer no válido (se espera 'Clave: valor'): {line!r}"
            )
        trailers.append(line)
    return trailers


def commit_message(version: str, trailers: list[str]) -> str:
    message = (
        f"chore: release main\n\nRayito {version} (python, typescript, rayd). Lockfiles y CHANGELOG\n"
        "preparados con scripts/prepare_release_pr.py."
    )
    return message + ("\n\n" + "\n".join(trailers) if trailers else "")


# --- git ---------------------------------------------------------------------


def git(*args: str, capture: bool = False) -> str:
    result = subprocess.run(
        ["git", *args], check=True, text=True, capture_output=capture
    )
    return result.stdout.strip() if capture else ""


def show(ref: str, path: str) -> str:
    return git("show", f"{ref}:{path}", capture=True)


def blob(ref: str, path: str) -> str:
    """Contenido exacto de `path` en `ref` (sin recortar el salto final)."""
    return subprocess.run(
        ["git", "show", f"{ref}:{path}"], check=True, text=True, capture_output=True
    ).stdout


def changed_paths(base: str) -> set[str]:
    """Ficheros que difieren de `base` en el árbol de trabajo, más los nuevos
    sin seguimiento."""
    tracked = git("diff", "--name-only", base, capture=True).splitlines()
    untracked = git(
        "ls-files", "--others", "--exclude-standard", capture=True
    ).splitlines()
    return {path for path in (*tracked, *untracked) if path}


def release_please_base() -> str:
    """El commit de `main` del que cuelga la rama de release-please."""
    return git("merge-base", MAIN_REF, RELEASE_REF, capture=True)


def release_please_paths() -> set[str]:
    """Ficheros que release-please cambió respecto a su base."""
    base = release_please_base()
    return set(git("diff", "--name-only", base, RELEASE_REF, capture=True).splitlines())


def refresh_lockfile(package: Package) -> None:
    command, _ = LOCK_COMMANDS[package.release_type]
    subprocess.run(list(command), cwd=package.path or ".", check=True)


# --- pasos -------------------------------------------------------------------


def apply_release(plan: ReleasePlan, manifest: dict, version: str, date: str) -> None:
    """Escribe la release sobre el árbol actual (el de `origin/main`)."""
    Path(MANIFEST).write_text(bump_manifest(manifest, version), encoding="utf-8")
    for entry in plan.version_files:
        path = Path(entry.path)
        path.write_text(
            bump_version_file(path.read_text(encoding="utf-8"), entry, version),
            encoding="utf-8",
        )
    for package in plan.packages:
        if package.release_type in LOCK_COMMANDS:
            refresh_lockfile(package)
        path = Path(package.changelog)
        text = normalize_changelog(path.read_text(encoding="utf-8"), version, date)
        path.write_text(
            update_link_refs(text, version, plan.tag(package.component, version)),
            encoding="utf-8",
        )


def check_release_please_scope(plan: ReleasePlan) -> None:
    """Si release-please movió un fichero que el plan no conoce, una versión
    quedaría sin subir: se para."""
    unknown = unexpected_paths(release_please_paths(), plan.allowed_paths)
    if unknown:
        raise ReleaseError(
            f"{RELEASE_BRANCH} cambia ficheros que el plan no deriva de {CONFIG}: "
            + ", ".join(unknown)
        )


def check_bumps_match_release_please(plan: ReleasePlan) -> None:
    """Contraste con lo que hizo release-please: en cada fichero de versión
    que release-please cambió y que `main` no ha tocado desde su base, el
    resultado debe ser idéntico al de su rama. Si difiere, un actualizador
    de aquí no reproduce el suyo y se para antes de subir nada."""
    base = release_please_base()
    # Cargo.lock no: el actualizador TOML de release-please no lo mueve (punto 1).
    candidates = sorted(
        {MANIFEST, *(entry.path for entry in plan.version_files)} - {CARGO_LOCK}
    )
    touched = release_please_paths()
    disagree = [
        path
        for path in candidates
        if path in touched
        and blob(base, path) == blob(MAIN_REF, path)
        and Path(path).read_text(encoding="utf-8") != blob(RELEASE_REF, path)
    ]
    if disagree:
        raise ReleaseError(
            "el cambio de versión no coincide con el de "
            f"{RELEASE_BRANCH} en: {', '.join(disagree)}"
        )


def check_release_scope(plan: ReleasePlan) -> list[str]:
    """La guarda: el commit sólo puede diferir de `origin/main` en versiones,
    lockfiles y CHANGELOG. Devuelve las rutas a commitear."""
    changed = changed_paths(MAIN_REF)
    unexpected = unexpected_paths(changed, plan.allowed_paths)
    if unexpected:
        raise ReleaseError(
            f"el commit de release cambiaría ficheros ajenos a la release respecto a "
            f"{MAIN_REF}: {', '.join(unexpected)}. No se ha commiteado ni subido nada; "
            f"revisa el árbol de {RELEASE_BRANCH} (git switch - para volver)"
        )
    return sorted(changed)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="prepara y muestra el diff, sin commit ni push",
    )
    parser.add_argument(
        "--date",
        default=dt.datetime.now(tz=dt.timezone.utc).date().isoformat(),
        help="fecha de la release (AAAA-MM-DD)",
    )
    parser.add_argument(
        "--trailer",
        action="append",
        default=[],
        help=f"trailer 'Clave: valor' del commit (repetible; también {TRAILERS_ENV})",
    )
    return parser.parse_args(argv)


def prepare(args: argparse.Namespace) -> int:
    trailers = parse_trailers(args.trailer, os.environ.get(TRAILERS_ENV, ""))
    if git("status", "--porcelain", capture=True):
        raise ReleaseError("el árbol tiene cambios sin commit")
    git("fetch", "--quiet", REMOTE)

    version = linked_version(json.loads(show(RELEASE_REF, MANIFEST)))
    current = json.loads(show(MAIN_REF, MANIFEST))
    if linked_version(current) == version:
        raise ReleaseError(
            f"{MAIN_REF} ya está en {version}; no hay release que preparar"
        )
    if not has_compat_row(show(MAIN_REF, COMPAT_TABLE), version):
        raise ReleaseError(
            f"falta la fila {version} en {COMPAT_TABLE} y en "
            "docs/site/docs/limits.md (RELEASING.md §6.7)"
        )

    git("switch", "--quiet", "-C", RELEASE_BRANCH, MAIN_REF)
    plan = release_plan(json.loads(Path(CONFIG).read_text(encoding="utf-8")), Path("."))
    check_release_please_scope(plan)
    apply_release(plan, current, version, args.date)
    check_bumps_match_release_please(plan)
    paths = check_release_scope(plan)

    git("--no-pager", "diff", "--stat")
    if args.dry_run:
        print(
            f"prepare_release_pr: dry-run de {version}; el árbol queda con los cambios sin commit"
        )
        return 0

    git("add", "--", *paths)
    git("commit", "--quiet", *COMMIT_FLAGS, "-m", commit_message(version, trailers))
    git(
        "push",
        "--quiet",
        f"--force-with-lease={RELEASE_BRANCH}:{RELEASE_REF}",
        REMOTE,
        f"HEAD:{RELEASE_BRANCH}",
    )
    print(
        f"prepare_release_pr: {version} listo en {RELEASE_BRANCH} ({git('rev-parse', '--short', 'HEAD', capture=True)})"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return prepare(parse_args(argv))
    except (ReleaseError, ValueError) as error:
        print(f"prepare_release_pr: {error}; abortado", file=sys.stderr)
        return EXIT_ABORT


if __name__ == "__main__":
    sys.exit(main())
