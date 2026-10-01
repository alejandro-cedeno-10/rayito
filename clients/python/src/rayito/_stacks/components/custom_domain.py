"""Stub de componente para `m15-custom-domain` (M15 foundations). La
feature sustituye `COMPONENT` por la definición real (`infra/custom-domain.yaml`:
distribución CloudFront, la CloudFront Function de enrutado, el
KeyValueStore y el refresher opcional) en su propio cambio, que necesita un
dominio del mantenedor y un certificado ACM en us-east-1 (D3); hasta
entonces `OptionalStacks.deploy/status/destroy` lanzan `UnimplementedError`
para este nombre (`supported=False`).
"""

from __future__ import annotations

from rayito._stacks._model import CostStatement, StackComponent

COMPONENT: StackComponent = StackComponent(
    name="custom-domain",
    description=(
        "Pendiente de m15-custom-domain: distribución CloudFront, CloudFront Function "
        "de enrutado y KeyValueStore, para domain=."
    ),
    supported=False,
    cost=CostStatement(creates=(), idle_monthly="pendiente de m15-custom-domain"),
)
