"""Componente `otlp-export` (m15-rayd-otlp): la política IAM
`RayitoOtlpExport` de `infra/otlp-export.yaml`, para `telemetry=
TelemetryExport(auth=OtlpAuth.execution_role())`. Sin parámetros: la
política se adjunta directamente al execution role que use `telemetry=` con
auth por rol (fuera de este stack; `deploy()` sólo crea la política, no la
adjunta a nada).
"""

from __future__ import annotations

from rayito._stacks._model import CostStatement, StackComponent

COMPONENT: StackComponent = StackComponent(
    name="otlp-export",
    description=(
        "Política IAM RayitoOtlpExport (cloudwatch:PutMetricData sobre el dataset OTLP "
        "por defecto de la cuenta), para telemetry=TelemetryExport(auth=OtlpAuth.execution_role())."
    ),
    capabilities=("CAPABILITY_IAM",),
    cost=CostStatement(
        creates=("AWS::IAM::ManagedPolicy",),
        idle_monthly="$0 (sólo IAM)",
        per_use=(
            "Las métricas que de verdad se exporten se facturan como cualquier métrica "
            "personalizada de CloudWatch, no por esta política.",
        ),
        removal="destroy() borra la política; no borra ninguna métrica ya exportada",
        source="AWS_API_NOTES.md §26 (research OT1/OT9)",
    ),
)
