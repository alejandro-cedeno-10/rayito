from __future__ import annotations

import itertools
import json
import pickle
from datetime import UTC, datetime, timedelta

import pytest

from rayito._limits import HOOKS_PORT, MAX_DURATION_SECONDS
from rayito._models import (
    CheckpointResult,
    CodeContext,
    DownloadLink,
    EgressProxy,
    EntryInfo,
    Execution,
    ExecutionError,
    HostAccess,
    IdlePolicy,
    Logs,
    MicrovmListPage,
    RestoreResult,
    S3Prefix,
    S3Staging,
    SandboxInfo,
    SandboxMetrics,
    TransferStatus,
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


def test_sandbox_metrics_keeps_positional_construction_and_defaults_mem_cache() -> None:
    metrics = SandboxMetrics(1.0, 2, 3, 4, 5, 1, STARTED_AT)
    assert metrics.mem_cache_bytes == 0
    assert SandboxMetrics(1.0, 2, 3, 4, 5, 1, STARTED_AT, 7).mem_cache_bytes == 7


def test_sandbox_info_guest_facts_default_to_unknown() -> None:
    info = SandboxInfo(
        sandbox_id=SANDBOX_ID,
        state="RUNNING",
        endpoint="host",
        template=IMAGE_ARN,
        template_version="1",
        started_at=STARTED_AT,
        maximum_duration_seconds=900,
    )
    assert (info.agent_version, info.cpu_count, info.memory_mb) == (None, None, None)


def test_microvm_list_page_is_a_frozen_page_with_an_optional_token() -> None:
    page = MicrovmListPage(items=())
    assert page.next_token is None
    with pytest.raises(AttributeError):
        page.next_token = "t"  # type: ignore[misc]


def test_egress_proxy_repr_hides_the_password() -> None:
    proxy = EgressProxy("10.0.0.5:1080", username="operator", password="s3cr3t-value")
    assert "s3cr3t-value" not in repr(proxy)
    assert "s3cr3t-value" not in str(proxy)
    assert "operator" in repr(proxy)


def test_logs_to_json_round_trips() -> None:
    logs = Logs(stdout=["a\n", "b"], stderr=["err"])
    assert json.loads(logs.to_json()) == {"stdout": ["a\n", "b"], "stderr": ["err"]}
    assert Logs(**json.loads(logs.to_json())) == logs


def test_execution_error_to_json_round_trips() -> None:
    error = ExecutionError(name="ZeroDivisionError", value="division by zero", traceback="t")
    assert ExecutionError(**json.loads(error.to_json())) == error


def test_execution_nests_logs_as_a_json_string_like_e2b() -> None:
    logs = Logs(stdout=["hola\n"], stderr=[])
    execution = Execution(logs=logs)
    assert json.loads(execution.to_json())["logs"] == logs.to_json()


def test_code_context_from_json_round_trips() -> None:
    context = CodeContext(id="ctx-1", language="python", cwd="/home/user")
    data = {"id": context.id, "language": context.language, "cwd": context.cwd}
    assert CodeContext.from_json(data) == context


@pytest.mark.parametrize("missing", ["id", "language", "cwd"])
def test_code_context_from_json_names_the_missing_key(missing: str) -> None:
    data = {"id": "ctx-1", "language": "python", "cwd": "/home/user"}
    del data[missing]
    with pytest.raises(InvalidArgumentException, match=missing):
        CodeContext.from_json(data)


def test_download_link_is_the_url_but_never_shows_or_pickles_it() -> None:
    url = "https://amzn-s3-demo-bucket.s3.us-east-1.amazonaws.com/k?X-Amz-Signature=abc"
    expires = datetime(2026, 1, 1, tzinfo=UTC)
    link = DownloadLink(url, path="/a", expires_at=expires, transfer_id="t", size=3, sha256="f")
    assert str(link) == url == link.url
    assert (link.path, link.size, link.sha256, link.transfer_id) == ("/a", 3, "f", "t")
    assert "Signature" not in repr(link) and "amzn-s3-demo-bucket" not in repr(link)
    with pytest.raises(TypeError, match="credencial"):
        pickle.dumps(link)


def test_entry_info_metadata_defaults_empty_and_is_read_only() -> None:
    entry = EntryInfo(
        name="a",
        type=None,
        path="/a",
        size=0,
        mode=0o644,
        permissions="-rw-r--r--",
        owner="user",
        group="user",
        modified_time=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert dict(entry.metadata) == {}
    with pytest.raises(TypeError):
        entry.metadata["k"] = "v"  # type: ignore[index]


def test_transfer_status_and_staging_defaults() -> None:
    status = TransferStatus("t", "import", "running", 1, 2, 0)
    assert (status.error_code, status.error_reason) == (None, None)
    staging = S3Staging(bucket="amzn-s3-demo-bucket")
    assert staging.region is None
    assert staging.threshold_bytes <= staging.multipart_threshold_bytes
