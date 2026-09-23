"""Metadata del proxy, refresco a los 45 min y readiness contra un `rayd` falso."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Sequence
from typing import cast

import grpc
import grpc.aio
import pytest

from rayito._aws import ControlPlane, PortSpec, sandbox_info_from_response
from rayito._limits import TOKEN_REFRESH_AFTER_MINUTES
from rayito._transport import (
    ACCESS_TOKEN_KEY,
    CHANNEL_OPTIONS,
    KERNEL_GATE_PREFIX,
    PROXY_AUTH_KEY,
    PROXY_FORBIDDEN_MARKER,
    PROXY_FORCE_H2_KEY,
    PROXY_PORT_KEY,
    TOKEN_REFRESH_AFTER_SECONDS,
    AsyncTokenRefresher,
    ProxyAuthPlugin,
    ProxyToken,
    TokenRefresher,
    TokenStore,
    TransportSettings,
    is_kernel_gate,
    is_phase_gate,
    is_reconnectable,
    is_stream_reset,
    translate_rpc_error,
)
from rayito.exceptions import (
    AuthenticationException,
    SandboxException,
    SandboxStateException,
)
from rayito.sandbox_sync.main import probe_health
from rayito.v1 import health_pb2, health_pb2_grpc

from .conftest import ACCESS_TOKEN, FakeClock, FakeRpcError, RaydEndpoint, microvm_response
from .log_capture import capture_logs


def minted_store(jwe: str = "jwe-0") -> TokenStore:
    store = TokenStore()
    store.put(ProxyToken(jwe=jwe, ports=(PortSpec.single(8080),), minted_at=0.0))
    return store


def test_kernel_gate_is_unavailable_with_the_prefix_and_never_a_reset() -> None:
    gate = FakeRpcError(
        grpc.StatusCode.UNAVAILABLE, details=f"{KERNEL_GATE_PREFIX}: sidecar relaunching"
    )
    assert is_kernel_gate(gate)
    assert not is_phase_gate(gate)
    assert not is_stream_reset(gate)
    mapped = translate_rpc_error(gate)
    assert type(mapped) is SandboxException
    assert not isinstance(mapped, SandboxStateException)
    assert "kernel no está listo" in str(mapped)
    assert "sidecar relaunching" in str(mapped)
    assert mapped.grpc_code is grpc.StatusCode.UNAVAILABLE
    assert not is_kernel_gate(FakeRpcError(grpc.StatusCode.UNAVAILABLE, details="Socket closed"))
    assert not is_kernel_gate(FakeRpcError(grpc.StatusCode.NOT_FOUND, details="kernel not ready"))
    assert is_stream_reset(FakeRpcError(grpc.StatusCode.UNAVAILABLE, details="Socket closed"))


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (FakeRpcError(grpc.StatusCode.UNAVAILABLE, details="Socket closed"), True),
        (FakeRpcError(grpc.StatusCode.UNAVAILABLE, details="GOAWAY received"), True),
        (FakeRpcError(grpc.StatusCode.UNAVAILABLE, details="suspending"), True),
        (FakeRpcError(grpc.StatusCode.UNAVAILABLE, details="terminating"), True),
        (FakeRpcError(grpc.StatusCode.INTERNAL, details="RST_STREAM received"), True),
        (FakeRpcError(grpc.StatusCode.INTERNAL, details="x", debug="Connection reset"), True),
        (FakeRpcError(grpc.StatusCode.UNAVAILABLE, details=f"{KERNEL_GATE_PREFIX}: x"), False),
        (FakeRpcError(grpc.StatusCode.DEADLINE_EXCEEDED, details="Deadline Exceeded"), False),
        (FakeRpcError(grpc.StatusCode.NOT_FOUND, details="pid 1 not found"), False),
        (FakeRpcError(grpc.StatusCode.INTERNAL, details="panic in rayd"), False),
        (FakeRpcError(grpc.StatusCode.CANCELLED, details="Locally cancelled"), False),
        (
            FakeRpcError(
                grpc.StatusCode.PERMISSION_DENIED,
                details="Received http2 header with status: 403",
                debug=PROXY_FORBIDDEN_MARKER,
            ),
            False,
        ),
    ],
)
def test_is_reconnectable_truth_table(error: FakeRpcError, expected: bool) -> None:
    assert is_reconnectable(error) is expected


def test_channel_options_cap_the_reconnect_backoff() -> None:
    options = dict(CHANNEL_OPTIONS)
    assert options["grpc.initial_reconnect_backoff_ms"] == 500
    assert options["grpc.min_reconnect_backoff_ms"] == 500
    assert options["grpc.max_reconnect_backoff_ms"] == 2000
    assert options["grpc.keepalive_time_ms"] == 30_000


def test_plugin_sends_the_four_lowercase_headers(fake_rayd: RaydEndpoint) -> None:
    plugin = ProxyAuthPlugin(minted_store(), port=8080, access_token=ACCESS_TOKEN)
    with fake_rayd.transport.open_channel(fake_rayd.host, plugin) as channel:
        stub = health_pb2_grpc.HealthServiceStub(channel)
        assert stub.Health(health_pb2.HealthRequest(), timeout=5).agent_ready is True
        assert stub.Metrics(health_pb2.MetricsRequest(), timeout=5).cpu_count == 1
    seen = fake_rayd.servicer.health_calls[0]
    assert seen[PROXY_AUTH_KEY] == "jwe-0"
    assert seen[PROXY_PORT_KEY] == "8080"
    assert seen[PROXY_FORCE_H2_KEY] == "true"
    assert seen[ACCESS_TOKEN_KEY] == ACCESS_TOKEN


def test_anonymous_plugin_omits_access_token_and_rayd_rejects_metrics(
    fake_rayd: RaydEndpoint,
) -> None:
    plugin = ProxyAuthPlugin(minted_store(), port=8080, access_token=None)
    with fake_rayd.transport.open_channel(fake_rayd.host, plugin) as channel:
        stub = health_pb2_grpc.HealthServiceStub(channel)
        assert stub.Health(health_pb2.HealthRequest(), timeout=5).agent_ready is True
        with pytest.raises(grpc.RpcError) as excinfo:
            stub.Metrics(health_pb2.MetricsRequest(), timeout=5)
    assert excinfo.value.code() is grpc.StatusCode.UNAUTHENTICATED
    assert ACCESS_TOKEN_KEY not in fake_rayd.servicer.health_calls[0]


def plugin_metadata(plugin: ProxyAuthPlugin) -> tuple[tuple[str, str], ...]:
    captured: list[tuple[tuple[str, str], ...]] = []

    def callback(metadata: tuple[tuple[str, str], ...], error: Exception | None) -> None:
        assert error is None
        captured.append(metadata)

    plugin(
        cast("grpc.AuthMetadataContext", None), cast("grpc.AuthMetadataPluginCallback", callback)
    )
    return captured[0]


@pytest.mark.parametrize("access_token", [ACCESS_TOKEN, None])
def test_extra_metadata_goes_after_the_reserved_keys(access_token: str | None) -> None:
    plugin = ProxyAuthPlugin(
        minted_store(), port=8080, access_token=access_token, extra=(("x-trace", "1"),)
    )
    metadata = plugin_metadata(plugin)
    assert metadata[-1] == ("x-trace", "1")
    reserved = [key for key, _ in metadata[:-1]]
    expected = [PROXY_AUTH_KEY, PROXY_PORT_KEY, PROXY_FORCE_H2_KEY]
    assert reserved == (expected if access_token is None else [*expected, ACCESS_TOKEN_KEY])


def test_extra_metadata_reaches_rayd_on_the_anonymous_health_probe(
    fake_rayd: RaydEndpoint,
) -> None:
    transport = dataclasses.replace(fake_rayd.transport, extra_metadata=(("x-trace", "7"),))

    class MintingPlane:
        def create_auth_token(self, sandbox_id: str, ports: Sequence[PortSpec]) -> str:
            return "jwe-probe"

    info = sandbox_info_from_response(microvm_response(state="RUNNING", endpoint=fake_rayd.host))
    response = probe_health(cast("ControlPlane", MintingPlane()), info, transport, 5.0)
    assert response is not None and response.agent_ready
    seen = fake_rayd.servicer.health_calls[-1]
    assert seen["x-trace"] == "7"
    assert ACCESS_TOKEN_KEY not in seen


def test_http_proxy_becomes_a_channel_option(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[list[tuple[str, object]]] = []

    def capture(target: str, credentials: object, options: list[tuple[str, object]]) -> object:
        opened.append(options)
        return object()

    monkeypatch.setattr(grpc, "secure_channel", capture)
    monkeypatch.setattr(grpc.aio, "secure_channel", capture)
    plugin = ProxyAuthPlugin(minted_store(), port=8080, access_token=None)
    proxied = TransportSettings(http_proxy="http://127.0.0.1:3128")
    proxied.open_channel("host", plugin)
    proxied.open_aio_channel("host", plugin)
    TransportSettings().open_channel("host", plugin)
    assert opened[0][-1] == ("grpc.http_proxy", "http://127.0.0.1:3128")
    assert opened[1][-1] == ("grpc.http_proxy", "http://127.0.0.1:3128")
    assert all(key != "grpc.http_proxy" for key, _ in opened[2])
    assert opened[0][:-1] == list(CHANNEL_OPTIONS)


def test_plugin_without_token_fails_the_call_locally(fake_rayd: RaydEndpoint) -> None:
    plugin = ProxyAuthPlugin(TokenStore(), port=8080, access_token=ACCESS_TOKEN)
    with fake_rayd.transport.open_channel(fake_rayd.host, plugin) as channel:
        stub = health_pb2_grpc.HealthServiceStub(channel)
        with pytest.raises(grpc.RpcError) as excinfo:
            stub.Health(health_pb2.HealthRequest(), timeout=5)
    assert excinfo.value.code() is grpc.StatusCode.UNAVAILABLE
    assert fake_rayd.servicer.health_calls == []


def test_refresher_rotates_at_45_minutes_without_rebuilding_the_channel(
    fake_rayd: RaydEndpoint,
) -> None:
    clock = FakeClock(start=0.0)
    minted: list[Sequence[PortSpec]] = []

    def mint(ports: Sequence[PortSpec]) -> str:
        minted.append(tuple(ports))
        return f"jwe-{len(minted)}"

    refresher = TokenRefresher(TokenStore(), mint, clock=clock)
    refresher.mint((PortSpec.single(8080),))
    plugin = ProxyAuthPlugin(refresher.store, port=8080, access_token=ACCESS_TOKEN)
    with fake_rayd.transport.open_channel(fake_rayd.host, plugin) as channel:
        stub = health_pb2_grpc.HealthServiceStub(channel)
        stub.Health(health_pb2.HealthRequest(), timeout=5)
        clock.advance(TOKEN_REFRESH_AFTER_MINUTES * 60 - 1)
        assert refresher.refresh_due() is True
        assert len(minted) == 1
        assert refresher.seconds_until_next_refresh() == pytest.approx(1.0)
        clock.advance(1)
        assert refresher.refresh_due() is True
        assert len(minted) == 2
        stub.Health(health_pb2.HealthRequest(), timeout=5)
    jwes = [call[PROXY_AUTH_KEY] for call in fake_rayd.servicer.health_calls]
    assert jwes == ["jwe-1", "jwe-2"]


def test_refresher_failure_goes_to_the_sandbox_logger() -> None:
    clock = FakeClock(start=0.0)

    def failing_mint(ports: Sequence[PortSpec]) -> str:
        raise RuntimeError("aws caído")

    custom = logging.getLogger("tests.custom.refresher")
    refresher = TokenRefresher(minted_store(), failing_mint, clock=clock, logger=custom)
    clock.advance(TOKEN_REFRESH_AFTER_SECONDS)
    with (
        capture_logs("tests.custom.refresher") as mine,
        capture_logs("rayito.transport") as default,
    ):
        assert refresher.refresh_due() is False
        refresher.route_logs_to(logging.getLogger("tests.custom.refresher.other"))
        assert refresher.refresh_due() is False
    assert len(mine.records) == 2
    assert default.records == []


def test_refresher_reports_failure_and_keeps_the_old_token() -> None:
    clock = FakeClock(start=0.0)
    attempts = {"n": 0}

    def mint(ports: Sequence[PortSpec]) -> str:
        attempts["n"] += 1
        if attempts["n"] > 1:
            raise RuntimeError("aws down")
        return "jwe-1"

    refresher = TokenRefresher(TokenStore(), mint, clock=clock)
    refresher.mint((PortSpec.single(8080),))
    clock.advance(TOKEN_REFRESH_AFTER_MINUTES * 60)
    assert refresher.refresh_due() is False
    assert refresher.store.jwe_for(8080) == "jwe-1"


def test_ensure_reuses_covering_token_or_mints_single_port() -> None:
    minted: list[tuple[PortSpec, ...]] = []

    def mint(ports: Sequence[PortSpec]) -> str:
        minted.append(tuple(ports))
        return f"jwe-{len(minted)}"

    refresher = TokenRefresher(TokenStore(), mint, clock=lambda: 0.0)
    refresher.mint((PortSpec.single(8080), PortSpec.range(8000, 8999)))
    assert refresher.ensure(8500).jwe == "jwe-1"
    assert refresher.ensure(3000).jwe == "jwe-2"
    assert minted[1] == (PortSpec.single(3000),)
    assert refresher.store.jwe_for(3000) == "jwe-2"
    assert refresher.store.jwe_for(9000) is None


def test_store_replaces_token_with_same_ports() -> None:
    store = TokenStore()
    store.put(ProxyToken("a", (PortSpec.single(8080),), 0.0))
    store.put(ProxyToken("b", (PortSpec.single(8080),), 1.0))
    assert [token.jwe for token in store.tokens()] == ["b"]


def test_refresher_thread_starts_and_stops_cleanly() -> None:
    refresher = TokenRefresher(TokenStore(), lambda ports: "jwe", clock=lambda: 0.0)
    refresher.start()
    refresher.start()
    refresher.stop()
    refresher.stop()


def test_authentication_exception_for_missing_token_in_callback() -> None:
    errors: list[BaseException | None] = []
    plugin = ProxyAuthPlugin(TokenStore(), port=8080, access_token=None)
    plugin(None, lambda metadata, error: errors.append(error))  # type: ignore[arg-type]
    assert isinstance(errors[0], AuthenticationException)


async def test_async_refresher_refreshes_on_schedule_and_is_idempotent() -> None:
    clock = FakeClock(start=0.0)
    minted: list[tuple[PortSpec, ...]] = []

    def mint(ports: Sequence[PortSpec]) -> str:
        minted.append(tuple(ports))
        return f"jwe-{len(minted)}"

    inner = TokenRefresher(TokenStore(), mint, clock=clock)
    inner.mint((PortSpec.single(8080),))
    sleeps: list[float] = []
    second_sleep = asyncio.Event()

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.advance(seconds)
        if len(sleeps) == 2:
            second_sleep.set()
            await asyncio.Event().wait()

    refresher = AsyncTokenRefresher(inner, sleep=fake_sleep)
    refresher.start()
    task = refresher._task
    refresher.start()
    assert refresher._task is task

    await asyncio.wait_for(second_sleep.wait(), timeout=5)
    assert sleeps == pytest.approx([TOKEN_REFRESH_AFTER_SECONDS, TOKEN_REFRESH_AFTER_SECONDS])
    assert inner.store.jwe_for(8080) == "jwe-2"

    await refresher.stop()
    await refresher.stop()
    assert refresher._task is None
    assert task is not None and task.cancelled()
