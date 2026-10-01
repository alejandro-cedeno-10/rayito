"""Stub de componente para `m15-sizes-catalog` (M15 foundations). La
feature sustituye `COMPONENT` por la definición real (`infra/sizes-guard.yaml`:
la política `RayitoRunAllowedSizes`) en su propio cambio; hasta entonces
`OptionalStacks.deploy/status/destroy` lanzan `UnimplementedError` para
este nombre (`supported=False`).
"""

from __future__ import annotations

from rayito._stacks._model import CostStatement, StackComponent

COMPONENT: StackComponent = StackComponent(
    name="sizes-guard",
    description="Pendiente de m15-sizes-catalog: política RayitoRunAllowedSizes por tamaño.",
    supported=False,
    cost=CostStatement(creates=(), idle_monthly="$0 (sólo IAM, cuando exista)"),
)
