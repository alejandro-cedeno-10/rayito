"""Errores cerrados de `--target agentcore` (ADR-031): su código, el mensaje
en español de cada uno y el código de salida de la CLI, como en
`testdata/agentcore-deploy/contract.json` (`errorExitCodes` y
`messages.errors`). Puro: sólo la biblioteca estándar. Ningún mensaje lleva un
ARN, una URL, un token ni el texto crudo de AWS.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, Literal

AgentCoreErrorCode = Literal[
    "unsupported_region",
    "option_not_supported",
    "option_requires_agentcore",
    "target_unsupported",
    "provider_unsupported",
    "network_policy_unsupported",
    "name_invalid",
    "name_conflict",
    "model_required",
    "image_missing",
    "lifecycle_invalid",
    "tags_invalid",
    "env_invalid",
    "raw_protocol",
    "no_bucket",
    "bucket_region",
    "needs_confirmation",
    "session_required",
    "stack_blocked",
    "stack_failed",
    "image_build_failed",
    "image_build_timeout",
    "build_quota",
    "image_too_large",
    "runtime_blocked",
    "runtime_failed",
    "runtime_timeout",
    "runtime_not_found",
    "delete_failed",
    "access_denied",
    "aws_error",
    "lifecycle_order",
    "delete_needs_confirmation",
]

#: Código de salida de cada error: 2 uso o entorno, 1 fallo de AWS.
ERROR_EXIT_CODES: Final[Mapping[AgentCoreErrorCode, int]] = MappingProxyType(
    {
        "unsupported_region": 2,
        "option_not_supported": 2,
        "option_requires_agentcore": 2,
        "target_unsupported": 2,
        "provider_unsupported": 2,
        "network_policy_unsupported": 2,
        "name_invalid": 2,
        "name_conflict": 2,
        "model_required": 2,
        "image_missing": 2,
        "lifecycle_invalid": 2,
        "tags_invalid": 2,
        "env_invalid": 2,
        "raw_protocol": 2,
        "no_bucket": 2,
        "bucket_region": 2,
        "needs_confirmation": 2,
        "session_required": 2,
        "stack_blocked": 1,
        "stack_failed": 1,
        "image_build_failed": 1,
        "image_build_timeout": 1,
        "build_quota": 1,
        "image_too_large": 1,
        "runtime_blocked": 1,
        "runtime_failed": 1,
        "runtime_timeout": 1,
        "runtime_not_found": 1,
        "delete_failed": 1,
        "access_denied": 1,
        "aws_error": 1,
        "lifecycle_order": 2,
        "delete_needs_confirmation": 2,
    }
)

ERROR_MESSAGES: Final[Mapping[AgentCoreErrorCode, str]] = MappingProxyType(
    {
        "unsupported_region": (
            "la región {region} no tiene AgentCore Runtime con builds arm64 de CodeBuild: usa "
            "una de {regions}"
        ),
        "option_not_supported": "{option} no aplica a --target agentcore: {reason}",
        "option_requires_agentcore": "{option} sólo aplica a --target agentcore",
        "target_unsupported": (
            "rayito agent {command} sólo aplica a --target agentcore: para una flota de MicroVMs "
            "usa rayito agent stop o status"
        ),
        "provider_unsupported": (
            "el proveedor de modelo {provider} no está soportado en AgentCore: sin pasarela de "
            "secretos sólo se cablea bedrock, con el rol del runtime"
        ),
        "network_policy_unsupported": (
            "AgentCore no puede aplicar la política de salida del proyecto ({what}): en modo "
            "PUBLIC el agente sale a Internet sin filtro; quítala para desplegar en AgentCore"
        ),
        "name_invalid": (
            "el nombre {name} no vale para AgentCore: hasta {max} caracteres, minúsculas y "
            "dígitos, con - o _ sueltos entre ellos; cámbialo con [image].template"
        ),
        "name_conflict": (
            "ya existe un runtime {runtimeName} que no es del agente {name} de Rayito: cambia el "
            "nombre con [image].template"
        ),
        "image_missing": (
            "--no-build: no hay ninguna imagen {imageTag} en {repositoryName}; despliega sin "
            "--no-build"
        ),
        "model_required": (
            "no se sabe qué modelo de Bedrock usa el agente: pásalo con --bedrock-model "
            "(bedrock_models en el SDK)"
        ),
        "lifecycle_invalid": "{option} debe ser un número entero de segundos entre {min} y {max}",
        "tags_invalid": (
            "--tag necesita CLAVE=VALOR, hasta {max} etiquetas, con letras, dígitos, espacios y "
            "_ . : / = + - @"
        ),
        "env_invalid": (
            "AgentCore admite como mucho {maxVariables} variables de entorno de hasta {maxChars} "
            "caracteres"
        ),
        "raw_protocol": (
            "una app con protocol='raw' no cumple el contrato de AgentCore (/ping + /invocations)"
        ),
        "no_bucket": (
            "no hay bucket para el contexto de build: pasa --bucket, [deploy].bucket o "
            "RAYITO_BUCKET, o crea uno con rayito setup"
        ),
        "bucket_region": (
            "el bucket {bucket} está en {bucketRegion}, no en {region}: CodeBuild sólo lee el "
            "código de un bucket de su región"
        ),
        "needs_confirmation": (
            "desplegar en AgentCore crea recursos con coste: confírmalo con --yes (o desde una "
            "terminal)"
        ),
        "delete_needs_confirmation": (
            "borrar un agente no se puede deshacer: confírmalo con --yes (o desde una terminal)"
        ),
        "session_required": (
            "con --target agentcore, rayito agent stop necesita --session-id: AgentCore no cobra "
            "sin sesiones; para borrar el agente usa rayito agent delete"
        ),
        "stack_blocked": (
            "la pila {stackName} está en {status}: bórrala con rayito agent delete {name} "
            "--target agentcore y vuelve a desplegar"
        ),
        "stack_failed": (
            "la pila {stackName} no se pudo desplegar ({status}): mira sus eventos en la consola "
            "de CloudFormation"
        ),
        "image_build_failed": (
            "el build {buildId} terminó en {status}: los logs están en el grupo {logGroup}"
        ),
        "image_build_timeout": (
            "el build {buildId} sigue en curso tras {seconds} s: síguelo en el grupo {logGroup}"
        ),
        "build_quota": (
            "CodeBuild rechazó el build por la cuota de builds simultáneos de la cuenta: espera "
            "a que acaben los que hay o pide más cuota"
        ),
        "image_too_large": (
            "la imagen ocupa {megabytes} MB y AgentCore admite como mucho {maxMegabytes} MB"
        ),
        "runtime_blocked": (
            "el runtime {runtimeName} está en {status}: bórralo con rayito agent delete {name} "
            "--target agentcore y vuelve a desplegar"
        ),
        "runtime_failed": (
            "el runtime {runtimeName} quedó en {status}: mira sus logs en {logGroup}"
        ),
        "runtime_timeout": "el runtime {runtimeName} sigue en {status} tras {seconds} s",
        "runtime_not_found": (
            "no hay ningún agente de Rayito llamado {name} en AgentCore en {region}"
        ),
        "delete_failed": "no se pudo borrar {resource} {resourceName} ({status})",
        "access_denied": (
            "faltan permisos para {operation} ({awsCode}): mira los permisos de --target "
            "agentcore en la guía"
        ),
        "aws_error": "AWS rechazó {operation} ({awsCode})",
        "lifecycle_order": (
            "idle_session_timeout ({idle} s) no puede superar max_session_lifetime ({max} s)"
        ),
    }
)
