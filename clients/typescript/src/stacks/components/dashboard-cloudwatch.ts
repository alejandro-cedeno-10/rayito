/**
 * Componente `dashboard-cloudwatch` (ADR-026, fase 3): espejo exacto de
 * `rayito._stacks.components.dashboard_cloudwatch` (Python). Un dashboard de
 * CloudWatch con widgets de Logs Insights sobre `/rayito/*` y, opcionalmente,
 * las métricas OTLP y el uso de la cuota de memoria. El JSON lo genera la CLI
 * de Python (`rayito dashboard cloudwatch`); este componente sólo lo despliega.
 */

import {
  CLOUDWATCH_DASHBOARD_PRICE_USD_PER_MONTH,
  CLOUDWATCH_FREE_DASHBOARD_METRICS,
  CLOUDWATCH_FREE_DASHBOARDS,
  CLOUDWATCH_LOGS_INSIGHTS_PRICE_USD_PER_GB,
} from "../../limits.js";
import type { StackComponent } from "../model.js";

/** Un precio con coma decimal; mismo formato que `price_es` de Python. */
function priceEs(value: number): string {
  return String(value).replace(".", ",");
}

const DASHBOARD_IDLE_COST =
  `$0 con los ${CLOUDWATCH_FREE_DASHBOARDS} dashboards gratuitos de la cuenta (hasta ` +
  `${CLOUDWATCH_FREE_DASHBOARD_METRICS} métricas cada uno); ` +
  `${priceEs(CLOUDWATCH_DASHBOARD_PRICE_USD_PER_MONTH)} USD al mes por dashboard por ` +
  `encima de esos ${CLOUDWATCH_FREE_DASHBOARDS}`;
const LOGS_INSIGHTS_COST =
  "Logs Insights: cada widget de log ejecuta su consulta al abrir o refrescar el " +
  "dashboard y se factura por GB escaneado " +
  `(${priceEs(CLOUDWATCH_LOGS_INSIGHTS_PRICE_USD_PER_GB)} USD/GB); acota el intervalo`;

/** Prefijo por defecto de los grupos de logs, con la barra final para no abarcar `/rayito-otro`. */
export const DEFAULT_LOG_GROUP_PREFIX = "/rayito/";

export const COMPONENT: StackComponent = {
  name: "dashboard-cloudwatch",
  description:
    "Dashboard de CloudWatch con logs de los sandboxes (Logs Insights sobre /rayito/*) " +
    "y, opcionalmente, métricas OTLP y uso de cuota.",
  parameters: [
    {
      name: "DashboardName",
      description:
        "Nombre del dashboard en CloudWatch (único por cuenta y región). Vacío usa el " +
        "nombre de la pila, único en la región.",
      default: "",
    },
    {
      name: "LogGroupPrefix",
      description:
        "Prefijo de los grupos de logs que consultan los widgets; conserva la barra final.",
      default: DEFAULT_LOG_GROUP_PREFIX,
    },
    {
      name: "IncludeOtlp",
      description:
        "true añade gráficas de CPU, memoria y disco de las métricas OTLP de rayd " +
        "(telemetry=); sin sandboxes que exporten no tienen datos.",
      default: "false",
    },
    {
      name: "IncludeQuota",
      description:
        "true añade la gráfica del uso de la cuota de memoria: grafica la métrica de la " +
        "pila quota-alerts, así que está vacía hasta que esa pila corra en la región.",
      default: "false",
    },
  ],
  cost: {
    creates: ["AWS::CloudWatch::Dashboard (1)"],
    idleMonthly: DASHBOARD_IDLE_COST,
    perUse: [
      LOGS_INSIGHTS_COST,
      "Gráficas OTLP (IncludeOtlp=true): las consultas PromQL hechas desde la consola, " +
        "dashboards incluidos, no se facturan; la ingesta OTLP la factura telemetry=",
    ],
    removal: "destroy() borra el dashboard; no deja logs ni métricas",
    source: "https://aws.amazon.com/cloudwatch/pricing/ us-east-1, consultado 2026-10-13",
  },
};
