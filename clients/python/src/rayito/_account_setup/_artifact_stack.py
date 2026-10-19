"""Componente `artifact-bucket` de `rayito setup`: la pila `rayito-m0-artifacts`
de `infra/artifact-bucket.yaml` (un bucket S3 privado, cifrado, sólo TLS y con
los zips de la imagen caducando; diseño D10). Se pasa a `OptionalStacks` como
objeto con `stack_name="rayito-m0-artifacts"`; no está en
`_stacks/_registry.COMPONENTS` por la misma razón que `iam`: es un requisito
previo de la cuenta, no una función opcional del catálogo `rayito stack`.
"""

from __future__ import annotations

from typing import Final

from rayito._account_setup._domain import (
    ARTIFACT_COMPONENT_NAME,
    ARTIFACT_EXPIRATION_DAYS,
    BUCKET_NAME_PARAMETER,
)
from rayito._stacks._model import CostStatement, StackComponent, StackParameter

ARTIFACT_STACK_COMPONENT: Final = StackComponent(
    name=ARTIFACT_COMPONENT_NAME,
    description=(
        "Bucket S3 de los zips de la imagen de infra/artifact-bucket.yaml, para "
        "rayito setup: cifrado, sin acceso público, sólo TLS y con los zips "
        f"caducando a los {ARTIFACT_EXPIRATION_DAYS} días."
    ),
    parameters=(
        StackParameter(
            BUCKET_NAME_PARAMETER,
            "Nombre del bucket, en la región de la pila.",
            required=True,
        ),
        StackParameter(
            "ArtifactExpirationDays",
            "Días tras los que se borra un objeto bajo rayito/images/.",
        ),
    ),
    capabilities=(),
    cost=CostStatement(
        creates=("AWS::S3::Bucket", "AWS::S3::BucketPolicy"),
        idle_monthly=(
            "céntimos: el almacenamiento de los zips de la imagen, que caducan a los "
            f"{ARTIFACT_EXPIRATION_DAYS} días"
        ),
        removal=(
            "destroy() borra la pila y la política del bucket; el bucket se conserva "
            "(DeletionPolicy: Retain)"
        ),
        source="AWS_API_NOTES.md, Preparación de la cuenta",
    ),
)
