"""Adaptador boto3 contra `botocore.stub.Stubber`: parámetros exactos, mapeo y ritmo."""

from __future__ import annotations

from typing import Any

import boto3
import pytest
from botocore.stub import Stubber

from rayito import _aws
from rayito._aws import (
    ClientSettings,
    LambdaMicrovmsControlPlane,
    LaunchRequest,
    PortSpec,
    TokenBucket,
    client_config,
    control_plane_session,
    normalize_endpoint,
    proxy_jwe_from_response,
    shared_control_plane,
)
from rayito._models import IdlePolicy
from rayito._version import __version__
from rayito.exceptions import (
    InvalidArgumentException,
    RateLimitException,
    SandboxException,
    SandboxNotFoundException,
)

from .conftest import (
    ACCOUNT_ID,
    IMAGE_ARN,
    IMAGE_NAME,
    JWE,
    REGION,
    SANDBOX_ID,
    FakeClock,
    StubbedControlPlane,
    auth_token_response,
    list_item,
    microvm_response,
    no_retry_client,
)


def fake_session() -> boto3.session.Session:
    return boto3.session.Session(
        region_name=REGION, aws_access_key_id="testing", aws_secret_access_key="testing"
    )


def test_run_microvm_sends_exactly_the_modelled_parameters(
    control_plane: StubbedControlPlane,
) -> None:
    request = LaunchRequest(
        image_arn=IMAGE_ARN,
        image_version="2.0",
        maximum_duration_seconds=900,
        run_hook_payload='{"v":1}',
        client_token="c" * 32,
        logging={"cloudWatch": {"logGroup": "/rayito/x"}},
        execution_role_arn=f"arn:aws:iam::{ACCOUNT_ID}:role/r",
        idle=IdlePolicy(max_idle_seconds=120, suspended_duration_seconds=780, auto_resume=True),
        ingress_connectors=(
            "arn:aws:lambda:us-east-1:aws:network-connector:aws-network-connector:ALL_INGRESS",
        ),
    )
    control_plane.microvms.add_response(
        "run_microvm",
        microvm_response(
            idle={
                "maxIdleDurationSeconds": 120,
                "suspendedDurationSeconds": 780,
                "autoResumeEnabled": True,
            }
        ),
        expected_params={
            "imageIdentifier": IMAGE_ARN,
            "imageVersion": "2.0",
            "maximumDurationInSeconds": 900,
            "runHookPayload": '{"v":1}',
            "clientToken": "c" * 32,
            "logging": {"cloudWatch": {"logGroup": "/rayito/x"}},
            "executionRoleArn": f"arn:aws:iam::{ACCOUNT_ID}:role/r",
            "idlePolicy": {
                "maxIdleDurationSeconds": 120,
                "suspendedDurationSeconds": 780,
                "autoResumeEnabled": True,
            },
            "ingressNetworkConnectors": [
                "arn:aws:lambda:us-east-1:aws:network-connector:aws-network-connector:ALL_INGRESS"
            ],
        },
    )
    info = control_plane.plane.run_microvm(request)
    assert info.sandbox_id == SANDBOX_ID
    assert info.state == "PENDING"
    assert info.endpoint == "abc.lambda-microvm.us-east-1.on.aws"
    assert info.idle == IdlePolicy(
        max_idle_seconds=120, suspended_duration_seconds=780, auto_resume=True
    )


def test_create_auth_token_uses_60_minutes_and_port_specs(
    control_plane: StubbedControlPlane,
) -> None:
    control_plane.microvms.add_response(
        "create_microvm_auth_token",
        auth_token_response(),
        expected_params={
            "microvmIdentifier": SANDBOX_ID,
            "expirationInMinutes": 60,
            "allowedPorts": [{"port": 8080}, {"range": {"startPort": 8000, "endPort": 8999}}],
        },
    )
    jwe = control_plane.plane.create_auth_token(
        SANDBOX_ID, [PortSpec.single(8080), PortSpec.range(8000, 8999)]
    )
    assert jwe == JWE


def test_auth_token_map_tolerates_other_keys() -> None:
    assert proxy_jwe_from_response({"X-aws-proxy-auth": "a"}) == "a"
    assert proxy_jwe_from_response({"x-aws-proxy-auth": "b"}) == "b"
    assert proxy_jwe_from_response({"Something": "c"}) == "c"
    with pytest.raises(SandboxException):
        proxy_jwe_from_response({"A": "1", "B": "2"})


