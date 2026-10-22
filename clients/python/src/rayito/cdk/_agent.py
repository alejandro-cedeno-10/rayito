"""`RayitoAgent` y `AgentImageBuild` (ADR-032 D3-D6): un agente de Rayito en
Amazon Bedrock AgentCore Runtime declarado en un stack de CDK, con los mismos
recursos e IAM que `rayito agent deploy --target agentcore`.

Es un adaptador sobre la planificación pura de `rayito._agentcore`
(`plan_agentcore`, `bedrock_resource_arns`, `secret_resource_arns`,
`source_zip`): la síntesis sólo lee ficheros locales y nunca llama a AWS. Los
ids de cada hijo son los de `testdata/agentcore-cdk/contract.json`
(`constructIds.RayitoAgent`), así que los logical ids coinciden con los de
TypeScript.
"""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final, Literal, cast

import aws_cdk as cdk
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3_assets as s3_assets
from aws_cdk import cloudformation_include as cfn_include
from constructs import Construct

from rayito._agent._project_domain import AgentProject
from rayito._agentcore._deploy import plan_agentcore
from rayito._agentcore._domain import (
    IMAGE_URI_TEMPLATE,
    STACK_PARAMETER_AGENT_DISPLAY_NAME,
    STACK_PARAMETER_AGENT_NAME,
    STACK_PARAMETER_BUCKET,
    STACK_PARAMETER_BUILD_TIMEOUT,
    STACK_PARAMETER_LOG_DAYS,
    STACK_PARAMETER_MODEL_ARNS,
    STACK_PARAMETER_RETENTION,
    STACK_PARAMETER_RUNTIME_NAME,
    STACK_PARAMETER_SECRET_ARNS,
    base_image_reference,
    bedrock_resource_arns,
    canonical_agent_name,
    runtime_description_for,
    secret_resource_arns,
    source_zip,
)
from rayito._agentcore._iam_resources import ARN_PREFIX
from rayito._agentcore._models import AgentCoreOptions
from rayito._limits import (
    AGENT_EXPORT_BASE_IMAGE_DIGEST,
    AGENTCORE_BUILD_LOG_RETENTION_DAYS,
    AGENTCORE_BUILD_TIMEOUT_MINUTES,
    AGENTCORE_IMAGE_RETENTION_COUNT,
    AGENTCORE_MAX_ENVIRONMENT_VARIABLES,
    AGENTCORE_MAX_IMAGE_BYTES,
    AGENTCORE_NETWORK_MODE,
    AGENTCORE_SERVER_PROTOCOL,
    CDK_BUILD_TOTAL_TIMEOUT_MARGIN_MINUTES,
    CDK_RUNTIME_ENVIRONMENT_VALUE_MAX_CHARS,
)
from rayito.cdk._environment import AGENT_COMPONENT, StackEnvironment, apply_tags, stack_environment
from rayito.cdk._errors import RayitoCdkException
from rayito.cdk._handler import (
    AgentCoreHandler,
    build_provider,
    runtime_template,
    staging_directory,
)
from rayito.exceptions import AgentCoreException

