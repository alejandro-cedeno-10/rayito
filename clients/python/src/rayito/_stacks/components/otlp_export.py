"""Stub de componente para `m15-rayd-otlp` (M15 foundations). La feature
sustituye `COMPONENT` por la definición real (`infra/otlp-export.yaml`: la
política `RayitoOtlpExport` sobre `cloudwatch:PutMetricData`) en su propio
cambio; hasta entonces `OptionalStacks.deploy/status/destroy` lanzan
`UnimplementedError` para este nombre (`supported=False`).
"""

from __future__ import annotations

from rayito._stacks._model import CostStatement, StackComponent

COMPONENT: StackComponent = StackComponent(
    name="otlp-export",
    description="Pendiente de m15-rayd-otlp: política RayitoOtlpExport para telemetry=.",
    supported=False,
    cost=CostStatement(creates=(), idle_monthly="$0 (sólo IAM, cuando exista)"),
)
