"""`parse_chart` sobre los documentos `e2b/chart` de `fixtures/charts/`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rayito import (
    BarChart,
    BarData,
    BoxAndWhiskerChart,
    BoxAndWhiskerData,
    Chart,
    ChartType,
    LineChart,
    PieChart,
    PieData,
    PointData,
    ScaleType,
    ScatterChart,
    SuperChart,
)
from rayito._charts import parse_chart

FIXTURES = Path(__file__).parent / "fixtures" / "charts"


def fixture_text(name: str) -> str:
    return (FIXTURES / f"{name}.json").read_text(encoding="utf-8")


def test_line_chart_carries_axes_scales_and_points() -> None:
    chart = parse_chart(fixture_text("line"))
    assert isinstance(chart, LineChart)
    assert chart.type is ChartType.LINE
    assert chart.title == "Line chart"
    assert (chart.x_label, chart.y_label, chart.x_unit, chart.y_unit) == ("x", "y", None, "cm")
    assert chart.x_ticks == [0.0, 1.0, 2.0]
    assert chart.x_tick_labels == ["0", "1", "2"]
    assert chart.x_scale is ScaleType.LINEAR
    assert chart.y_scale is ScaleType.LOG
    assert chart.elements == [
        PointData(label="_child0", points=[(0.0, 1.0), (1.0, 2.0), (2.0, 3.0)]),
        PointData(label="series b", points=[("2024-01-01", 5.0), ("2024-01-02", 6.5)]),
    ]
    assert len(chart.elements[0].points) == 3


def test_scatter_chart_keeps_categorical_coordinates() -> None:
    chart = parse_chart(json.loads(fixture_text("scatter")))
    assert isinstance(chart, ScatterChart)
    assert chart.type is ChartType.SCATTER
    assert chart.x_scale is ScaleType.DATETIME
    assert chart.y_scale is ScaleType.CATEGORICAL
    assert chart.x_ticks == []
    assert chart.elements == [PointData(label="dots", points=[(1, "a"), (2, "b")])]


def test_bar_chart_elements() -> None:
    chart = parse_chart(fixture_text("bar"))
    assert isinstance(chart, BarChart)
    assert chart.type is ChartType.BAR
    assert (chart.x_label, chart.y_label) == ("category", "count")
    assert chart.elements == [
        BarData(label="a", group="_container0", value="1.0"),
        BarData(label="b", group="_container0", value=2.5),
    ]


def test_pie_chart_elements() -> None:
    chart = parse_chart(fixture_text("pie"))
    assert isinstance(chart, PieChart)
    assert chart.type is ChartType.PIE
    assert chart.elements == [
        PieData(label="a", angle=120.0, radius=1.0, autopct="33.3%"),
        PieData(label="b", angle=240.0, radius=1.0, autopct=66.7),
    ]


def test_box_and_whisker_chart_elements() -> None:
    chart = parse_chart(fixture_text("box_and_whisker"))
    assert isinstance(chart, BoxAndWhiskerChart)
    assert chart.type is ChartType.BOX_AND_WHISKER
    assert chart.elements == [
        BoxAndWhiskerData(
            label="a",
            min=1.0,
            first_quartile=2.0,
            median=3.0,
            third_quartile=4.0,
            max=5.0,
            outliers=[9.0, 10.5],
        )
    ]


def test_superchart_parses_children_recursively() -> None:
    chart = parse_chart(fixture_text("superchart"))
    assert isinstance(chart, SuperChart)
    assert chart.type is ChartType.SUPERCHART
    assert chart.title == "Super"
    assert [type(child) for child in chart.elements] == [LineChart, BarChart]
    assert chart.elements[0] == parse_chart(fixture_text("line"))


def test_unknown_type_is_a_bare_chart() -> None:
    chart = parse_chart(fixture_text("unknown"))
    assert type(chart) is Chart
    assert chart.type is ChartType.UNKNOWN
    assert chart.title == "Heat"
    assert chart.elements == []


@pytest.mark.parametrize("document", ["{not json", "[1, 2]", "42", '"line"', ""])
def test_documents_that_are_not_objects_are_unknown(document: str) -> None:
    chart = parse_chart(document)
    assert chart == Chart(type=ChartType.UNKNOWN, title=None, elements=[])


def test_missing_and_malformed_keys_default_instead_of_raising() -> None:
    chart = parse_chart({"type": "line"})
    assert isinstance(chart, LineChart)
    assert chart.title is None
    assert chart.x_scale is None
    assert chart.elements == []
    malformed = parse_chart(
        {
            "type": "line",
            "title": 7,
            "x_scale": "polar",
            "x_ticks": "nope",
            "elements": [{"label": "a", "points": [[1], [1, 2, 3], "x", [1, 2]]}, "junk"],
        }
    )
    assert isinstance(malformed, LineChart)
    assert malformed.title == "7"
    assert malformed.x_scale is None
    assert malformed.x_ticks == []
    assert malformed.elements == [PointData(label="a", points=[(1, 2)])]
    assert parse_chart({"type": "pie", "elements": [{"angle": "wide"}]}).elements == [
        PieData(label=None, angle=None, radius=None, autopct=None)
    ]
    assert parse_chart({"type": "box_and_whisker", "elements": [{"outliers": 3}]}).elements == [
        BoxAndWhiskerData()
    ]
    assert parse_chart({}).type is ChartType.UNKNOWN


def test_chart_types_and_scales_are_string_enums() -> None:
    assert ChartType.BOX_AND_WHISKER.value == "box_and_whisker"
    assert ChartType("superchart") is ChartType.SUPERCHART
    assert ScaleType.FUNCTIONLOG.value == "functionlog"
    assert {scale.value for scale in ScaleType} == {
        "linear",
        "datetime",
        "categorical",
        "log",
        "symlog",
        "logit",
        "function",
        "functionlog",
        "asinh",
    }