CONSTRUCT_NAME: Final = "RayitoAgent"
IMAGE_BUILD_TYPE: Final = "Custom::RayitoImageBuild"
RUNTIME_MMDS_TYPE: Final = "Custom::RayitoRuntimeMmds"
#: Atributo de `Mmds`: la versión viva del runtime que ya exige MMDSv2.
RUNTIME_VERSION_ATTRIBUTE: Final = "AgentRuntimeVersion"
BASE_IMAGE_ARG: Final = "AGENT_BASE_IMAGE"
#: Plazo de un build: `MinValue` de `BuildTimeoutMinutes` en
#: `infra/agentcore-runtime.yaml` y, con el margen, el `totalTimeout` máximo
#: del `Provider` de `custom_resources` (2 horas).
BUILD_TIMEOUT_MIN_MINUTES: Final = 5
PROVIDER_MAX_TOTAL_TIMEOUT_MINUTES: Final = 120
BUILD_TIMEOUT_MAX_MINUTES: Final = (
    PROVIDER_MAX_TOTAL_TIMEOUT_MINUTES - CDK_BUILD_TOTAL_TIMEOUT_MARGIN_MINUTES
)
#: Lo que `localDocker()` quita de la plantilla incluida (el repositorio, el
#: build y sus salidas): la imagen va al repositorio del bootstrap de CDK.
LOCAL_DOCKER_REMOVED_RESOURCES: Final = ("Repository", "BuildLogGroup", "BuildRole", "BuildProject")
LOCAL_DOCKER_REMOVED_OUTPUTS: Final = (
    "RepositoryName",
    "RepositoryUri",
    "RepositoryArn",
    "BuildProjectName",
    "BuildLogGroupName",
)
OUTPUTS_CHILD: Final = "$Outputs"
#: `PullImage` es la primera sentencia de la política del rol del runtime.
PULL_IMAGE_RESOURCE_OVERRIDE: Final = "Policies.0.PolicyDocument.Statement.0.Resource"
#: La política extra del rol de build: leer sólo el objeto del asset.
SOURCE_POLICY_OVERRIDE: Final = "Policies.1"
SOURCE_POLICY_NAME: Final = "source-asset"
SOURCE_STATEMENT_SID: Final = "ReadSourceAsset"
#: Las mismas acciones que `ReadSource` de `infra/agentcore-runtime.yaml`: el
#: bucket de assets del bootstrap tiene versionado y CodeBuild puede leer la
#: fuente por versión.
SOURCE_ACTIONS: Final = ("s3:GetObject", "s3:GetObjectVersion")
POLICY_VERSION: Final = "2012-10-17"
#: Sin `localDocker()` no hay build, así que `ArtifactBucket` no se usa.
UNUSED_BUCKET: Final = ""
SOURCE_LOCATION_TEMPLATE: Final = "{bucket}/{key}"
S3_OBJECT_ARN_TEMPLATE: Final = "arn:{partition}:s3:::{bucket}/{key}"
SOURCE_ZIP_TEMPLATE: Final = "{image_tag}.zip"
TREE_DIRECTORY_TEMPLATE: Final = "agent-{image_tag}"
RUNTIME_ARN_SUFFIX: Final = "{runtime_name}-*"
INVOKE_ACTIONS: Final = ("bedrock-agentcore:InvokeAgentRuntime",)
MMDS_ACTIONS: Final = ("bedrock-agentcore:GetAgentRuntime", "bedrock-agentcore:UpdateAgentRuntime")
BUILD_ACTIONS: Final = ("codebuild:StartBuild", "codebuild:BatchGetBuilds")
DESCRIBE_IMAGES_ACTIONS: Final = ("ecr:DescribeImages",)
PASS_ROLE_ACTIONS: Final = ("iam:PassRole",)
ENDPOINTS_SUFFIX: Final = "/*"
AGENTCORE_SERVICE: Final = "bedrock-agentcore"
RUNTIME_RESOURCE: Final = "runtime"
ECR_SERVICE: Final = "ecr"
REPOSITORY_RESOURCE: Final = "repository"

BuildKind = Literal["codeBuild", "localDocker"]


class AgentImageBuild:
    """Cómo se construye la imagen `linux/arm64` del agente.

    `code_build()` (por defecto) la construye en AWS con el proyecto de
    CodeBuild de `infra/agentcore-runtime.yaml`, igual que la CLI, sin Docker
    local. `local_docker()` la construye `cdk deploy` con el Docker (o Finch)
    de la máquina, con soporte arm64, y la sube al repositorio del bootstrap
    de CDK.

    Coste y activación
    -------------------
    Activa: nada por sí mismo; lo usa `RayitoAgent(build=...)`.
    Recursos y llamadas AWS: `code_build` hace un build de CodeBuild en cada
        `cdk deploy` cuyo contenido cambió; `local_docker` ninguno en AWS al
        construir (sube la imagen a ECR).
    Coste aproximado: `code_build`, minutos de CodeBuild arm1.small;
        `local_docker`, nada en CodeBuild.
    IAM: ver `RayitoAgent`.
    Cómo apagarla: no aplica (es una opción del constructo).
    Ejemplo:
        RayitoAgent(stack, "Agent", project="./agent", build=AgentImageBuild.local_docker())
    """

    def __init__(self, kind: BuildKind, timeout_minutes: int) -> None:
        self._kind: BuildKind = kind
        self._timeout_minutes = timeout_minutes

    @property
    def kind(self) -> BuildKind:
        return self._kind

    @property
    def timeout_minutes(self) -> int:
        return self._timeout_minutes

    @staticmethod
    def code_build(*, timeout_minutes: int | None = None) -> AgentImageBuild:
        """El build en CodeBuild (por defecto). `timeout_minutes` (5-110,
        por defecto 30) es el plazo del build; el recurso se rinde 10 minutos
        después."""
        chosen = AGENTCORE_BUILD_TIMEOUT_MINUTES if timeout_minutes is None else timeout_minutes
        if (
            isinstance(chosen, bool)
            or not isinstance(chosen, int)
            or not BUILD_TIMEOUT_MIN_MINUTES <= chosen <= BUILD_TIMEOUT_MAX_MINUTES
        ):
            raise RayitoCdkException(
                "option_invalid",
                option="timeout_minutes",
                reason=(
                    f"un número entero de minutos entre {BUILD_TIMEOUT_MIN_MINUTES} y "
                    f"{BUILD_TIMEOUT_MAX_MINUTES}"
                ),
            )
        return AgentImageBuild("codeBuild", chosen)

    @staticmethod
    def local_docker() -> AgentImageBuild:
        """El build con el Docker local (`DockerImageAsset`, `linux/arm64`)."""
        return AgentImageBuild("localDocker", AGENTCORE_BUILD_TIMEOUT_MINUTES)


