"""Manejador de los recursos personalizados de los constructos CDK de Rayito
(`agentcore-cdk`, ADR-032).

Una sola función Lambda (Python 3.13, `CDK_HANDLER_RUNTIME`) sirve los dos
recursos que `RayitoAgent` declara con el `Provider` de `custom_resources`
(`onEvent` = `on_event`, `isComplete` = `is_complete`):

- ``Custom::RayitoImageBuild``: construye la imagen `linux/arm64` del agente
  con el proyecto de CodeBuild de `infra/agentcore-runtime.yaml`, igual que
  `rayito agent deploy --target agentcore`. Si la etiqueta `src-<hash>` ya
  está en ECR (las etiquetas son inmutables) no construye nada. Comprueba el
  límite de 2 GB de AgentCore antes de dar el recurso por creado.
- ``Custom::RayitoRuntimeMmds``: exige MMDSv2 en el runtime. El recurso
  `AWS::BedrockAgentCore::Runtime` no tiene ninguna propiedad de metadatos
  (AWS_API_NOTES.md Q181) y `CreateAgentRuntime` tampoco: se manda un
  `UpdateAgentRuntime` con la configuración que devuelve `GetAgentRuntime` y
  `metadataConfiguration.requireMMDSV2=true`, cada vez que cambia la versión
  del runtime (la propiedad `AgentRuntimeVersion` del recurso la fuerza). El
  recurso devuelve en `AgentRuntimeVersion` la versión viva que ya exige
  MMDSv2 (la que publica esa actualización), no la que creó CloudFormation.

Borrar cualquiera de los dos no llama a AWS: el repositorio se vacía con su
pila (`EmptyOnDelete`) y el runtime lo borra CloudFormation.

Cada operación y cada parámetro aparecen literalmente en `AWS_API_NOTES.md`
(«Despliegue en AgentCore Runtime» y «Constructos CDK»). Los mensajes de
error van en español y nunca llevan un ARN, una URL ni el texto crudo de AWS:
CloudFormation los muestra en sus eventos.

El fichero es la única fuente: `scripts/gen_stack_assets.py` lo empaqueta,
junto con el modelo de botocore de `bedrock-agentcore-control` que trae
`metadataConfiguration` (el boto3 del runtime de Lambda puede ser anterior),
en `agentcore-cdk-handler.zip` para los dos SDK (el `.json.gz` tal cual lo
publica botocore), así que el asset tiene el mismo hash en Python y en
TypeScript. Sólo biblioteca estándar y `boto3`.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Final

#: Modelos de botocore empaquetados junto al manejador (`models/<servicio>/
#: <apiVersion>/`); el cargador de botocore los busca antes que los suyos.
MODELS_DIRECTORY: Final = Path(__file__).resolve().parent / "models"

IMAGE_BUILD_TYPE: Final = "Custom::RayitoImageBuild"
RUNTIME_MMDS_TYPE: Final = "Custom::RayitoRuntimeMmds"

#: Estados de `BatchGetBuilds` (`buildStatus`).
BUILD_IN_PROGRESS: Final = "IN_PROGRESS"
BUILD_SUCCEEDED: Final = "SUCCEEDED"

#: Estados de `GetAgentRuntime` (`status`).
RUNTIME_READY: Final = "READY"
RUNTIME_FAILED_STATES: Final = frozenset({"CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED"})

ENVIRONMENT_VARIABLE_TYPE: Final = "PLAINTEXT"
IMAGE_TAG_VARIABLE: Final = "IMAGE_TAG"
BASE_IMAGE_VARIABLE: Final = "AGENT_BASE_IMAGE"

#: `clientToken` de `UpdateAgentRuntime`: 33-256 caracteres de
#: `[a-zA-Z0-9](-*[a-zA-Z0-9]){0,256}`; el id del runtime lleva `_`, así que
#: se resume con sha256.
CLIENT_TOKEN_PREFIX: Final = "rayitocdkmmds"
CLIENT_TOKEN_HEX_CHARS: Final = 40

#: Campos de `GetAgentRuntime` que `UpdateAgentRuntime` necesita otra vez
#: (los tres primeros son obligatorios en el modelo).
RUNTIME_UPDATE_FIELDS: Final = (
    "agentRuntimeArtifact",
    "roleArn",
    "networkConfiguration",
    "protocolConfiguration",
    "lifecycleConfiguration",
    "environmentVariables",
    "description",
)

ERROR_MESSAGES: Final = {
    "unknown_resource": "recurso personalizado desconocido: {resource_type}",
    "build_failed": "el build de la imagen {tag} terminó en {status}",
    "image_missing": "la imagen {tag} no está en el repositorio tras el build",
    "image_too_large": "la imagen {tag} ocupa {size} bytes y AgentCore admite como mucho {limit}",
    "runtime_failed": "el runtime quedó en {status} al exigir MMDSv2",
}


def _client(service: str) -> Any:
    """Cliente de boto3 que ve primero los modelos empaquetados. `boto3` se
    importa aquí para que `AWS_DATA_PATH` ya esté puesto cuando botocore
    cree su cargador."""
    if MODELS_DIRECTORY.is_dir():
        os.environ.setdefault("AWS_DATA_PATH", str(MODELS_DIRECTORY))
    import boto3

    return boto3.client(service)


class HandlerError(Exception):
    """Error que CloudFormation muestra en sus eventos: sólo el mensaje del
    contrato, nunca el texto de AWS."""

    def __init__(self, code: str, **fields: object) -> None:
        super().__init__(ERROR_MESSAGES[code].format(**fields))
        self.code = code


def on_event(event: dict[str, Any], _context: object = None) -> dict[str, Any]:
    """`onEvent` del `Provider`: arranca el trabajo y devuelve el id físico;
    `is_complete` decide cuándo termina."""
    resource_type = str(event["ResourceType"])
    if event["RequestType"] == "Delete":
        return {"PhysicalResourceId": event["PhysicalResourceId"]}
    properties = event["ResourceProperties"]
    if resource_type == IMAGE_BUILD_TYPE:
        return _start_image_build(properties)
    if resource_type == RUNTIME_MMDS_TYPE:
        return _require_mmds(properties)
    raise HandlerError("unknown_resource", resource_type=resource_type)


def is_complete(event: dict[str, Any], _context: object = None) -> dict[str, Any]:
    """`isComplete` del `Provider`: recibe el evento con lo que devolvió
    `on_event` y sondea hasta que la imagen o el runtime están listos."""
    if event["RequestType"] == "Delete":
        return {"IsComplete": True}
    resource_type = str(event["ResourceType"])
    properties = event["ResourceProperties"]
    if resource_type == IMAGE_BUILD_TYPE:
        return _image_build_complete(properties, event.get("Data", {}))
    if resource_type == RUNTIME_MMDS_TYPE:
        return _mmds_complete(properties)
    raise HandlerError("unknown_resource", resource_type=resource_type)


def _start_image_build(properties: dict[str, Any]) -> dict[str, Any]:
    tag = str(properties["ImageTag"])
    if _image_size(properties) is not None:
        return {"PhysicalResourceId": tag, "Data": {"ImageTag": tag, "BuildId": ""}}
    started = _client("codebuild").start_build(
        projectName=str(properties["ProjectName"]),
        sourceLocationOverride=str(properties["SourceLocation"]),
        environmentVariablesOverride=[
            {"name": IMAGE_TAG_VARIABLE, "value": tag, "type": ENVIRONMENT_VARIABLE_TYPE},
            {
                "name": BASE_IMAGE_VARIABLE,
                "value": str(properties["AgentBaseImage"]),
                "type": ENVIRONMENT_VARIABLE_TYPE,
            },
        ],
        idempotencyToken=tag,
    )
    return {
        "PhysicalResourceId": tag,
        "Data": {"ImageTag": tag, "BuildId": str(started["build"]["id"])},
    }


def _image_build_complete(properties: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    tag = str(properties["ImageTag"])
    build_id = str(data.get("BuildId", ""))
    if build_id:
        builds = _client("codebuild").batch_get_builds(ids=[build_id])["builds"]
        status = str(builds[0]["buildStatus"]) if builds else BUILD_IN_PROGRESS
        if status == BUILD_IN_PROGRESS:
            return {"IsComplete": False}
        if status != BUILD_SUCCEEDED:
            raise HandlerError("build_failed", tag=tag, status=status)
    size = _image_size(properties)
    if size is None:
        raise HandlerError("image_missing", tag=tag)
    limit = int(properties["MaxImageBytes"])
    if size > limit:
        raise HandlerError("image_too_large", tag=tag, size=size, limit=limit)
    return {"IsComplete": True, "Data": {"ImageTag": tag}}


def _image_size(properties: dict[str, Any]) -> int | None:
    """`imageSizeInBytes` de la etiqueta, o `None` si no existe
    (`ImageNotFoundException`)."""
    ecr = _client("ecr")
    try:
        details = ecr.describe_images(
            repositoryName=str(properties["RepositoryName"]),
            imageIds=[{"imageTag": str(properties["ImageTag"])}],
        )["imageDetails"]
    except ecr.exceptions.ImageNotFoundException:
        return None
    return int(details[0]["imageSizeInBytes"]) if details else None


#: Atributo de `Custom::RayitoRuntimeMmds` (`GetAtt Mmds.AgentRuntimeVersion`).
RUNTIME_VERSION_ATTRIBUTE: Final = "AgentRuntimeVersion"


def _require_mmds(properties: dict[str, Any]) -> dict[str, Any]:
    """La propiedad `AgentRuntimeVersion` sólo dispara la re-ejecución: el
    token de idempotencia sale de la versión *viva* que devuelve
    `GetAgentRuntime` (como `client_token_for` de la CLI con la versión
    base), porque la de CloudFormation queda atrás en cuanto esta misma
    actualización publica la siguiente, y un rollback podría repetirla con
    otra petición."""
    runtime_id = str(properties["AgentRuntimeId"])
    control = _client("bedrock-agentcore-control")
    runtime = control.get_agent_runtime(agentRuntimeId=runtime_id)
    physical_id = f"{runtime_id}/mmds"
    if _mmds_required(runtime):
        return {"PhysicalResourceId": physical_id}
    version = str(runtime.get("agentRuntimeVersion") or properties["AgentRuntimeVersion"])
    request = {field: runtime[field] for field in RUNTIME_UPDATE_FIELDS if field in runtime}
    control.update_agent_runtime(
        agentRuntimeId=runtime_id,
        metadataConfiguration={"requireMMDSV2": True},
        clientToken=client_token(runtime_id, version),
        **request,
    )
    return {"PhysicalResourceId": physical_id}


def _mmds_complete(properties: dict[str, Any]) -> dict[str, Any]:
    runtime = _client("bedrock-agentcore-control").get_agent_runtime(
        agentRuntimeId=str(properties["AgentRuntimeId"])
    )
    status = str(runtime.get("status", ""))
    if status in RUNTIME_FAILED_STATES:
        raise HandlerError("runtime_failed", status=status)
    if status != RUNTIME_READY or not _mmds_required(runtime):
        return {"IsComplete": False}
    return {
        "IsComplete": True,
        "Data": {RUNTIME_VERSION_ATTRIBUTE: str(runtime["agentRuntimeVersion"])},
    }


def _mmds_required(runtime: dict[str, Any]) -> bool:
    return bool(runtime.get("metadataConfiguration", {}).get("requireMMDSV2"))


def client_token(runtime_id: str, version: str) -> str:
    """Token de idempotencia de la actualización de MMDSv2 de una versión
    concreta del runtime: el reintento del `Provider` no crea otra versión."""
    digest = hashlib.sha256(f"{runtime_id}:{version}".encode()).hexdigest()
    return f"{CLIENT_TOKEN_PREFIX}{digest[:CLIENT_TOKEN_HEX_CHARS]}"
