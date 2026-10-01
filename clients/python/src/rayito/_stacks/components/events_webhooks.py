"""Componente `events-webhooks` (M15, m15-events-webhooks):
`infra/events-webhooks.yaml` — el secreto HMAC del stack, la tabla de
eventos/webhooks, el forwarder/deliverer/reconciliador y la regla de
EventBridge Scheduler, para `events=`/`LifecycleEvents`. Ver
`infra/README.md` para el detalle completo; este módulo sólo describe la
pila para `OptionalStacks`.
"""

from __future__ import annotations

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
            "Frecuencia del reconciliador (rate), en minutos; 5 por defecto.",
            default="5",
        ),
        StackParameter(
            "ReconcilerImageArn",
            "ARN de imagen a anotar en un evento killed sintetizado; informativo.",
            default="",
        ),
        StackParameter(
            "ReconcilerImageVersion",
            "Versión de imagen a anotar en un evento killed sintetizado; informativo.",
            default="",
        ),
    ),
    artifacts=(StackArtifact(name="events-webhooks", parameter_key="ArtifactS3Key"),),
    capabilities=("CAPABILITY_IAM",),
    cost=CostStatement(
        creates=(
            "AWS::SecretsManager::Secret",
            "AWS::DynamoDB::Table (streams habilitados)",
            "AWS::Lambda::Function (x3)",
            "AWS::Logs::SubscriptionFilter",
            "AWS::Scheduler::Schedule",
            "AWS::IAM::Role (x4) + AWS::IAM::ManagedPolicy",
        ),
        idle_monthly="~$0,40/mes (el secreto; DynamoDB y Lambda son on-demand/por invocación)",
        per_use=(
            "Forwarder: 1 invocación Lambda por lote de líneas del log",
            "Deliverer: 1 invocación por lote del stream de DynamoDB",
            "Reconciliador: 1 invocación cada ReconcilerIntervalMinutes (~$0,0000002 c/u)",
            "~$1,25 por millón de eventos escritos (WRU) + lecturas de get_events (RRU)",
        ),
        removal=(
            "destroy() borra el secreto (force-delete: cualquier webhook ya registrado "
            "deja de poder verificarse), la tabla, las tres Lambdas, la suscripción y el "
            "scheduler"
        ),
        source="AWS_API_NOTES.md §25; precios de Lambda/DynamoDB/Scheduler us-east-1, 2026-09-30",
    ),
)
