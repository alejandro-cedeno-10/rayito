from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

import pytest

from rayito._limits import HOOKS_PORT, MAX_DURATION_SECONDS
from rayito._models import (
    CheckpointResult,
    HostAccess,
    IdlePolicy,
    RestoreResult,
    S3Prefix,
    SandboxInfo,
)
from rayito._sandbox_base import (
    ReadinessPoll,
    connector_arns,
    logging_config,
    proxy_port_specs,
    resolve_idle_policy,
    validate_host_port,
    validate_timeout,
)
from rayito.exceptions import InvalidArgumentException, SandboxLifetimeException

from .conftest import IMAGE_ARN, SANDBOX_ID, STARTED_AT


def test_host_access_behaves_like_the_hostname() -> None:
    tokens = itertools.chain(["jwe-1"], itertools.repeat("jwe-2"))
    host = HostAccess(
        "abc.lambda-microvm.us-east-1.on.aws", port=3000, token_provider=lambda: next(tokens)
    )
    assert host == "abc.lambda-microvm.us-east-1.on.aws"
    assert f"https://{host}" == "https://abc.lambda-microvm.us-east-1.on.aws"
    assert host.url == "https://abc.lambda-microvm.us-east-1.on.aws"
    assert host.port == 3000
    assert host.headers == {"x-aws-proxy-auth": "jwe-1", "x-aws-proxy-port": "3000"}
    assert host.headers["x-aws-proxy-auth"] == "jwe-2"
    assert "x-aws-proxy-force-h2" not in host.headers
    assert "jwe" not in repr(host)


def test_sandbox_info_expiry_and_remaining() -> None:
    info = SandboxInfo(
        sandbox_id=SANDBOX_ID,
        state="RUNNING",
        endpoint="abc.on.aws",
        template=IMAGE_ARN,
        template_version="1.0",
        started_at=STARTED_AT,
        maximum_duration_seconds=3600,
    )
    assert info.expires_at == STARTED_AT + timedelta(hours=1)
    assert info.remaining_seconds(now=STARTED_AT + timedelta(minutes=10)) == 3000
    assert info.remaining_seconds(now=STARTED_AT + timedelta(hours=2)) == 0
    assert info.endpoint_url == "https://abc.on.aws"
    assert info.template_name == "rayito-base-2gb"


def test_idle_policy_validation() -> None:
    with pytest.raises(InvalidArgumentException):
        IdlePolicy(max_idle_seconds=59)
    with pytest.raises(InvalidArgumentException):
        IdlePolicy(suspended_duration_seconds=-1)
    assert IdlePolicy().max_idle_seconds == 300


def test_idle_policy_resolution_fills_suspended_duration() -> None:
    resolved = resolve_idle_policy(IdlePolicy(max_idle_seconds=300), timeout=3600)
    assert resolved is not None
    assert resolved.suspended_duration_seconds == 3300
    assert resolve_idle_policy(None, timeout=60) is None
    with pytest.raises(InvalidArgumentException):
        resolve_idle_policy(IdlePolicy(max_idle_seconds=3600), timeout=3600)


def test_timeout_validation() -> None:
    assert validate_timeout(MAX_DURATION_SECONDS) == MAX_DURATION_SECONDS
    with pytest.raises(SandboxLifetimeException, match="8 h"):
        validate_timeout(MAX_DURATION_SECONDS + 1)
    with pytest.raises(InvalidArgumentException):
        validate_timeout(0)
    with pytest.raises(InvalidArgumentException):
        validate_timeout(True)


def test_proxy_ports_always_include_8080_and_never_hooks_port() -> None:
    specs = proxy_port_specs([3000, (8000, 8999), 8080])
    assert [spec.to_api() for spec in specs] == [
        {"port": 8080},
        {"port": 3000},
        {"range": {"startPort": 8000, "endPort": 8999}},
    ]
    with pytest.raises(InvalidArgumentException, match="hooks"):
        proxy_port_specs([HOOKS_PORT])
    with pytest.raises(InvalidArgumentException, match="hooks"):
        proxy_port_specs([(8000, 9500)])
    with pytest.raises(InvalidArgumentException):
        validate_host_port(HOOKS_PORT)
    with pytest.raises(InvalidArgumentException):
        validate_host_port(70000)


def test_connectors_resolve_managed_names() -> None:
    assert connector_arns(["ALL_INGRESS"], region="us-east-1", field="ingress") == (
        "arn:aws:lambda:us-east-1:aws:network-connector:aws-network-connector:ALL_INGRESS",
    )
    own = "arn:aws:lambda:us-east-1:123456789012:network-connector:mine"
    assert connector_arns([own], region="us-east-1", field="egress") == (own,)
    with pytest.raises(InvalidArgumentException):
        connector_arns(["BOGUS"], region="us-east-1", field="egress")
    with pytest.raises(InvalidArgumentException, match="10"):
        connector_arns([own] * 11, region="us-east-1", field="egress")


