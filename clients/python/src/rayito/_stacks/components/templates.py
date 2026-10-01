"""Stub de componente para `m15-templates` (M15 foundations). La feature
sustituye `COMPONENT` por la definición real (`infra/templates.yaml`: el
bucket de artefactos y la política `RayitoTemplateBuilder`) en su propio
cambio; hasta entonces `OptionalStacks.deploy/status/destroy` lanzan
`UnimplementedError` para este nombre (`supported=False`).
"""

from __future__ import annotations

from rayito._stacks._model import CostStatement, StackComponent

COMPONENT: StackComponent = StackComponent(
    name="templates",
    description=(
        "Pendiente de m15-templates: bucket de artefactos y política "
        "RayitoTemplateBuilder, para Template.build()."
    ),
    supported=False,
    cost=CostStatement(creates=(), idle_monthly="pendiente de m15-templates"),
)
