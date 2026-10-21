"""Salida de `rayito agent ... --target agentcore`: las líneas del plan, del
resultado, del estado, del listado, del borrado y de la parada, y los
documentos JSON de `--json`, con los textos y las claves del contrato
(`testdata/agentcore-deploy/contract.json`). Sin `typer`: cada función es pura.
"""

from __future__ import annotations

from typing import Any, Final

from rayito._agentcore import _messages as text
from rayito._agentcore._models import (
    AgentCoreDeletePlan,
    AgentCoreDeleteResult,
    AgentCoreDeployment,
    AgentCoreDeployPlan,
    AgentCoreListItem,
    AgentCoreStatus,
    AgentCoreStep,
)
from rayito.exceptions import AgentCoreException

TARGET: Final = "agentcore"
#: Clave de `ACTION_TEXTS` de cada (recurso, acción) del plan y del despliegue.
ACTION_KEYS: Final = {
    ("stack", "planned"): "stackPlanned",
    ("stack", "create"): "stackCreate",
    ("stack", "update"): "stackUpdate",
    ("stack", "reuse"): "stackReuse",
    ("stack", "wait"): "stackWait",
    ("image", "planned"): "imagePlanned",
    ("image", "build"): "imageBuild",
    ("image", "reuse"): "imageReuse",
    ("runtime", "planned"): "runtimePlanned",
    ("runtime", "create"): "runtimeCreate",
    ("runtime", "update"): "runtimeUpdate",
    ("runtime", "reuse"): "runtimeReuse",
    ("runtime", "wait"): "runtimeWait",
}
STACK_RESOURCE: Final = "stack"
#: Lo que nombra la línea `secretEnv` cuando las variables sólo se conocen al
#: leer el secreto (un `secret_env_from` sin `keys`).
SECRET_ENV_UNLISTED: Final = "las claves de sus secretos JSON"


def step_text(step: AgentCoreStep, version: str = "") -> str:
    key = ACTION_KEYS[(step.resource, step.action)]
    return text.ACTION_TEXTS[key].format(status=step.status, version=version)


def step_line(step: AgentCoreStep, version: str = "") -> str:
    return text.STEP_TEMPLATE.format(
        label=text.RESOURCE_LABELS[step.resource], name=step.name, action=step_text(step, version)
    )


def _security_line(key: str, plan: AgentCoreDeployPlan) -> str:
    sentence = text.SECURITY_TEXTS[key].format(
        models=", ".join(plan.bedrock_models),
        secrets=", ".join(plan.readable_secrets),
        names=", ".join(plan.secret_env_names) or SECRET_ENV_UNLISTED,
    )
    return text.SECURITY_LINE_TEMPLATE.format(text=sentence)


def plan_lines(plan: AgentCoreDeployPlan) -> list[str]:
    """El encabezado, los tres pasos, la seguridad (con la diferencia con la
    pasarela de MicroVMs primero), los avisos y el coste de lista."""
    lines = [text.HEADER_TEMPLATE.format(name=plan.name, region=plan.region)]
    for step in plan.steps:
        lines.append(step_line(step))
        if step.resource == STACK_RESOURCE:
            lines.append(text.STACK_CONTENTS)
    lines.append(text.SECURITY_HEADER)
    lines.extend(_security_line(key, plan) for key in plan.security)
    if plan.warnings:
        lines.append(text.WARNINGS_HEADER)
        lines.extend(
            text.WARNING_LINE_TEMPLATE.format(text=text.WARNING_TEXTS[key]) for key in plan.warnings
        )
    lines.append(text.COST_HEADER)
    lines.extend(text.COST_LINE_TEMPLATE.format(text=line) for line in plan.cost)
    return lines


def step_document(step: AgentCoreStep) -> dict[str, object]:
    return {
        "resource": step.resource,
        "name": step.name,
        "action": step.action,
        "status": step.status,
    }


def plan_document(plan: AgentCoreDeployPlan) -> dict[str, Any]:
    """El plan con las claves del contrato (`json.planKeys`); `security` y
    `warnings` llevan las claves de los textos, estables para un script."""
    return {
        "target": TARGET,
        "name": plan.name,
        "region": plan.region,
        "runtimeName": plan.runtime_name,
        "stackName": plan.stack_name,
        "repositoryName": plan.repository_name,
        "buildProjectName": plan.build_project_name,
        "imageTag": plan.image_tag,
        "bucket": plan.bucket,
        "bucketSource": plan.bucket_source,
        "network": plan.network,
        "bedrockModels": list(plan.bedrock_models),
        "readableSecrets": list(plan.readable_secrets),
        "environment": dict(plan.environment),
        "lifecycle": {
            "idleSessionTimeoutSeconds": plan.lifecycle.idle_session_timeout_seconds,
            "maxSessionLifetimeSeconds": plan.lifecycle.max_session_lifetime_seconds,
        },
        "tags": dict(plan.tags),
        "steps": [step_document(step) for step in plan.steps],
        "security": list(plan.security),
        "warnings": list(plan.warnings),
        "cost": list(plan.cost),
        **_secret_env_document(plan),
    }


def _secret_env_document(plan: AgentCoreDeployPlan) -> dict[str, Any]:
    """`secretEnvNames` sólo si el plan pone variables desde Secrets Manager:
    sin ellas el documento no cambia."""
    return {"secretEnvNames": list(plan.secret_env_names)} if plan.secret_env_names else {}


