"""Generador del dashboard de CloudWatch (ADR-026, fase 3): el JSON de un
`DashboardBody` con widgets de Logs Insights sobre los grupos `/rayito/*` y,
si se piden, los de las métricas OTLP de `rayd` y el uso de la cuota de
memoria que publica la pila `quota-alerts`.

Es dominio puro: no importa `boto3` ni hace ninguna llamada. `rayito dashboard
cloudwatch` imprime el JSON; la misma estructura renderiza el
`DashboardBody` de `infra/dashboard-cloudwatch.yaml`
(`scripts/gen_cloudwatch_template.py`), con marcadores donde la plantilla
pone la región y el prefijo de los grupos.

Los widgets no llevan `x`/`y`: CloudWatch los coloca en la siguiente posición
libre, así que los opcionales no dejan huecos cuando faltan.

Lo que no se ha podido medir en AWS real (tarea de aceptación del cambio
`dashboard`): que el `SOURCE logGroups(namePrefix: [...])` se acepte dentro de
un widget `log` (la doc de widgets sólo muestra `SOURCE 'grupo'`) y la forma
exacta de las consultas PromQL de las métricas OTLP.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, Final

from rayito._limits import (
    QUOTA_CRITICAL_FRACTION,
    QUOTA_METRIC_NAME,
    QUOTA_METRIC_NAMESPACE,
    QUOTA_WARNING_FRACTION,
)
from rayito._sandbox_base import LOG_GROUP_PREFIX
from rayito.exceptions import InvalidArgumentException

Widget = dict[str, Any]

#: Prefijo por defecto de los grupos (`/rayito/<imagen>`), con la barra final
#: para no abarcar `/rayito-otro`.
DEFAULT_LOG_GROUP_PREFIX: Final = f"{LOG_GROUP_PREFIX}/"
#: Marcadores de la plantilla de CloudFormation dentro de un `Fn::Sub`.
TEMPLATE_REGION: Final = "${AWS::Region}"
TEMPLATE_LOG_GROUP_PREFIX: Final = "${LogGroupPrefix}"

#: El intervalo del dashboard al abrirlo (ISO 8601) y el cubo de las series.
DASHBOARD_START: Final = "-PT6H"
LOG_BIN: Final = "5m"
GRID_COLUMNS: Final = 24
HALF_WIDTH: Final = GRID_COLUMNS // 2
HEADER_HEIGHT: Final = 3
CHART_HEIGHT: Final = 6
#: Filas de las tablas «más ruidosos» y «errores recientes».
TOP_ROWS: Final = 20
RECENT_ERROR_ROWS: Final = 50
#: Lo que cuenta como error en una línea de log, sin distinguir mayúsculas.
ERROR_PATTERN: Final = "/(?i)error|exception|traceback|fatal|panic/"
#: Período de la serie de uso de cuota: la comprobación corre cada hora por
#: defecto (`QUOTA_ALERTS_SCHEDULE_EXPRESSION`).
QUOTA_PERIOD_SECONDS: Final = 3600
PERCENT_AXIS_MAX: Final = 1
CHART_VIEW: Final = "line"
#: Las métricas que `rayd` exporta por OTLP (`docs/site/docs/funciones-opcionales/
#: exportacion-otlp.md`); el nombre lleva puntos, así que PromQL lo cita.
OTLP_CPU_USED_PCT: Final = "rayito.sandbox.cpu.used_pct"
OTLP_MEMORY_USED_BYTES: Final = "rayito.sandbox.memory.used_bytes"
OTLP_MEMORY_TOTAL_BYTES: Final = "rayito.sandbox.memory.total_bytes"
OTLP_DISK_USED_BYTES: Final = "rayito.sandbox.disk.used_bytes"
OTLP_DISK_TOTAL_BYTES: Final = "rayito.sandbox.disk.total_bytes"
LOG_GROUP_PREFIX_CHARS: Final = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/_.#-"
)
HEADER_MARKDOWN: Final = (
    "# Rayito\n"
    "Logs de los sandboxes (Logs Insights sobre `{scope}`). Cada widget de log "
    "ejecuta su consulta al abrir o refrescar el dashboard y CloudWatch factura "
    "los datos escaneados: usa un intervalo corto."
)


def validate_log_group_prefix(prefix: str) -> str:
    """El prefijo entra tal cual en una consulta entre comillas simples: sólo
    caracteres de un nombre de grupo, empezando por `/`."""
    if not prefix.startswith("/") or not set(prefix) <= LOG_GROUP_PREFIX_CHARS:
        raise InvalidArgumentException(
            "prefijo de grupo de logs inválido: empieza por «/» y sólo lleva letras, "
            "números y «_ . # - /»"
        )
    return prefix


def validate_log_group(name: str) -> str:
    """Un nombre de grupo explícito, con la misma gramática que el prefijo."""
    if not name or not set(name) <= LOG_GROUP_PREFIX_CHARS:
        raise InvalidArgumentException(
            "nombre de grupo de logs inválido: sólo letras, números y «_ . # - /»"
        )
    return name


def log_source(prefix: str, log_groups: Sequence[str] = ()) -> str:
    """La cláusula `SOURCE` de las consultas: los grupos nombrados o, sin
    ninguno, todos los que empiezan por `prefix`."""
    if log_groups:
        return " | ".join(f"SOURCE '{name}'" for name in log_groups)
    return f"SOURCE logGroups(namePrefix: ['{prefix}'])"


def _query(source: str, *clauses: str) -> str:
    return source + " | " + "\n| ".join(clauses)


def _log_widget(region: str, title: str, query: str, view: str, width: int) -> Widget:
    properties: dict[str, Any] = {"region": region, "title": title, "query": query}
    if view:
        properties["view"] = view
    return {"type": "log", "width": width, "height": CHART_HEIGHT, "properties": properties}


def log_widgets(region: str, source: str, scope: str) -> list[Widget]:
    """El encabezado y los cinco widgets de Logs Insights (siempre presentes)."""
    errors = f"filter @message like {ERROR_PATTERN}"
    return [
        {
            "type": "text",
            "width": GRID_COLUMNS,
            "height": HEADER_HEIGHT,
            "properties": {"markdown": HEADER_MARKDOWN.format(scope=scope)},
        },
        _log_widget(
            region,
            "Líneas de log por intervalo",
            _query(source, f"stats count(*) as lines by bin({LOG_BIN})"),
            "timeSeries",
            HALF_WIDTH,
        ),
        _log_widget(
            region,
            "Errores por intervalo",
            _query(source, errors, f"stats count(*) as errors by bin({LOG_BIN})"),
            "timeSeries",
            HALF_WIDTH,
        ),
        _log_widget(
            region,
            "Líneas por grupo de logs",
            _query(
                source, "stats count(*) as lines by @log", "sort lines desc", f"limit {TOP_ROWS}"
            ),
            "table",
            HALF_WIDTH,
        ),
        _log_widget(
            region,
            "Streams más ruidosos",
            _query(
                source,
                "stats count(*) as lines by @logStream",
                "sort lines desc",
                f"limit {TOP_ROWS}",
            ),
            "table",
            HALF_WIDTH,
        ),
        _log_widget(
            region,
            "Errores recientes",
            _query(
                source,
                errors,
                "fields @timestamp, @logStream, @message",
                "sort @timestamp desc",
                f"limit {RECENT_ERROR_ROWS}",
            ),
            "table",
            GRID_COLUMNS,
        ),
    ]


def _promql(identifier: str, query: str, label: str) -> dict[str, str]:
    return {
        "id": identifier,
        "type": "cloudwatch-metrics",
        "language": "PromQL",
        "query": query,
        "label": label,
    }


def _chart(region: str, title: str, description: str, queries: list[dict[str, str]]) -> Widget:
    return {
        "type": "chart",
        "width": HALF_WIDTH,
        "height": CHART_HEIGHT,
        "properties": {
            "view": CHART_VIEW,
            "title": title,
            "description": description,
            "region": region,
            "data": {"queries": queries},
        },
    }


def _gauge(metric: str) -> str:
    return f'avg({{"{metric}"}})'


def otlp_widgets(region: str) -> list[Widget]:
    """CPU, memoria y disco de los sandboxes que exportan por OTLP
    (`telemetry=`), con PromQL sobre las siete métricas `rayito.sandbox.*`
    (medias entre sandboxes)."""
    note = "Métricas OTLP de rayd (telemetry=); media entre sandboxes."
    return [
        _chart(
            region,
            "CPU en uso (%)",
            note,
            [_promql("cpu", _gauge(OTLP_CPU_USED_PCT), "CPU %")],
        ),
        _chart(
            region,
            "Memoria (bytes)",
            note,
            [
                _promql("used", _gauge(OTLP_MEMORY_USED_BYTES), "en uso"),
                _promql("total", _gauge(OTLP_MEMORY_TOTAL_BYTES), "total"),
            ],
        ),
        _chart(
            region,
            "Disco (bytes)",
            note,
            [
                _promql("used", _gauge(OTLP_DISK_USED_BYTES), "en uso"),
                _promql("total", _gauge(OTLP_DISK_TOTAL_BYTES), "total"),
            ],
        ),
    ]


def quota_widgets(region: str) -> list[Widget]:
    """La memoria asignada contra la cuota (la métrica que publica la pila
    `quota-alerts`) con los umbrales de aviso y crítico como líneas."""
    return [
        {
            "type": "metric",
            "width": HALF_WIDTH,
            "height": CHART_HEIGHT,
            "properties": {
                "region": region,
                "title": "Memoria asignada / cuota",
                "view": "timeSeries",
                "metrics": [[QUOTA_METRIC_NAMESPACE, QUOTA_METRIC_NAME, "Region", region]],
                "stat": "Maximum",
                "period": QUOTA_PERIOD_SECONDS,
                "yAxis": {"left": {"min": 0, "max": PERCENT_AXIS_MAX}},
                "annotations": {
                    "horizontal": [
                        {"label": "Aviso", "value": QUOTA_WARNING_FRACTION},
                        {"label": "Crítico", "value": QUOTA_CRITICAL_FRACTION},
                    ]
                },
            },
        }
    ]


def _assemble(
    region: str, source: str, scope: str, *, include_otlp: bool, include_quota: bool
) -> dict[str, Any]:
    widgets = log_widgets(region, source, scope)
    if include_otlp:
        widgets += otlp_widgets(region)
    if include_quota:
        widgets += quota_widgets(region)
    return {"start": DASHBOARD_START, "widgets": widgets}


def build_board(
    *,
    region: str,
    log_group_prefix: str = DEFAULT_LOG_GROUP_PREFIX,
    log_groups: Sequence[str] = (),
    include_otlp: bool = False,
    include_quota: bool = False,
) -> dict[str, Any]:
    """El `DashboardBody` como diccionario. Valida el prefijo y los nombres
    antes de construir ninguna consulta."""
    prefix = validate_log_group_prefix(log_group_prefix)
    groups = tuple(validate_log_group(name) for name in log_groups)
    scope = ", ".join(groups) if groups else f"{prefix}*"
    return _assemble(
        region,
        log_source(prefix, groups),
        scope,
        include_otlp=include_otlp,
        include_quota=include_quota,
    )


def render_board(board: dict[str, Any], *, indent: int | None = None) -> str:
    return json.dumps(board, ensure_ascii=False, indent=indent)


def template_parts() -> tuple[str, str, str]:
    """Las tres piezas del `DashboardBody` de la plantilla, con los marcadores
    de región y prefijo: el cuerpo base con una variable `${OtlpWidgets}` y
    otra `${QuotaWidgets}` al final de `widgets`, y los fragmentos
    (`,{...},{...}`) que cada variable toma cuando está activada."""
    base = _assemble(
        TEMPLATE_REGION,
        log_source(TEMPLATE_LOG_GROUP_PREFIX),
        f"{TEMPLATE_LOG_GROUP_PREFIX}*",
        include_otlp=False,
        include_quota=False,
    )
    opening = render_board({**base, "widgets": []})
    opening = opening[: opening.rindex("]")]
    body = ",".join(render_board(widget) for widget in base["widgets"])
    return (
        f"{opening}{body}${{OtlpWidgets}}${{QuotaWidgets}}]}}",
        _fragment(otlp_widgets(TEMPLATE_REGION)),
        _fragment(quota_widgets(TEMPLATE_REGION)),
    )


def _fragment(widgets: list[Widget]) -> str:
    return "".join("," + render_board(widget) for widget in widgets)
