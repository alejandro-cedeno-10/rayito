"""El hook de MkDocs que sustituye `{{nombre}}` por el valor de `limits.json`."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

pytest.importorskip("mkdocs")

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / "docs" / "site" / "hooks" / "limits_macros.py"
LIMITS = json.loads((REPO_ROOT / "limits.json").read_text(encoding="utf-8"))


def load_hook():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("limits_macros", HOOK)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_known_name_becomes_its_limits_json_value() -> None:
    hook = load_hook()
    text = hook.on_page_markdown("hasta {{dashboardStackCostMissesPerDay}} al día")
    assert text == f"hasta {LIMITS['dashboardStackCostMissesPerDay']} al día"


def test_an_unknown_name_breaks_the_build() -> None:
    from mkdocs.exceptions import PluginError

    with pytest.raises(PluginError):
        load_hook().on_page_markdown("{{noExiste}}")


def test_other_double_braces_are_left_alone() -> None:
    assert load_hook().on_page_markdown("${{ secrets.X }} {{ a.b }}") == "${{ secrets.X }} {{ a.b }}"