def test_template_name_resolves_to_arn_via_sts_once(control_plane: StubbedControlPlane) -> None:
    control_plane.sts.add_response(
        "get_caller_identity",
        {
            "Account": ACCOUNT_ID,
            "Arn": f"arn:aws:sts::{ACCOUNT_ID}:assumed-role/x/y",
            "UserId": "u",
        },
    )
    assert control_plane.plane.resolve_template_arn(IMAGE_NAME) == IMAGE_ARN
    assert control_plane.plane.resolve_template_arn("other") == IMAGE_ARN.replace(
        IMAGE_NAME, "other"
    )
    assert control_plane.plane.resolve_template_arn(IMAGE_ARN) == IMAGE_ARN
    with pytest.raises(InvalidArgumentException):
        control_plane.plane.resolve_template_arn("bad name!")


def test_error_mapping_through_the_adapter(control_plane: StubbedControlPlane) -> None:
    control_plane.microvms.add_client_error(
        "get_microvm",
        service_error_code="ResourceNotFoundException",
        service_message="no such microvm",
        http_status_code=404,
        expected_params={"microvmIdentifier": SANDBOX_ID},
    )
    with pytest.raises(SandboxNotFoundException, match="no such microvm") as excinfo:
        control_plane.plane.get_microvm(SANDBOX_ID)
    assert excinfo.value.aws_code == "ResourceNotFoundException"

    control_plane.microvms.add_client_error(
        "run_microvm",
        service_error_code="ThrottlingException",
        service_message="slow down",
        http_status_code=429,
        modeled_fields={"retryAfterSeconds": 2},
    )
    request = LaunchRequest(
        image_arn=IMAGE_ARN,
        maximum_duration_seconds=60,
        run_hook_payload="{}",
        client_token="t",
        logging={"disabled": {}},
    )
    with pytest.raises(RateLimitException) as throttled:
        control_plane.plane.run_microvm(request)
    assert throttled.value.retry_after == 2.0


def test_terminate_suspend_resume_semantics(control_plane: StubbedControlPlane) -> None:
    control_plane.microvms.add_response("terminate_microvm", {}, {"microvmIdentifier": SANDBOX_ID})
    assert control_plane.plane.terminate_microvm(SANDBOX_ID) is True
    control_plane.microvms.add_client_error(
        "terminate_microvm", service_error_code="ResourceNotFoundException", http_status_code=404
    )
    assert control_plane.plane.terminate_microvm(SANDBOX_ID) is False

    control_plane.microvms.add_client_error(
        "suspend_microvm", service_error_code="ConflictException", http_status_code=409
    )
    assert control_plane.plane.suspend_microvm(SANDBOX_ID) is False
    control_plane.microvms.add_response("resume_microvm", {}, {"microvmIdentifier": SANDBOX_ID})
    assert control_plane.plane.resume_microvm(SANDBOX_ID) is True


def test_list_filters_terminated_by_default_and_paginates(
    control_plane: StubbedControlPlane,
) -> None:
    control_plane.microvms.add_response(
        "list_microvms",
        {"items": [list_item("a", "RUNNING"), list_item("b", "TERMINATED")], "nextToken": "n1"},
        expected_params={"maxResults": 50, "imageIdentifier": IMAGE_ARN},
    )
    control_plane.microvms.add_response(
        "list_microvms",
        {"items": [list_item("c", "TERMINATING"), list_item("d", "SUSPENDED")]},
        expected_params={"maxResults": 50, "imageIdentifier": IMAGE_ARN, "nextToken": "n1"},
    )
    items = list(control_plane.plane.list_microvms(image_arn=IMAGE_ARN))
    assert [item.sandbox_id for item in items] == ["a", "d"]
    assert items[0].template_name == IMAGE_NAME

    control_plane.microvms.add_response(
        "list_microvms",
        {"items": [list_item("a", "RUNNING"), list_item("b", "TERMINATED")]},
        expected_params={"maxResults": 50, "imageVersion": "1.0"},
    )
    only_dead = list(control_plane.plane.list_microvms(image_version="1.0", states={"TERMINATED"}))
    assert [item.sandbox_id for item in only_dead] == ["b"]


def test_token_bucket_paces_at_the_published_rate() -> None:
    clock = FakeClock()
    bucket = TokenBucket(5, clock=clock, sleep=clock.sleep)
    waits = [bucket.acquire() for _ in range(8)]
    assert waits[:5] == [0.0] * 5
    assert waits[5:] == pytest.approx([0.2, 0.2, 0.2])
    assert clock.sleeps == pytest.approx([0.2, 0.2, 0.2])
    clock.advance(10)
    assert bucket.acquire() == 0.0


