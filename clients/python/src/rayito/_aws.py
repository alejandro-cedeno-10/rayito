"""Plano de control: puerto `ControlPlane` y adaptador boto3 `lambda-microvms`.

Todo parámetro enviado a AWS aparece literalmente en `AWS_API_NOTES.md` §2, §3,
§5 y §6. Los errores se mapean por **nombre** de excepción de botocore, nunca
por status HTTP (`ServiceQuotaExceededException` llega con 402).
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from rayito._limits import (
    API_TPS,
    LIST_MAX_RESULTS,
    TERMINAL_STATES,
    TOKEN_TTL_MINUTES,
)
from rayito._models import IdlePolicy, SandboxInfo, SandboxListItem
from rayito._version import __version__
from rayito.exceptions import (
    AuthenticationException,
    CapacityException,
    InvalidArgumentException,
    QuotaExceededException,
    RateLimitException,
    SandboxException,
    SandboxNotFoundException,
    SandboxStateException,
)

MonotonicClock = Callable[[], float]
Sleeper = Callable[[float], None]

AUTH_TOKEN_RESPONSE_KEY = "X-aws-proxy-auth"
IMAGE_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


@dataclass(frozen=True)
class PortSpec:
    """Un `PortSpecification` de `create-microvm-auth-token`: puerto o rango.

    `allPorts` no se modela a propósito: expondría el puerto de hooks (ADR-006).
    """

    start: int
    end: int

    @classmethod
    def single(cls, port: int) -> PortSpec:
        return cls(port, port)

    @classmethod
    def range(cls, start: int, end: int) -> PortSpec:
        if start > end:
            raise InvalidArgumentException(f"rango de puertos invertido: {start}-{end}")
        return cls(start, end)

    def covers(self, port: int) -> bool:
        return self.start <= port <= self.end

    def to_api(self) -> dict[str, Any]:
        if self.start == self.end:
            return {"port": self.start}
        return {"range": {"startPort": self.start, "endPort": self.end}}


@dataclass(frozen=True)
class LaunchRequest:
    """`RunMicrovmRequest` ya validado. `idle` trae los tres campos resueltos."""

    image_arn: str
    maximum_duration_seconds: int
    run_hook_payload: str
    client_token: str
    logging: dict[str, Any]
    image_version: str | None = None
    execution_role_arn: str | None = None
    idle: IdlePolicy | None = None
    ingress_connectors: tuple[str, ...] = ()
    egress_connectors: tuple[str, ...] = ()

    def to_api(self) -> dict[str, Any]:
        params: dict[str, Any] = {
            "imageIdentifier": self.image_arn,
            "maximumDurationInSeconds": self.maximum_duration_seconds,
            "runHookPayload": self.run_hook_payload,
            "clientToken": self.client_token,
            "logging": self.logging,
        }
        if self.image_version is not None:
            params["imageVersion"] = self.image_version
        if self.execution_role_arn is not None:
            params["executionRoleArn"] = self.execution_role_arn
        if self.idle is not None:
            params["idlePolicy"] = idle_policy_to_api(self.idle)
        if self.ingress_connectors:
            params["ingressNetworkConnectors"] = list(self.ingress_connectors)
        if self.egress_connectors:
            params["egressNetworkConnectors"] = list(self.egress_connectors)
        return params


class ControlPlane(Protocol):
    """Lo que el dominio necesita del plano de control de AWS."""

    @property
    def region(self) -> str: ...

    def resolve_template_arn(self, template: str) -> str: ...

    def run_microvm(self, request: LaunchRequest) -> SandboxInfo: ...

    def get_microvm(self, sandbox_id: str) -> SandboxInfo: ...

    def list_microvms(
        self,
        *,
        image_arn: str | None = None,
        image_version: str | None = None,
        states: Iterable[str] | None = None,
    ) -> Iterator[SandboxListItem]: ...

    def terminate_microvm(self, sandbox_id: str) -> bool: ...

    def suspend_microvm(self, sandbox_id: str) -> bool: ...

    def resume_microvm(self, sandbox_id: str) -> bool: ...

    def create_auth_token(self, sandbox_id: str, ports: Sequence[PortSpec]) -> str: ...


class TokenBucket:
    """Limitador por operación alineado con la cuota publicada (burst = rate).

    Los tokens pueden quedar en negativo: cada `acquire` reserva su turno y
    duerme lo que falte, así N llamadas concurrentes se serializan al ritmo
    de la cuota sin que ninguna se pierda.
    """

    def __init__(
        self,
        rate_per_second: float,
        *,
        clock: MonotonicClock = time.monotonic,
        sleep: Sleeper = time.sleep,
    ) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate_per_second debe ser > 0")
        self._rate = rate_per_second
        self._capacity = rate_per_second
        self._tokens = rate_per_second
        self._clock = clock
        self._sleep = sleep
        self._updated_at = clock()
        self._lock = threading.Lock()

    def acquire(self) -> float:
        with self._lock:
            self._refill()
            wait = max(0.0, (1.0 - self._tokens) / self._rate)
            self._tokens -= 1.0
        if wait > 0:
            self._sleep(wait)
        return wait

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._updated_at)
        self._updated_at = now
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)


def client_config() -> Config:
    return Config(
        retries={"mode": "standard", "total_max_attempts": 5},
        connect_timeout=5,
        read_timeout=60,
        user_agent_extra=f"rayito/{__version__}",
    )


class LambdaMicrovmsControlPlane:
    """Adaptador boto3 del puerto `ControlPlane`. Testable con `botocore.stub.Stubber`."""

    def __init__(
        self,
        client: Any,
        *,
        sts_client: Any | None = None,
        sts_client_factory: Callable[[], Any] | None = None,
        clock: MonotonicClock = time.monotonic,
        sleep: Sleeper = time.sleep,
    ) -> None:
        self._client = client
        self._sts_client = sts_client
        self._sts_client_factory = sts_client_factory
        self._buckets = {
            operation: TokenBucket(rate, clock=clock, sleep=sleep)
            for operation, rate in API_TPS.items()
        }
        self._caller_identity: dict[str, str] | None = None
        self._identity_lock = threading.Lock()

    @classmethod
    def from_session(
        cls,
        session: boto3.session.Session | None = None,
        *,
        region: str | None = None,
    ) -> LambdaMicrovmsControlPlane:
        """El cliente STS se construye sólo si algún template se resuelve por nombre."""
        resolved_session = session or boto3.session.Session(region_name=region)
        config = client_config()
        return cls(
            resolved_session.client("lambda-microvms", region_name=region, config=config),
            sts_client_factory=lambda: resolved_session.client(
                "sts", region_name=region, config=config
            ),
        )

    @property
    def region(self) -> str:
        return str(self._client.meta.region_name)

    def resolve_template_arn(self, template: str) -> str:
        """Un nombre pelado no vale en ninguna operación: siempre se pasa el ARN."""
        if template.startswith("arn:"):
            return template
        if not IMAGE_NAME_PATTERN.match(template):
            raise InvalidArgumentException(
                f"template inválido: {template!r} (ARN o nombre [a-zA-Z0-9-_], máx. 64)"
            )
        identity = self._identity()
        partition = identity["Arn"].split(":")[1]
        return (
            f"arn:{partition}:lambda:{self.region}:{identity['Account']}:microvm-image:{template}"
        )

    def run_microvm(self, request: LaunchRequest) -> SandboxInfo:
        response = self._invoke("RunMicrovm", self._client.run_microvm, **request.to_api())
        return sandbox_info_from_response(response)

    def get_microvm(self, sandbox_id: str) -> SandboxInfo:
        response = self._invoke(
            "GetMicrovm", self._client.get_microvm, microvmIdentifier=sandbox_id
        )
        return sandbox_info_from_response(response)

    def list_microvms(
        self,
        *,
        image_arn: str | None = None,
        image_version: str | None = None,
        states: Iterable[str] | None = None,
    ) -> Iterator[SandboxListItem]:
        """Pagina `list-microvms`; sin `states` omite `TERMINATING|TERMINATED`."""
        params: dict[str, Any] = {"PaginationConfig": {"PageSize": LIST_MAX_RESULTS}}
        if image_arn is not None:
            params["imageIdentifier"] = image_arn
        if image_version is not None:
            params["imageVersion"] = image_version
        wanted = frozenset(states) if states is not None else None
        paginator = self._client.get_paginator("list_microvms")
        try:
            for page in paginator.paginate(**params):
                for item in page.get("items", []):
                    if _listed_state_wanted(item["state"], wanted):
                        yield sandbox_list_item_from_response(item)
        except ClientError as exc:
            raise translate_client_error(exc) from exc

    def terminate_microvm(self, sandbox_id: str) -> bool:
        """Idempotente en el modelo; False sólo si el MicroVM no existe."""
        try:
            self._invoke(
                "TerminateMicrovm", self._client.terminate_microvm, microvmIdentifier=sandbox_id
            )
        except SandboxNotFoundException:
            return False
        return True

    def suspend_microvm(self, sandbox_id: str) -> bool:
        """False cuando AWS responde `ConflictException` (no estaba `RUNNING`)."""
        try:
            self._invoke(
                "SuspendMicrovm", self._client.suspend_microvm, microvmIdentifier=sandbox_id
            )
        except SandboxStateException:
            return False
        return True

    def resume_microvm(self, sandbox_id: str) -> bool:
        """False cuando AWS responde `ConflictException` (no estaba `SUSPENDED`)."""
        try:
            self._invoke("ResumeMicrovm", self._client.resume_microvm, microvmIdentifier=sandbox_id)
        except SandboxStateException:
            return False
        return True

    def create_auth_token(self, sandbox_id: str, ports: Sequence[PortSpec]) -> str:
        if not ports:
            raise InvalidArgumentException("allowedPorts necesita al menos un puerto")
        response = self._invoke(
            "CreateMicrovmAuthToken",
            self._client.create_microvm_auth_token,
            microvmIdentifier=sandbox_id,
            expirationInMinutes=TOKEN_TTL_MINUTES,
            allowedPorts=[spec.to_api() for spec in ports],
        )
        return proxy_jwe_from_response(response["authToken"])

    def _invoke(
        self, operation: str, method: Callable[..., dict[str, Any]], **params: Any
    ) -> dict[str, Any]:
        bucket = self._buckets.get(operation)
        if bucket is not None:
            bucket.acquire()
        try:
            return method(**params)
        except ClientError as exc:
            raise translate_client_error(exc) from exc

    def _identity(self) -> dict[str, str]:
        with self._identity_lock:
            if self._caller_identity is None:
                try:
                    response = self._sts().get_caller_identity()
                except ClientError as exc:
                    raise translate_client_error(exc) from exc
                self._caller_identity = {"Account": response["Account"], "Arn": response["Arn"]}
            return self._caller_identity

    def _sts(self) -> Any:
        if self._sts_client is None and self._sts_client_factory is not None:
            self._sts_client = self._sts_client_factory()
        if self._sts_client is None:
            raise InvalidArgumentException(
                "resolver un template por nombre requiere STS; pasa el ARN de la imagen"
            )
        return self._sts_client


ControlPlaneKey = tuple[Any, str | None]

_shared_planes: dict[ControlPlaneKey, LambdaMicrovmsControlPlane] = {}
_shared_planes_lock = threading.Lock()


def shared_control_plane(
    session: boto3.session.Session | None = None, *, region: str | None = None
) -> LambdaMicrovmsControlPlane:
    """Un plano por `(session, region)` y proceso (ARCHITECTURE.md, "Token buckets
    por proceso"): N `Sandbox.create()` concurrentes sin `control_plane` explícito
    comparten los mismos buckets y el mismo cliente boto3.

    La sesión es la clave (no su `id()`) para que el registro la mantenga viva
    y un id reciclado nunca devuelva el plano de otra sesión.
    """
    key: ControlPlaneKey = (session, region)
    with _shared_planes_lock:
        plane = _shared_planes.get(key)
        if plane is None:
            plane = LambdaMicrovmsControlPlane.from_session(session, region=region)
            _shared_planes[key] = plane
        return plane


def idle_policy_to_api(idle: IdlePolicy) -> dict[str, Any]:
    if idle.suspended_duration_seconds is None:
        raise InvalidArgumentException(
            "suspended_duration_seconds debe estar resuelto antes de run-microvm"
        )
    return {
        "maxIdleDurationSeconds": idle.max_idle_seconds,
        "suspendedDurationSeconds": idle.suspended_duration_seconds,
        "autoResumeEnabled": idle.auto_resume,
    }


def idle_policy_from_response(payload: dict[str, Any] | None) -> IdlePolicy | None:
    if not payload:
        return None
    return IdlePolicy(
        max_idle_seconds=int(payload["maxIdleDurationSeconds"]),
        suspended_duration_seconds=int(payload["suspendedDurationSeconds"]),
        auto_resume=bool(payload["autoResumeEnabled"]),
    )


def normalize_endpoint(endpoint: str) -> str:
    """`endpoint` llega como hostname pelado (medido); tolera `https://host/`."""
    host = endpoint.strip()
    if "://" in host:
        host = host.split("://", 1)[1]
    return host.split("/", 1)[0]


def sandbox_info_from_response(response: dict[str, Any]) -> SandboxInfo:
    return SandboxInfo(
        sandbox_id=response["microvmId"],
        state=response["state"],
        endpoint=normalize_endpoint(response["endpoint"]),
        template=response["imageArn"],
        template_version=response["imageVersion"],
        started_at=response["startedAt"],
        maximum_duration_seconds=int(response["maximumDurationInSeconds"]),
        terminated_at=response.get("terminatedAt"),
        state_reason=response.get("stateReason"),
        idle=idle_policy_from_response(response.get("idlePolicy")),
        execution_role_arn=response.get("executionRoleArn"),
        ingress=tuple(str(arn) for arn in response.get("ingressNetworkConnectors", ())),
        egress=tuple(str(arn) for arn in response.get("egressNetworkConnectors", ())),
    )


def sandbox_list_item_from_response(item: dict[str, Any]) -> SandboxListItem:
    return SandboxListItem(
        sandbox_id=item["microvmId"],
        state=item["state"],
        template=item["imageArn"],
        template_version=item["imageVersion"],
        started_at=item["startedAt"],
    )


def proxy_jwe_from_response(parts: dict[str, str]) -> str:
    """El map trae una única clave `X-aws-proxy-auth` (medido); se toleran otras."""
    exact = parts.get(AUTH_TOKEN_RESPONSE_KEY)
    if exact:
        return exact
    for key, value in parts.items():
        if key.lower() == AUTH_TOKEN_RESPONSE_KEY.lower():
            return value
    if len(parts) == 1:
        return next(iter(parts.values()))
    raise SandboxException(
        f"authToken sin {AUTH_TOKEN_RESPONSE_KEY}; claves recibidas: {sorted(parts)}"
    )


def translate_client_error(exc: ClientError) -> Exception:
    error = exc.response.get("Error", {})
    code = str(error.get("Code", ""))
    message = str(error.get("Message") or exc)
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    if code == "ResourceNotFoundException":
        return SandboxNotFoundException(message, status_code=status, aws_code=code)
    if code == "ValidationException":
        return InvalidArgumentException(message, status_code=status, aws_code=code)
    if code == "AccessDeniedException":
        return AuthenticationException(message, aws_code=code)
    if code == "ThrottlingException":
        retry_after = exc.response.get("retryAfterSeconds")
        return RateLimitException(
            message,
            retry_after=float(retry_after) if retry_after is not None else None,
            status_code=status,
            aws_code=code,
        )
    if code == "ConflictException":
        return SandboxStateException(message, status_code=status, aws_code=code)
    if code == "ServiceQuotaExceededException":
        return QuotaExceededException(message, quota_code=exc.response.get("quotaCode"))
    if code == "InsufficientCapacityException":
        return CapacityException(message)
    return SandboxException(message, status_code=status, aws_code=code or None)


def _listed_state_wanted(state: str, wanted: frozenset[str] | None) -> bool:
    if wanted is None:
        return state not in TERMINAL_STATES
    return state in wanted
