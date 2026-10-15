"""Componente `dashboard-cloudwatch` (ADR-026, fase 3):
`infra/dashboard-cloudwatch.yaml`, un dashboard de CloudWatch con widgets de
Logs Insights sobre `/rayito/*` y, opcionalmente, las métricas OTLP y el uso
de la cuota de memoria. Es el mismo JSON que imprime `rayito dashboard
cloudwatch`: el `DashboardBody` de la plantilla lo genera
`scripts/gen_cloudwatch_template.py` desde el generador del SDK.
"""

from __future__ import annotations

from typing import Final

from rayito._limits import (
    CLOUDWATCH_DASHBOARD_PRICE_USD_PER_MONTH,
    CLOUDWATCH_FREE_DASHBOARD_METRICS,
    CLOUDWATCH_FREE_DASHBOARDS,
    CLOUDWATCH_LOGS_INSIGHTS_PRICE_USD_PER_GB,
)
from rayito._stacks._model import CostStatement, StackComponent, StackParameter


def price_es(value: float) -> str:
    """Un precio con coma decimal; mismo formato que `priceEs` de TypeScript."""
    return f"{value:g}".replace(".", ",")


#: El coste de tener el dashboard, que la ayuda de `rayito dashboard
#: cloudwatch` y la caja de la guía repiten con las mismas cifras.
DASHBOARD_IDLE_COST: Final = (
    f"$0 con los {CLOUDWATCH_FREE_DASHBOARDS} dashboards gratuitos de la cuenta (hasta "
    f"{CLOUDWATCH_FREE_DASHBOARD_METRICS} métricas cada uno); "
    f"{price_es(CLOUDWATCH_DASHBOARD_PRICE_USD_PER_MONTH)} USD al mes por dashboard por "
    f"encima de esos {CLOUDWATCH_FREE_DASHBOARDS}"
)
LOGS_INSIGHTS_COST: Final = (
    "Logs Insights: cada widget de log ejecuta su consulta al abrir o refrescar el "
    "dashboard y se factura por GB escaneado "
    f"({price_es(CLOUDWATCH_LOGS_INSIGHTS_PRICE_USD_PER_GB)} USD/GB); acota el intervalo"
)

#: Prefijo por defecto de los grupos (`/rayito/<imagen>`), con la barra final
#: para no abarcar `/rayito-otro`. Igual que `DEFAULT_LOG_GROUP_PREFIX` del
#: generador (`scripts/tests/test_cloudwatch_template.py` lo comprueba); no se
#: importa de allí para que catalogar los componentes no cargue el panel.
DEFAULT_LOG_GROUP_PREFIX: Final = "/rayito/"

COMPONENT: StackComponent = StackComponent(
    name="dashboard-cloudwatch",
    description=(
        "Dashboard de CloudWatch con logs de los sandboxes (Logs Insights sobre /rayito/*) "
        "y, opcionalmente, métricas OTLP y uso de cuota."
    ),
    parameters=(
        StackParameter(
            "DashboardName",
            "Nombre del dashboard en CloudWatch (único por cuenta y región). Vacío usa el "
            "nombre de la pila, único en la región.",
            default="",
        ),
        StackParameter(
            "LogGroupPrefix",
            "Prefijo de los grupos de logs que consultan los widgets; conserva la barra final.",
            default=DEFAULT_LOG_GROUP_PREFIX,
        ),
        StackParameter(
            "IncludeOtlp",
            "true añade gráficas de CPU, memoria y disco de las métricas OTLP de rayd "
            "(telemetry=); sin sandboxes que exporten no tienen datos.",
            default="false",
        ),
        StackParameter(
            "IncludeQuota",
            "true añade la gráfica del uso de la cuota de memoria: grafica la métrica de la "
            "pila quota-alerts, así que está vacía hasta que esa pila corra en la región.",
            default="false",
        ),
    ),
    cost=CostStatement(
        creates=("AWS::CloudWatch::Dashboard (1)",),
        idle_monthly=DASHBOARD_IDLE_COST,
        per_use=(
            LOGS_INSIGHTS_COST,
            "Gráficas OTLP (IncludeOtlp=true): las consultas PromQL hechas desde la consola, "
            "dashboards incluidos, no se facturan; la ingesta OTLP la factura telemetry=",
        ),
        removal="destroy() borra el dashboard; no deja logs ni métricas",
        source="https://aws.amazon.com/cloudwatch/pricing/ us-east-1, consultado 2026-10-13",
    ),
)
