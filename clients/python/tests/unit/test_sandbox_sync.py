"""`Sandbox` de punta a punta con Stubber (AWS) y el `rayd` falso (gRPC)."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import grpc
import pytest

from rayito import IdlePolicy, Sandbox, _aws
from rayito._aws import LambdaMicrovmsControlPlane
from rayito._limits import DEFAULT_PORT, HOOKS_PORT
from rayito._payload import access_token_sha256
from rayito._transport import (
    ACCESS_TOKEN_KEY,
    PROXY_AUTH_KEY,
    PROXY_FORBIDDEN_MARKER,
    PROXY_PORT_KEY,
)
from rayito.exceptions import (
    AuthenticationException,
    InvalidArgumentException,
    RateLimitException,
    SandboxNotFoundException,
    SandboxNotReadyException,
)
from rayito.sandbox_sync.pty import Pty

from .conftest import (
    ACCESS_TOKEN,
    ACCOUNT_ID,
    IMAGE_ARN,
    IMAGE_NAME,
    JWE,
    REGION,
    SANDBOX_ID,
    FakeRpcError,
    RaydEndpoint,
    StubbedControlPlane,
    auth_token_response,
    list_item,
    microvm_response,
)


def proxy_forbidden() -> FakeRpcError:
    return FakeRpcError(
        grpc.StatusCode.PERMISSION_DENIED,
        details="Received http2 header with status: 403",
        debug=f'{{"grpc_status":7,"description":"{PROXY_FORBIDDEN_MARKER}"}}',
    )


def agent_permission_denied() -> FakeRpcError:
    return FakeRpcError(grpc.StatusCode.PERMISSION_DENIED, details="EACCES", debug="EACCES")


def failing_first(real: Callable[..., Any], failures: list[FakeRpcError]) -> Callable[..., Any]:
    """Sustituto de `stub.Health` que lanza los errores encolados y luego delega."""

    def health(request: Any, timeout: float | None = None) -> Any:
        if failures:
            raise failures.pop(0)
        return real(request, timeout=timeout)

    return health


def stub_remint(control_plane: StubbedControlPlane, jwe: str) -> None:
    control_plane.microvms.add_response(
        "create_microvm_auth_token",
        auth_token_response(jwe),
        expected_params={
            "microvmIdentifier": SANDBOX_ID,
            "expirationInMinutes": 60,
            "allowedPorts": [{"port": DEFAULT_PORT}],
        },
    )


def stub_launch(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, *, jwe: str = JWE
) -> None:
    control_plane.microvms.add_response("run_microvm", microvm_response(endpoint=fake_rayd.host))
    control_plane.microvms.add_response(
        "create_microvm_auth_token",
        auth_token_response(jwe),
        expected_params={
            "microvmIdentifier": SANDBOX_ID,
            "expirationInMinutes": 60,
            "allowedPorts": [{"port": DEFAULT_PORT}],
        },
    )


def test_create_launches_polls_health_and_kills(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.unavailable_calls = 2
    fake_rayd.servicer.not_ready_calls = 1
    stub_launch(control_plane, fake_rayd)
    control_plane.microvms.add_response(
        "terminate_microvm", {}, expected_params={"microvmIdentifier": SANDBOX_ID}
    )

    with Sandbox.create(
        IMAGE_ARN,
        timeout=900,
        idle=None,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
        ready_timeout=20,
    ) as sandbox:
        assert sandbox.sandbox_id == SANDBOX_ID
        assert sandbox.access_token == ACCESS_TOKEN
        assert sandbox.endpoint == fake_rayd.host
        assert sandbox.endpoint_url == f"https://{fake_rayd.host}"
        assert sandbox.is_running() is True
        assert repr(sandbox) == f"Sandbox(sandbox_id={SANDBOX_ID!r}, state='PENDING')"

    assert len(fake_rayd.servicer.health_calls) == 5
    first = fake_rayd.servicer.health_calls[0]
    assert first[PROXY_AUTH_KEY] == JWE
    assert first[PROXY_PORT_KEY] == str(DEFAULT_PORT)
    assert first[ACCESS_TOKEN_KEY] == ACCESS_TOKEN


def test_create_request_shape_with_name_template_and_idle(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    control_plane.sts.add_response(
        "get_caller_identity",
        {"Account": ACCOUNT_ID, "Arn": f"arn:aws:iam::{ACCOUNT_ID}:user/dev", "UserId": "u"},
    )
    captured: dict[str, object] = {}
    control_plane.microvms.add_response("run_microvm", microvm_response(endpoint=fake_rayd.host))
    control_plane.microvms.add_response(
        "create_microvm_auth_token",
        auth_token_response(),
        expected_params={
            "microvmIdentifier": SANDBOX_ID,
            "expirationInMinutes": 60,
            "allowedPorts": [{"port": 8080}, {"port": 3000}],
        },
    )
    control_plane.microvms.add_response("terminate_microvm", {})
    original = control_plane.plane.run_microvm

    def spy(request):  # type: ignore[no-untyped-def]
        captured.update(request.to_api())
        return original(request)

    control_plane.plane.run_microvm = spy  # type: ignore[method-assign]

    sandbox = Sandbox.create(
        IMAGE_NAME,
        template_version="2.0",
        timeout=3600,
        idle=IdlePolicy(max_idle_seconds=120),
        envs={"FOO": "bar"},
        allowed_ports=[3000],
        ingress=["ALL_INGRESS"],
        logging="cloudwatch",
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    )
    sandbox.kill()

    assert captured["imageIdentifier"] == IMAGE_ARN
    assert captured["imageVersion"] == "2.0"
    assert captured["maximumDurationInSeconds"] == 3600
    assert captured["idlePolicy"] == {
        "maxIdleDurationSeconds": 120,
        "suspendedDurationSeconds": 3480,
        "autoResumeEnabled": True,
    }
    assert captured["logging"] == {"cloudWatch": {"logGroup": f"/rayito/{IMAGE_NAME}"}}
    assert captured["ingressNetworkConnectors"] == [
        "arn:aws:lambda:us-east-1:aws:network-connector:aws-network-connector:ALL_INGRESS"
    ]
    assert "executionRoleArn" not in captured
    assert len(str(captured["clientToken"])) == 32
    payload = json.loads(str(captured["runHookPayload"]))
    assert payload["token_sha256"] == access_token_sha256(ACCESS_TOKEN)
    assert payload["envs"] == {"FOO": "bar"}
    assert ACCESS_TOKEN not in str(captured["runHookPayload"])


def test_ready_timeout_terminates_and_reports_state_reason(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.not_ready_calls = 10_000
    stub_launch(control_plane, fake_rayd)
    control_plane.microvms.add_response(
        "get_microvm",
        microvm_response(endpoint=fake_rayd.host, state="PENDING", state_reason="warming"),
    )
    control_plane.microvms.add_response(
        "terminate_microvm", {}, expected_params={"microvmIdentifier": SANDBOX_ID}
    )
    with pytest.raises(SandboxNotReadyException) as excinfo:
        Sandbox.create(
            IMAGE_ARN,
            idle=None,
            access_token=ACCESS_TOKEN,
            control_plane=control_plane.plane,
            transport=fake_rayd.transport,
            ready_timeout=0.6,
        )
    assert excinfo.value.state == "PENDING"
    assert excinfo.value.state_reason == "warming"
    assert "terminado" in str(excinfo.value)


def test_ready_timeout_keeps_the_vm_when_asked(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.not_ready_calls = 10_000
    stub_launch(control_plane, fake_rayd)
    control_plane.microvms.add_response("get_microvm", microvm_response(endpoint=fake_rayd.host))
    with pytest.raises(SandboxNotReadyException):
        Sandbox.create(
            IMAGE_ARN,
            idle=None,
            access_token=ACCESS_TOKEN,
            control_plane=control_plane.plane,
            transport=fake_rayd.transport,
            ready_timeout=0.3,
            keep_on_failure=True,
        )


def test_connect_requires_token_and_rejects_terminated(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RAYITO_ACCESS_TOKEN", raising=False)
    with pytest.raises(AuthenticationException, match="RAYITO_ACCESS_TOKEN"):
        Sandbox.connect(SANDBOX_ID, control_plane=control_plane.plane)

    control_plane.microvms.add_response(
        "get_microvm",
        microvm_response(
            endpoint=fake_rayd.host,
            state="TERMINATED",
            state_reason="Success.",
            terminated_at=datetime(2026, 9, 15, 15, 0, tzinfo=UTC),
        ),
    )
    with pytest.raises(SandboxNotFoundException, match="Success"):
        Sandbox.connect(SANDBOX_ID, access_token=ACCESS_TOKEN, control_plane=control_plane.plane)


def test_connect_resumes_suspended_without_auto_resume(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    control_plane.microvms.add_response(
        "get_microvm",
        microvm_response(
            endpoint=fake_rayd.host,
            state="SUSPENDED",
            idle={
                "maxIdleDurationSeconds": 60,
                "suspendedDurationSeconds": 0,
                "autoResumeEnabled": False,
            },
        ),
    )
    control_plane.microvms.add_response(
        "resume_microvm", {}, expected_params={"microvmIdentifier": SANDBOX_ID}
    )
    control_plane.microvms.add_response("create_microvm_auth_token", auth_token_response())
    sandbox = Sandbox.connect(
        SANDBOX_ID,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    )
    assert sandbox.is_running()
    sandbox.close()
    assert fake_rayd.servicer.health_calls[0][ACCESS_TOKEN_KEY] == ACCESS_TOKEN


def test_get_host_mints_port_scoped_token_lazily(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    stub_launch(control_plane, fake_rayd)
    control_plane.microvms.add_response(
        "create_microvm_auth_token",
        auth_token_response("jwe-3000"),
        expected_params={
            "microvmIdentifier": SANDBOX_ID,
            "expirationInMinutes": 60,
            "allowedPorts": [{"port": 3000}],
        },
    )
    control_plane.microvms.add_response("terminate_microvm", {})
    with Sandbox.create(
        IMAGE_ARN,
        idle=None,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    ) as sandbox:
        primary = sandbox.get_host(DEFAULT_PORT)
        assert primary.headers == {PROXY_AUTH_KEY: JWE, PROXY_PORT_KEY: "8080"}
        host = sandbox.get_host(3000)
        assert host == fake_rayd.host
        assert host.url == f"https://{fake_rayd.host}"
        assert host.headers == {PROXY_AUTH_KEY: "jwe-3000", PROXY_PORT_KEY: "3000"}
        assert sandbox.get_host(3000).headers[PROXY_AUTH_KEY] == "jwe-3000"
        with pytest.raises(InvalidArgumentException):
            sandbox.get_host(HOOKS_PORT)


def test_class_method_variants_and_list(control_plane: StubbedControlPlane) -> None:
    control_plane.microvms.add_response(
        "terminate_microvm", {}, expected_params={"microvmIdentifier": SANDBOX_ID}
    )
    assert Sandbox.kill(SANDBOX_ID, control_plane=control_plane.plane) is True

    control_plane.microvms.add_response("get_microvm", microvm_response(state="RUNNING"))
    info = Sandbox.get_info(SANDBOX_ID, read_metadata=False, control_plane=control_plane.plane)
    assert info.state == "RUNNING"

    control_plane.microvms.add_response(
        "list_microvms",
        {"items": [list_item("x", "RUNNING"), list_item("y", "TERMINATED")]},
        expected_params={"maxResults": 50, "imageIdentifier": IMAGE_ARN},
    )
    listed = list(Sandbox.list(template=IMAGE_ARN, control_plane=control_plane.plane))
    assert [item.sandbox_id for item in listed] == ["x"]

    with pytest.raises(InvalidArgumentException):
        Sandbox.kill("", control_plane=control_plane.plane)


def test_pause_and_resume(control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint) -> None:
    """`pause()` lee `get-microvm` antes de `suspend-microvm` (idempotente en
    AWS: un VM ya `SUSPENDED` responde 200) y devuelve `False` sin llamarlo
    cuando ya está `SUSPENDING|SUSPENDED`."""
    stub_launch(control_plane, fake_rayd)
    control_plane.microvms.add_response(
        "get_microvm", microvm_response(endpoint=fake_rayd.host, state="RUNNING")
    )
    control_plane.microvms.add_response("suspend_microvm", {}, {"microvmIdentifier": SANDBOX_ID})
    control_plane.microvms.add_response(
        "get_microvm", microvm_response(endpoint=fake_rayd.host, state="SUSPENDING")
    )
    control_plane.microvms.add_response(
        "get_microvm", microvm_response(endpoint=fake_rayd.host, state="SUSPENDED")
    )
    control_plane.microvms.add_response(
        "get_microvm", microvm_response(endpoint=fake_rayd.host, state="SUSPENDED")
    )
    control_plane.microvms.add_response("resume_microvm", {}, {"microvmIdentifier": SANDBOX_ID})
    control_plane.microvms.add_response("create_microvm_auth_token", auth_token_response("jwe-2"))
    control_plane.microvms.add_response("terminate_microvm", {})

    with Sandbox.create(
        IMAGE_ARN,
        idle=None,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    ) as sandbox:
        assert sandbox.pause() is True
        assert sandbox.info.state == "SUSPENDED"
        assert sandbox.pause() is False
        sandbox.resume()
        assert fake_rayd.servicer.health_calls[-1][PROXY_AUTH_KEY] == "jwe-2"


def test_sub_clients_are_singletons(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    stub_launch(control_plane, fake_rayd)
    control_plane.microvms.add_response("terminate_microvm", {})
    with Sandbox.create(
        IMAGE_ARN,
        idle=None,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    ) as sandbox:
        assert sandbox.commands is sandbox.commands
        assert sandbox.files is sandbox.files
        assert isinstance(sandbox.pty, Pty)
        assert sandbox.pty is sandbox.pty
        assert sandbox.resume_generation == 0
        assert sandbox.run_code("1+1").text == "2"


def test_missing_template_is_an_argument_error(
    control_plane: StubbedControlPlane, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RAYITO_TEMPLATE", raising=False)
    with pytest.raises(InvalidArgumentException, match="RAYITO_TEMPLATE"):
        Sandbox.create(control_plane=control_plane.plane)


def test_create_terminates_the_vm_when_token_minting_fails(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    control_plane.microvms.add_response("run_microvm", microvm_response(endpoint=fake_rayd.host))
    control_plane.microvms.add_client_error(
        "create_microvm_auth_token",
        service_error_code="AccessDeniedException",
        http_status_code=403,
    )
    control_plane.microvms.add_response(
        "terminate_microvm", {}, expected_params={"microvmIdentifier": SANDBOX_ID}
    )
    with pytest.raises(AuthenticationException):
        Sandbox.create(
            IMAGE_ARN,
            idle=None,
            access_token=ACCESS_TOKEN,
            control_plane=control_plane.plane,
            transport=fake_rayd.transport,
        )
    control_plane.microvms.assert_no_pending_responses()
    assert fake_rayd.servicer.health_calls == []


def test_create_keeps_the_vm_on_token_failure_when_asked(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    control_plane.microvms.add_response("run_microvm", microvm_response(endpoint=fake_rayd.host))
    control_plane.microvms.add_client_error(
        "create_microvm_auth_token", service_error_code="ThrottlingException", http_status_code=429
    )
    with pytest.raises(RateLimitException):
        Sandbox.create(
            IMAGE_ARN,
            idle=None,
            access_token=ACCESS_TOKEN,
            control_plane=control_plane.plane,
            transport=fake_rayd.transport,
            keep_on_failure=True,
        )
    control_plane.microvms.assert_no_pending_responses()


def test_create_terminates_the_vm_when_the_agent_rejects_health(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_launch(control_plane, fake_rayd)
    control_plane.microvms.add_response(
        "terminate_microvm", {}, expected_params={"microvmIdentifier": SANDBOX_ID}
    )
    original_init = Sandbox.__init__

    def rejecting_init(self: Sandbox, **kwargs: Any) -> None:
        original_init(self, **kwargs)
        monkeypatch.setattr(
            self._health, "Health", failing_first(self._health.Health, [agent_permission_denied()])
        )

    monkeypatch.setattr(Sandbox, "__init__", rejecting_init)
    with pytest.raises(AuthenticationException) as excinfo:
        Sandbox.create(
            IMAGE_ARN,
            idle=None,
            access_token=ACCESS_TOKEN,
            control_plane=control_plane.plane,
            transport=fake_rayd.transport,
        )
    assert excinfo.value.proxy_rejected is False
    control_plane.microvms.assert_no_pending_responses()


def test_proxy_403_remints_once_and_retries(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_launch(control_plane, fake_rayd)
    stub_remint(control_plane, "jwe-2")
    control_plane.microvms.add_response("terminate_microvm", {})
    with Sandbox.create(
        IMAGE_ARN,
        idle=None,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    ) as sandbox:
        monkeypatch.setattr(
            sandbox._health, "Health", failing_first(sandbox._health.Health, [proxy_forbidden()])
        )
        assert sandbox.is_running() is True
        assert sandbox.get_host(DEFAULT_PORT).headers[PROXY_AUTH_KEY] == "jwe-2"
    control_plane.microvms.assert_no_pending_responses()
    assert fake_rayd.servicer.health_calls[-1][PROXY_AUTH_KEY] == "jwe-2"


def test_genuine_permission_denied_from_the_agent_is_not_retried(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_launch(control_plane, fake_rayd)
    control_plane.microvms.add_response("terminate_microvm", {})
    with Sandbox.create(
        IMAGE_ARN,
        idle=None,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    ) as sandbox:
        health_calls_before = len(fake_rayd.servicer.health_calls)
        monkeypatch.setattr(
            sandbox._health,
            "Health",
            failing_first(sandbox._health.Health, [agent_permission_denied()]),
        )
        with pytest.raises(AuthenticationException, match="EACCES") as excinfo:
            sandbox.is_running()
        assert excinfo.value.proxy_rejected is False
        assert len(fake_rayd.servicer.health_calls) == health_calls_before
    control_plane.microvms.assert_no_pending_responses()


def test_double_proxy_403_surfaces_proxy_rejected(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_launch(control_plane, fake_rayd)
    stub_remint(control_plane, "jwe-2")
    control_plane.microvms.add_response("terminate_microvm", {})
    with Sandbox.create(
        IMAGE_ARN,
        idle=None,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    ) as sandbox:
        monkeypatch.setattr(
            sandbox._health,
            "Health",
            failing_first(sandbox._health.Health, [proxy_forbidden(), proxy_forbidden()]),
        )
        with pytest.raises(AuthenticationException) as excinfo:
            sandbox.is_running()
        assert excinfo.value.proxy_rejected is True
    control_plane.microvms.assert_no_pending_responses()


def test_class_calls_without_control_plane_share_one_plane_per_process(
    control_plane: StubbedControlPlane, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[tuple[object, str | None]] = []

    def fake_from_session(session: object = None, *, region: str | None = None) -> Any:
        built.append((session, region))
        return control_plane.plane

    monkeypatch.setattr(_aws, "_shared_planes", {})
    monkeypatch.setattr(LambdaMicrovmsControlPlane, "from_session", fake_from_session)
    for _ in range(3):
        control_plane.microvms.add_response("get_microvm", microvm_response(state="RUNNING"))
        control_plane.microvms.add_response(
            "suspend_microvm", {}, {"microvmIdentifier": SANDBOX_ID}
        )
        assert Sandbox.pause(SANDBOX_ID, wait=False, region=REGION) is True
    assert built == [(None, REGION)]
    assert control_plane.clock.sleeps == pytest.approx([0.5])
