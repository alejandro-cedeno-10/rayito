/**
 * Componente `dashboard` (ADR-026, fase 2): espejo exacto de
 * `rayito._stacks.components.dashboard` (Python). Ver ese módulo y
 * `infra/README.md` para el detalle completo.
 */

import {
  COST_EXPLORER_REQUEST_PRICE_USD,
  DASHBOARD_POLL_INTERVAL_SECONDS,
  DASHBOARD_STACK_COST_EXAMPLE_DAYS_PER_MONTH,
  DASHBOARD_STACK_COST_EXAMPLE_OPEN_HOURS_PER_DAY,
  DASHBOARD_STACK_COST_LOOKBACK_MONTHS,
  DASHBOARD_STACK_COST_MISSES_PER_DAY,
  DASHBOARD_STACK_SSM_ADVANCED_PARAMETER_MAX_BYTES,
  DASHBOARD_STACK_THEME_PARAMETER_MAX_BYTES,
  DASHBOARD_STACK_THEME_REFRESH_SECONDS,
  LAMBDA_FREE_TIER_GB_SECONDS_PER_MONTH,
  LAMBDA_FREE_TIER_REQUESTS_PER_MONTH,
  LAMBDA_REQUEST_PRICE_USD_PER_MILLION,
  SSM_ADVANCED_PARAMETER_PRICE_USD_PER_MONTH,
  SSM_API_INTERACTION_PRICE_USD_PER_TEN_THOUSAND,
} from "../../limits.js";
import type { StackComponent } from "../model.js";

const SECONDS_PER_HOUR = 3600;
const SECONDS_PER_DAY = 86_400;
const API_INTERACTIONS_PER_BILLING_UNIT = 10_000;
/** La función lee el tema y el logo: dos parámetros por relectura. */
const SSM_PARAMETERS_READ = 2;

/** Un precio con coma decimal; mismo formato que `usd_es` de Python. */
function usdEs(value: number, decimals: number): string {
  return `$${value.toFixed(decimals)}`.replace(".", ",");
}

const EXAMPLE_INVOCATIONS_PER_MONTH =
  Math.floor(
    (DASHBOARD_STACK_COST_EXAMPLE_OPEN_HOURS_PER_DAY * SECONDS_PER_HOUR) /
      DASHBOARD_POLL_INTERVAL_SECONDS,
  ) * DASHBOARD_STACK_COST_EXAMPLE_DAYS_PER_MONTH;
const THEME_READS_PER_MONTH =
  Math.floor(
    (DASHBOARD_STACK_COST_EXAMPLE_DAYS_PER_MONTH * SECONDS_PER_DAY) /
      DASHBOARD_STACK_THEME_REFRESH_SECONDS,
  ) * SSM_PARAMETERS_READ;
const SSM_ADVANCED_COST_NOTE =
  `Un parámetro Advanced cuesta ${usdEs(SSM_ADVANCED_PARAMETER_PRICE_USD_PER_MONTH, 2)} al mes ` +
  "más las interacciones de API (precios de SSM Parameter Store, 2026-10-13); la función " +
  `lo relee cada ${DASHBOARD_STACK_THEME_REFRESH_SECONDS} s.`;
const COST_EXPLORER_WORST_DAY_USD =
  DASHBOARD_STACK_COST_MISSES_PER_DAY * COST_EXPLORER_REQUEST_PRICE_USD;
const COST_EXPLORER_WORST_MONTH_USD =
  COST_EXPLORER_WORST_DAY_USD * DASHBOARD_STACK_COST_EXAMPLE_DAYS_PER_MONTH;
const THEME_READS_MONTHLY_USD =
  (THEME_READS_PER_MONTH / API_INTERACTIONS_PER_BILLING_UNIT) *
  SSM_API_INTERACTION_PRICE_USD_PER_TEN_THOUSAND;

