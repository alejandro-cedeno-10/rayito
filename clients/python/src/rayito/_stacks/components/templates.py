"""Componente `templates` (m15-templates): la política `RayitoTemplateBuilder`
de `infra/templates.yaml`, para quien llama a `Template.build()`. No crea
ningún bucket, imagen ni función Lambda ($0 en reposo: sólo IAM).
"""

from __future__ import annotations

from rayito._stacks._model import CostStatement, StackComponent, StackParameter

COMPONENT: StackComponent = StackComponent(
    name="templates",
    description=(
        "Política RayitoTemplateBuilder (CreateMicrovmImage/UpdateMicrovmImage/"
        "GetMicrovmImage*/ListMicrovmImageVersions sobre las imágenes de la cuenta, nunca "
        "sobre las imágenes base publicadas; PassRole sobre el rol de build, S3 del artefacto "
        "y lectura de logs de build), para Template.build()."
    ),
    parameters=(
        StackParameter(
            "ArtifactBucketArn",
            "ARN del bucket S3 al que Template.build() sube el zip de contexto (bucket= de "
            "Template.build()); se crea y se gestiona fuera de esta pila.",
            required=True,
        ),
        StackParameter(
            "BuildRoleArn",
            "ARN del rol de IAM que Template.build() pasa como build_role_arn "
            "(la salida BuildRoleArn de infra/iam.yaml).",
            required=True,
        ),
        StackParameter(
            "ImageLogGroupPrefix",
            "Prefijo de los grupos de logs de imagen que Template.build() relee si un build "
            "falla (/rayito/<nombre>).",
            default="/rayito",
        ),
        StackParameter(
            "BaseImageBucketArn",
            "ARN del bucket con el zip de codeArtifact de rayito-base (from_base_image()) si no "
            "es el bucket de artefactos; vacío significa el bucket de artefactos.",
        ),
        StackParameter(
            "ProtectedImageNamePrefix",
            "Prefijo de las imágenes que Template.build() nunca puede crear ni actualizar (las "
            "imágenes base publicadas).",
            default="rayito-base",
        ),
    ),
    capabilities=("CAPABILITY_IAM",),
    cost=CostStatement(
        creates=("AWS::IAM::ManagedPolicy",),
        idle_monthly="$0 (sólo IAM)",
        per_use=("almacenamiento de snapshot por versión de imagen nueva, no de esta pila",),
        removal="destroy() borra la política; no borra ninguna versión de imagen ni objeto S3",
        source="AWS_API_NOTES.md §27",
    ),
)
