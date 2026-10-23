"""Componente `iam` de `rayito setup`: la pila `rayito-m0-iam` de
`infra/iam.yaml` (los dos roles y las tres políticas del SDK). Se pasa a
`OptionalStacks` como objeto con `stack_name="rayito-m0-iam"`; no está en
`_stacks/_registry.COMPONENTS` porque `rayito stack list` muestra
funciones opcionales con coste y ésta es un requisito previo con su nombre
histórico.
"""

from __future__ import annotations

from typing import Final

from rayito._account_setup._domain import (
    ARTIFACT_BUCKET_PARAMETER,
    IAM_CAPABILITIES,
    IAM_COMPONENT_NAME,
    TEMPLATE_FINGERPRINT_PARAMETER,
)
from rayito._stacks._model import CostStatement, StackComponent, StackParameter

IAM_STACK_COMPONENT: Final = StackComponent(
    name=IAM_COMPONENT_NAME,
    description=(
        "Roles de build y de ejecución y las políticas SandboxLauncherPolicy, "
        "ImagePublisherPolicy y CallerPolicy de infra/iam.yaml, para rayito setup."
    ),
    parameters=(
        StackParameter(
            ARTIFACT_BUCKET_PARAMETER,
            "Bucket S3 con los zips de la imagen, en la región de las imágenes.",
            required=True,
        ),
        StackParameter("LogGroupPrefix", "Prefijo explícito de los grupos de logs."),
        StackParameter("NetworkConnectorArns", "ARNs de conectores de red propios permitidos."),
        StackParameter("PersistenceBucket", "Bucket de persist= (vacío: sin acceso a S3)."),
        StackParameter("PersistencePrefix", "Prefijo de las claves bajo PersistenceBucket."),
        StackParameter("TransferBucket", "Bucket de upload_url/download_url (vacío: ninguno)."),
        StackParameter("TransferPrefix", "Prefijo de las claves bajo TransferBucket."),
        StackParameter(
            TEMPLATE_FINGERPRINT_PARAMETER,
            "sha256 de la plantilla que desplegó la pila (lo pone rayito setup).",
        ),
    ),
    capabilities=IAM_CAPABILITIES,
    cost=CostStatement(
        creates=("AWS::IAM::Role (x2)", "AWS::IAM::ManagedPolicy (x3)"),
        idle_monthly="$0 (IAM no se factura)",
        removal="destroy() borra los roles y las políticas; no toca ningún bucket ni imagen",
        source="AWS_API_NOTES.md §10",
    ),
)
