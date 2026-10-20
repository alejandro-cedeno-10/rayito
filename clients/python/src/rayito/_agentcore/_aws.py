"""Adaptadores de `--target agentcore` sobre AWS (ADR-031): los clientes boto3
de AgentCore (plano de control y de datos), CodeBuild, ECR, CloudWatch Logs, S3
y STS, y la pila de CloudFormation del agente a través de `OptionalStacks`.
Construirlos no llama a AWS: los clientes son `LazyClient`.

Cada parámetro de AWS usado aquí aparece en `AWS_API_NOTES.md` («Despliegue en
AgentCore Runtime» y §21). Los errores se traducen por **código**
(`contract.json`, `awsErrorMap`), nunca por el mensaje, y el `__cause__` pasa
por `sanitize_aws_error`: ningún tipo de botocore cruza los puertos.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Callable, Collection, Mapping
from typing import Any, Final, TypeVar

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from rayito._agent._aws_resolve import ACCESS_DENIED_CODES, AwsResolver, discover_bucket
from rayito._agent._events import failure_message
from rayito._agentcore._domain import client_token_for, stack_name_for
from rayito._agentcore._models import (
    BuildObservation,
    ImageObservation,
    RuntimeDetails,
    RuntimeListing,
    RuntimeRef,
    RuntimeSpec,
)
from rayito._agentcore._ports import (
    AgentCoreGateways,
    InvokeResponse,
    RuntimeAlreadyExists,
    RuntimeDescription,
    RuntimeNotFound,
    StackDeployment,
    StackResourceMissing,
)
from rayito._agentcore._stack import AGENTCORE_STACK_COMPONENT
from rayito._aws import LazyClient, aws_code, client_config
from rayito._aws_region import aws_session, resolve_region
from rayito._aws_sanitize import sanitize_aws_error
from rayito._limits import (
    AGENTCORE_NETWORK_MODE,
    AGENTCORE_REQUEST_TIMEOUT_SECONDS,
    AGENTCORE_SERVER_PROTOCOL,
    AGENTCORE_STREAM_MAX_SECONDS,
)
from rayito._stacks._cloudformation import CloudFormationProvisioner
from rayito._stacks._condition import StackObservation, stack_observation
from rayito._stacks._port import NO_CHANGES_OUTCOME, StackProvisioner
from rayito._stacks._service import DEFAULT_WAIT_TIMEOUT_SECONDS, OptionalStacks
from rayito.exceptions import (
    AgentCoreException,
    AgentException,
    QuotaExceededException,
    RateLimitException,
    StackException,
)

T = TypeVar("T")
#: `maxResults` de `ListAgentRuntimes` (1-100, `AWS_API_NOTES.md`).
LIST_PAGE_SIZE: Final = 100
#: Tamaño de lectura de la respuesta en streaming de `InvokeAgentRuntime`.
STREAM_CHUNK_BYTES: Final = 8192
CONTENT_TYPE_JSON: Final = "application/json"
ENV_IMAGE_TAG: Final = "IMAGE_TAG"
ENV_BASE_IMAGE: Final = "AGENT_BASE_IMAGE"
ENV_TYPE_PLAINTEXT: Final = "PLAINTEXT"
#: Estado que se muestra de una pila que ni siquiera llegó a crearse.
STACK_NOT_CREATED_STATUS: Final = "NOT_CREATED"
UNKNOWN_AWS_CODE: Final = "unknown"
THROTTLED_MESSAGE: Final = (
    "AgentCore limitó la tasa de invocaciones de la cuenta: reintenta en unos segundos"
)
QUOTA_MESSAGE: Final = (
    "se agotó una cuota de AgentCore de la cuenta (sesiones nuevas o activas): espera a que "
    "terminen sesiones o pide más cuota"
)
ACCESS_DENIED_ERROR_CODES: Final = ACCESS_DENIED_CODES | {"UnauthorizedException"}
#: Operaciones que nombran los mensajes de `access_denied` y `aws_error`.
OP_LIST_RUNTIMES: Final = "ListAgentRuntimes"
OP_GET_RUNTIME: Final = "GetAgentRuntime"
OP_CREATE_RUNTIME: Final = "CreateAgentRuntime"
OP_UPDATE_RUNTIME: Final = "UpdateAgentRuntime"
OP_DELETE_RUNTIME: Final = "DeleteAgentRuntime"
OP_INVOKE: Final = "InvokeAgentRuntime"
OP_STOP_SESSION: Final = "StopRuntimeSession"
OP_START_BUILD: Final = "StartBuild"
OP_GET_BUILD: Final = "BatchGetBuilds"
OP_DESCRIBE_IMAGES: Final = "DescribeImages"
OP_HEAD_BUCKET: Final = "HeadBucket"
OP_PUT_OBJECT: Final = "PutObject"
OP_DELETE_OBJECT: Final = "DeleteObject"
OP_DELETE_LOG_GROUP: Final = "DeleteLogGroup"
OP_CALLER_IDENTITY: Final = "GetCallerIdentity"
OP_STACK: Final = "CloudFormation"
BUCKET_NOT_FOUND_CODES: Final = frozenset({"404", "NoSuchBucket", "NotFound"})
BUCKET_FORBIDDEN_CODES: Final = frozenset({"403"})


def translate_error(exc: BotoCoreError | ClientError, operation: str) -> AgentCoreException:
    """La excepción propia de un error de AWS sin tratamiento especial:
    `access_denied` si es un acceso denegado y `aws_error` en cualquier otro
    caso, con la operación y el código, nunca el mensaje de AWS."""
    code = aws_code(exc) or type(exc).__name__
    if code in ACCESS_DENIED_ERROR_CODES:
        return AgentCoreException("access_denied", operation=operation, awsCode=code)
    return AgentCoreException("aws_error", operation=operation, awsCode=code)


class Absent:
    """Lo que devuelve `guarded` cuando AWS dice que el recurso no existe y el
    llamante lo tiene previsto."""


ABSENT: Final = Absent()


def guarded(
    operation: str,
    call: Callable[[], T],
    *,
    absent: Collection[str] = (),
    handled: Mapping[str, Callable[[], Exception]] | None = None,
) -> T | Absent:
    """El resultado de `call()`. Un código de `absent` da `ABSENT`; uno de
    `handled`, la excepción que produce su fábrica; cualquier otro error de
    AWS, `translate_error`. El `__cause__` es siempre el resumen saneado."""
    try:
        return call()
    except ClientError as exc:
        code = aws_code(exc)
        if code is not None and code in absent:
            return ABSENT
        factory = None if code is None else (handled or {}).get(code)
        if factory is not None:
            raise factory() from sanitize_aws_error(exc)
        raise translate_error(exc, operation) from sanitize_aws_error(exc)
    except BotoCoreError as exc:
        raise translate_error(exc, operation) from sanitize_aws_error(exc)


def required(
    operation: str,
    call: Callable[[], T],
    *,
    handled: Mapping[str, Callable[[], Exception]] | None = None,
) -> T:
    """Como `guarded` cuando nada puede faltar: nunca devuelve `ABSENT`."""
    result = guarded(operation, call, handled=handled)
    if isinstance(result, Absent):
        raise AssertionError(operation)
    return result


class AgentCoreClients:
    """Los clientes boto3 que usa `--target agentcore`, creados al primer uso.
    Los de datos llevan un plazo de lectura propio (`InvokeAgentRuntime`
    espera hasta 15 min una respuesta y hasta 60 un stream)."""

    def __init__(self, region: str | None, session: boto3.session.Session | None) -> None:
        self.region = region
        self.session = session
        self.control = LazyClient("bedrock-agentcore-control", region=region, session=session)
        self.codebuild = LazyClient("codebuild", region=region, session=session)
        self.ecr = LazyClient("ecr", region=region, session=session)
        self.logs = LazyClient("logs", region=region, session=session)
        self.s3 = LazyClient("s3", region=region, session=session)
        self.sts = LazyClient("sts", region=region, session=session)
        self._data: dict[float, Any] = {}

    def data(self, read_timeout_seconds: float) -> Any:
        """El cliente de `bedrock-agentcore` con ese plazo de lectura."""
        if read_timeout_seconds not in self._data:
            session = aws_session(self.session, self.region)
            self._data[read_timeout_seconds] = session.client(
                "bedrock-agentcore",
                region_name=resolve_region(self.region, self.session),
                config=client_config().merge(Config(read_timeout=read_timeout_seconds)),
            )
        return self._data[read_timeout_seconds]


class StsIdentity:
    """`AccountIdentity`: la cuenta de las credenciales (`GetCallerIdentity`)."""

    def __init__(self, clients: AgentCoreClients) -> None:
        self._clients = clients
        self._account: str | None = None

    def account_id(self) -> str:
        if self._account is None:
            identity = required(OP_CALLER_IDENTITY, self._clients.sts.get().get_caller_identity)
            self._account = str(identity["Account"])
        return self._account


class S3ArtifactBucket:
    """`ArtifactBucket`: el bucket de los contextos de build. Cada llamada a S3
    lleva `ExpectedBucketOwner`, así que un bucket de otra cuenta falla en vez de
    recibir el código del agente."""

    def __init__(self, clients: AgentCoreClients, identity: StsIdentity) -> None:
        self._clients = clients
        self._identity = identity

    def discover(self) -> str | None:
        resolver = AwsResolver(region=self._clients.region, session=self._clients.session)
        return discover_bucket(resolver, resolver.resolved_region)

    def region_of(self, bucket: str) -> str | None:
        client = self._clients.s3.get()
        response = guarded(
            OP_HEAD_BUCKET,
            lambda: client.head_bucket(Bucket=bucket),
            absent=BUCKET_NOT_FOUND_CODES | BUCKET_FORBIDDEN_CODES,
        )
        if isinstance(response, Absent):
            return None
        return str(response.get("BucketRegion") or "") or None

    def put(self, bucket: str, key: str, data: bytes) -> None:
        digest = base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")
        client = self._clients.s3.get()
        owner = self._identity.account_id()
        required(
            OP_PUT_OBJECT,
            lambda: client.put_object(
                Bucket=bucket, Key=key, ExpectedBucketOwner=owner, Body=data, ChecksumSHA256=digest
            ),
        )

    def delete(self, bucket: str, key: str) -> None:
        client = self._clients.s3.get()
        owner = self._identity.account_id()
        required(
            OP_DELETE_OBJECT,
            lambda: client.delete_object(Bucket=bucket, Key=key, ExpectedBucketOwner=owner),
        )


class EcrImageRegistry:
    """`ImageRegistry` sobre `DescribeImages`."""

    def __init__(self, clients: AgentCoreClients) -> None:
        self._clients = clients

    def describe(self, repository: str, image_tag: str) -> ImageObservation:
        client = self._clients.ecr.get()
        response = guarded(
            OP_DESCRIBE_IMAGES,
            lambda: client.describe_images(
                repositoryName=repository, imageIds=[{"imageTag": image_tag}]
            ),
            absent={"ImageNotFoundException"},
            handled={"RepositoryNotFoundException": StackResourceMissing},
        )
        details = [] if isinstance(response, Absent) else response.get("imageDetails") or []
        if not details:
            return ImageObservation(exists=False)
        return ImageObservation(exists=True, size_bytes=int(details[0]["imageSizeInBytes"]))


class CodeBuildImageBuilder:
    """`ImageBuilder` sobre `StartBuild` y `BatchGetBuilds`."""

    def __init__(self, clients: AgentCoreClients) -> None:
        self._clients = clients

    def start(
        self, project: str, bucket: str, key: str, image_tag: str, base_image: str
    ) -> BuildObservation:
        client = self._clients.codebuild.get()
        response = required(
            OP_START_BUILD,
            lambda: client.start_build(
                projectName=project,
                sourceLocationOverride=f"{bucket}/{key}",
                environmentVariablesOverride=[
                    {"name": ENV_IMAGE_TAG, "value": image_tag, "type": ENV_TYPE_PLAINTEXT},
                    {"name": ENV_BASE_IMAGE, "value": base_image, "type": ENV_TYPE_PLAINTEXT},
                ],
                idempotencyToken=image_tag,
            ),
            handled={
                "AccountLimitExceededException": lambda: AgentCoreException("build_quota"),
                "ResourceNotFoundException": StackResourceMissing,
            },
        )
        build = response["build"]
        return BuildObservation(
            build_id=str(build["id"]), status=str(build["buildStatus"]), log_group=""
        )

    def get(self, build_id: str) -> BuildObservation:
        client = self._clients.codebuild.get()
        response = required(OP_GET_BUILD, lambda: client.batch_get_builds(ids=[build_id]))
        build = response["builds"][0]
        logs = build.get("logs") or {}
        return BuildObservation(
            build_id=str(build["id"]),
            status=str(build["buildStatus"]),
            log_group=str(logs.get("groupName") or ""),
            phase=str(build.get("currentPhase") or ""),
        )


def _listing_of(item: Mapping[str, Any]) -> RuntimeListing:
    return RuntimeListing(
        runtime_id=str(item["agentRuntimeId"]),
        arn=str(item["agentRuntimeArn"]),
        name=str(item["agentRuntimeName"]),
        version=str(item["agentRuntimeVersion"]),
        status=str(item["status"]),
        description=str(item.get("description") or ""),
    )


def _details_of(response: Mapping[str, Any]) -> RuntimeDetails:
    artifact = (response.get("agentRuntimeArtifact") or {}).get("containerConfiguration") or {}
    lifecycle = response.get("lifecycleConfiguration") or {}
    return RuntimeDetails(
        container_uri=str(artifact.get("containerUri") or ""),
        role_arn=str(response.get("roleArn") or ""),
        network_mode=str((response.get("networkConfiguration") or {}).get("networkMode") or ""),
        server_protocol=str(
            (response.get("protocolConfiguration") or {}).get("serverProtocol") or ""
        ),
        environment_variables=dict(response.get("environmentVariables") or {}),
        idle_runtime_session_timeout=int(lifecycle.get("idleRuntimeSessionTimeout") or 0),
        max_lifetime=int(lifecycle.get("maxLifetime") or 0),
        require_mmds_v2=bool((response.get("metadataConfiguration") or {}).get("requireMMDSV2")),
    )


def _runtime_request(spec: RuntimeSpec) -> dict[str, Any]:
    """Lo que `CreateAgentRuntime` y `UpdateAgentRuntime` comparten (sólo
    parámetros de `AWS_API_NOTES.md`)."""
    return {
        "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": spec.container_uri}},
        "roleArn": spec.role_arn,
        "networkConfiguration": {"networkMode": AGENTCORE_NETWORK_MODE},
        "protocolConfiguration": {"serverProtocol": AGENTCORE_SERVER_PROTOCOL},
        "lifecycleConfiguration": {
            "idleRuntimeSessionTimeout": spec.idle_runtime_session_timeout,
            "maxLifetime": spec.max_lifetime,
        },
        "environmentVariables": dict(spec.environment_variables),
        "description": spec.description,
    }


class AgentCoreRuntimes:
    """`RuntimeControl` sobre el plano de control de AgentCore."""

    def __init__(self, clients: AgentCoreClients) -> None:
        self._clients = clients

    def list_runtimes(self) -> tuple[RuntimeListing, ...]:
        client = self._clients.control.get()
        listings: list[RuntimeListing] = []
        request: dict[str, Any] = {"maxResults": LIST_PAGE_SIZE}
        while True:
            page = required(OP_LIST_RUNTIMES, lambda: client.list_agent_runtimes(**request))
            listings.extend(_listing_of(item) for item in page.get("agentRuntimes") or [])
            token = page.get("nextToken")
            if not token:
                return tuple(listings)
            request["nextToken"] = token

    def get_runtime(self, runtime_id: str) -> RuntimeDescription:
        client = self._clients.control.get()
        response = required(
            OP_GET_RUNTIME,
            lambda: client.get_agent_runtime(agentRuntimeId=runtime_id),
            handled={"ResourceNotFoundException": RuntimeNotFound},
        )
        listing = _listing_of({**response, "agentRuntimeId": runtime_id})
        return RuntimeDescription(listing=listing, details=_details_of(response))

    def create_runtime(self, spec: RuntimeSpec) -> RuntimeRef:
        client = self._clients.control.get()
        request = {"agentRuntimeName": spec.name, **_runtime_request(spec), "tags": dict(spec.tags)}
        request["clientToken"] = client_token_for(request)
        response = required(
            OP_CREATE_RUNTIME,
            lambda: client.create_agent_runtime(**request),
            handled={"ConflictException": RuntimeAlreadyExists},
        )
        return RuntimeRef(
            runtime_id=str(response["agentRuntimeId"]),
            arn=str(response["agentRuntimeArn"]),
            version=str(response["agentRuntimeVersion"]),
            status=str(response["status"]),
        )

    def update_runtime(self, runtime_id: str, spec: RuntimeSpec) -> RuntimeRef:
        client = self._clients.control.get()
        request = {
            "agentRuntimeId": runtime_id,
            **_runtime_request(spec),
            "metadataConfiguration": {"requireMMDSV2": True},
        }
        request["clientToken"] = client_token_for(request)
        response = required(
            OP_UPDATE_RUNTIME,
            lambda: client.update_agent_runtime(**request),
            handled={"ResourceNotFoundException": RuntimeNotFound},
        )
        return RuntimeRef(
            runtime_id=runtime_id,
            arn="",
            version=str(response["agentRuntimeVersion"]),
            status=str(response["status"]),
        )

    def delete_runtime(self, runtime_id: str) -> None:
        client = self._clients.control.get()
        guarded(
            OP_DELETE_RUNTIME,
            lambda: client.delete_agent_runtime(agentRuntimeId=runtime_id),
            absent={"ResourceNotFoundException"},
        )

    def runtime_exists(self, runtime_id: str) -> bool:
        client = self._clients.control.get()
        found = guarded(
            OP_GET_RUNTIME,
            lambda: client.get_agent_runtime(agentRuntimeId=runtime_id),
            absent={"ResourceNotFoundException"},
        )
        return not isinstance(found, Absent)


class AgentCoreInvoker:
    """`RuntimeInvoker` sobre el plano de datos, firmado con las credenciales
    del llamante (SigV4). `invoke` espera hasta 15 minutos una respuesta y
    `stream` hasta 60 un stream."""

    def __init__(self, clients: AgentCoreClients) -> None:
        self._clients = clients

    def invoke(
        self, runtime_arn: str, session_id: str, payload: bytes, accept: str
    ) -> InvokeResponse:
        return self._invoke(
            runtime_arn, session_id, payload, accept, AGENTCORE_REQUEST_TIMEOUT_SECONDS
        )

    def stream(
        self, runtime_arn: str, session_id: str, payload: bytes, accept: str
    ) -> InvokeResponse:
        return self._invoke(runtime_arn, session_id, payload, accept, AGENTCORE_STREAM_MAX_SECONDS)

    def _invoke(
        self, runtime_arn: str, session_id: str, payload: bytes, accept: str, timeout: float
    ) -> InvokeResponse:
        client = self._clients.data(timeout)
        response = required(
            OP_INVOKE,
            lambda: client.invoke_agent_runtime(
                agentRuntimeArn=runtime_arn,
                runtimeSessionId=session_id,
                contentType=CONTENT_TYPE_JSON,
                accept=accept,
                payload=payload,
            ),
            handled={
                "ResourceNotFoundException": RuntimeNotFound,
                "ThrottlingException": _throttled,
                "ServiceQuotaExceededException": _over_quota,
                "RuntimeClientError": _runtime_error,
            },
        )
        body = response["response"]
        return InvokeResponse(chunks=body.iter_chunks(STREAM_CHUNK_BYTES), close=body.close)

    def stop_session(self, runtime_arn: str, session_id: str) -> bool:
        client = self._clients.data(AGENTCORE_REQUEST_TIMEOUT_SECONDS)
        stopped = guarded(
            OP_STOP_SESSION,
            lambda: client.stop_runtime_session(
                agentRuntimeArn=runtime_arn, runtimeSessionId=session_id
            ),
            absent={"ResourceNotFoundException"},
        )
        return not isinstance(stopped, Absent)


def _throttled() -> Exception:
    return RateLimitException(THROTTLED_MESSAGE, aws_code="ThrottlingException")


def _over_quota() -> Exception:
    return QuotaExceededException(QUOTA_MESSAGE)


def _runtime_error() -> Exception:
    return AgentException(failure_message("runtime_error"), reason="runtime_error")


class CloudWatchRuntimeLogs:
    """`RuntimeLogs` sobre `DeleteLogGroup`."""

    def __init__(self, clients: AgentCoreClients) -> None:
        self._clients = clients

    def delete_log_group(self, log_group: str) -> bool:
        client = self._clients.logs.get()
        deleted = guarded(
            OP_DELETE_LOG_GROUP,
            lambda: client.delete_log_group(logGroupName=log_group),
            absent={"ResourceNotFoundException"},
        )
        return not isinstance(deleted, Absent)


class CloudFormationAgentStack:
    """`AgentStack` sobre `OptionalStacks`: crear y actualizar son el mismo
    `deploy`, que decide por el estado de la pila; `tags` (`--tag`) se manda
    como etiquetas de la pila, que CloudFormation propaga a sus recursos."""

    def __init__(
        self,
        provisioner: StackProvisioner,
        *,
        stack_name: str,
        tags: Mapping[str, str] | None = None,
        wait_timeout_seconds: float = DEFAULT_WAIT_TIMEOUT_SECONDS,
    ) -> None:
        self._stacks = OptionalStacks(provisioner=provisioner)
        self._provisioner = provisioner
        self._stack_name = stack_name
        self._tags = dict(tags or {})
        self._wait_timeout = wait_timeout_seconds

    def observe(self) -> StackObservation:
        try:
            status = self._stacks.status(AGENTCORE_STACK_COMPONENT, stack_name=self._stack_name)
        except StackException as exc:
            raise self._translate(exc) from exc.__cause__
        return stack_observation(status, self._stack_name)

    def deploy(self, parameters: Mapping[str, str]) -> StackDeployment:
        try:
            result = self._stacks.deploy_with_outcome(
                AGENTCORE_STACK_COMPONENT,
                stack_name=self._stack_name,
                parameters=dict(parameters),
                tags=self._tags,
                wait_timeout=self._wait_timeout,
            )
        except StackException as exc:
            raise self._translate(exc) from exc.__cause__
        changed = result.update_outcome != NO_CHANGES_OUTCOME
        return StackDeployment(stack_observation(result.status, self._stack_name), changed)

    def wait(self) -> StackObservation:
        try:
            self._provisioner.wait(self._stack_name, "deployed", self._wait_timeout)
        except StackException as exc:
            raise self._translate(exc) from exc.__cause__
        return self.observe()

    def destroy(self) -> None:
        try:
            self._stacks.destroy(
                AGENTCORE_STACK_COMPONENT,
                stack_name=self._stack_name,
                wait_timeout=self._wait_timeout,
            )
        except StackException as exc:
            raise AgentCoreException(
                "delete_failed",
                resource="stack",
                resourceName=self._stack_name,
                status=self._status_text() or STACK_NOT_CREATED_STATUS,
            ) from exc.__cause__

    def _status_text(self) -> str | None:
        try:
            status = self._provisioner.describe(self._stack_name)
        except StackException:
            return None
        return None if status is None else status.state

    def _translate(self, exc: StackException) -> AgentCoreException:
        cause = exc.__cause__
        code = getattr(cause, "code", None) or getattr(cause, "name", None)
        if isinstance(code, str) and code in ACCESS_DENIED_ERROR_CODES:
            return AgentCoreException("access_denied", operation=OP_STACK, awsCode=code)
        return AgentCoreException(
            "stack_failed",
            stackName=self._stack_name,
            status=self._status_text() or STACK_NOT_CREATED_STATUS,
        )


def deployment_region(region: str | None, session: boto3.session.Session | None) -> str | None:
    """La región de la llamada: argumento, sesión, `AWS_REGION` y la cadena de
    boto3 (`AWS_DEFAULT_REGION` y el perfil)."""
    return resolve_region(region, session) or aws_session(session, region).region_name


def gateways_for(
    name: str,
    region: str,
    *,
    session: boto3.session.Session | None = None,
    tags: Mapping[str, str] | None = None,
) -> AgentCoreGateways:
    """Los puertos de AWS para el agente `name` en `region`. No llama a AWS:
    todos los clientes se crean al usarse."""
    clients = AgentCoreClients(region, session)
    identity = StsIdentity(clients)
    provisioner = CloudFormationProvisioner(region=region, session=session)
    return AgentCoreGateways(
        region=region,
        identity=identity,
        bucket=S3ArtifactBucket(clients, identity),
        stack=CloudFormationAgentStack(provisioner, stack_name=stack_name_for(name), tags=tags),
        registry=EcrImageRegistry(clients),
        builder=CodeBuildImageBuilder(clients),
        runtimes=AgentCoreRuntimes(clients),
        logs=CloudWatchRuntimeLogs(clients),
    )


__all__ = [
    "AgentCoreClients",
    "AgentCoreInvoker",
    "AgentCoreRuntimes",
    "CloudFormationAgentStack",
    "deployment_region",
    "gateways_for",
]
