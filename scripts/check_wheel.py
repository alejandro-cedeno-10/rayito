"""Comprueba el contenido de la wheel de `rayito` antes de publicarla.

Afirma que la wheel lleva `rayito/py.typed`, el shim `rayito/e2b`, el cliente
generado (`health_pb2.py` y su `.pyi`), nada bajo `tests/`, los ficheros de
licencia `LICENSE` y `NOTICE` bajo `*.dist-info/licenses/`, y que `METADATA`
declara `Requires-Python: >=3.11`, las tres dependencias de runtime y la
licencia según PEP 639 (`License-Expression: Apache-2.0`, un `License-File`
por fichero y ningún clasificador `License ::`). Desde M7 comprueba además el
servidor MCP: el subpaquete `rayito/mcp` (`__init__.py` y `__main__.py`), el
extra `mcp` (`Provides-Extra: mcp` y `Requires-Dist: mcp>=2.2,<3 ; extra ==
'mcp'`, tal como los renderiza `uv_build`) y el console script `rayito-mcp`
en `entry_points.txt`; y la CLI (`m7-cli`): el subpaquete `rayito/cli`
(`__init__.py`), el extra `cli` (`Provides-Extra: cli` y `Requires-Dist:
typer>=0.15,<1 ; extra == 'cli'`, misma grafía de `uv_build`) y el console
script `rayito`. Se ejecuta desde la raíz del repo:

    python scripts/check_wheel.py clients/python/dist/rayito-*.whl
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

REQUIRED_FILES = (
    "rayito/py.typed",
    "rayito/e2b/__init__.py",
    "rayito/v1/health_pb2.py",
    "rayito/v1/health_pb2.pyi",
    "rayito/mcp/__init__.py",
    "rayito/mcp/__main__.py",
    "rayito/cli/__init__.py",
)
FORBIDDEN_PREFIXES = ("tests/",)
REQUIRED_PYTHON = "Requires-Python: >=3.11"
RUNTIME_DEPENDENCIES = ("grpcio", "protobuf", "boto3")
REQUIRED_METADATA_LINES = (
    "License-Expression: Apache-2.0",
    "License-File: LICENSE",
    "License-File: NOTICE",
)
FORBIDDEN_METADATA_PREFIX = "Classifier: License ::"
REQUIRED_LICENSE_SUFFIXES = (
    ".dist-info/licenses/LICENSE",
    ".dist-info/licenses/NOTICE",
)
MCP_METADATA_LINES = (
    "Provides-Extra: mcp",
    "Requires-Dist: mcp>=2.2,<3 ; extra == 'mcp'",
)
MCP_ENTRY_POINT = "rayito-mcp = rayito.mcp.__main__:main"
CLI_METADATA_LINES = (
    "Provides-Extra: cli",
    "Requires-Dist: typer>=0.15,<1 ; extra == 'cli'",
)
CLI_ENTRY_POINT = "rayito = rayito.cli.__main__:main"
ENTRY_POINTS_SUFFIX = ".dist-info/entry_points.txt"


def wheel_entries(wheel: Path) -> list[str]:
    with zipfile.ZipFile(wheel) as archive:
        return archive.namelist()


def wheel_metadata(wheel: Path) -> str:
    return dist_info_file(wheel, ".dist-info/METADATA")


def wheel_entry_points(wheel: Path) -> str:
    return dist_info_file(wheel, ENTRY_POINTS_SUFFIX, missing_ok=True)


def dist_info_file(wheel: Path, suffix: str, *, missing_ok: bool = False) -> str:
    with zipfile.ZipFile(wheel) as archive:
        matches = [name for name in archive.namelist() if name.endswith(suffix)]
        if not matches and missing_ok:
            return ""
        if len(matches) != 1:
            raise SystemExit(f"{wheel}: esperaba un {suffix}, hay {len(matches)}")
        return archive.read(matches[0]).decode("utf-8")


def problems_for(wheel: Path) -> list[str]:
    entries = wheel_entries(wheel)
    problems = [f"falta {name}" for name in REQUIRED_FILES if name not in entries]
    problems += [
        f"contiene {name}"
        for name in entries
        if any(name.startswith(prefix) for prefix in FORBIDDEN_PREFIXES)
    ]
    metadata = wheel_metadata(wheel)
    if REQUIRED_PYTHON not in metadata:
        problems.append(f"METADATA sin `{REQUIRED_PYTHON}`")
    declared = [
        line for line in metadata.splitlines() if line.startswith("Requires-Dist:")
    ]
    for dependency in RUNTIME_DEPENDENCIES:
        if not any(
            line.split(":", 1)[1].strip().startswith(dependency) for line in declared
        ):
            problems.append(f"METADATA sin Requires-Dist de {dependency}")
    entry_points = wheel_entry_points(wheel)
    return (
        problems
        + license_problems(entries, metadata)
        + mcp_problems(metadata, entry_points)
        + cli_problems(metadata, entry_points)
    )


def cli_problems(metadata: str, entry_points: str) -> list[str]:
    lines = metadata.splitlines()
    problems = [
        f"METADATA sin `{required}`"
        for required in CLI_METADATA_LINES
        if required not in lines
    ]
    if CLI_ENTRY_POINT not in entry_points.splitlines():
        problems.append(f"entry_points.txt sin `{CLI_ENTRY_POINT}`")
    return problems


def mcp_problems(metadata: str, entry_points: str) -> list[str]:
    lines = metadata.splitlines()
    problems = [
        f"METADATA sin `{required}`"
        for required in MCP_METADATA_LINES
        if required not in lines
    ]
    if MCP_ENTRY_POINT not in entry_points.splitlines():
        problems.append(f"entry_points.txt sin `{MCP_ENTRY_POINT}`")
    return problems


def license_problems(entries: list[str], metadata: str) -> list[str]:
    lines = metadata.splitlines()
    problems = [
        f"METADATA sin `{required}`"
        for required in REQUIRED_METADATA_LINES
        if required not in lines
    ]
    problems += [
        f"METADATA con clasificador de licencia `{line}`"
        for line in lines
        if line.startswith(FORBIDDEN_METADATA_PREFIX)
    ]
    problems += [
        f"falta una entrada que termine en {suffix}"
        for suffix in REQUIRED_LICENSE_SUFFIXES
        if not any(name.endswith(suffix) for name in entries)
    ]
    return problems


def main(argv: list[str]) -> int:
    wheels = [Path(argument) for argument in argv]
    if not wheels:
        print("uso: check_wheel.py <wheel>...", file=sys.stderr)
        return 2
    failed = False
    for wheel in wheels:
        problems = problems_for(wheel)
        if problems:
            failed = True
            print(f"{wheel}: KO\n  " + "\n  ".join(problems))
        else:
            print(f"{wheel}: OK ({len(wheel_entries(wheel))} entradas)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
