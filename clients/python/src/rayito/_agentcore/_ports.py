"""Puertos de `--target agentcore` (ADR-031): lo que el servicio necesita del
mundo exterior, como `Protocol`s. Los adaptadores viven en `_aws.py` (boto3) y
`_stack.py` (CloudFormation); los tests usan fakes en memoria
(`tests/unit/fake_agentcore.py`). Ningún tipo de botocore cruza estos puertos.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Protocol

from rayito._agentcore._models import (
    BuildObservation,
    ImageObservation,
    RuntimeDetails,
    RuntimeListing,
    RuntimeRef,
    RuntimeSpec,
)
from rayito._stacks._condition import StackObservation


class RuntimeNotFound(Exception):
    """AgentCore respondió `ResourceNotFoundException`: el runtime ya no
    existe. Quien lo recibe sabe el nombre del agente y lanza `runtime_not_found`."""


class StackResourceMissing(Exception):
    """La pila está lista pero le falta un recurso (el repositorio de ECR o el
    proyecto de CodeBuild): no es la plantilla de Rayito (`stack_failed`)."""


class RuntimeAlreadyExists(Exception):
    """`CreateAgentRuntime` respondió `ConflictException`: otro despliegue
    creó el runtime a la vez. Quien lo recibe lista de nuevo y actualiza."""


@dataclass(frozen=True)
class StackDeployment:
    """Lo que dejó desplegar la pila: lo observado al terminar y si
    CloudFormation cambió algo (`False` ante «No updates are to be performed»)."""

    observation: StackObservation
    changed: bool


@dataclass(frozen=True)
class RuntimeDescription:
    """`GetAgentRuntime`: el elemento del listado y su configuración."""

    listing: RuntimeListing
    details: RuntimeDetails


@dataclass(frozen=True)
class InvokeResponse:
    """La respuesta de `InvokeAgentRuntime`: el cuerpo, en trozos, y cómo
    cerrarlo."""

    chunks: Iterator[bytes]
    close: Callable[[], None]


class AgentStack(Protocol):
    """La pila `rayito-agentcore-<nombre>` de un agente (repositorio de ECR,
    proyecto de CodeBuild y los dos roles), a través de `OptionalStacks`."""

    def observe(self) -> StackObservation: ...

    def deploy(self, parameters: Mapping[str, str]) -> StackDeployment: ...

    def wait(self) -> StackObservation: ...

    def destroy(self) -> None: ...


class ImageRegistry(Protocol):
    def describe(self, repository: str, image_tag: str) -> ImageObservation:
        """`DescribeImages` de la etiqueta; `exists=False` si no está."""
        ...


class ImageBuilder(Protocol):
    def start(
        self, project: str, bucket: str, key: str, image_tag: str, base_image: str
    ) -> BuildObservation:
        """`StartBuild` sobre el zip `bucket/key`, con `idempotencyToken` la
        etiqueta de la imagen."""
        ...

    def get(self, build_id: str) -> BuildObservation: ...


class ArtifactBucket(Protocol):
    def discover(self) -> str | None:
        """El bucket del bootstrap de CDK o el de `rayito setup`, si existe."""
        ...

    def region_of(self, bucket: str) -> str | None:
        """La región del bucket (`HeadBucket`) o `None` si no existe."""
        ...

    def put(self, bucket: str, key: str, data: bytes) -> None: ...

    def delete(self, bucket: str, key: str) -> None: ...


class RuntimeControl(Protocol):
    def list_runtimes(self) -> tuple[RuntimeListing, ...]: ...

    def get_runtime(self, runtime_id: str) -> RuntimeDescription: ...

    def create_runtime(self, spec: RuntimeSpec) -> RuntimeRef:
        """Lanza `RuntimeAlreadyExists` si otro lo creó a la vez."""
        ...

    def update_runtime(self, runtime_id: str, spec: RuntimeSpec) -> RuntimeRef:
        """`UpdateAgentRuntime` con la configuración completa y
        `requireMMDSV2=true`."""
        ...

    def delete_runtime(self, runtime_id: str) -> None: ...

    def runtime_exists(self, runtime_id: str) -> bool:
        """`False` cuando `GetAgentRuntime` responde `ResourceNotFoundException`."""
        ...


class RuntimeInvoker(Protocol):
    def invoke(
        self, runtime_arn: str, session_id: str, payload: bytes, accept: str
    ) -> InvokeResponse:
        """`InvokeAgentRuntime` de una respuesta síncrona (hasta 15 min)."""
        ...

    def stream(
        self, runtime_arn: str, session_id: str, payload: bytes, accept: str
    ) -> InvokeResponse:
        """`InvokeAgentRuntime` de una respuesta en streaming (hasta 60 min)."""
        ...

    def stop_session(self, runtime_arn: str, session_id: str) -> bool:
        """`StopRuntimeSession`; `False` si la sesión ya no existe."""
        ...


class RuntimeLogs(Protocol):
    def delete_log_group(self, log_group: str) -> bool:
        """`DeleteLogGroup`; `False` si ya no existe."""
        ...


class AccountIdentity(Protocol):
    def account_id(self) -> str: ...


@dataclass(frozen=True)
class AgentCoreGateways:
    """Todos los puertos de un despliegue de un agente. `stack` es el de ese
    agente y `region` la región en la que actúan."""

    region: str
    identity: AccountIdentity
    bucket: ArtifactBucket
    stack: AgentStack
    registry: ImageRegistry
    builder: ImageBuilder
    runtimes: RuntimeControl
    logs: RuntimeLogs
