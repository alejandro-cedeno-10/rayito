"""`rayito.cli._compat`: las tres evaluaciones del escenario, versiones mal
formadas, la versión de imagen fuera del veredicto y la tabla de
`docs/site/docs/limits.md` igual a `COMPATIBILITY`."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from rayito import __version__
from rayito.cli import _compat

LIMITS_MD = Path(__file__).resolve().parents[5] / "docs" / "site" / "docs" / "limits.md"
SECTION_HEADING = "## Compatibilidad SDK ↔ rayd ↔ imagen"


def test_assessments() -> None:
    assert _compat.assess("0.1.0", "0.1.0").status == "OK"
    below = _compat.assess("0.1.0", "0.0.9")
    assert below.status == "FAIL" and "0.0.9" in below.reason and "0.1.0" in below.reason
    newer = _compat.assess("0.1.0", "0.2.0")
    assert newer.status == "WARN" and "0.2.0" in newer.reason


def test_image_build_number_is_not_a_criterion() -> None:
    """`imageVersion` cuenta los builds de cada imagen en cada cuenta: la
    primera imagen de una cuenta nueva (1.0) o `rayito-base-poly` 3.0 llevan
    el mismo `rayd` que `rayito-base` 17.0, así que la tabla no la mira."""
    assert not hasattr(_compat, "parse_image_version")
    assert all(not hasattr(row, "min_image_version") for row in _compat.COMPATIBILITY)
    assert _compat.assess("0.1.0", "0.1.0").row == _compat.COMPATIBILITY[0]


def test_patch_may_diverge_and_prerelease_is_ignored() -> None:
    assert _compat.assess("0.1.3", "0.1.0").status == "OK"
    assert _compat.assess("0.1.0", "0.1.7-rc1").status == "OK"
    assert _compat.parse_semver("v1.2.3+build.5") == (1, 2, 3)


def test_malformed_versions() -> None:
    with pytest.raises(ValueError, match=r"MAJOR\.MINOR\.PATCH"):
        _compat.parse_semver("abc")
    failed = _compat.assess("0.1.0", "abc")
    assert failed.status == "FAIL" and "abc" in failed.reason and failed.row is None


def test_unknown_sdk_series_fails() -> None:
    assessment = _compat.assess("9.9.0", "0.1.0")
    assert assessment.status == "FAIL" and "9.9" in assessment.reason


def docs_rows(text: str) -> list[_compat.CompatibilityRow]:
    section = text.split(SECTION_HEADING, 1)[1]
    rows: list[_compat.CompatibilityRow] = []
    for line in section.splitlines():
        if not line.startswith("|") or set(line.replace("|", "").strip()) <= {"-", " "}:
            continue
        cells = [cell.strip().strip("`") for cell in line.strip().strip("|").split("|")]
        if cells[0] == "SDK":
            continue
        rows.append(_compat.CompatibilityRow(*cells[:3]))
    return rows


def test_docs_table_matches_code() -> None:
    if not LIMITS_MD.is_file():
        pytest.skip(f"{LIMITS_MD} ausente")
    text = LIMITS_MD.read_text(encoding="utf-8")
    assert SECTION_HEADING in text
    assert docs_rows(text) == list(_compat.COMPATIBILITY)


def test_row_series_are_major_minor() -> None:
    for row in _compat.COMPATIBILITY:
        assert re.fullmatch(r"\d+\.\d+", row.sdk_series)
        _compat.parse_semver(row.min_agent_version)


@pytest.mark.parametrize("version", ["0.3.0", "0.3.1", "0.3.9-rc1", __version__])
def test_current_and_upcoming_sdk_series_have_a_row(version: str) -> None:
    """`rayito doctor` da `FAIL` sin fila para la serie del SDK: la serie 0.3
    (M9) y la del `__version__` instalado tienen que estar en la tabla."""
    series = _compat.series_of(_compat.parse_semver(version))
    row = _compat.row_for(series)
    assert row is not None, f"sin fila de compatibilidad para {series}"
    assert _compat.assess(version, row.min_agent_version).status == "OK"


def test_series_0_3_requires_the_m9_agent() -> None:
    row = _compat.row_for("0.3")
    assert row is not None and row.min_agent_version == "0.3.0"
    assert _compat.assess("0.3.0", "0.2.0").status == "FAIL"
