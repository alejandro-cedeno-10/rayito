/**
 * Componente `quota-alerts` (ADR-026, fase 3): espejo exacto de
 * `rayito._stacks.components.quota_alerts` (Python). Una comprobación
 * programada (Lambda más una regla de EventBridge) que publica en SNS cuando
 * la memoria asignada a MicroVMs cruza un umbral de la cuota de la región, y
 * publica el uso como una métrica personalizada. Un aviso por cruce.
 */

import { PERCENT } from "../../dashboard/domain.js";
import { InvalidArgumentError } from "../../errors.js";
import {
  CLOUDWATCH_CUSTOM_METRIC_PRICE_USD_PER_MONTH,
  QUOTA_ALERTS_SCHEDULE_EXPRESSION,
  QUOTA_CRITICAL_FRACTION,
  QUOTA_METRIC_NAME,
  QUOTA_METRIC_NAMESPACE,
  QUOTA_WARNING_FRACTION,
} from "../../limits.js";
import type { StackComponent } from "../model.js";

/** El coste mensual de la métrica que publica la pila, con coma decimal. */
const CUSTOM_METRIC_MONTHLY_COST = CLOUDWATCH_CUSTOM_METRIC_PRICE_USD_PER_MONTH.toFixed(2).replace(
  ".",
  ",",
);
const WARNING_PARAMETER = "WarningPercent";
const CRITICAL_PARAMETER = "CriticalPercent";

/**
 * El aviso crítico debe ir por encima del primero: con el orden al revés la
 * función fallaría en cada ejecución programada y el aviso quedaría muerto sin
 * señal. Un valor que no es número lo rechaza la propia función.
 */
function requireOrderedThresholds(values: Readonly<Record<string, string>>): void {
  const warning = Number(values[WARNING_PARAMETER]);
  const critical = Number(values[CRITICAL_PARAMETER]);
  if (Number.isNaN(warning) || Number.isNaN(critical)) {
    return;
  }
  if (warning >= critical) {
    throw new InvalidArgumentError(`${CRITICAL_PARAMETER} debe ser mayor que ${WARNING_PARAMETER}`);
  }
}

export const COMPONENT: StackComponent = {
  name: "quota-alerts",
  description:
    "Avisos de cuota: una Lambda programada publica en SNS cuando la memoria asignada " +
    "a MicroVMs cruza un umbral de la cuota de la región.",
  parameters: [
    {
      name: "ArtifactBucket",
      description: "Bucket S3 donde deploy({ artifactBucket }) subió el código.",
      required: true,
    },
    {
      name: "ScheduleExpression",
      description: "Programación de EventBridge de la comprobación (rate(...) o cron(...)).",
      default: QUOTA_ALERTS_SCHEDULE_EXPRESSION,
    },
    {
      name: WARNING_PARAMETER,
      description: "Porcentaje de la cuota de memoria que dispara el primer aviso.",
      default: String(QUOTA_WARNING_FRACTION * PERCENT),
    },
    {
      name: CRITICAL_PARAMETER,
      description: "Porcentaje que dispara el aviso crítico; debe ser mayor que WarningPercent.",
      default: String(QUOTA_CRITICAL_FRACTION * PERCENT),
    },
    {
      name: "NotificationEmail",
      description:
        "Opcional: suscribe este correo al tema que crea la pila (hay que confirmar la " +
        "suscripción). Se ignora con TopicArn.",
      default: "",
    },
    {
      name: "TopicArn",
      description:
        "Opcional: publica en este tema SNS existente (misma cuenta y región) en vez de " +
        "crear uno.",
      default: "",
    },
    {
      name: "LogRetentionDays",
      description: "Retención del log group de la función (días).",
      default: "30",
    },
  ],
  artifacts: [
    {
      name: "quota-alerts",
      parameterKey: "ArtifactS3Key",
      bucketParameterKey: "ArtifactBucket",
    },
  ],
  capabilities: ["CAPABILITY_IAM"],
  cost: {
    creates: [
      "AWS::Lambda::Function (1) + AWS::Logs::LogGroup",
      "AWS::Events::Rule (1) + AWS::Lambda::Permission",
      "AWS::SNS::Topic (sin TopicArn) + AWS::SNS::Subscription (con NotificationEmail)",
      "AWS::DynamoDB::Table (1, bajo demanda, una fila de estado)",
      "AWS::IAM::Role (1, sólo lectura más sns:Publish, una métrica y la fila de estado)",
    ],
    idleMonthly:
      `~${CUSTOM_METRIC_MONTHLY_COST} USD al mes: una métrica personalizada ` +
      `(${QUOTA_METRIC_NAMESPACE}/` +
      `${QUOTA_METRIC_NAME}); Lambda, EventBridge, DynamoDB bajo demanda y SNS ` +
      "(correo) caben en sus capas gratuitas",
    perUse: [
      `Una ejecución por programación (${QUOTA_ALERTS_SCHEDULE_EXPRESSION} por defecto, ` +
        "~720 al mes): ListMicrovms, ListServiceQuotas, un PutMetricData, una lectura y " +
        "una escritura de DynamoDB",
      "SNS: un mensaje por cruce de umbral, no por ejecución",
    ],
    removal:
      "destroy() borra todo lo que la pila creó, incluido el tema SNS (no el de " +
      "TopicArn) y las suscripciones; la métrica deja de actualizarse",
    source:
      "AWS_API_NOTES.md §10 y §11; precios de CloudWatch, Lambda, EventBridge, " +
      "DynamoDB y SNS us-east-1, 2026-10-13",
  },
  validate: requireOrderedThresholds,
};
