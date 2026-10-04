"""Componente `events-webhooks` (M15, m15-events-webhooks):
`infra/events-webhooks.yaml` — el secreto HMAC del stack, la tabla de
eventos/webhooks, el forwarder/deliverer/reconciliador y la regla de
EventBridge Scheduler, para `events=`/`LifecycleEvents`. Ver
`infra/README.md` para el detalle completo; este módulo sólo describe la
pila para `OptionalStacks`.
"""

from __future__ import annotations

from rayito._lifecycle_events._domain import (
    DEFAULT_RECONCILER_INTERVAL_MINUTES,
    MIN_RECONCILER_INTERVAL_MINUTES,
)
from rayito._stacks._model import CostStatement, StackArtifact, StackComponent, StackParameter

COMPONENT: StackComponent = StackComponent(
    name="events-webhooks",
    description=(
        "Secreto HMAC del stack, tabla de eventos/webhooks, forwarder/deliverer/"
        "reconciliador y el scheduler, para events=/LifecycleEvents."
    ),
    parameters=(
        StackParameter(
            "LogGroupName",
            'Log group de la imagen (el mismo que logging="cloudwatch" ya usa).',
            required=True,
        ),
        StackParameter(
            "ArtifactBucket",
            "Bucket S3 donde OptionalStacks.deploy(artifact_bucket=...) subió el código.",
            required=True,
        ),
        StackParameter(
            "ReconcilerIntervalMinutes",
            (
                "Frecuencia del reconciliador (rate), en minutos; "
                f"{DEFAULT_RECONCILER_INTERVAL_MINUTES} por defecto, "
                f"mínimo {MIN_RECONCILER_INTERVAL_MINUTES}."
            ),
            default=str(DEFAULT_RECONCILER_INTERVAL_MINUTES),
        ),
    ),
    artifacts=(
        StackArtifact(
            name="events-webhooks",
            parameter_key="ArtifactS3Key",
            bucket_parameter_key="ArtifactBucket",
        ),
    ),
    capabilities=("CAPABILITY_IAM",),
    cost=CostStatement(
        creates=(
            "AWS::SecretsManager::Secret",
            "AWS::DynamoDB::Table (streams habilitados)",
            "AWS::Lambda::Function (x3) + AWS::Logs::LogGroup (x3, uno por función)",
            "AWS::Logs::SubscriptionFilter",
            "AWS::Scheduler::Schedule",
            "AWS::SQS::Queue (x2: destinos OnFailure del forwarder y del deliverer; vacías en "
            "condiciones normales)",
            "AWS::IAM::Role (x4) + AWS::IAM::ManagedPolicy (x4: lanzador, lector, "
            "administración de webhooks y la unión obsoleta)",
        ),
        idle_monthly="~$0,40/mes (el secreto; DynamoDB, Lambda y SQS son on-demand/por uso)",
        per_use=(
            "Forwarder: 1 invocación Lambda por lote de líneas del log",
            "Deliverer: 1 invocación por lote del stream de DynamoDB",
            "Reconciliador: 1 invocación cada ReconcilerIntervalMinutes (~$0,0000002 c/u)",
            "~$1,25 por millón de eventos escritos (WRU) + lecturas de get_events (RRU)",
            "SQS: sólo factura cuando una invocación del forwarder agota sus reintentos o un "
            "lote entero del deliverer falla (BisectBatchOnFunctionError ya aísla el registro "
            "problemático); dentro de la capa gratuita al volumen de este stack",
        ),
        removal=(
            "destroy() borra el secreto (force-delete: cualquier webhook ya registrado "
            "deja de poder verificarse), la tabla, las tres Lambdas con sus log groups, la "
            "suscripción, las dos colas de fallos y el scheduler"
        ),
        source=(
            "AWS_API_NOTES.md §25; precios de Lambda/DynamoDB/Scheduler/SQS us-east-1, 2026-09-30"
        ),
    ),
)
