"""Componente `agentcore-runtime`: la pila `rayito-agentcore-<nombre>` de
`infra/agentcore-runtime.yaml` (ADR-031), con el repositorio de ECR, el proyecto
de CodeBuild y su grupo de logs, y los roles de build y del runtime. Se pasa a
`OptionalStacks` como objeto con `stack_name=rayito-agentcore-<nombre>`; no está
en `_stacks/_registry.COMPONENTS` porque `rayito stack list` muestra funciones
opcionales del catálogo y ésta la despliega `rayito agent deploy --target
agentcore`, como las pilas de `rayito setup`.
"""

from __future__ import annotations

from typing import Final

from rayito._agentcore._domain import (
    STACK_PARAMETER_AGENT_NAME,
    STACK_PARAMETER_BUCKET,
    STACK_PARAMETER_BUILD_TIMEOUT,
    STACK_PARAMETER_LOG_DAYS,
    STACK_PARAMETER_MODEL_ARNS,
    STACK_PARAMETER_RETENTION,
    STACK_PARAMETER_RUNTIME_NAME,
    STACK_PARAMETER_SECRET_ARNS,
)
from rayito._limits import (
    AGENTCORE_BUILD_LOG_RETENTION_DAYS,
    AGENTCORE_BUILD_TIMEOUT_MINUTES,
    AGENTCORE_IMAGE_RETENTION_COUNT,
)
from rayito._stacks._model import CostStatement, StackComponent, StackParameter

STACK_COMPONENT_NAME: Final = "agentcore-runtime"
STACK_CAPABILITIES: Final = ("CAPABILITY_IAM",)

AGENTCORE_STACK_COMPONENT: Final = StackComponent(
    name=STACK_COMPONENT_NAME,
    description=(
        "Repositorio de ECR, proyecto de CodeBuild con su grupo de logs, y los roles de build y "
        "del runtime de un agente en AgentCore Runtime (infra/agentcore-runtime.yaml)."
    ),
    parameters=(
        StackParameter(STACK_PARAMETER_AGENT_NAME, "Nombre del agente de Rayito.", required=True),
        StackParameter(
            STACK_PARAMETER_RUNTIME_NAME, "Nombre del runtime de AgentCore.", required=True
        ),
        StackParameter(
            STACK_PARAMETER_BUCKET,
            "Bucket de artefactos de la región con los zips del contexto de build.",
            required=True,
        ),
        StackParameter(
            STACK_PARAMETER_MODEL_ARNS,
            "ARNs de modelos y perfiles de Bedrock que el rol del runtime puede invocar.",
            default="",
        ),
        StackParameter(
            STACK_PARAMETER_SECRET_ARNS,
            "ARNs de secretos de Secrets Manager que el rol del runtime puede leer.",
            default="",
        ),
        StackParameter(
            STACK_PARAMETER_RETENTION,
            "Imágenes que se conservan en el repositorio.",
            default=str(AGENTCORE_IMAGE_RETENTION_COUNT),
        ),
        StackParameter(
            STACK_PARAMETER_LOG_DAYS,
            "Días que se conservan los logs del build.",
            default=str(AGENTCORE_BUILD_LOG_RETENTION_DAYS),
        ),
        StackParameter(
            STACK_PARAMETER_BUILD_TIMEOUT,
            "Minutos tras los que CodeBuild para un build.",
            default=str(AGENTCORE_BUILD_TIMEOUT_MINUTES),
        ),
    ),
    capabilities=STACK_CAPABILITIES,
    cost=CostStatement(
        creates=(
            "AWS::ECR::Repository",
            "AWS::CodeBuild::Project",
            "AWS::Logs::LogGroup",
            "AWS::IAM::Role (x2)",
        ),
        idle_monthly="céntimos: las últimas imágenes en ECR y los logs del build",
        per_use=("CodeBuild: por minuto de build (arm1.small)",),
        removal="destroy() borra la pila, el repositorio con sus imágenes y los dos roles",
        source="AWS_API_NOTES.md, Despliegue en AgentCore Runtime",
    ),
)
