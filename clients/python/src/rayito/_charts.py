"""Gráficos extraídos de matplotlib por el kernel (`e2b/chart`), con la misma
forma que los de E2B: un `Chart` por tipo y `parse_chart` para el documento
JSON que viaja en `ExecutionResult.chart`. Sin I/O.

`parse_chart` nunca falla por un documento incompleto: cada campo ausente
queda en `None` o `[]`, y un `type` desconocido (o un documento que no es un
objeto JSON) produce un `Chart` de tipo `UNKNOWN`.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final


class ChartType(StrEnum):
    LINE = "line"
    SCATTER = "scatter"
    BAR = "bar"
    PIE = "pie"
    BOX_AND_WHISKER = "box_and_whisker"
    SUPERCHART = "superchart"
    UNKNOWN = "unknown"


class ScaleType(StrEnum):
    LINEAR = "linear"
    DATETIME = "datetime"
    CATEGORICAL = "categorical"
    LOG = "log"
    SYMLOG = "symlog"
    LOGIT = "logit"
    FUNCTION = "function"
    FUNCTIONLOG = "functionlog"
    ASINH = "asinh"


Coordinate = float | str
Point = tuple[Coordinate, Coordinate]


@dataclass(frozen=True, kw_only=True)
class Chart:
    """Base de todos los gráficos; `elements` depende del tipo."""

    type: ChartType
    title: str | None = None
    elements: list[Any] = field(default_factory=list)


@dataclass(frozen=True, kw_only=True)
class Chart2D(Chart):
    x_label: str | None = None
    y_label: str | None = None
    x_unit: str | None = None
    y_unit: str | None = None


@dataclass(frozen=True, kw_only=True)
class PointData:
    label: str | None = None
    points: list[Point] = field(default_factory=list)


@dataclass(frozen=True, kw_only=True)
class PointChart(Chart2D):
    x_ticks: list[Coordinate] = field(default_factory=list)
    x_tick_labels: list[str] = field(default_factory=list)
    x_scale: ScaleType | None = None
    y_ticks: list[Coordinate] = field(default_factory=list)
    y_tick_labels: list[str] = field(default_factory=list)
    y_scale: ScaleType | None = None
    elements: list[PointData] = field(default_factory=list)


@dataclass(frozen=True, kw_only=True)
class LineChart(PointChart):
    type: ChartType = ChartType.LINE


@dataclass(frozen=True, kw_only=True)
class ScatterChart(PointChart):
    type: ChartType = ChartType.SCATTER


@dataclass(frozen=True, kw_only=True)
class BarData:
    label: str | None = None
    group: str | None = None
    value: Coordinate | None = None


@dataclass(frozen=True, kw_only=True)
class BarChart(Chart2D):
    type: ChartType = ChartType.BAR
    elements: list[BarData] = field(default_factory=list)


@dataclass(frozen=True, kw_only=True)
class PieData:
    label: str | None = None
    angle: float | None = None
    radius: float | None = None
    autopct: Coordinate | None = None


@dataclass(frozen=True, kw_only=True)
class PieChart(Chart):
    type: ChartType = ChartType.PIE
    elements: list[PieData] = field(default_factory=list)


@dataclass(frozen=True, kw_only=True)
class BoxAndWhiskerData:
    label: str | None = None
    min: float | None = None
    first_quartile: float | None = None
    median: float | None = None
    third_quartile: float | None = None
    max: float | None = None
    outliers: list[float] = field(default_factory=list)


@dataclass(frozen=True, kw_only=True)
class BoxAndWhiskerChart(Chart2D):
    type: ChartType = ChartType.BOX_AND_WHISKER
    elements: list[BoxAndWhiskerData] = field(default_factory=list)


@dataclass(frozen=True, kw_only=True)
class SuperChart(Chart):
    type: ChartType = ChartType.SUPERCHART
    elements: list[Chart] = field(default_factory=list)


ChartDocument = Mapping[str, Any]


def parse_chart(document: str | ChartDocument) -> Chart:
    """Convierte el documento `e2b/chart` (JSON o dict ya parseado) en el
    `Chart` de su tipo; nunca lanza por campos ausentes o de tipo inesperado."""
    data = _as_mapping(document)
    if data is None:
        return Chart(type=ChartType.UNKNOWN)
    chart_type = _chart_type(data.get("type"))
    parser = CHART_PARSERS.get(chart_type)
    if parser is None:
        return Chart(type=ChartType.UNKNOWN, title=_optional_str(data.get("title")))
    return parser(data)


def _as_mapping(document: object) -> ChartDocument | None:
    if isinstance(document, Mapping):
        return document
    if not isinstance(document, str):
        return None
    try:
        loaded = json.loads(document)
    except ValueError:
        return None
    return loaded if isinstance(loaded, Mapping) else None


def _chart_type(value: object) -> ChartType:
    try:
        return ChartType(str(value))
    except ValueError:
        return ChartType.UNKNOWN


def _scale_type(value: object) -> ScaleType | None:
    if value is None:
        return None
    try:
        return ScaleType(str(value))
    except ValueError:
        return None


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _coordinate(value: object) -> Coordinate | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return value
    return None if value is None else str(value)


def _coordinates(value: object) -> list[Coordinate]:
    if not isinstance(value, list):
        return []
    return [item for item in (_coordinate(raw) for raw in value) if item is not None]


def _floats(value: object) -> list[float]:
    if not isinstance(value, list):
        return []
    return [item for item in (_optional_float(raw) for raw in value) if item is not None]


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _items(value: object) -> list[ChartDocument]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _point(value: object) -> Point | None:
    if not isinstance(value, list | tuple) or len(value) != 2:
        return None
    x, y = _coordinate(value[0]), _coordinate(value[1])
    if x is None or y is None:
        return None
    return x, y


def _points(value: object) -> list[Point]:
    if not isinstance(value, list):
        return []
    return [point for point in (_point(raw) for raw in value) if point is not None]


def _point_data(data: ChartDocument) -> PointData:
    return PointData(label=_optional_str(data.get("label")), points=_points(data.get("points")))


def _bar_data(data: ChartDocument) -> BarData:
    return BarData(
        label=_optional_str(data.get("label")),
        group=_optional_str(data.get("group")),
        value=_coordinate(data.get("value")),
    )


def _pie_data(data: ChartDocument) -> PieData:
    return PieData(
        label=_optional_str(data.get("label")),
        angle=_optional_float(data.get("angle")),
        radius=_optional_float(data.get("radius")),
        autopct=_coordinate(data.get("autopct")),
    )


def _box_data(data: ChartDocument) -> BoxAndWhiskerData:
    return BoxAndWhiskerData(
        label=_optional_str(data.get("label")),
        min=_optional_float(data.get("min")),
        first_quartile=_optional_float(data.get("first_quartile")),
        median=_optional_float(data.get("median")),
        third_quartile=_optional_float(data.get("third_quartile")),
        max=_optional_float(data.get("max")),
        outliers=_floats(data.get("outliers")),
    )


def _chart_2d_fields(data: ChartDocument) -> dict[str, Any]:
    return {
        "title": _optional_str(data.get("title")),
        "x_label": _optional_str(data.get("x_label")),
        "y_label": _optional_str(data.get("y_label")),
        "x_unit": _optional_str(data.get("x_unit")),
        "y_unit": _optional_str(data.get("y_unit")),
    }


def _point_chart_fields(data: ChartDocument) -> dict[str, Any]:
    return {
        **_chart_2d_fields(data),
        "x_ticks": _coordinates(data.get("x_ticks")),
        "x_tick_labels": _strings(data.get("x_tick_labels")),
        "x_scale": _scale_type(data.get("x_scale")),
        "y_ticks": _coordinates(data.get("y_ticks")),
        "y_tick_labels": _strings(data.get("y_tick_labels")),
        "y_scale": _scale_type(data.get("y_scale")),
        "elements": [_point_data(item) for item in _items(data.get("elements"))],
    }


def _parse_line(data: ChartDocument) -> Chart:
    return LineChart(**_point_chart_fields(data))


def _parse_scatter(data: ChartDocument) -> Chart:
    return ScatterChart(**_point_chart_fields(data))


def _parse_bar(data: ChartDocument) -> Chart:
    return BarChart(
        **_chart_2d_fields(data),
        elements=[_bar_data(item) for item in _items(data.get("elements"))],
    )


def _parse_pie(data: ChartDocument) -> Chart:
    return PieChart(
        title=_optional_str(data.get("title")),
        elements=[_pie_data(item) for item in _items(data.get("elements"))],
    )


def _parse_box(data: ChartDocument) -> Chart:
    return BoxAndWhiskerChart(
        **_chart_2d_fields(data),
        elements=[_box_data(item) for item in _items(data.get("elements"))],
    )


def _parse_super(data: ChartDocument) -> Chart:
    return SuperChart(
        title=_optional_str(data.get("title")),
        elements=[parse_chart(item) for item in _items(data.get("elements"))],
    )


CHART_PARSERS: Final[dict[ChartType, Callable[[ChartDocument], Chart]]] = {
    ChartType.LINE: _parse_line,
    ChartType.SCATTER: _parse_scatter,
    ChartType.BAR: _parse_bar,
    ChartType.PIE: _parse_pie,
    ChartType.BOX_AND_WHISKER: _parse_box,
    ChartType.SUPERCHART: _parse_super,
}
