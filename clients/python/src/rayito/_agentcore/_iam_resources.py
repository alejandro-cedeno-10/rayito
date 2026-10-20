"""Validación pura de lo que acaba como `Resource` de IAM en el rol del
runtime de `--target agentcore`: los secretos legibles, los modelos de Bedrock
y el modelo del proyecto (`contract.json`, `iamResourceRules`). Un nombre o id
sólo lleva los caracteres de su servicio, sin `*`, `?` ni `,`; un ARN debe
nombrar la partición, la región y la cuenta del despliegue, sin comodines.
Sólo importa la biblioteca estándar y las excepciones del SDK."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final

from rayito.exceptions import AgentCoreException

ARN_PREFIX: Final = "arn:"
#: La partición de las regiones comerciales de AgentCore (`awsPartition`).
AWS_PARTITION: Final = "aws"

#: Un nombre de secreto de Secrets Manager: hasta 512 caracteres de
#: `[A-Za-z0-9/_+=.@-]` (`contract.json`, `iamResourceRules`).
SECRET_NAME_PATTERN: Final = re.compile(r"[A-Za-z0-9/_+=.@-]+")
SECRET_NAME_MAX_LENGTH: Final = 512
#: Un id de modelo o de perfil de inferencia de Bedrock, sin comodines.
MODEL_ID_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*")
ACCOUNT_PATTERN: Final = re.compile(r"[0-9]{12}")

ARN_FIELD_SEPARATOR: Final = ":"
ARN_FIELD_COUNT: Final = 6
ARN_PARTITION_INDEX: Final = 1
ARN_SERVICE_INDEX: Final = 2
ARN_REGION_INDEX: Final = 3
ARN_ACCOUNT_INDEX: Final = 4
ARN_RESOURCE_INDEX: Final = 5
SECRETS_MANAGER_SERVICE: Final = "secretsmanager"
SECRET_RESOURCE_PREFIX: Final = "secret:"
BEDROCK_SERVICE: Final = "bedrock"
FOUNDATION_MODEL_PREFIX: Final = "foundation-model/"
ANY_REGION: Final = "*"
GLOBAL_REGION: Final = ""
PROFILE_RESOURCE_PREFIXES: Final = ("inference-profile/", "application-inference-profile/")

FIELD_READABLE_SECRETS: Final = "readable_secrets"
FIELD_BEDROCK_MODELS: Final = "bedrock_models"
FIELD_MODEL_ID: Final = "model.id"


def _arn_fields(value: str, partition: str) -> list[str] | None:
    fields = value.split(ARN_FIELD_SEPARATOR, ARN_FIELD_COUNT - 1)
    if len(fields) != ARN_FIELD_COUNT or fields[ARN_PARTITION_INDEX] != partition:
        return None
    return fields


def _account_matches(found: str, account: str | None) -> bool:
    if account is not None:
        return found == account
    return ACCOUNT_PATTERN.fullmatch(found) is not None


def _secret_name_is_valid(name: str) -> bool:
    return len(name) <= SECRET_NAME_MAX_LENGTH and SECRET_NAME_PATTERN.fullmatch(name) is not None


def _secret_arn_is_valid(fields: list[str], region: str, account: str | None) -> bool:
    resource = fields[ARN_RESOURCE_INDEX]
    return (
        fields[ARN_SERVICE_INDEX] == SECRETS_MANAGER_SERVICE
        and fields[ARN_REGION_INDEX] == region
        and _account_matches(fields[ARN_ACCOUNT_INDEX], account)
        and resource.startswith(SECRET_RESOURCE_PREFIX)
        and SECRET_NAME_PATTERN.fullmatch(resource.removeprefix(SECRET_RESOURCE_PREFIX)) is not None
    )


def _model_arn_is_valid(fields: list[str], region: str, account: str | None) -> bool:
    if fields[ARN_SERVICE_INDEX] != BEDROCK_SERVICE:
        return False
    resource = fields[ARN_RESOURCE_INDEX]
    found_region = fields[ARN_REGION_INDEX]
    found_account = fields[ARN_ACCOUNT_INDEX]
    if resource.startswith(FOUNDATION_MODEL_PREFIX):
        model_id = resource.removeprefix(FOUNDATION_MODEL_PREFIX)
        return (
            found_region in (region, ANY_REGION, GLOBAL_REGION)
            and not found_account
            and MODEL_ID_PATTERN.fullmatch(model_id) is not None
        )
    for prefix in PROFILE_RESOURCE_PREFIXES:
        if resource.startswith(prefix):
            return (
                found_region == region
                and _account_matches(found_account, account)
                and MODEL_ID_PATTERN.fullmatch(resource.removeprefix(prefix)) is not None
            )
    return False


def check_secret_reference(
    secret: str,
    region: str,
    account: str | None = None,
    partition: str = AWS_PARTITION,
    *,
    field: str = FIELD_READABLE_SECRETS,
) -> None:
    """Un nombre de secreto o un ARN de Secrets Manager de `region` y `account`
    (`None` sólo exige que sea de doce dígitos), o `iam_resource_invalid`."""
    if secret.startswith(ARN_PREFIX):
        fields = _arn_fields(secret, partition)
        valid = fields is not None and _secret_arn_is_valid(fields, region, account)
    else:
        valid = _secret_name_is_valid(secret)
    if not valid:
        raise AgentCoreException("iam_resource_invalid", field=field)


def check_model_reference(
    model: str,
    region: str,
    account: str | None = None,
    partition: str = AWS_PARTITION,
    *,
    field: str = FIELD_BEDROCK_MODELS,
) -> None:
    """Un id de modelo o de perfil de Bedrock, o un ARN de Bedrock de `region` y
    `account` (el modelo base también sin región o en cualquiera), o
    `iam_resource_invalid`."""
    if model.startswith(ARN_PREFIX):
        fields = _arn_fields(model, partition)
        valid = fields is not None and _model_arn_is_valid(fields, region, account)
    else:
        valid = MODEL_ID_PATTERN.fullmatch(model) is not None
    if not valid:
        raise AgentCoreException("iam_resource_invalid", field=field)


def check_iam_inputs(
    project_model: str,
    bedrock_models: Sequence[str],
    readable_secrets: Sequence[str],
    *,
    model_region: str,
    region: str,
) -> None:
    """Valida, sin AWS, el modelo del proyecto, `bedrock_models` y
    `readable_secrets`, en ese orden."""
    check_model_reference(project_model, model_region, field=FIELD_MODEL_ID)
    for model in bedrock_models:
        check_model_reference(model, model_region)
    for secret in readable_secrets:
        check_secret_reference(secret, region)
