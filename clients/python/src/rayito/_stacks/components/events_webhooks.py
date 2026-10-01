"""Stub de componente para `m15-events-webhooks` (M15 foundations). La
feature sustituye `COMPONENT` por la definición real (`infra/events-webhooks.yaml`:
secreto HMAC, tabla DynamoDB, suscripción de Logs, Lambdas de forwarder/
deliverer/reconciler, regla de EventBridge Scheduler) en su propio cambio;
hasta entonces `OptionalStacks.deploy/status/destroy` lanzan
`UnimplementedError` para este nombre (`supported=False`).
"""

from __future__ import annotations

from rayito._stacks._model import CostStatement, StackComponent

COMPONENT: StackComponent = StackComponent(
    name="events-webhooks",
    description=(
        "Pendiente de m15-events-webhooks: secreto HMAC, tabla de eventos/webhooks, "
        "forwarder/deliverer/reconciler y el scheduler, para events=."
    ),
    supported=False,
    cost=CostStatement(creates=(), idle_monthly="pendiente de m15-events-webhooks"),
)
