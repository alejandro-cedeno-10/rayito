"""M1 "hello rayd": `Health` a través del proxy (h2c + trailers, Q17), auth de
dos niveles (`x-aws-proxy-auth` del proxy y `x-access-token` del agente) y
`terminate`. Cuesta ~$0.03 por ejecución.

Desde M2 `rayd` sirve `Health` y `ProcessService`, desde M3
`FilesystemService`, desde M4 `CodeService` y desde M5 `PtyService`; un RPC
autenticado sobre un pid desconocido responde `NOT_FOUND` una vez pasada la
capa de token, y `UNAUTHENTICATED` antes de pasarla."""

from __future__ import annotations

import time

import grpc
import pytest

from rayito import Sandbox
from rayito._aws import LambdaMicrovmsControlPlane, PortSpec
from rayito._limits import DEFAULT_PORT, TERMINAL_STATES
from rayito._models import SandboxInfo
from rayito._transport import (
    PROXY_AUTH_KEY,
    ProxyAuthPlugin,
    ProxyToken,
    TokenStore,
    TransportSettings,
    is_proxy_forbidden,
)
from rayito.v1 import (
    health_pb2,
    health_pb2_grpc,
    process_pb2,
    process_pb2_grpc,
    pty_pb2,
    pty_pb2_grpc,
)

RPC_TIMEOUT_SECONDS = 30
UNKNOWN_PID = 1
TERMINATE_VISIBLE_TIMEOUT_SECONDS = 30.0
TERMINATE_POLL_INTERVAL_SECONDS = 0.5


def store_with(jwe: str) -> TokenStore:
    store = TokenStore()
    store.put(ProxyToken(jwe=jwe, ports=(PortSpec.single(DEFAULT_PORT),), minted_at=time.time()))
    return store


def list_processes_status(channel: grpc.Channel) -> grpc.StatusCode:
    with pytest.raises(grpc.RpcError) as error:
        process_pb2_grpc.ProcessServiceStub(channel).List(
            process_pb2.ListRequest(), timeout=RPC_TIMEOUT_SECONDS
        )
    return error.value.code()


def resize_pty_status(channel: grpc.Channel) -> grpc.StatusCode:
    """`Resize` sobre un pid que no existe con un tamaño válido: desde M5 el
    agente responde `NOT_FOUND` (antes, `UNIMPLEMENTED`)."""
    with pytest.raises(grpc.RpcError) as error:
        pty_pb2_grpc.PtyServiceStub(channel).Resize(
            pty_pb2.ResizeRequest(pid=UNKNOWN_PID, size=pty_pb2.PtySize(cols=80, rows=24)),
            timeout=RPC_TIMEOUT_SECONDS,
        )
    return error.value.code()


@pytest.mark.e2e
def test_hello_rayd(sandbox: Sandbox, control_plane: LambdaMicrovmsControlPlane) -> None:
    assert sandbox.is_running() is True
    info = sandbox.get_info()
    assert info.state in {"PENDING", "RUNNING"}
    assert info.endpoint == sandbox.endpoint
    assert info.remaining_seconds() <= 900

    jwe = sandbox.get_host(DEFAULT_PORT).headers[PROXY_AUTH_KEY]
    anonymous = ProxyAuthPlugin(store_with(jwe), port=DEFAULT_PORT, access_token=None)
    with TransportSettings().open_channel(sandbox.endpoint, anonymous) as channel:
        health = health_pb2_grpc.HealthServiceStub(channel).Health(
            health_pb2.HealthRequest(), timeout=RPC_TIMEOUT_SECONDS
        )
        assert health.agent_ready
        assert health.sandbox_id == sandbox.sandbox_id
        assert list_processes_status(channel) is grpc.StatusCode.UNAUTHENTICATED

    authenticated = ProxyAuthPlugin(
        store_with(jwe), port=DEFAULT_PORT, access_token=sandbox.access_token
    )
    with TransportSettings().open_channel(sandbox.endpoint, authenticated) as channel:
        listed = process_pb2_grpc.ProcessServiceStub(channel).List(
            process_pb2.ListRequest(), timeout=RPC_TIMEOUT_SECONDS
        )
        assert list(listed.processes) == []
        assert resize_pty_status(channel) is grpc.StatusCode.NOT_FOUND

    bogus = ProxyAuthPlugin(store_with("not-a-jwe"), port=DEFAULT_PORT, access_token=None)
    with TransportSettings().open_channel(sandbox.endpoint, bogus) as channel:
        stub = health_pb2_grpc.HealthServiceStub(channel)
        with pytest.raises(grpc.RpcError) as forbidden:
            stub.Health(health_pb2.HealthRequest(), timeout=RPC_TIMEOUT_SECONDS)
    assert forbidden.value.code() is grpc.StatusCode.PERMISSION_DENIED
    assert is_proxy_forbidden(forbidden.value)

    reconnected = Sandbox.connect(
        sandbox.sandbox_id, access_token=sandbox.access_token, control_plane=control_plane
    )
    try:
        assert reconnected.is_running() is True
    finally:
        reconnected.close()

    assert sandbox.kill() is True
    final, seconds = wait_for_terminal_state(sandbox.sandbox_id, control_plane)
    print(f"\n{sandbox.sandbox_id}: terminate-microvm -> {final.state} a los {seconds:.2f} s")
    assert final.state in TERMINAL_STATES


def wait_for_terminal_state(
    sandbox_id: str, control_plane: LambdaMicrovmsControlPlane
) -> tuple[SandboxInfo, float]:
    """`get-microvm` es eventualmente consistente (AWS_API_NOTES.md §6): justo
    después de `terminate-microvm` puede seguir respondiendo `RUNNING`."""
    started = time.perf_counter()
    while True:
        info = Sandbox.get_info(sandbox_id, read_metadata=False, control_plane=control_plane)
        elapsed = time.perf_counter() - started
        if info.state in TERMINAL_STATES or elapsed >= TERMINATE_VISIBLE_TIMEOUT_SECONDS:
            return info, elapsed
        time.sleep(TERMINATE_POLL_INTERVAL_SECONDS)
