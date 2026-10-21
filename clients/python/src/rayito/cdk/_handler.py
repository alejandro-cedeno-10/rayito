"""Las funciones del manejador de los recursos personalizados de
`RayitoAgent` (ADR-032 D3.3, D13) y los ficheros que el constructo copia a un
directorio temporal: el zip del manejador (`agentcore-cdk-handler.zip`,
empaquetado en el SDK por `scripts/gen_stack_assets.py`) y la plantilla
`agentcore-runtime.yaml` que lee `CfnInclude`.

El zip se escribe una vez por proceso con el mismo nombre y los mismos bytes
que en TypeScript, así que el asset tiene el mismo hash en los dos SDK.
"""

from __future__ import annotations

import functools
import tempfile
from importlib import resources
from pathlib import Path
from typing import Final

import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import custom_resources as cr
from constructs import Construct

from rayito._limits import (
    CDK_BUILD_QUERY_INTERVAL_SECONDS,
    CDK_BUILD_TOTAL_TIMEOUT_MARGIN_MINUTES,
    CDK_HANDLER_RUNTIME,
    CDK_HANDLER_TIMEOUT_SECONDS,
)
from rayito._stacks._packaging import STACKS_PACKAGE
from rayito.cdk._environment import HANDLER_LOG_RETENTION

HANDLER_ASSET_NAME: Final = "agentcore-cdk-handler.zip"
RUNTIME_TEMPLATE_NAME: Final = "agentcore-runtime.yaml"
ON_EVENT_HANDLER: Final = "index.on_event"
IS_COMPLETE_HANDLER: Final = "index.is_complete"
STAGING_PREFIX: Final = "rayito-cdk-"
LAMBDA_SERVICE: Final = "lambda.amazonaws.com"


@functools.cache
def staging_directory() -> Path:
    """Un directorio temporal por proceso para lo que el constructo escribe
    (el zip del manejador, la plantilla, el zip del código del agente)."""
    return Path(tempfile.mkdtemp(prefix=STAGING_PREFIX))


@functools.cache
def handler_zip() -> Path:
    """El zip del manejador, copiado del paquete a `agentcore-cdk-handler.zip`."""
    data = resources.files(STACKS_PACKAGE).joinpath("_artifacts", HANDLER_ASSET_NAME).read_bytes()
    path = staging_directory() / HANDLER_ASSET_NAME
    path.write_bytes(data)
    return path


@functools.cache
def runtime_template() -> Path:
    """`infra/agentcore-runtime.yaml` tal cual lo empaqueta el SDK."""
    text = (
        resources.files(STACKS_PACKAGE)
        .joinpath("_templates", RUNTIME_TEMPLATE_NAME)
        .read_text(encoding="utf-8")
    )
    path = staging_directory() / RUNTIME_TEMPLATE_NAME
    path.write_text(text, encoding="utf-8")
    return path


class AgentCoreHandler(Construct):
    """Las dos funciones del `Provider` (`OnEvent` e `IsComplete`) con un rol
    compartido (`Role`, que sólo escribe en esos grupos) y sus grupos de logs
    de 14 días; el resto de permisos los
    añade `RayitoAgent` con `add_to_policy`, acotados a su proyecto de
    CodeBuild, su repositorio y su runtime."""

    def __init__(self, scope: Construct, construct_id: str) -> None:
        super().__init__(scope, construct_id)
        self.role = iam.Role(self, "Role", assumed_by=iam.ServicePrincipal(LAMBDA_SERVICE))
        code = lambda_.Code.from_asset(str(handler_zip()))
        self.on_event = self._function("OnEvent", ON_EVENT_HANDLER, code)
        self.is_complete = self._function("IsComplete", IS_COMPLETE_HANDLER, code)

    def _function(self, construct_id: str, handler: str, code: lambda_.Code) -> lambda_.Function:
        log_group = logs.LogGroup(
            self,
            f"{construct_id}Logs",
            retention=HANDLER_LOG_RETENTION,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        log_group.grant_write(self.role)
        return lambda_.Function(
            self,
            construct_id,
            runtime=lambda_.Runtime(CDK_HANDLER_RUNTIME, lambda_.RuntimeFamily.PYTHON),
            handler=handler,
            code=code,
            role=self.role,
            timeout=cdk.Duration.seconds(CDK_HANDLER_TIMEOUT_SECONDS),
            log_group=log_group,
        )

    def add_to_policy(self, statement: iam.PolicyStatement) -> None:
        self.role.add_to_principal_policy(statement)


def build_provider(
    scope: Construct, handler: AgentCoreHandler, build_timeout_minutes: int
) -> cr.Provider:
    """El `Provider` (`custom_resources`) del constructo: consulta cada
    `cdkBuildQueryIntervalSeconds` y se rinde tras el plazo del build más
    `cdkBuildTotalTimeoutMarginMinutes`. Sus funciones de marco escriben en
    `ProviderLogs` (14 días)."""
    log_group = logs.LogGroup(
        scope,
        "ProviderLogs",
        retention=HANDLER_LOG_RETENTION,
        removal_policy=cdk.RemovalPolicy.DESTROY,
    )
    return cr.Provider(
        scope,
        "Provider",
        on_event_handler=handler.on_event,
        is_complete_handler=handler.is_complete,
        query_interval=cdk.Duration.seconds(CDK_BUILD_QUERY_INTERVAL_SECONDS),
        total_timeout=cdk.Duration.minutes(
            build_timeout_minutes + CDK_BUILD_TOTAL_TIMEOUT_MARGIN_MINUTES
        ),
        log_group=log_group,
    )


__all__ = [
    "AgentCoreHandler",
    "build_provider",
    "handler_zip",
    "runtime_template",
    "staging_directory",
]