def test_token_bucket_reserves_turns_for_concurrent_callers() -> None:
    """Sin que el reloj avance durante el sleep, cada turno acumula su espera."""
    clock = FakeClock()
    recorded: list[float] = []
    bucket = TokenBucket(5, clock=clock, sleep=recorded.append)
    waits = [bucket.acquire() for _ in range(7)]
    assert waits[5:] == pytest.approx([0.2, 0.4])
    assert recorded == pytest.approx([0.2, 0.4])


def test_suspend_bucket_is_2_tps(control_plane: StubbedControlPlane) -> None:
    for _ in range(3):
        control_plane.microvms.add_response(
            "suspend_microvm", {}, {"microvmIdentifier": SANDBOX_ID}
        )
        control_plane.plane.suspend_microvm(SANDBOX_ID)
    assert control_plane.clock.sleeps == pytest.approx([0.5])


@pytest.mark.parametrize(
    ("raw", "host"),
    [
        ("abc.lambda-microvm.us-east-1.on.aws", "abc.lambda-microvm.us-east-1.on.aws"),
        ("https://abc.lambda-microvm.us-east-1.on.aws/", "abc.lambda-microvm.us-east-1.on.aws"),
        (" https://abc.on.aws/path ", "abc.on.aws"),
        ("localhost:50051", "localhost:50051"),
    ],
)
def test_endpoint_normalization(raw: str, host: str) -> None:
    assert normalize_endpoint(raw) == host


