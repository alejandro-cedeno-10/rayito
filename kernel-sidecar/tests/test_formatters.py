"""The lazy ``e2b/chart`` and ``e2b/data`` formatters of ``ipython/startup``,
in-process and on any host: the startup scripts must import nothing
scientific, and their registration must survive IPython's inline backend
setup (``select_figure_formats`` pops ``Figure`` from every formatter on the
first ``pyplot`` import, which erased the ``for_type_by_name`` registration
on ``rayito-base-slim`` 1.0). Needs the pinned requirements (IPython,
matplotlib, pandas): ``uv run --with-requirements requirements.txt pytest``."""

from __future__ import annotations

import importlib.util
import runpy
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

STARTUP = Path(__file__).resolve().parents[1] / "ipython" / "startup"
FORMATTER_SCRIPTS = ("0001_charts.py", "0002_data.py")
SCIENTIFIC_MODULES = ("numpy", "pandas", "matplotlib")

pytestmark = pytest.mark.skipif(
    any(importlib.util.find_spec(name) is None for name in ("IPython", *SCIENTIFIC_MODULES)),
    reason="needs the pinned requirements (IPython, numpy, pandas, matplotlib)",
)


def loaded_scientific_modules() -> set[str]:
    return {name for name in SCIENTIFIC_MODULES if name in sys.modules}


@pytest.fixture
def shell() -> Iterator[Any]:
    from IPython.core.interactiveshell import InteractiveShell

    InteractiveShell.clear_instance()
    instance = InteractiveShell.instance()
    try:
        yield instance
    finally:
        InteractiveShell.clear_instance()


def run_formatter_scripts() -> None:
    for name in FORMATTER_SCRIPTS:
        runpy.run_path(str(STARTUP / name))


def test_formatter_scripts_import_nothing_scientific(shell: Any) -> None:
    before = loaded_scientific_modules()
    assert not before, f"polluted test process, already loaded: {sorted(before)}"
    run_formatter_scripts()
    assert loaded_scientific_modules() == set()
    assert set(shell.display_formatter.formatters) >= {"e2b/chart", "e2b/data"}


def test_chart_formatter_survives_select_figure_formats(shell: Any) -> None:
    run_formatter_scripts()
    import matplotlib

    matplotlib.use("Agg")
    from IPython.core.pylabtools import select_figure_formats
    from matplotlib.figure import Figure

    select_figure_formats(shell, {"png"})
    figure = Figure()
    figure.subplots().plot([1, 2, 3])
    data, _ = shell.display_formatter.format(figure)
    assert "image/png" in data
    chart = data["e2b/chart"]
    assert isinstance(chart, dict)
    assert chart["type"] == "line"
    assert len(chart["elements"][0]["points"]) == 3


def test_data_formatter_resolves_frames_and_series_lazily(shell: Any) -> None:
    run_formatter_scripts()
    import pandas as pd

    frame = pd.DataFrame({"a": [1, 2], "b": [0.5, 1.5]})
    data, _ = shell.display_formatter.format(frame)
    assert data["e2b/data"] == {"a": [1, 2], "b": [0.5, 1.5]}
    assert all(type(v) in (int, float) for vs in data["e2b/data"].values() for v in vs)
    series, _ = shell.display_formatter.format(pd.Series([3, 4], name="s"))
    assert series["e2b/data"] == {"s": [3, 4]}
