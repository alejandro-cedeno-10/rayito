"""Componente `quota-alerts` (ADR-026, fase 3): `infra/quota-alerts.yaml`, una
comprobación programada (Lambda más una regla de EventBridge) que publica en
SNS cuando la memoria asignada a MicroVMs cruza un umbral de la cuota de la
región, y publica el uso como una métrica personalizada. Un aviso por cruce,
no uno por ejecución. Ver `infra/README.md`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from rayito._limits import (
    CLOUDWATCH_CUSTOM_METRIC_PRICE_USD_PER_MONTH,
    QUOTA_ALERTS_SCHEDULE_EXPRESSION,
    QUOTA_CRITICAL_FRACTION,
    QUOTA_METRIC_NAME,
    QUOTA_METRIC_NAMESPACE,
    QUOTA_WARNING_FRACTION,
)
from rayito._percent import PERCENT
from rayito._stacks._model import CostStatement, StackArtifact, StackComponent, StackParameter
from rayito.exceptions import InvalidArgumentException

#: El coste mensual de la métrica que publica la pila, con coma decimal; la ayuda
#: de `rayito dashboard cloudwatch --quota` lo repite.
CUSTOM_METRIC_MONTHLY_COST: Final = f"{CLOUDWATCH_CUSTOM_METRIC_PRICE_USD_PER_MONTH:.2f}".replace(
    ".", ","
)
WARNING_PARAMETER: Final = "WarningPercent"
CRITICAL_PARAMETER: Final = "CriticalPercent"


def require_ordered_thresholds(values: Mapping[str, str]) -> None:
    """El aviso crítico debe ir por encima del primero: con el orden al revés
    la función fallaría en cada ejecución programada y el aviso quedaría
    muerto sin señal. Un valor que no es número lo rechaza la propia función."""
    try:
        warning = float(values[WARNING_PARAMETER])
        critical = float(values[CRITICAL_PARAMETER])
    except (KeyError, ValueError):
        return
    if warning >= critical:
        raise InvalidArgumentException(
            f"{CRITICAL_PARAMETER} debe ser mayor que {WARNING_PARAMETER}"
        )


COMPONENT: StackComponent = StackComponent(
    name="quota-alerts",
    description=(
        "Avisos de cuota: una Lambda programada publica en SNS cuando la memoria asignada "
        "a MicroVMs cruza un umbral de la cuota de la región."
    ),
    parameters=(
        StackParameter(
            "ArtifactBucket",
            "Bucket S3 donde OptionalStacks.deploy(artifact_bucket=...) subió el código.",
            required=True,
        ),
        StackParameter(
            "ScheduleExpression",
            "Programación de EventBridge de la comprobación (rate(...) o cron(...)).",
            default=QUOTA_ALERTS_SCHEDULE_EXPRESSION,
        ),
        StackParameter(
            WARNING_PARAMETER,
            "Porcentaje de la cuota de memoria que dispara el primer aviso.",
            default=f"{QUOTA_WARNING_FRACTION * PERCENT:g}",
        ),
        StackParameter(
            CRITICAL_PARAMETER,
            "Porcentaje que dispara el aviso crítico; debe ser mayor que WarningPercent.",
            default=f"{QUOTA_CRITICAL_FRACTION * PERCENT:g}",
        ),
        StackParameter(
            "NotificationEmail",
            "Opcional: suscribe este correo al tema que crea la pila (hay que confirmar la "
            "suscripción). Se ignora con TopicArn.",
            default="",
        ),
        StackParameter(
            "TopicArn",
            "Opcional: publica en este tema SNS existente (misma cuenta y región) en vez de "
            "crear uno.",
            default="",
        ),
        StackParameter(
            "LogRetentionDays",
            "Retención del log group de la función (días).",
            default="30",
        ),
    ),
    artifacts=(
        StackArtifact(
            name="quota-alerts",
            parameter_key="ArtifactS3Key",
            bucket_parameter_key="ArtifactBucket",
        ),
    ),
    capabilities=("CAPABILITY_IAM",),
    cost=CostStatement(
        creates=(
            "AWS::Lambda::Function (1) + AWS::Logs::LogGroup",
            "AWS::Events::Rule (1) + AWS::Lambda::Permission",
            "AWS::SNS::Topic (sin TopicArn) + AWS::SNS::Subscription (con NotificationEmail)",
            "AWS::DynamoDB::Table (1, bajo demanda, una fila de estado)",
            "AWS::IAM::Role (1, sólo lectura más sns:Publish, una métrica y la fila de estado)",
        ),
        idle_monthly=(
            f"~{CUSTOM_METRIC_MONTHLY_COST} USD al mes: una métrica personalizada "
            f"({QUOTA_METRIC_NAMESPACE}/"
            f"{QUOTA_METRIC_NAME}); Lambda, EventBridge, DynamoDB bajo demanda y SNS "
            "(correo) caben en sus capas gratuitas"
        ),
        per_use=(
            f"Una ejecución por programación ({QUOTA_ALERTS_SCHEDULE_EXPRESSION} por defecto, "
            "~720 al mes): ListMicrovms, ListServiceQuotas, un PutMetricData, una lectura y "
            "una escritura de DynamoDB",
            "SNS: un mensaje por cruce de umbral, no por ejecución",
        ),
        removal=(
            "destroy() borra todo lo que la pila creó, incluido el tema SNS (no el de "
            "TopicArn) y las suscripciones; la métrica deja de actualizarse"
        ),
        source=(
            "AWS_API_NOTES.md §10 y §11; precios de CloudWatch, Lambda, EventBridge, "
            "DynamoDB y SNS us-east-1, 2026-10-13"
        ),
    ),
    validate=require_ordered_thresholds,
)