def test_shared_control_plane_is_one_per_session_and_region(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[tuple[object, str | None]] = []

    def fake_from_session(
        session: object = None, *, region: str | None = None, settings: object = None
    ) -> Any:
        built.append((session, region))
        return LambdaMicrovmsControlPlane(no_retry_client("lambda-microvms"))

    monkeypatch.setattr(_aws, "_shared_planes", {})
    monkeypatch.setattr(LambdaMicrovmsControlPlane, "from_session", fake_from_session)
    session = fake_session()

    default = shared_control_plane(region=REGION)
    assert shared_control_plane(region=REGION) is default
    assert shared_control_plane(region="eu-west-1") is not default
    scoped = shared_control_plane(session, region=REGION)
    assert scoped is not default
    assert shared_control_plane(session, region=REGION) is scoped
    assert built == [(None, REGION), (None, "eu-west-1"), (session, REGION)]


def test_client_config_applies_retries_proxy_and_integration() -> None:
    config = client_config(ClientSettings(2, "http://h:1", "acme/1"))
    assert config.retries == {"mode": "standard", "total_max_attempts": 3}
    assert config.proxies == {"http": "http://h:1", "https": "http://h:1"}
    assert config.user_agent_extra == f"rayito/{__version__} acme/1"
    assert (config.connect_timeout, config.read_timeout) == (5, 60)


def test_client_config_defaults_are_unchanged() -> None:
    for config in (client_config(), client_config(ClientSettings())):
        assert config.retries == {"mode": "standard", "total_max_attempts": 5}
        assert config.proxies is None
        assert config.user_agent_extra == f"rayito/{__version__}"


def test_from_session_puts_the_settings_on_the_botocore_client() -> None:
    plane = LambdaMicrovmsControlPlane.from_session(
        fake_session(),
        region=REGION,
        settings=ClientSettings(retries=0, proxy="http://127.0.0.1:3128", integration="acme/1.0"),
    )
    config = plane._client.meta.config
    assert config.retries["total_max_attempts"] == 1
    assert config.proxies == {"http": "http://127.0.0.1:3128", "https": "http://127.0.0.1:3128"}
    assert config.user_agent_extra.endswith("acme/1.0")


def test_from_session_keeps_the_session_for_signing() -> None:
    session = fake_session()
    plane = LambdaMicrovmsControlPlane.from_session(session, region=REGION)
    assert plane.session is session
    assert control_plane_session(plane) is session
    assert control_plane_session(object()) is None


def test_shared_control_plane_is_one_per_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[object] = []

    def fake_from_session(
        session: object = None, *, region: str | None = None, settings: object = None
    ) -> Any:
        built.append(settings)
        return LambdaMicrovmsControlPlane(no_retry_client("lambda-microvms"))

    monkeypatch.setattr(_aws, "_shared_planes", {})
    monkeypatch.setattr(LambdaMicrovmsControlPlane, "from_session", fake_from_session)
    retried = ClientSettings(retries=2)
    default = shared_control_plane(region=REGION)
    first = shared_control_plane(region=REGION, settings=retried)
    assert first is not default
    assert shared_control_plane(region=REGION, settings=ClientSettings(retries=2)) is first
    assert shared_control_plane(region=REGION, settings=ClientSettings(retries=3)) is not first
    assert built == [None, retried, ClientSettings(retries=3)]


def test_client_settings_repr_hides_the_proxy_url() -> None:
    assert "clave" not in repr(ClientSettings(proxy="http://u:clave@h:1"))


def test_from_session_builds_the_sts_client_lazily(monkeypatch: pytest.MonkeyPatch) -> None:
    session = fake_session()
    created: list[str] = []
    real_client = session.client

    def counting_client(service: str, **kwargs: Any) -> Any:
        created.append(service)
        return real_client(service, **kwargs)

    monkeypatch.setattr(session, "client", counting_client)
    plane = LambdaMicrovmsControlPlane.from_session(session, region=REGION)
    assert created == ["lambda-microvms"]
    assert plane.resolve_template_arn(IMAGE_ARN) == IMAGE_ARN
    assert created == ["lambda-microvms"]


def test_sts_factory_runs_once_and_only_for_template_names() -> None:
    sts = no_retry_client("sts")
    factory_calls: list[int] = []

    def factory() -> Any:
        factory_calls.append(1)
        return sts

    plane = LambdaMicrovmsControlPlane(
        no_retry_client("lambda-microvms"), sts_client_factory=factory
    )
    assert plane.resolve_template_arn(IMAGE_ARN) == IMAGE_ARN
    assert factory_calls == []
    with Stubber(sts) as stub:
        stub.add_response(
            "get_caller_identity",
            {"Account": ACCOUNT_ID, "Arn": f"arn:aws:iam::{ACCOUNT_ID}:user/dev", "UserId": "u"},
        )
        assert plane.resolve_template_arn(IMAGE_NAME) == IMAGE_ARN
        assert plane.resolve_template_arn("other") == IMAGE_ARN.replace(IMAGE_NAME, "other")
        stub.assert_no_pending_responses()
    assert factory_calls == [1]


def test_list_microvms_page_sends_exactly_the_given_keys_and_filters_nothing(
    control_plane: StubbedControlPlane,
) -> None:
    control_plane.microvms.add_response(
        "list_microvms",
        {"items": [list_item("a", "RUNNING"), list_item("b", "TERMINATED")], "nextToken": "t2"},
        expected_params={"maxResults": 50, "nextToken": "t1", "imageIdentifier": IMAGE_ARN},
    )
    page = control_plane.plane.list_microvms_page(
        image_arn=IMAGE_ARN, image_version=None, max_results=50, next_token="t1"
    )
    assert [item.sandbox_id for item in page.items] == ["a", "b"]
    assert page.items[1].state == "TERMINATED"
    assert page.next_token == "t2"

    control_plane.microvms.add_response(
        "list_microvms",
        {"items": [list_item("c", "SUSPENDED")]},
        expected_params={"maxResults": 50},
    )
    last = control_plane.plane.list_microvms_page(
        image_arn=None, image_version=None, max_results=50, next_token=None
    )
    assert [item.sandbox_id for item in last.items] == ["c"]
    assert last.next_token is None

    control_plane.microvms.add_response(
        "list_microvms",
        {"items": []},
        expected_params={"maxResults": 7, "imageVersion": "3"},
    )
    empty = control_plane.plane.list_microvms_page(
        image_arn=None, image_version="3", max_results=7, next_token=None
    )
    assert empty.items == () and empty.next_token is None


def test_list_microvms_page_rejects_a_page_size_outside_the_api_range(
    control_plane: StubbedControlPlane,
) -> None:
    for size in (0, 51):
        with pytest.raises(ValueError, match="max_results"):
            control_plane.plane.list_microvms_page(
                image_arn=None, image_version=None, max_results=size, next_token=None
            )


def test_list_microvms_page_translates_a_stale_token(control_plane: StubbedControlPlane) -> None:
    control_plane.microvms.add_client_error(
        "list_microvms", service_error_code="ValidationException", http_status_code=400
    )
    with pytest.raises(InvalidArgumentException):
        control_plane.plane.list_microvms_page(
            image_arn=None, image_version=None, max_results=50, next_token="stale"
        )
