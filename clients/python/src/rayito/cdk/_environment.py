"""El entorno del stack de un constructo de `rayito.cdk` (ADR-032 D7) y sus
etiquetas fijas (D6, D11). La síntesis nunca llama a AWS: la región tiene que
ser concreta (`region_unresolved` si es un token) y la cuenta puede ser un
token, con los ARNs armados con `AWS::AccountId` y `AWS::Partition`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

import aws_cdk as cdk
from aws_cdk import aws_logs as logs
from constructs import Construct, IConstruct

from rayito._agentcore._iam_resources import AWS_PARTITION
from rayito._limits import (
    AGENTCORE_BUILD_LOG_RETENTION_DAYS,
    CDK_MANAGED_BY_TAG_VALUE,
    CDK_MICROVM_IMAGE_LOG_RETENTION_DAYS,
)
from rayito._stacks._model import FIXED_TAG_KEYS
from rayito._version import __version__
from rayito.cdk._errors import RayitoCdkException

ACCOUNT_PATTERN: Final = re.compile(r"[0-9]{12}")
COMPONENT_TAG, MANAGED_BY_TAG, SDK_VERSION_TAG = FIXED_TAG_KEYS
#: `rayito:component` de cada constructo (`contract.constants.componentTagValues`).
AGENT_COMPONENT: Final = "agentcore-runtime"
MICROVM_IMAGE_COMPONENT: Final = "microvm-image"
#: Las etiquetas fijas ganan a las del usuario (`stack_tags`): prioridad mayor
#: que la de `Tags.of(...).add` por defecto (100).
FIXED_TAG_PRIORITY: Final = 200
#: `logs.RetentionDays` de los días de `limits.json`; un valor nuevo en
#: `limits.json` sin su entrada aquí falla al importar, no en un despliegue.
RETENTION_BY_DAYS: Final[Mapping[int, logs.RetentionDays]] = {
    1: logs.RetentionDays.ONE_DAY,
    3: logs.RetentionDays.THREE_DAYS,
    5: logs.RetentionDays.FIVE_DAYS,
    7: logs.RetentionDays.ONE_WEEK,
    14: logs.RetentionDays.TWO_WEEKS,
    30: logs.RetentionDays.ONE_MONTH,
}
HANDLER_LOG_RETENTION: Final = RETENTION_BY_DAYS[AGENTCORE_BUILD_LOG_RETENTION_DAYS]
IMAGE_LOG_RETENTION: Final = RETENTION_BY_DAYS[CDK_MICROVM_IMAGE_LOG_RETENTION_DAYS]


@dataclass(frozen=True)
class StackEnvironment:
    """Dónde sintetiza el constructo: la región (siempre concreta), la cuenta
    (un id de 12 dígitos o el token `AWS::AccountId`) y la partición (`aws`
    con una cuenta concreta; el token `AWS::Partition` con una cuenta token)."""

    stack: cdk.Stack
    region: str
    account: str
    partition: str
    account_is_token: bool


def stack_environment(scope: Construct, construct: str) -> StackEnvironment:
    """El entorno del stack de `scope`, o `region_unresolved` /
    `account_invalid`, antes de añadir nada al árbol."""
    stack = cdk.Stack.of(scope)
    region = stack.region
    if cdk.Token.is_unresolved(region):
        raise RayitoCdkException("region_unresolved", construct=construct)
    account = stack.account
    if cdk.Token.is_unresolved(account):
        return StackEnvironment(stack, region, cdk.Aws.ACCOUNT_ID, cdk.Aws.PARTITION, True)
    if ACCOUNT_PATTERN.fullmatch(account) is None:
        raise RayitoCdkException("account_invalid", account=account)
    return StackEnvironment(stack, region, account, AWS_PARTITION, False)


def fixed_tags(component: str) -> dict[str, str]:
    """Las tres etiquetas fijas (`contract.constants.fixedTagKeys`)."""
    return {
        COMPONENT_TAG: component,
        MANAGED_BY_TAG: CDK_MANAGED_BY_TAG_VALUE,
        SDK_VERSION_TAG: __version__,
    }


def apply_tags(scope: IConstruct, component: str, user_tags: Mapping[str, str]) -> None:
    """Las etiquetas del usuario y las fijas en todo recurso etiquetable del
    constructo (pila anidada incluida), al crearlo: así se cumplen las
    políticas que exigen etiquetas al crear."""
    tags = cdk.Tags.of(scope)
    for key, value in user_tags.items():
        tags.add(key, value)
    for key, value in fixed_tags(component).items():
        tags.add(key, value, priority=FIXED_TAG_PRIORITY)


__all__ = [
    "AGENT_COMPONENT",
    "HANDLER_LOG_RETENTION",
    "IMAGE_LOG_RETENTION",
    "MICROVM_IMAGE_COMPONENT",
    "StackEnvironment",
    "apply_tags",
    "fixed_tags",
    "stack_environment",
]
