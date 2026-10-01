/**
 * Componente `otlp-export` (m15-rayd-otlp): la política IAM
 * `RayitoOtlpExport` de `infra/otlp-export.yaml`, para `telemetry:
 * new TelemetryExport({ auth: OtlpAuth.executionRole() })`.
 */

import type { StackComponent } from "../model.js";

export const COMPONENT: StackComponent = {
  name: "otlp-export",
  description:
    "Política IAM RayitoOtlpExport (cloudwatch:PutMetricData sobre el dataset OTLP por " +
    "defecto de la cuenta), para telemetry con OtlpAuth.executionRole().",
  capabilities: ["CAPABILITY_IAM"],
  cost: {
    creates: ["AWS::IAM::ManagedPolicy"],
    idleMonthly: "$0 (sólo IAM)",
    perUse: [
      "Las métricas que de verdad se exporten se facturan como cualquier métrica " +
        "personalizada de CloudWatch, no por esta política.",
    ],
    removal: "destroy() borra la política; no borra ninguna métrica ya exportada",
    source: "AWS_API_NOTES.md §26 (research OT1/OT9)",
  },
};
