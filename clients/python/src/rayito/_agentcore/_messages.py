"""Los textos en español de `--target agentcore` (ADR-031), tal cual el
contrato `testdata/agentcore-deploy/contract.json` (`messages`): el plan, la
seguridad, los avisos, el coste, el progreso del despliegue y las líneas de
`status`, `list`, `delete` y `stop`. Puro: sólo la biblioteca estándar. Ninguno
lleva un ARN, una URL ni un token. Los precios de `COST_TEXTS` son marcadores
que `_domain.cost_lines()` rellena con las constantes de `limits.json`.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

HEADER_TEMPLATE: Final = (
    "rayito agent deploy: {name} en Amazon Bedrock AgentCore Runtime, región {region}"
)
STEP_TEMPLATE: Final = "  {label} {name}: {action}"
STACK_CONTENTS: Final = (
    "  (la pila crea el repositorio ECR, el proyecto de CodeBuild, su grupo de logs, el rol de "
    "build y el rol del runtime)"
)
SECURITY_HEADER: Final = "Seguridad:"
WARNINGS_HEADER: Final = "Avisos:"
WARNING_LINE_TEMPLATE: Final = "  aviso: {text}"
SECURITY_LINE_TEMPLATE: Final = "  - {text}"
COST_HEADER: Final = "Coste aproximado (us-east-1, precios de lista):"
COST_LINE_TEMPLATE: Final = "  {text}"
DRY_RUN_FOOTER: Final = "no se creó ni cambió nada (--dry-run)"
CONFIRM_QUESTION: Final = "¿Desplegar en AgentCore?"
LIST_LINE_TEMPLATE: Final = (
    "{name} runtime={runtimeName} id={runtimeId} version={version} status={status}"
)
LIST_EMPTY_TEMPLATE: Final = "no hay agentes de Rayito en AgentCore en {region}"
DELETE_HEADER_TEMPLATE: Final = "rayito agent delete: {name} en AgentCore, región {region}"
DELETE_RUNTIME_TEMPLATE: Final = "  runtime {runtimeName} ({runtimeId})"
DELETE_STACK_TEMPLATE: Final = (
    "  pila {stackName} (repositorio ECR con sus imágenes, proyecto y logs de CodeBuild, rol de "
    "build y rol del runtime)"
)
DELETE_LOG_GROUP_TEMPLATE: Final = "  grupo de logs del runtime {logGroup}"
DELETE_CONFIRM_QUESTION: Final = "¿Borrar el agente y sus recursos?"
DELETE_NOTHING_TEMPLATE: Final = (
    "no hay ningún despliegue de Rayito llamado {name} en {region}: nada que borrar"
)
DELETED_TEMPLATE: Final = (
    "borrado {name}: runtime {runtimeDeleted}, pila {stackDeleted}, grupo de logs {logGroupDeleted}"
)
STOPPED_TEMPLATE: Final = "sesión {sessionId} parada en {name}"
ERROR_PREFIX: Final = "rayito: "

#: Cómo se nombra cada recurso del plan.
RESOURCE_LABELS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "stack": "pila",
        "image": "imagen",
        "runtime": "runtime",
    }
)

#: Lo que se hará (o se hizo) con cada recurso, por `<recurso><Acción>`.
ACTION_TEXTS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "stackPlanned": "crear o actualizar (se decide al leer la cuenta)",
        "stackCreate": "crear",
        "stackUpdate": (
            "actualizar si cambió (CloudFormation no toca nada si la plantilla y los parámetros "
            "son los mismos)"
        ),
        "stackReuse": "ya existe ({status})",
        "stackWait": "en curso ({status}): esperar",
        "imagePlanned": "construir con CodeBuild si no existe (sin Docker local)",
        "imageBuild": "construir con CodeBuild (sin Docker local)",
        "imageReuse": "ya existe",
        "runtimePlanned": "crear o actualizar",
        "runtimeCreate": "crear y exigir MMDSv2",
        "runtimeUpdate": "actualizar (versión nueva)",
        "runtimeReuse": "ya está al día (versión {version})",
        "runtimeWait": "en curso ({status}): esperar",
    }
)

#: Las líneas de la sección «Seguridad», por clave.
SECURITY_TEXTS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "noGateway": (
            "AgentCore no tiene la pasarela de secretos de Rayito: el agente obtiene las "
            "credenciales del rol del runtime, y cualquier código que corra en la sesión puede "
            "leerlas"
        ),
        "bedrockModels": "el rol del runtime puede invocar en Bedrock: {models}",
        "noBedrockModels": "el rol del runtime no puede invocar ningún modelo de Bedrock",
        "readableSecrets": "el rol del runtime puede leer los secretos: {secrets}",
        "publicNetwork": (
            "red PUBLIC: el agente tiene salida a Internet y la política de salida de Rayito no "
            "se aplica"
        ),
        "sessionIsolation": (
            "cada sesión corre en su propia microVM de AgentCore, aislada de las demás; "
            "AgentCore no asocia sesiones a usuarios: eso es cosa de tu backend"
        ),
        "envCredentials": (
            "este framework lee las credenciales de variables de entorno: el host se las pone, "
            "leídas del rol del runtime, en cada invocación"
        ),
        "secretEnv": (
            "el host pone en el entorno del agente, al arrancar cada sesión, los valores de "
            "Secrets Manager de {names}: el agente y todo lo que lance pueden leer su entorno, y "
            "cualquier código de la sesión puede leer esos secretos con el rol del runtime"
        ),
    }
)

#: Los avisos de valores del fichero de proyecto que no aplican, por clave.
WARNING_TEXTS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "fileScaling": (
            "[scaling] del proyecto no aplica en AgentCore: AgentCore crea una microVM por "
            "sesión y escala solo"
        ),
        "fileMemory": (
            "memory_mib del proyecto no aplica en AgentCore: cada sesión usa hasta 2 vCPU y 8 GB "
            "y se cobra por consumo real"
        ),
        "filePort": "[serve].port no aplica en AgentCore: el contenedor sirve en el 8080",
        "fileTimeout": (
            "[serve].timeout no aplica en AgentCore: la vida de una sesión es "
            "max_session_lifetime (8 h por defecto)"
        ),
        "fileIdle": (
            "[serve].idle no aplica en AgentCore: usa idle_session_timeout (15 min por defecto)"
        ),
        "fileBaseImage": (
            "[deploy].base_image_version no aplica en AgentCore: no usa la imagen base de MicroVMs"
        ),
    }
)

#: Las líneas de coste de lista, por clave (con marcadores de precio).
COST_TEXTS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "runtime": (
            "runtime (plataforma V1, la que crea Rayito): {runtimeVcpuHour} por vCPU-hora y "
            "{runtimeGbHour} por GB-hora de uso real, por segundo; la CPU no se cobra en espera "
            "de E/S y la memoria sí, hasta que la sesión termina (por defecto, {idleMinutes} min "
            "sin uso); sin sesiones no hay cómputo"
        ),
        "build": (
            "build: {buildMinute} por minuto en CodeBuild arm1.small, redondeado al minuto "
            "({freeMinutes} minutos al mes en el nivel gratuito)"
        ),
        "image": "imagen en ECR: {ecrGbMonth} por GB-mes; se conservan las últimas {retained}",
        "logs": "logs: {logsGb} por GB ingerido en CloudWatch Logs",
        "model": "el modelo lo cobra su proveedor",
    }
)

#: Las líneas de progreso del despliegue, por clave.
PROGRESS_MESSAGES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "stackDeploy": "desplegando la pila {stackName}",
        "stackWait": "esperando a la pila {stackName} ({status})",
        "stackReady": "pila {stackName} lista",
        "sourceUpload": "subiendo el contexto de build ({bytes} bytes)",
        "buildStart": "construyendo la imagen {imageTag} con CodeBuild (build {buildId})",
        "imageReady": "imagen {imageTag} lista ({megabytes} MB)",
        "imageReused": "imagen {imageTag} reutilizada: el contenido no cambió",
        "runtimeCreate": "creando el runtime {runtimeName}",
        "runtimeRequireMmdsV2": "exigiendo MMDSv2 en el runtime {runtimeName}",
        "runtimeUpdate": "actualizando el runtime {runtimeName}",
        "runtimeWait": "esperando al runtime {runtimeName} ({status})",
        "runtimeReady": "runtime {runtimeName} listo (versión {version})",
        "runtimeReuse": "runtime {runtimeName} al día (versión {version}): nada que cambiar",
        "stackNoChanges": "pila {stackName} sin cambios",
        "sourceDeleted": "contexto de build borrado del bucket",
    }
)

#: Las líneas de progreso del borrado, por recurso.
DELETE_PROGRESS_MESSAGES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "runtime": "borrando el runtime {runtimeName}",
        "stack": "borrando la pila {stackName}",
        "logGroup": "borrando el grupo de logs {logGroup}",
    }
)

DEPLOYED_LINES: Final[tuple[str, ...]] = (
    "target=agentcore",
    "name={name}",
    "runtime_id={runtimeId}",
    "runtime_arn={runtimeArn}",
    "version={version}",
    "region={region}",
    "logs={logGroup}",
    'invoca con: rayito agent invoke {name} "<prompt>" --target agentcore',
)

STATUS_LINES: Final[tuple[str, ...]] = (
    "target: agentcore",
    "name: {name}",
    "runtime_id: {runtimeId}",
    "version: {version}",
    "status: {status}",
    "image: {imageUri}",
    "region: {region}",
    "logs: {logGroup}",
)