def dry_run_document(plan: AgentCoreDeployPlan, detection: Any) -> dict[str, Any]:
    return {
        "dryRun": True,
        "target": TARGET,
        "detection": detection,
        "plan": plan_document(plan),
    }


def deployed_lines(deployment: AgentCoreDeployment) -> list[str]:
    fields = {
        "name": deployment.name,
        "runtimeId": deployment.runtime_id,
        "runtimeArn": deployment.runtime_arn,
        "version": deployment.runtime_version,
        "region": deployment.region,
        "logGroup": deployment.log_group,
    }
    return [line.format(**fields) for line in text.DEPLOYED_LINES]


def deploy_document(deployment: AgentCoreDeployment) -> dict[str, Any]:
    return {
        "target": TARGET,
        "name": deployment.name,
        "region": deployment.region,
        "runtimeName": deployment.runtime_name,
        "runtimeId": deployment.runtime_id,
        "runtimeArn": deployment.runtime_arn,
        "runtimeVersion": deployment.runtime_version,
        "status": deployment.status,
        "imageUri": deployment.image_uri,
        "stackName": deployment.stack_name,
        "logGroup": deployment.log_group,
        "steps": [step_document(step) for step in deployment.steps],
    }


def status_lines(status: AgentCoreStatus) -> list[str]:
    fields = {
        "name": status.name,
        "runtimeId": status.runtime_id,
        "version": status.runtime_version,
        "status": status.status,
        "imageUri": status.image_uri,
        "region": status.region,
        "logGroup": status.log_group,
    }
    return [line.format(**fields) for line in text.STATUS_LINES]


def status_document(status: AgentCoreStatus) -> dict[str, Any]:
    return {
        "target": TARGET,
        "name": status.name,
        "region": status.region,
        "runtimeName": status.runtime_name,
        "runtimeId": status.runtime_id,
        "runtimeArn": status.runtime_arn,
        "runtimeVersion": status.runtime_version,
        "status": status.status,
        "imageUri": status.image_uri,
        "logGroup": status.log_group,
    }


def list_lines(items: tuple[AgentCoreListItem, ...], region: str) -> list[str]:
    if not items:
        return [text.LIST_EMPTY_TEMPLATE.format(region=region)]
    return [
        text.LIST_LINE_TEMPLATE.format(
            name=item.name,
            runtimeName=item.runtime_name,
            runtimeId=item.runtime_id,
            version=item.runtime_version,
            status=item.status,
        )
        for item in items
    ]


def list_document(items: tuple[AgentCoreListItem, ...], region: str) -> dict[str, Any]:
    return {
        "target": TARGET,
        "region": region,
        "agents": [
            {
                "name": item.name,
                "runtimeName": item.runtime_name,
                "runtimeId": item.runtime_id,
                "runtimeVersion": item.runtime_version,
                "status": item.status,
            }
            for item in items
        ],
    }


def delete_plan_lines(plan: AgentCoreDeletePlan) -> list[str]:
    """Lo que se borraría: el encabezado y, de lo que existe, el runtime, la
    pila y el grupo de logs. Sin nada, el aviso de que no hay nada que borrar."""
    if plan.nothing:
        return [text.DELETE_NOTHING_TEMPLATE.format(name=plan.name, region=plan.region)]
    lines = [text.DELETE_HEADER_TEMPLATE.format(name=plan.name, region=plan.region)]
    if plan.runtime is not None:
        lines.append(
            text.DELETE_RUNTIME_TEMPLATE.format(
                runtimeName=plan.runtime.name, runtimeId=plan.runtime.runtime_id
            )
        )
    if plan.stack_exists:
        lines.append(text.DELETE_STACK_TEMPLATE.format(stackName=plan.stack_name))
    if plan.log_group is not None:
        lines.append(text.DELETE_LOG_GROUP_TEMPLATE.format(logGroup=plan.log_group))
    return lines


def deleted_line(result: AgentCoreDeleteResult) -> str:
    return text.DELETED_TEMPLATE.format(
        name=result.name,
        runtimeDeleted=_bool_text(result.runtime_deleted),
        stackDeleted=_bool_text(result.stack_deleted),
        logGroupDeleted=_bool_text(result.log_group_deleted),
    )


def _bool_text(value: bool) -> str:
    return str(value).lower()


def delete_document(result: AgentCoreDeleteResult) -> dict[str, Any]:
    return {
        "target": TARGET,
        "name": result.name,
        "region": result.region,
        "runtimeDeleted": result.runtime_deleted,
        "stackDeleted": result.stack_deleted,
        "logGroupDeleted": result.log_group_deleted,
    }


def stop_document(name: str, session_id: str, *, stopped: bool) -> dict[str, Any]:
    return {"target": TARGET, "name": name, "sessionId": session_id, "stopped": stopped}


def stopped_line(name: str, session_id: str) -> str:
    return text.STOPPED_TEMPLATE.format(name=name, sessionId=session_id)


def error_document(error: AgentCoreException) -> dict[str, Any]:
    """`{"error": {"code", "message"}, "exitCode"}` (`json.errorDocument`)."""
    return {
        "error": {"code": error.code, "message": str(error)},
        "exitCode": error.exit_code,
    }