export const COMPONENT: StackComponent = {
  name: "dashboard",
  description:
    "Panel de gestión de MicroVMs en tu cuenta: Lambda con Function URL, Cognito, " +
    "roles de lectura y de acciones separados, auditoría append-only en DynamoDB.",
  parameters: [
    {
      name: "ArtifactBucket",
      description: "Bucket S3 donde deploy({ artifactBucket }) subió el código.",
      required: true,
    },
    {
      name: "AllowActions",
      description:
        "true crea el rol de acciones (matar, suspender, reanudar), el grupo de Cognito " +
        "que puede usarlo y la auditoría; sin él el panel es de sólo lectura.",
      default: "false",
    },
    {
      name: "EnableCostExplorer",
      description:
        "true permite consultar el coste facturado (ce:GetCostAndUsage, 0,01 USD por " +
        "consulta, cacheada 24 h en la tabla de caché). Sólo se aceptan ventanas de meses " +
        `completos de los últimos ${DASHBOARD_STACK_COST_LOOKBACK_MONTHS} meses y, en total, ` +
        `como mucho ${DASHBOARD_STACK_COST_MISSES_PER_DAY} consultas de pago por día UTC.`,
      default: "false",
    },
    {
      name: "ImageNamePrefix",
      description: "El rol de acciones sólo actúa sobre imágenes cuyo nombre empieza así.",
      default: "rayito-",
    },
    {
      name: "InitialUserEmail",
      description:
        "Opcional: crea este usuario de Cognito (invitación por correo) y, con " +
        "AllowActions=true, lo añade al grupo de acciones.",
      default: "",
    },
    {
      name: "DomainPrefix",
      description: "Opcional: prefijo del dominio del Hosted UI de Cognito (único en la región).",
      default: "",
    },
    {
      name: "ThemeJson",
      description:
        `Opcional: tema de color propio en JSON (hasta ${DASHBOARD_STACK_THEME_PARAMETER_MAX_BYTES} bytes; formato de ` +
        "examples/dashboard-theme). La pila lo guarda en un parámetro de SSM.",
      default: "",
    },
    {
      name: "ThemeParameterName",
      description:
        "Opcional: nombre (con «/» inicial) de un parámetro de SSM tuyo con el tema JSON, " +
        `para uno de más de ${DASHBOARD_STACK_THEME_PARAMETER_MAX_BYTES} bytes. ` +
        `Tiene prioridad sobre ThemeJson. ${SSM_ADVANCED_COST_NOTE}`,
      default: "",
    },
    {
      name: "LogoParameterName",
      description:
        "Opcional: nombre (con «/» inicial) de un parámetro de SSM con un logo SVG " +
        `(hasta ${DASHBOARD_STACK_SSM_ADVANCED_PARAMETER_MAX_BYTES} bytes). ${SSM_ADVANCED_COST_NOTE}`,
      default: "",
    },
    {
      name: "ReservedConcurrency",
      description:
        "Opcional: concurrencia reservada de la función; acota lo que cuesta una " +
        "avalancha de peticiones sin autenticar.",
      default: "",
    },
    {
      name: "LogRetentionDays",
      description: "Retención del log group de la función (días); nunca contiene tokens.",
      default: "30",
    },
  ],
  artifacts: [
    {
      name: "dashboard",
      parameterKey: "ArtifactS3Key",
      bucketParameterKey: "ArtifactBucket",
    },
  ],
  capabilities: ["CAPABILITY_IAM"],
  cost: {
    creates: [
      "AWS::Lambda::Function (1) + AWS::Lambda::Url + AWS::Logs::LogGroup",
      "AWS::Cognito::UserPool + UserPoolClient + UserPoolDomain " +
        "(+ UserPoolGroup con AllowActions=true)",
      "AWS::DynamoDB::Table (x2: auditoría y caché de coste, bajo demanda)",
      "AWS::IAM::Role (x1 de lectura; +1 de acciones con AllowActions=true)",
      "AWS::SSM::Parameter (Standard, sin coste; sólo con ThemeJson)",
    ],
    idleMonthly:
      "~$0 en reposo: sin tráfico no hay invocaciones, y Lambda, DynamoDB bajo demanda " +
      "y Cognito (capa gratuita de usuarios activos) sólo facturan por uso. Un " +
      "parámetro SSM Advanced propio (tema o logo) cuesta " +
      `${usdEs(SSM_ADVANCED_PARAMETER_PRICE_USD_PER_MONTH, 2)} al mes aunque no haya tráfico`,
    perUse: [
      "Lambda: una invocación por petición de la SPA, que sondea cada " +
        `${DASHBOARD_POLL_INTERVAL_SECONDS} s con la pestaña abierta. Ejemplo: una pestaña ` +
        `abierta ${DASHBOARD_STACK_COST_EXAMPLE_OPEN_HOURS_PER_DAY} h al día durante ` +
        `${DASHBOARD_STACK_COST_EXAMPLE_DAYS_PER_MONTH} días son unas ` +
        `${EXAMPLE_INVOCATIONS_PER_MONTH} invocaciones al mes, dentro de las ` +
        `${LAMBDA_FREE_TIER_REQUESTS_PER_MONTH} peticiones y los ` +
        `${LAMBDA_FREE_TIER_GB_SECONDS_PER_MONTH} GB-s gratuitos al mes; pasada la capa ` +
        `gratuita, ${usdEs(LAMBDA_REQUEST_PRICE_USD_PER_MILLION, 2)} por millón de ` +
        "peticiones más la duración. La Function URL es pública (AuthType NONE): el " +
        "tráfico sin autenticar también factura invocaciones, así que fija " +
        "ReservedConcurrency para acotarlo",
      "Cost Explorer (sólo con EnableCostExplorer=true): " +
        `${usdEs(COST_EXPLORER_REQUEST_PRICE_USD, 2)} por consulta, una cada 24 h por ` +
        "ventana gracias a la caché y, en total, como mucho " +
        `${DASHBOARD_STACK_COST_MISSES_PER_DAY} al día UTC (tope de la pila): en el peor ` +
        `caso ${usdEs(COST_EXPLORER_WORST_DAY_USD, 2)} al día, unos ` +
        `${usdEs(COST_EXPLORER_WORST_MONTH_USD, 2)} al mes`,
      "DynamoDB: una escritura por sandbox y acción (auditoría)",
      "SSM (sólo con tema o logo en parámetros Advanced): " +
        `${usdEs(SSM_API_INTERACTION_PRICE_USD_PER_TEN_THOUSAND, 2)} por cada ` +
        `${API_INTERACTIONS_PER_BILLING_UNIT} interacciones de API; la función relee cada ` +
        `${DASHBOARD_STACK_THEME_REFRESH_SECONDS} s por contenedor activo, hasta unas ` +
        `${THEME_READS_PER_MONTH} interacciones al mes ` +
        `(${usdEs(THEME_READS_MONTHLY_USD, 2)})`,
    ],
    removal:
      "destroy() borra todo lo que la pila creó, incluidas las filas de auditoría " +
      "(expórtalas antes si las necesitas) y los usuarios de Cognito",
    source:
      "AWS_API_NOTES.md §10; precios de Lambda, DynamoDB, Cognito, SSM Parameter Store " +
      "y Cost Explorer us-east-1, 2026-10-13",
  },
};