def test_logging_is_always_explicit() -> None:
    assert logging_config("disabled", template_name="t") == {"disabled": {}}
    assert logging_config("cloudwatch", template_name="rayito-base-2gb") == {
        "cloudWatch": {"logGroup": "/rayito/rayito-base-2gb"}
    }
    custom = {"cloudWatch": {"logGroup": "/custom", "logStream": "s"}}
    assert logging_config(custom, template_name="t") == custom
    with pytest.raises(InvalidArgumentException):
        logging_config("verbose", template_name="t")  # type: ignore[arg-type]


def test_readiness_schedule_doubles_to_two_seconds() -> None:
    now = [0.0]
    poll = ReadinessPoll(timeout=90, monotonic=lambda: now[0])
    delays = [poll.next_delay() for _ in range(5)]
    assert delays == [0.25, 0.5, 1.0, 2.0, 2.0]
    assert poll.should_check_state() is False
    now[0] = 5.0
    assert poll.should_check_state() is True
    assert poll.should_check_state() is False
    now[0] = 89.9
    assert poll.timed_out() is False
    assert poll.rpc_timeout() == pytest.approx(0.5)
    now[0] = 90.0
    assert poll.timed_out() is True
    assert poll.next_delay() == 0.0


def test_utc_now_is_used_for_remaining() -> None:
    info = SandboxInfo(
        sandbox_id=SANDBOX_ID,
        state="RUNNING",
        endpoint="abc",
        template=IMAGE_ARN,
        template_version="1.0",
        started_at=datetime.now(UTC),
        maximum_duration_seconds=600,
    )
    assert 590 < info.remaining_seconds() <= 600


# ------------------------------------------------------------- persistence


def test_s3_prefix_defaults_and_keys() -> None:
    prefix = S3Prefix("my-bucket")
    assert prefix.prefix == "rayito"
    assert prefix.name is None and prefix.region is None
    with pytest.raises(InvalidArgumentException, match="name"):
        _ = prefix.key_prefix
    named = prefix.with_name("agent-7")
    assert named.key_prefix == "rayito/agent-7"
    assert named.archive_key == "rayito/agent-7/home.tar.gz"
    assert named.manifest_key == "rayito/agent-7/manifest.json"
    assert named.uri == "s3://my-bucket/rayito/agent-7"
    assert S3Prefix("my-bucket", prefix="a/b", name="c/d", region="eu-west-1").key_prefix == (
        "a/b/c/d"
    )


@pytest.mark.parametrize(
    "bucket",
    ["ab", "a" * 64, "Bucket", "my_bucket", "-abc", "abc.", "a..b", "192.168.0.1", 42],
)
def test_s3_prefix_rejects_bad_buckets(bucket: object) -> None:
    with pytest.raises(InvalidArgumentException, match="bucket"):
        S3Prefix(bucket)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "prefix",
    ["", "/rayito", "rayito/", "a//b", "a/./b", "a/../b", "ray ito", "rayitó", "a&b", "a" * 901],
)
def test_s3_prefix_rejects_bad_prefixes(prefix: str) -> None:
    with pytest.raises(InvalidArgumentException, match="prefix"):
        S3Prefix("my-bucket", prefix=prefix)


@pytest.mark.parametrize("name", ["", "/x", "x/", "a//b", "..", "x y"])
def test_s3_prefix_rejects_bad_names(name: str) -> None:
    with pytest.raises(InvalidArgumentException, match="name"):
        S3Prefix("my-bucket", name=name)


def test_s3_prefix_accepts_the_safe_set_and_rejects_an_empty_region() -> None:
    assert S3Prefix("my-bucket", prefix="a!b_c.d*e'f(g)h-i", name="n").key_prefix == (
        "a!b_c.d*e'f(g)h-i/n"
    )
    assert S3Prefix("my-bucket", prefix="a" * 900).prefix == "a" * 900
    with pytest.raises(InvalidArgumentException, match="region"):
        S3Prefix("my-bucket", region="")


def test_results_expose_the_uri() -> None:
    checkpoint = CheckpointResult(
        bucket="b-1",
        key_prefix="p/n",
        files=1,
        bytes_read=2,
        archive_bytes=3,
        sha256="ab" * 32,
        skipped=0,
        duration=1.5,
    )
    restore = RestoreResult(
        bucket="b-1",
        key_prefix="p/n",
        files=1,
        bytes_written=2,
        archive_bytes=3,
        sha256="ab" * 32,
        skipped=0,
        duration=0.5,
    )
    assert checkpoint.uri == restore.uri == "s3://b-1/p/n"
