"""Comprueba que la licencia Apache-2.0 está declarada igual en los cinco sitios.

Sin red y sólo con la biblioteca estándar: lee el árbol del repo y falla
nombrando cada problema cuando `Cargo.toml`, `clients/python/pyproject.toml`,
`kernel-sidecar/pyproject.toml` o `clients/typescript/package.json` no
declaran `Apache-2.0` (o conservan un clasificador `License ::`, o `files`
sin `LICENSE`/`NOTICE`), cuando el `LICENSE` raíz no es el texto de apache.org
(sha256 de https://www.apache.org/licenses/LICENSE-2.0.txt descargado el
2026-09-16; si el fichero trae finales CRLF el mensaje lo dice, porque la
causa habitual es un checkout que ignoró el `.gitattributes`), cuando una
copia de `LICENSE` o `NOTICE` difiere de la raíz, o cuando el `NOTICE` raíz
no menciona el `e2b_charts` vendorizado. Se ejecuta desde la raíz del repo:

    python scripts/check_license.py            # OK / KO ... y exit 0 / 1
    python scripts/check_license.py --root DIR # otro árbol (tests)
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import tomllib

EXPECTED_LICENSE = "Apache-2.0"
APACHE_LICENSE_SHA256 = (
    "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"
)
PYTHON_LICENSE_FILES = ["LICENSE", "NOTICE"]
TYPESCRIPT_REQUIRED_FILES = ("LICENSE", "NOTICE")
VENDORED_MENTION = "e2b_charts"
FORBIDDEN_LICENSE_TOKEN = "MIT"
CRLF = b"\r\n"
CRLF_HINT = (
    " (tiene finales CRLF: el checkout ignoró .gitattributes, ver CONTRIBUTING.md §3)"
)

CARGO_MANIFEST = "Cargo.toml"
PYTHON_MANIFEST = "clients/python/pyproject.toml"
SIDECAR_MANIFEST = "kernel-sidecar/pyproject.toml"
TYPESCRIPT_MANIFEST = "clients/typescript/package.json"
ROOT_LICENSE = "LICENSE"
ROOT_NOTICE = "NOTICE"
LICENSE_COPIES = (
    "clients/python/LICENSE",
    "clients/typescript/LICENSE",
    "crates/rayd/LICENSE",
)
NOTICE_COPIES = ("clients/python/NOTICE", "clients/typescript/NOTICE")


def load_toml(root: Path, relative: str) -> dict[str, Any] | None:
    path = root / relative
    if not path.is_file():
        return None
    with path.open("rb") as handle:
        return tomllib.load(handle)


def load_json(root: Path, relative: str) -> dict[str, Any] | None:
    path = root / relative
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def license_value_problems(relative: str, value: object) -> list[str]:
    if value is None:
        return [f"{relative}: sin campo license"]
    if not isinstance(value, str):
        return [f"{relative}: license debe ser una cadena, es {type(value).__name__}"]
    problems = []
    if FORBIDDEN_LICENSE_TOKEN in value:
        problems.append(
            f"{relative}: license todavía declara {FORBIDDEN_LICENSE_TOKEN}"
        )
    if value != EXPECTED_LICENSE:
        problems.append(
            f"{relative}: license es {value!r}, se esperaba {EXPECTED_LICENSE!r}"
        )
    return problems


def cargo_problems(root: Path) -> list[str]:
    manifest = load_toml(root, CARGO_MANIFEST)
    if manifest is None:
        return [f"{CARGO_MANIFEST}: no existe"]
    package = manifest.get("workspace", {}).get("package", {})
    return license_value_problems(CARGO_MANIFEST, package.get("license"))


def python_problems(root: Path) -> list[str]:
    manifest = load_toml(root, PYTHON_MANIFEST)
    if manifest is None:
        return [f"{PYTHON_MANIFEST}: no existe"]
    project = manifest.get("project", {})
    problems = license_value_problems(PYTHON_MANIFEST, project.get("license"))
    license_files = project.get("license-files")
    if license_files != PYTHON_LICENSE_FILES:
        problems.append(
            f"{PYTHON_MANIFEST}: license-files es {license_files!r}, "
            f"se esperaba {PYTHON_LICENSE_FILES!r}"
        )
    problems += [
        f"{PYTHON_MANIFEST}: clasificador de licencia sobrante {classifier!r}"
        for classifier in project.get("classifiers", [])
        if isinstance(classifier, str) and classifier.startswith("License ::")
    ]
    return problems


def sidecar_problems(root: Path) -> list[str]:
    manifest = load_toml(root, SIDECAR_MANIFEST)
    if manifest is None:
        return [f"{SIDECAR_MANIFEST}: no existe"]
    return license_value_problems(
        SIDECAR_MANIFEST, manifest.get("project", {}).get("license")
    )


def typescript_problems(root: Path) -> list[str]:
    manifest = load_json(root, TYPESCRIPT_MANIFEST)
    if manifest is None:
        return [f"{TYPESCRIPT_MANIFEST}: no existe"]
    problems = license_value_problems(TYPESCRIPT_MANIFEST, manifest.get("license"))
    files = manifest.get("files")
    if not isinstance(files, list):
        return [*problems, f"{TYPESCRIPT_MANIFEST}: sin lista files"]
    problems += [
        f"{TYPESCRIPT_MANIFEST}: files no incluye {name}"
        for name in TYPESCRIPT_REQUIRED_FILES
        if name not in files
    ]
    return problems


def copy_problems(root: Path, original: str, copies: tuple[str, ...]) -> list[str]:
    original_path = root / original
    if not original_path.is_file():
        return [f"{original}: no existe"]
    original_bytes = original_path.read_bytes()
    problems = []
    for relative in copies:
        path = root / relative
        if not path.is_file():
            problems.append(f"{relative}: no existe")
        elif path.read_bytes() != original_bytes:
            problems.append(f"{relative}: no es idéntico byte a byte a {original}")
    return problems


def line_ending_hint(content: bytes) -> str:
    return CRLF_HINT if CRLF in content else ""


def license_file_problems(root: Path) -> list[str]:
    path = root / ROOT_LICENSE
    if not path.is_file():
        return [f"{ROOT_LICENSE}: no existe"]
    problems = []
    content = path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    if digest != APACHE_LICENSE_SHA256:
        problems.append(
            f"{ROOT_LICENSE}: sha256 {digest} no es el texto de apache.org "
            f"({APACHE_LICENSE_SHA256}){line_ending_hint(content)}"
        )
    return problems + copy_problems(root, ROOT_LICENSE, LICENSE_COPIES)


def notice_file_problems(root: Path) -> list[str]:
    path = root / ROOT_NOTICE
    if not path.is_file():
        return [f"{ROOT_NOTICE}: no existe"]
    problems = []
    if VENDORED_MENTION not in path.read_text(encoding="utf-8"):
        problems.append(f"{ROOT_NOTICE}: no menciona {VENDORED_MENTION}")
    return problems + copy_problems(root, ROOT_NOTICE, NOTICE_COPIES)


def problems_for(root: Path) -> list[str]:
    return (
        cargo_problems(root)
        + python_problems(root)
        + sidecar_problems(root)
        + typescript_problems(root)
        + license_file_problems(root)
        + notice_file_problems(root)
    )


def parse_root(argv: list[str]) -> Path | None:
    if not argv:
        return Path.cwd()
    if len(argv) == 2 and argv[0] == "--root":
        return Path(argv[1])
    return None


def main(argv: list[str]) -> int:
    root = parse_root(argv)
    if root is None:
        print("uso: check_license.py [--root DIR]", file=sys.stderr)
        return 2
    problems = problems_for(root)
    if problems:
        for problem in problems:
            print(f"KO {problem}")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
