"""``check_wheel.py`` sobre una wheel sintética: con todas las líneas
requeridas (ficheros, licencia, extras `mcp`/`cli`/`otel`) no hay problemas;
quitando cualquiera de las dos líneas de `METADATA` del extra `otel`
(`Provides-Extra: otel`, `Requires-Dist: opentelemetry-api>=1.27,<2 ; extra
== 'otel'`) aparece exactamente ese problema y ningún otro extra se ve
afectado. `otel_problems` se prueba también de forma aislada."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import check_wheel

REQUIRED_LICENSE_FILES = (
    "rayito-0.5.0.dist-info/licenses/LICENSE",
    "rayito-0.5.0.dist-info/licenses/NOTICE",
)

BASE_METADATA_LINES = [
    "Metadata-Version: 2.4",
    "Name: rayito",
    "Version: 0.5.0",
    "Requires-Python: >=3.11",
    "License-Expression: Apache-2.0",
    "License-File: LICENSE",
    "License-File: NOTICE",
    "Requires-Dist: grpcio>=1.84,<2",
    "Requires-Dist: protobuf>=7.36.1,<8",
    "Requires-Dist: boto3>=1.43.82,<2",
    "Provides-Extra: mcp",
    "Requires-Dist: mcp>=2.2,<3 ; extra == 'mcp'",
    "Provides-Extra: cli",
    "Requires-Dist: typer>=0.15,<1 ; extra == 'cli'",
    "Provides-Extra: otel",
    "Requires-Dist: opentelemetry-api>=1.27,<2 ; extra == 'otel'",
]

ENTRY_POINTS = (
    "[console_scripts]\n"
    "rayito = rayito.cli.__main__:main\n"
    "rayito-mcp = rayito.mcp.__main__:main\n"
)


def build_wheel(tmp_path: Path, *, metadata_lines: list[str]) -> Path:
    wheel = tmp_path / "rayito-0.5.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name in check_wheel.REQUIRED_FILES:
            archive.writestr(name, "")
        for suffix in REQUIRED_LICENSE_FILES:
            archive.writestr(suffix, "")
        archive.writestr(
            "rayito-0.5.0.dist-info/METADATA", "\n".join(metadata_lines) + "\n"
        )
        archive.writestr("rayito-0.5.0.dist-info/entry_points.txt", ENTRY_POINTS)
    return wheel


def test_wheel_with_every_required_line_has_no_problems(tmp_path: Path) -> None:
    wheel = build_wheel(tmp_path, metadata_lines=BASE_METADATA_LINES)
    assert check_wheel.problems_for(wheel) == []


def test_missing_otel_provides_extra_is_the_only_problem(tmp_path: Path) -> None:
    lines = [line for line in BASE_METADATA_LINES if line != "Provides-Extra: otel"]
    wheel = build_wheel(tmp_path, metadata_lines=lines)
    assert check_wheel.problems_for(wheel) == ["METADATA sin `Provides-Extra: otel`"]


def test_missing_otel_requires_dist_is_the_only_problem(tmp_path: Path) -> None:
    lines = [
        line
        for line in BASE_METADATA_LINES
        if line != "Requires-Dist: opentelemetry-api>=1.27,<2 ; extra == 'otel'"
    ]
    wheel = build_wheel(tmp_path, metadata_lines=lines)
    assert check_wheel.problems_for(wheel) == [
        "METADATA sin `Requires-Dist: opentelemetry-api>=1.27,<2 ; extra == 'otel'`"
    ]


def test_otel_problems_in_isolation() -> None:
    assert check_wheel.otel_problems("\n".join(BASE_METADATA_LINES)) == []
    assert check_wheel.otel_problems("Metadata-Version: 2.4") == [
        "METADATA sin `Provides-Extra: otel`",
        "METADATA sin `Requires-Dist: opentelemetry-api>=1.27,<2 ; extra == 'otel'`",
    ]


def test_missing_mcp_or_cli_lines_do_not_affect_otel_problems() -> None:
    lines = [
        line
        for line in BASE_METADATA_LINES
        if line not in {"Provides-Extra: mcp", "Provides-Extra: cli"}
    ]
    metadata = "\n".join(lines)
    assert check_wheel.otel_problems(metadata) == []