def _renamed(name: str | None) -> Callable[[AgentProject], AgentProject] | None:
    if name is None:
        return None
    return lambda project: dataclasses.replace(project, template=name)


def _unique(items: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _model_arns(models: Sequence[str], model_region: str, env: StackEnvironment) -> list[str]:
    """`BedrockModelArns` como en la CLI; con una cuenta token, un ARN del
    usuario pasa tal cual (ya lo validó el plan) y el resto lleva el token."""
    if not env.account_is_token:
        return list(bedrock_resource_arns(models, model_region, env.account))
    return _unique(
        [
            arn
            for model in models
            for arn in (
                [model]
                if model.startswith(ARN_PREFIX)
                else bedrock_resource_arns([model], model_region, env.account, env.partition)
            )
        ]
    )


def _secret_arns(secrets: Sequence[str], env: StackEnvironment) -> list[str]:
    """`ReadableSecretArns` como en la CLI, con la misma regla de la cuenta
    token que `_model_arns`."""
    if not env.account_is_token:
        return list(secret_resource_arns(secrets, env.region, env.account))
    return _unique(
        [
            secret
            if secret.startswith(ARN_PREFIX)
            else secret_resource_arns([secret], env.region, env.account, env.partition)[0]
            for secret in secrets
        ]
    )


def check_runtime_environment(environment: Mapping[str, str]) -> None:
    """El esquema de `AWS::BedrockAgentCore::Runtime` admite valores de hasta
    2048 caracteres (la API, 5000; Q181): uno más largo es el `env_invalid`
    del plan, en la síntesis."""
    if any(len(value) > CDK_RUNTIME_ENVIRONMENT_VALUE_MAX_CHARS for value in environment.values()):
        raise AgentCoreException(
            "env_invalid",
            maxVariables=AGENTCORE_MAX_ENVIRONMENT_VARIABLES,
            maxChars=CDK_RUNTIME_ENVIRONMENT_VALUE_MAX_CHARS,
        )


class RayitoAgent(Construct):
    """Un agente de Rayito en Amazon Bedrock AgentCore Runtime, desplegado con
    `cdk deploy` (ADR-032).

    `project` es el directorio (o el fichero de proyecto) del agente, igual
    que `rayito agent deploy --target agentcore`; las demás opciones son las
    de `AgentCoreOptions` (`external_model=True` admite un modelo fuera de
    Bedrock sin dar al rol permisos de Bedrock que no pidas). Valida con el
    plan de AgentCore del SDK (los errores salen como `AgentCoreException`) y
    sintetiza: la pila anidada
    `Build` con `infra/agentcore-runtime.yaml` (repositorio de ECR, proyecto
    de CodeBuild, rol de build y rol del runtime, con la misma IAM que la
    CLI), el asset `Source` con el contexto de build, el recurso
    `ImageBuild` que construye la imagen si su etiqueta no existe, el
    runtime (`AWS::BedrockAgentCore::Runtime`) y el recurso `Mmds` que le
    exige MMDSv2. La región del stack tiene que ser concreta
    (`region_unresolved`); la cuenta puede ser un token.

    La CLI y los SDK leen e invocan el agente (`rayito agent list`,
    `status`, `invoke`, `AgentCoreFleet.connect`) pero `rayito agent deploy`
    y `delete` lo rechazan con `name_conflict`: se cambia con `cdk deploy` y
    se borra con `cdk destroy`.

    Coste y activación
    -------------------
    Activa: sólo `cdk deploy` de un stack que contiene el constructo;
        sintetizar no llama a AWS ni cuesta nada.
    Recursos y llamadas AWS: la pila anidada (repositorio de ECR que
        conserva 5 imágenes, proyecto de CodeBuild, grupo de logs de 14 días
        y dos roles), el zip del contexto en el bucket del bootstrap (queda
        hasta `cdk gc`), dos funciones Lambda y la máquina de estados del
        `Provider` (con sus grupos de logs de 14 días), un build de CodeBuild
        cuando el contenido cambia y el runtime de AgentCore.
    Coste aproximado: minutos de CodeBuild arm1.small por build, céntimos al
        mes de ECR y logs, céntimos por despliegue de Lambda y Step
        Functions, y AgentCore por uso (vCPU-hora y GB-hora por sesión).
        Trazas, VPC y entrega de logs quedan apagadas.
    IAM: la ejecución de CloudFormation crea roles (`CAPABILITY_IAM`); el
        manejador sólo puede arrancar y leer builds de su proyecto, describir
        imágenes de su repositorio, leer y actualizar su runtime y pasar el
        rol del runtime.
    Cómo apagarla: quitar el constructo del stack o `cdk destroy`.
    Ejemplo:
        agent = RayitoAgent(
            stack, "Agent", project="./agent", bedrock_models=["amazon.nova-lite-v1:0"]
        )
        agent.grant_invoke(my_function)
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        project: str | os.PathLike[str],
        name: str | None = None,
        bedrock_models: Sequence[str] = (),
        readable_secrets: Sequence[str] = (),
        idle_session_timeout_seconds: int | None = None,
        max_session_lifetime_seconds: int | None = None,
        tags: Mapping[str, str] | None = None,
        build: AgentImageBuild | None = None,
        export_outputs: bool = False,
        external_model: bool = False,
    ) -> None:
        env = stack_environment(scope, CONSTRUCT_NAME)
        chosen = build if build is not None else AgentImageBuild.code_build()
        planned = plan_agentcore(
            project,
            region=env.region,
            agentcore=AgentCoreOptions(
                bedrock_models=tuple(bedrock_models),
                readable_secrets=tuple(readable_secrets),
                idle_session_timeout_seconds=idle_session_timeout_seconds,
                max_session_lifetime_seconds=max_session_lifetime_seconds,
                tags=dict(tags or {}),
                external_model=external_model,
            ),
            environ={},
            adjust=_renamed(name),
        )
        plan = planned.plan
        check_runtime_environment(plan.environment)
        model_region = planned.project.spec.model.region or plan.region
        models = _model_arns(plan.bedrock_models, model_region, env)
        secrets = _secret_arns(plan.readable_secrets, env)
        super().__init__(scope, id)
        self._plan = plan
        nested = cdk.NestedStack(self, "Build")
        source = (
            s3_assets.Asset(self, "Source", path=str(self._source_zip(planned.tree)))
            if chosen.kind == "codeBuild"
            else None
        )
        bucket = UNUSED_BUCKET if source is None else source.s3_bucket_name
        template = self._include(nested, bucket, models, secrets, chosen)
        runtime_role = cast(iam.CfnRole, template.get_resource("RuntimeRole"))
        handler = AgentCoreHandler(self, "Handler")
        if source is None:
            repository_uri, image_uri = self._local_docker_image(nested, template, planned.tree)
        else:
            repository_uri, image_uri = self._code_build_image(template, source, handler)
        handler.add_to_policy(
            iam.PolicyStatement(
                actions=list(MMDS_ACTIONS),
                resources=[
                    env.stack.format_arn(
                        service=AGENTCORE_SERVICE,
                        resource=RUNTIME_RESOURCE,
                        resource_name=RUNTIME_ARN_SUFFIX.format(runtime_name=plan.runtime_name),
                    )
                ],
            )
        )
        handler.add_to_policy(
            iam.PolicyStatement(actions=list(PASS_ROLE_ACTIONS), resources=[runtime_role.attr_arn])
        )
        provider = build_provider(self, handler, chosen.timeout_minutes)
        runtime = agentcore.CfnRuntime(
            self,
            "Runtime",
            agent_runtime_name=plan.runtime_name,
            agent_runtime_artifact=agentcore.CfnRuntime.AgentRuntimeArtifactProperty(
                container_configuration=agentcore.CfnRuntime.ContainerConfigurationProperty(
                    container_uri=image_uri
                )
            ),
            role_arn=runtime_role.attr_arn,
            network_configuration=agentcore.CfnRuntime.NetworkConfigurationProperty(
                network_mode=AGENTCORE_NETWORK_MODE
            ),
            protocol_configuration=AGENTCORE_SERVER_PROTOCOL,
            lifecycle_configuration=agentcore.CfnRuntime.LifecycleConfigurationProperty(
                idle_runtime_session_timeout=plan.lifecycle.idle_session_timeout_seconds,
                max_lifetime=plan.lifecycle.max_session_lifetime_seconds,
            ),
            environment_variables=dict(plan.environment),
            description=runtime_description_for(plan.name, managed_by_cdk=True),
        )
        if source is not None:
            image_build = cdk.CustomResource(
                self,
                "ImageBuild",
                service_token=provider.service_token,
                resource_type=IMAGE_BUILD_TYPE,
                properties=self._image_build_properties(template, source),
            )
            runtime.node.add_dependency(image_build)
        mmds = cdk.CustomResource(
            self,
            "Mmds",
            service_token=provider.service_token,
            resource_type=RUNTIME_MMDS_TYPE,
            properties={
                "AgentRuntimeId": runtime.attr_agent_runtime_id,
                "AgentRuntimeVersion": runtime.attr_agent_runtime_version,
            },
        )
        self.agent_name: str = plan.name
        self.runtime_name: str = plan.runtime_name
        self.runtime_arn: str = runtime.attr_agent_runtime_arn
        self.runtime_id: str = runtime.attr_agent_runtime_id
        #: La versión viva del runtime, la que se puede fijar en un endpoint.
        #: Normalmente la de CloudFormation (AgentCore ya la crea con MMDSv2);
        #: `Mmds` sólo publica otra si una llegara sin MMDSv2.
        self.runtime_version: str = mmds.get_att_string(RUNTIME_VERSION_ATTRIBUTE)
        self.role_arn: str = runtime_role.attr_arn
        self.repository_uri: str = repository_uri
        self.image_uri: str = image_uri
        self.runtime: agentcore.IBedrockAgentRuntime = (
            agentcore.Runtime.from_agent_runtime_attributes(
                self,
                "RuntimeRef",
                agent_runtime_arn=self.runtime_arn,
                agent_runtime_id=self.runtime_id,
                agent_runtime_name=self.runtime_name,
                role_arn=self.role_arn,
                agent_runtime_version=self.runtime_version,
            )
        )
        if export_outputs:
            cdk.CfnOutput(self, "RuntimeArn", value=self.runtime_arn)
            cdk.CfnOutput(self, "RuntimeId", value=self.runtime_id)
        apply_tags(self, AGENT_COMPONENT, plan.tags)

    def grant_invoke(self, grantee: iam.IGrantable) -> iam.Grant:
        """`bedrock-agentcore:InvokeAgentRuntime` sobre el runtime y sus
        endpoints, para `grantee` (una función, un rol...)."""
        return iam.Grant.add_to_principal(
            grantee=grantee,
            actions=list(INVOKE_ACTIONS),
            resource_arns=[self.runtime_arn, f"{self.runtime_arn}{ENDPOINTS_SUFFIX}"],
        )

    # ---------------------------------------------------------- la plantilla

    def _source_zip(self, tree: Mapping[str, bytes]) -> Path:
        path = staging_directory() / SOURCE_ZIP_TEMPLATE.format(image_tag=self._plan.image_tag)
        path.write_bytes(source_zip(tree))
        return path

    def _include(
        self,
        nested: cdk.NestedStack,
        bucket: str,
        models: Sequence[str],
        secrets: Sequence[str],
        build: AgentImageBuild,
    ) -> cfn_include.CfnInclude:
        """`infra/agentcore-runtime.yaml` con cada parámetro plegado igual
        que `_stack_parameters` de la CLI (las listas como listas)."""
        plan = self._plan
        parameters: dict[str, Any] = {
            STACK_PARAMETER_AGENT_NAME: canonical_agent_name(plan.name),
            STACK_PARAMETER_AGENT_DISPLAY_NAME: plan.name,
            STACK_PARAMETER_RUNTIME_NAME: plan.runtime_name,
            STACK_PARAMETER_BUCKET: bucket,
            STACK_PARAMETER_MODEL_ARNS: list(models),
            STACK_PARAMETER_SECRET_ARNS: list(secrets),
            STACK_PARAMETER_RETENTION: str(AGENTCORE_IMAGE_RETENTION_COUNT),
            STACK_PARAMETER_LOG_DAYS: str(AGENTCORE_BUILD_LOG_RETENTION_DAYS),
            STACK_PARAMETER_BUILD_TIMEOUT: str(build.timeout_minutes),
        }
        return cfn_include.CfnInclude(
            nested, "Template", template_file=str(runtime_template()), parameters=parameters
        )

    # ---------------------------------------------------------------- imagen

    def _code_build_image(
        self,
        template: cfn_include.CfnInclude,
        source: s3_assets.Asset,
        handler: AgentCoreHandler,
    ) -> tuple[str, str]:
        """La imagen en el repositorio de la pila anidada: el rol de build
        gana una política que sólo lee el objeto del asset, y el manejador
        sólo puede usar ese proyecto y ese repositorio."""
        repository = cast(Any, template.get_resource("Repository"))
        project = cast(Any, template.get_resource("BuildProject"))
        template.get_resource("BuildRole").add_property_override(
            SOURCE_POLICY_OVERRIDE,
            {
                "PolicyName": SOURCE_POLICY_NAME,
                "PolicyDocument": {
                    "Version": POLICY_VERSION,
                    "Statement": [
                        {
                            "Sid": SOURCE_STATEMENT_SID,
                            "Effect": "Allow",
                            "Action": list(SOURCE_ACTIONS),
                            "Resource": S3_OBJECT_ARN_TEMPLATE.format(
                                partition=cdk.Aws.PARTITION,
                                bucket=source.s3_bucket_name,
                                key=source.s3_object_key,
                            ),
                        }
                    ],
                },
            },
        )
        handler.add_to_policy(
            iam.PolicyStatement(actions=list(BUILD_ACTIONS), resources=[project.attr_arn])
        )
        handler.add_to_policy(
            iam.PolicyStatement(
                actions=list(DESCRIBE_IMAGES_ACTIONS), resources=[repository.attr_arn]
            )
        )
        repository_uri: str = repository.attr_repository_uri
        image_uri = IMAGE_URI_TEMPLATE.format(
            repository_uri=repository_uri, image_tag=self._plan.image_tag
        )
        return repository_uri, image_uri

    def _image_build_properties(
        self, template: cfn_include.CfnInclude, source: s3_assets.Asset
    ) -> dict[str, Any]:
        """Las propiedades de `Custom::RayitoImageBuild`
        (`contract.constants.customResourceProperties`)."""
        return {
            "ProjectName": template.get_resource("BuildProject").ref,
            "RepositoryName": template.get_resource("Repository").ref,
            "ImageTag": self._plan.image_tag,
            "SourceLocation": SOURCE_LOCATION_TEMPLATE.format(
                bucket=source.s3_bucket_name, key=source.s3_object_key
            ),
            "AgentBaseImage": base_image_reference(AGENT_EXPORT_BASE_IMAGE_DIGEST),
            "MaxImageBytes": AGENTCORE_MAX_IMAGE_BYTES,
        }

    def _local_docker_image(
        self,
        nested: cdk.NestedStack,
        template: cfn_include.CfnInclude,
        tree: Mapping[str, bytes],
    ) -> tuple[str, str]:
        """La imagen construida por `cdk deploy` (`Image`, `linux/arm64`) en
        el repositorio del bootstrap: la pila anidada pierde el repositorio,
        el build y sus salidas, y `PullImage` del rol del runtime apunta a
        ese repositorio."""
        directory = staging_directory() / TREE_DIRECTORY_TEMPLATE.format(
            image_tag=self._plan.image_tag
        )
        for relative, data in tree.items():
            target = directory / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        image = ecr_assets.DockerImageAsset(
            self,
            "Image",
            directory=str(directory),
            platform=ecr_assets.Platform.LINUX_ARM64,
            build_args={BASE_IMAGE_ARG: base_image_reference(AGENT_EXPORT_BASE_IMAGE_DIGEST)},
        )
        for resource in LOCAL_DOCKER_REMOVED_RESOURCES:
            template.node.try_remove_child(resource)
        outputs = template.node.find_child(OUTPUTS_CHILD)
        for output in LOCAL_DOCKER_REMOVED_OUTPUTS:
            outputs.node.try_remove_child(output)
        template.get_resource("RuntimeRole").add_property_override(
            PULL_IMAGE_RESOURCE_OVERRIDE,
            nested.format_arn(
                service=ECR_SERVICE,
                resource=REPOSITORY_RESOURCE,
                resource_name=image.repository.repository_name,
            ),
        )
        return image.repository.repository_uri, image.image_uri


__all__ = ["AgentImageBuild", "RayitoAgent", "check_runtime_environment"]
