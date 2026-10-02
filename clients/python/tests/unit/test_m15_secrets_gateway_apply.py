"""`Sandbox._apply_configure_sections`/`_reapply_section` (sync and async)
and `SandboxPool.take(gateways=)`: any failure while configuring (a pre-0.6
agent, a missing flag, a bad `SectionResult`) must close the client and
terminate the MicroVM (unless `keep_on_failure`); a bad `SectionResult` on
`refresh()` must raise instead of returning an empty or partial status map;
`refresh()` must push the *current* secret value, never the cached one
(code review findings on PR #78, m15-secrets-gateway)."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any, cast

import pytest

import rayito.sandbox_async.main as async_main
import rayito.sandbox_sync.main as sync_main
from rayito import AsyncSandbox, Sandbox
from rayito._configure_base import AgentFeatures
from rayito._secret_gateway._domain import SecretGateway
from rayito._secret_gateway._section import GatewaySection, GatewaySectionFactory
from rayito._secrets import SecretCache, SecretStore
from rayito.exceptions import SandboxException, UnimplementedError
from rayito.v1 import configure_pb2, secret_gateway_pb2

from .fake_secrets import FakeSecretsManager, SpySession


def cache_over(api: FakeSecretsManager) -> SecretCache:
    return SecretCache(store=SecretStore(session=cast(Any, SpySession(api=api))))


def a_gateway() -> SecretGateway:
    return SecretGateway(
        upstream="https://example.com",
        headers={"x-api-key": "anthropic"},
        allow=[("GET", "/x")],
    )


def failed_response(error_class: str = "listen_failed") -> configure_pb2.ConfigureResponse:
    response = configure_pb2.ConfigureResponse()
    response.results.add(
        section=configure_pb2.ConfigSection.CONFIG_SECTION_SECRET_GATEWAY,
        code=configure_pb2.SectionCode.SECTION_CODE_FAILED,
        error_class=error_class,
    )
    return response


def fake_call_configure_failed(stub: object, request: object, *, timeout: float) -> Any:
    return failed_response()


@pytest.fixture
def api() -> FakeSecretsManager:
    fake = FakeSecretsManager()
    fake.put("rayito/anthropic", "sk-whatever")
    return fake


def bare_sync_sandbox(cache: SecretCache) -> Sandbox:
    sandbox = Sandbox.__new__(Sandbox)
    sandbox._agent_features = AgentFeatures(configure=True, secret_gateway=True)
    sandbox._secrets = cast(Any, SimpleNamespace(cache=cache))
    sandbox._control_plane = cast(Any, object())
    sandbox._configure = object()
    sandbox._logger = logging.getLogger("test.m15.secrets_gateway")
    sandbox._info = cast(Any, SimpleNamespace(sandbox_id="mvm-test-secrets-gateway"))
    sandbox._section_handles = {}
    return sandbox


def bare_async_sandbox(cache: SecretCache) -> AsyncSandbox:
    sandbox = AsyncSandbox.__new__(AsyncSandbox)
    sandbox._agent_features = AgentFeatures(configure=True, secret_gateway=True)
    sandbox._secrets = cast(Any, SimpleNamespace(cache=cache))
    sandbox._control_plane = cast(Any, object())
    sandbox._configure = object()
    sandbox._logger = logging.getLogger("test.m15.secrets_gateway")
    sandbox._info = cast(Any, SimpleNamespace(sandbox_id="mvm-test-secrets-gateway"))
    sandbox._section_handles = {}
    return sandbox


# --------------------------------------------------------------- sync: apply


def test_sync_apply_terminates_the_vm_on_a_failed_section(
    api: FakeSecretsManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = bare_sync_sandbox(cache_over(api))
    close_calls: list[None] = []
    sandbox.close = lambda: close_calls.append(None)  # type: ignore[method-assign]
    terminate_calls: list[str] = []
    monkeypatch.setattr(
        sync_main,
        "terminate_quietly",
        lambda control_plane, sandbox_id, logger: terminate_calls.append(sandbox_id),
    )
    monkeypatch.setattr(sync_main, "call_configure", fake_call_configure_failed)

    factories = (GatewaySectionFactory({"a": a_gateway()}),)
    with pytest.raises(SandboxException, match="listen_failed"):
        sandbox._apply_configure_sections(factories, timeout=5.0, terminate_on_failure=True)

    assert close_calls == [None]
    assert terminate_calls == ["mvm-test-secrets-gateway"]


def test_sync_apply_closes_but_does_not_terminate_with_keep_on_failure(
    api: FakeSecretsManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = bare_sync_sandbox(cache_over(api))
    close_calls: list[None] = []
    sandbox.close = lambda: close_calls.append(None)  # type: ignore[method-assign]
    terminate_calls: list[str] = []
    monkeypatch.setattr(
        sync_main,
        "terminate_quietly",
        lambda control_plane, sandbox_id, logger: terminate_calls.append(sandbox_id),
    )
    monkeypatch.setattr(sync_main, "call_configure", fake_call_configure_failed)

    factories = (GatewaySectionFactory({"a": a_gateway()}),)
    with pytest.raises(SandboxException):
        sandbox._apply_configure_sections(factories, timeout=5.0, terminate_on_failure=False)

    assert close_calls == [None]
    assert terminate_calls == []


def test_sync_apply_with_no_pending_sections_never_touches_the_vm(
    api: FakeSecretsManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = bare_sync_sandbox(cache_over(api))
    calls: list[str] = []
    sandbox.close = lambda: calls.append("close")  # type: ignore[method-assign]

    def fail_if_called(*_args: object, **_kwargs: object) -> configure_pb2.ConfigureResponse:
        calls.append("configure")
        return failed_response()

    monkeypatch.setattr(sync_main, "call_configure", fail_if_called)
    sandbox._apply_configure_sections((), timeout=5.0, terminate_on_failure=True)
    assert calls == []


@pytest.mark.parametrize(
    ("features", "expected"),
    [
        # A pre-0.6 agent: `Health.features` absent entirely.
        (None, UnimplementedError),
        # A 0.6 agent whose image does not implement the gateway.
        (AgentFeatures(configure=True, secret_gateway=False), UnimplementedError),
    ],
)
def test_sync_apply_terminates_the_vm_when_the_agent_cannot_configure(
    api: FakeSecretsManager,
    monkeypatch: pytest.MonkeyPatch,
    features: AgentFeatures | None,
    expected: type[Exception],
) -> None:
    sandbox = bare_sync_sandbox(cache_over(api))
    sandbox._agent_features = features
    sandbox.close = lambda: None  # type: ignore[method-assign]
    terminate_calls: list[str] = []
    monkeypatch.setattr(
        sync_main,
        "terminate_quietly",
        lambda control_plane, sandbox_id, logger: terminate_calls.append(sandbox_id),
    )

    def never_called(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("Configure must not be sent to an agent that cannot apply it")

    monkeypatch.setattr(sync_main, "call_configure", never_called)

    factories = (GatewaySectionFactory({"a": a_gateway()}),)
    with pytest.raises(expected):
        sandbox._apply_configure_sections(factories, timeout=5.0, terminate_on_failure=True)
    assert terminate_calls == ["mvm-test-secrets-gateway"]


# ------------------------------------------------- sync: after_apply + refresh


def applied_response() -> configure_pb2.ConfigureResponse:
    response = configure_pb2.ConfigureResponse()
    response.results.add(
        section=configure_pb2.ConfigSection.CONFIG_SECTION_SECRET_GATEWAY,
        code=configure_pb2.SectionCode.SECTION_CODE_APPLIED,
    )
    return response


def status_with_route(name: str, port: int) -> configure_pb2.ConfigureStatusResponse:
    return configure_pb2.ConfigureStatusResponse(
        secret_gateway=secret_gateway_pb2.SecretGatewayStatus(
            routes=[secret_gateway_pb2.SecretGatewayRouteStatus(name=name, port=port)]
        )
    )


def test_sync_apply_publishes_the_section_handle_through_after_apply(
    api: FakeSecretsManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = bare_sync_sandbox(cache_over(api))
    monkeypatch.setattr(sync_main, "call_configure", lambda *_a, **_k: applied_response())
    monkeypatch.setattr(
        sync_main, "call_configure_status", lambda *_a, **_k: status_with_route("a", 41_000)
    )
    factories = (GatewaySectionFactory({"a": a_gateway()}),)
    sandbox._apply_configure_sections(factories, timeout=5.0, terminate_on_failure=True)
    assert sandbox.gateways["a"].port == 41_000


def test_sync_refresh_pushes_the_rotated_value_not_the_cached_one(
    api: FakeSecretsManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = bare_sync_sandbox(cache_over(api))
    sent: list[str] = []

    def record_configure(stub: object, request: configure_pb2.ConfigureRequest, **_: object) -> Any:
        sent.append(request.secret_gateway.routes[0].headers["x-api-key"])
        return applied_response()

    monkeypatch.setattr(sync_main, "call_configure", record_configure)
    monkeypatch.setattr(
        sync_main, "call_configure_status", lambda *_a, **_k: status_with_route("a", 41_000)
    )
    factories = (GatewaySectionFactory({"a": a_gateway()}),)
    sandbox._apply_configure_sections(factories, timeout=5.0, terminate_on_failure=True)

    # Rotated in Secrets Manager well inside `SecretCache`'s TTL.
    api.put("rayito/anthropic", "sk-rotated", version_id="v2")
    sandbox.gateways.refresh()

    assert sent == ["sk-whatever", "sk-rotated"]


def test_sync_refresh_raises_on_a_failed_result_instead_of_an_empty_map(
    api: FakeSecretsManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = bare_sync_sandbox(cache_over(api))
    section = GatewaySection(gateways={"a": a_gateway()}, cache=cache_over(api))
    status_calls: list[None] = []
    monkeypatch.setattr(sync_main, "call_configure", fake_call_configure_failed)
    monkeypatch.setattr(
        sync_main,
        "call_configure_status",
        lambda stub, *, timeout: status_calls.append(None),
    )
    with pytest.raises(SandboxException, match="listen_failed"):
        sandbox._reapply_section(section, timeout=5.0)
    # `ConfigureStatus` must never be asked for after a rejected refresh:
    # there is no new, valid state to report.
    assert status_calls == []


# -------------------------------------------------------------- async: apply


@pytest.mark.asyncio
async def test_async_apply_terminates_the_vm_on_a_failed_section(
    api: FakeSecretsManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = bare_async_sandbox(cache_over(api))

    async def fake_close() -> None:
        close_calls.append(None)

    close_calls: list[None] = []
    sandbox.close = fake_close  # type: ignore[method-assign]
    terminate_calls: list[str] = []

    async def fake_call_configure(stub: object, request: object, *, timeout: float) -> Any:
        return failed_response()

    async def fake_to_thread(fn: Any, *args: Any, **kwargs: Any) -> Any:
        return fn(*args, **kwargs)

    monkeypatch.setattr(
        async_main,
        "terminate_quietly",
        lambda control_plane, sandbox_id, logger: terminate_calls.append(sandbox_id),
    )
    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(async_main, "call_configure", fake_call_configure)

    factories = (GatewaySectionFactory({"a": a_gateway()}),)
    with pytest.raises(SandboxException, match="listen_failed"):
        await sandbox._apply_configure_sections(factories, timeout=5.0, terminate_on_failure=True)

    assert close_calls == [None]
    assert terminate_calls == ["mvm-test-secrets-gateway"]


@pytest.mark.asyncio
async def test_async_apply_terminates_the_vm_on_a_pre_06_agent(
    api: FakeSecretsManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = bare_async_sandbox(cache_over(api))
    sandbox._agent_features = None

    async def fake_close() -> None:
        return None

    sandbox.close = fake_close  # type: ignore[method-assign]
    terminate_calls: list[str] = []

    async def fake_to_thread(fn: Any, *args: Any, **kwargs: Any) -> Any:
        return fn(*args, **kwargs)

    monkeypatch.setattr(
        async_main,
        "terminate_quietly",
        lambda control_plane, sandbox_id, logger: terminate_calls.append(sandbox_id),
    )
    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)

    factories = (GatewaySectionFactory({"a": a_gateway()}),)
    with pytest.raises(UnimplementedError):
        await sandbox._apply_configure_sections(factories, timeout=5.0, terminate_on_failure=True)
    assert terminate_calls == ["mvm-test-secrets-gateway"]


# ------------------------------------------------ async: after_apply + refresh


@pytest.mark.asyncio
async def test_async_refresh_pushes_the_rotated_value_not_the_cached_one(
    api: FakeSecretsManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = bare_async_sandbox(cache_over(api))
    sent: list[str] = []

    async def record_configure(
        stub: object, request: configure_pb2.ConfigureRequest, **_: object
    ) -> Any:
        sent.append(request.secret_gateway.routes[0].headers["x-api-key"])
        return applied_response()

    async def fake_call_configure_status(stub: object, *, timeout: float) -> Any:
        return status_with_route("a", 41_000)

    monkeypatch.setattr(async_main, "call_configure", record_configure)
    monkeypatch.setattr(async_main, "call_configure_status", fake_call_configure_status)
    factories = (GatewaySectionFactory({"a": a_gateway()}),)
    await sandbox._apply_configure_sections(factories, timeout=5.0, terminate_on_failure=True)
    assert sandbox.gateways["a"].port == 41_000

    api.put("rayito/anthropic", "sk-rotated", version_id="v2")
    await sandbox.gateways.arefresh()

    assert sent == ["sk-whatever", "sk-rotated"]


@pytest.mark.asyncio
async def test_async_refresh_raises_on_a_failed_result_instead_of_an_empty_map(
    api: FakeSecretsManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = bare_async_sandbox(cache_over(api))
    section = GatewaySection(gateways={"a": a_gateway()}, cache=cache_over(api))

    async def fake_call_configure(stub: object, request: object, *, timeout: float) -> Any:
        return failed_response()

    status_calls: list[None] = []

    async def fake_call_configure_status(stub: object, *, timeout: float) -> Any:
        status_calls.append(None)
        raise AssertionError("ConfigureStatus must never be reached after a FAILED result")

    monkeypatch.setattr(async_main, "call_configure", fake_call_configure)
    monkeypatch.setattr(async_main, "call_configure_status", fake_call_configure_status)

    with pytest.raises(SandboxException, match="listen_failed"):
        await sandbox._reapply_section(section, timeout=5.0)
    assert status_calls == []
