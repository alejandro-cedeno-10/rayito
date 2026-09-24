"""`LifecycleService` del `rayd` falso (ADR-011).

Aplica `SetTimeout` como `rayd`: exige `x-access-token`, `UNIMPLEMENTED` si
el `Health` falso no trae `lifecycle` (un agente anterior a M9),
`FAILED_PRECONDITION lifecycle_unmanaged` en `UNMANAGED`, `INVALID_ARGUMENT
timeout beyond cap; cap_unix_ms=<n>` más allá del tope, y EXACT/AT_LEAST
sobre el `LifecycleState` que también sirve `Health` (el mismo objeto), así
un `SetTimeout` se ve en el siguiente `Health`.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

import grpc

from rayito._transport import SANDBOX_TIMEOUT_DETAIL, metadata_dict
from rayito.v1 import lifecycle_pb2, lifecycle_pb2_grpc

from .fake_process import presented_token_sha256

DEFAULT_CAP_AHEAD_MS = 840_000


ADMITTED_PAST_DEADLINE = frozenset(
    {"/rayito.v1.HealthService/Health", "/rayito.v1.LifecycleService/SetTimeout"}
)
GATED_PHASES = frozenset(
    {lifecycle_pb2.LIFECYCLE_PHASE_EXPIRED, lifecycle_pb2.LIFECYCLE_PHASE_RESUME_GRACE}
)


class LifecycleHolder(Protocol):
    """El `FakeRayd` de `conftest`: guarda el `LifecycleState` de `Health`."""

    lifecycle: Any
    lock: threading.Lock


class GatedHolder(LifecycleHolder, Protocol):
    timeout_gate: bool


if TYPE_CHECKING:
    ServerInterceptor = grpc.ServerInterceptor[Any, Any]
else:
    ServerInterceptor = grpc.ServerInterceptor


class TimeoutGate(ServerInterceptor):
    """La puerta del plazo de `rayd` (`admits`), opcional: con
    `holder.timeout_gate` y el `LifecycleState` en `EXPIRED`/`RESUME_GRACE`,
    todo RPC salvo `Health` y `SetTimeout` recibe `FAILED_PRECONDITION
    sandbox_timeout` antes de llegar al servicio."""

    def __init__(self, holder: GatedHolder) -> None:
        self._holder = holder

    def refuses(self, method: str) -> bool:
        with self._holder.lock:
            current = self._holder.lifecycle
            gated = self._holder.timeout_gate
        return (
            gated
            and current is not None
            and current.phase in GATED_PHASES
            and method not in ADMITTED_PAST_DEADLINE
        )

    def intercept_service(self, continuation: Any, handler_call_details: Any) -> Any:
        handler = continuation(handler_call_details)
        if handler is None or not self.refuses(handler_call_details.method):
            return handler

        def refuse(request: Any, context: grpc.ServicerContext) -> Any:
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, SANDBOX_TIMEOUT_DETAIL)

        codecs = {
            "request_deserializer": handler.request_deserializer,
            "response_serializer": handler.response_serializer,
        }
        if handler.unary_stream is not None:
            return grpc.unary_stream_rpc_method_handler(refuse, **codecs)
        if handler.stream_unary is not None:
            return grpc.stream_unary_rpc_method_handler(refuse, **codecs)
        if handler.stream_stream is not None:
            return grpc.stream_stream_rpc_method_handler(refuse, **codecs)
        return grpc.unary_unary_rpc_method_handler(refuse, **codecs)


def now_unix_ms() -> int:
    return int(time.time() * 1000)


def lifecycle_state(
    *,
    phase: lifecycle_pb2.LifecyclePhase = lifecycle_pb2.LIFECYCLE_PHASE_ACTIVE,
    deadline_in_ms: int = 60_000,
    cap_in_ms: int = DEFAULT_CAP_AHEAD_MS,
    timeout_ms: int = 60_000,
    on_timeout: lifecycle_pb2.TimeoutAction = lifecycle_pb2.TIMEOUT_ACTION_KILL,
    auto_resume: bool = False,
    extensions: int = 0,
) -> lifecycle_pb2.LifecycleState:
    """Un `LifecycleState` con instantes relativos a ahora (reloj de pared);
    `UNMANAGED` lleva ceros como el de `rayd`."""
    if phase == lifecycle_pb2.LIFECYCLE_PHASE_UNMANAGED:
        return lifecycle_pb2.LifecycleState(phase=phase)
    now = now_unix_ms()
    return lifecycle_pb2.LifecycleState(
        phase=phase,
        deadline_unix_ms=now + deadline_in_ms,
        cap_unix_ms=now + cap_in_ms,
        timeout_ms=timeout_ms,
        on_timeout=on_timeout,
        auto_resume=auto_resume,
        extensions=extensions,
    )


@dataclass
class FakeLifecycleService(lifecycle_pb2_grpc.LifecycleServiceServicer):
    token_sha256: str
    holder: LifecycleHolder
    requests: list[lifecycle_pb2.SetTimeoutRequest] = field(default_factory=list)
    request_metadata: list[dict[str, str]] = field(default_factory=list)
    abort_with: tuple[grpc.StatusCode, str] | None = None

    def SetTimeout(
        self, request: lifecycle_pb2.SetTimeoutRequest, context: grpc.ServicerContext
    ) -> lifecycle_pb2.LifecycleState:
        metadata = metadata_dict(context.invocation_metadata())
        if presented_token_sha256(metadata) != self.token_sha256:
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "x-access-token ausente o inválido")
        with self.holder.lock:
            self.requests.append(request)
            self.request_metadata.append(metadata)
            current = self.holder.lifecycle
        if self.abort_with is not None:
            context.abort(*self.abort_with)
        if current is None:
            context.abort(grpc.StatusCode.UNIMPLEMENTED, "Method not found")
        if current.phase == lifecycle_pb2.LIFECYCLE_PHASE_UNMANAGED:
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, "lifecycle_unmanaged")
        target = now_unix_ms() + int(request.timeout_ms)
        if target > current.cap_unix_ms:
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                f"timeout beyond cap; cap_unix_ms={current.cap_unix_ms}",
            )
        updated = applied(current, request, target)
        with self.holder.lock:
            self.holder.lifecycle = updated
        return updated


def applied(
    current: lifecycle_pb2.LifecycleState,
    request: lifecycle_pb2.SetTimeoutRequest,
    target: int,
) -> lifecycle_pb2.LifecycleState:
    """EXACT fija el plazo; AT_LEAST nunca lo acorta mientras está `ACTIVE`."""
    deadline = target
    if (
        request.mode == lifecycle_pb2.TIMEOUT_MODE_AT_LEAST
        and current.phase == lifecycle_pb2.LIFECYCLE_PHASE_ACTIVE
    ):
        deadline = max(int(current.deadline_unix_ms), target)
    moved = deadline != current.deadline_unix_ms
    updated = lifecycle_pb2.LifecycleState()
    updated.CopyFrom(current)
    updated.phase = lifecycle_pb2.LIFECYCLE_PHASE_ACTIVE
    updated.deadline_unix_ms = deadline
    if moved:
        updated.timeout_ms = int(request.timeout_ms)
        updated.extensions = int(current.extensions) + 1
    return updated
