"""Deja el PR de release-please listo para fusionar, con un solo comando.

release-please mueve las versiones de los manifiestos, pero tres cosas quedan
siempre a mano (lo aprendido en 0.3.0, 0.3.1 y 0.3.2):

1. **Lockfiles**: su actualizador TOML no encuentra los paquetes del
   workspace en `Cargo.lock` ("No entries modified") y no toca
   `clients/python/uv.lock`, así que `cargo test --locked` y los tests de
   versión fallan en el PR.
2. **CHANGELOG**: añade su propio bloque `## [x.y.z](compare) (fecha)` encima
   de `## [Unreleased]`, con notas generadas de los títulos de commit. Aquí
   se escriben a mano en `## [Unreleased]` durante el ciclo; el bloque
   generado se descarta y `## [Unreleased]` pasa a ser `## [x.y.z] - fecha`
   (Keep a Changelog), fusionando las secciones `### ...` repetidas.
3. **Firma**: el ruleset de `main` exige commits firmados y los que crea
   release-please por la API no lo están. Se rehace el PR como un único
   commit firmado (`git commit -S -s`) sobre `origin/main` y se sube con
   `--force-with-lease`.
4. **Base atrasada**: release-please no rehace su rama en cada push a
   `main`, así que su commit puede colgar de un `main` anterior. El commit
   se porta sobre `origin/main` (sólo el diff de versiones de release-please,
   sin sus CHANGELOG) en vez de reescribir el árbol de su rama, que
   revertiría lo fusionado después.

Uso (desde la raíz del repositorio, con la firma SSH configurada):

    python scripts/prepare_release_pr.py            # prepara, firma y sube
    python scripts/prepare_release_pr.py --dry-run  # sólo muestra el diff

`make release-pr` es el mismo comando. No publica nada: la release sigue
empezando al fusionar el PR (`docs/RELEASING.md` §6)."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path

RELEASE_BRANCH = "release-please--branches--main"
MANIFEST = ".release-please-manifest.json"
CHANGELOGS = (
    "clients/python/CHANGELOG.md",
    "clients/typescript/CHANGELOG.md",
    "crates/rayd/CHANGELOG.md",
)
CARGO_LOCK = "Cargo.lock"
WORKSPACE_CRATES = ("rayd", "rayd-core", "rayito-proto")
PYTHON_CLIENT = "clients/python"
COMPAT_TABLE = "clients/python/src/rayito/cli/_compat.py"
UNRELEASED = "## [Unreleased]\n"


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


def has_compat_row(compat_source: str, version: str) -> bool:
    """La tabla de `rayito doctor` tiene la fila de la serie `MAJOR.MINOR`
    de `version` (docs/RELEASING.md §6.7; sin ella `doctor` da FAIL)."""
    series = ".".join(version.split(".")[:2])
    return (
        re.search(r'CompatibilityRow\(\s*"' + re.escape(series) + r'"', compat_source)
        is not None
    )


def bump_cargo_lock(text: str, version: str) -> str:
    """Pone `version` a los crates del workspace en `Cargo.lock`."""
    for name in WORKSPACE_CRATES:
        pattern = re.compile(
            r'(name = "' + re.escape(name) + r'"\nversion = ")[^"]+(")'
        )
        text, count = pattern.subn(r"\g<1>" + version + r"\g<2>", text)
        if count != 1:
            raise ValueError(
                f"{name}: se esperaba una entrada en Cargo.lock y hay {count}"
            )
    return text


def git(*args: str, capture: bool = False) -> str:
    result = subprocess.run(
        ["git", *args], check=True, text=True, capture_output=capture
    )
    return result.stdout.strip() if capture else ""


def port_release_commit(main_ref: str, release_ref: str) -> None:
    """Pone `RELEASE_BRANCH` en `main_ref` con los cambios de versión de
    `release_ref` en el índice. Sólo se aplica el diff de release-please
    respecto a su base (`git merge-base`), sin los CHANGELOG: su bloque
    generado se descarta igualmente y `normalize_changelog` parte del
    `## [Unreleased]` de `main_ref`. Así lo fusionado en `main` después de
    que release-please creara su commit no se revierte."""
    base = git("merge-base", main_ref, release_ref, capture=True)
    git("switch", "--quiet", "-C", RELEASE_BRANCH, main_ref)
    excludes = [f":(exclude){path}" for path in CHANGELOGS]
    diff = subprocess.run(
        ["git", "diff", "--binary", base, release_ref, "--", ".", *excludes],
        check=True,
        capture_output=True,
    ).stdout
    if diff:
        subprocess.run(["git", "apply", "--3way", "--index"], input=diff, check=True)


def main(argv: list[str] | None = None) -> int:
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
    args = parser.parse_args(argv)

    if git("status", "--porcelain", capture=True):
        print(
            "prepare_release_pr: el árbol tiene cambios sin commit; abortado",
            file=sys.stderr,
        )
        return 2
    git("fetch", "--quiet", "origin")
    port_release_commit("origin/main", f"origin/{RELEASE_BRANCH}")

    versions = set(json.loads(Path(MANIFEST).read_text(encoding="utf-8")).values())
    if len(versions) != 1:
        print(
            f"prepare_release_pr: versiones no enlazadas en {MANIFEST}: {sorted(versions)}",
            file=sys.stderr,
        )
        return 2
    (version,) = versions
    if not has_compat_row(Path(COMPAT_TABLE).read_text(encoding="utf-8"), version):
        print(
            f"prepare_release_pr: falta la fila {version} en {COMPAT_TABLE} y en "
            "docs/site/docs/limits.md (RELEASING.md §6.7); abortado",
            file=sys.stderr,
        )
        return 2

    lock = Path(CARGO_LOCK)
    lock.write_text(
        bump_cargo_lock(lock.read_text(encoding="utf-8"), version), encoding="utf-8"
    )
    subprocess.run(["uv", "lock", "--quiet"], cwd=PYTHON_CLIENT, check=True)
    for path in map(Path, CHANGELOGS):
        path.write_text(
            normalize_changelog(path.read_text(encoding="utf-8"), version, args.date),
            encoding="utf-8",
        )

    git("--no-pager", "diff", "--stat")
    if args.dry_run:
        print(
            f"prepare_release_pr: dry-run de {version}; el árbol queda con los cambios sin commit"
        )
        return 0

    git("add", CARGO_LOCK, f"{PYTHON_CLIENT}/uv.lock", *CHANGELOGS)
    git("reset", "--quiet", "--soft", "origin/main")
    message = (
        f"chore: release main\n\nRayito {version} (python, typescript, rayd). Lockfiles y CHANGELOG\n"
        "preparados con scripts/prepare_release_pr.py."
    )
    git("commit", "--quiet", "-S", "-s", "-m", message)
    git(
        "push",
        "--quiet",
        f"--force-with-lease={RELEASE_BRANCH}:origin/{RELEASE_BRANCH}",
        "origin",
        f"HEAD:{RELEASE_BRANCH}",
    )
    print(
        f"prepare_release_pr: {version} listo en {RELEASE_BRANCH} ({git('rev-parse', '--short', 'HEAD', capture=True)})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
