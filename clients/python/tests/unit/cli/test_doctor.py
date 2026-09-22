"""`rayito doctor`: cada comprobación en cada estado con `Stubber`, los
escenarios del spec (cuenta nueva sin imagen, cuotas reducidas y deny
implícito como WARN, `--launch` matado pase lo que pase, la sonda de
`Health` sobre un `rayd` falso) y que una excepción en una comprobación no
detiene las demás."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from typer.testing import CliRunner

from rayito import __version__ as SDK_VERSION
from rayito._aws import PortSpec
from rayito._transport import TransportSettings
from rayito.cli import _checks
from rayito.cli._checks import CheckResult, CheckSpec, DoctorContext, run_check
from rayito.cli._session import Clients
from rayito.cli.app import app
from rayito.exceptions import SandboxException
from rayito.v1 import health_pb2

from ..conftest import FakeRayd, RaydEndpoint, TrackingTransport, start_fake_rayd
from .conftest import (
    ACCOUNT_ID,
    BASE_IMAGE_ARN,
    IMAGE_ARN,
    REGION,
    USER_ARN,
    FakeControlPlane,
    Stubs,
    image_summary,
    list_item,
    sandbox_info,
    version_item,
)

QUOTAS_JSON = Path(__file__).resolve().parent / "fixtures" / "quotas.json"
QUOTA_MEMBERS = {"QuotaCode", "QuotaName", "Value", "Unit", "Adjustable", "ServiceCode"}
BUCKET = "artifacts"
ROLE_SESSION_ARN = f"arn:aws:sts::{ACCOUNT_ID}:assumed-role/AWSReservedSSO_Admin_abc/user"
IMAGE_VERSION = "17.0"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT_ID}:role/aws-reserved/sso.amazonaws.com/AWSReservedSSO_Admin_abc"
T0 = datetime(2026, 9, 1, tzinfo=UTC)
STATUSES = ("OK", "WARN", "FAIL", "SKIP")


def measured_quotas() -> list[dict[str, Any]]:
    return [
        {key: value for key, value in quota.items() if key in QUOTA_MEMBERS}
        for quota in json.loads(QUOTAS_JSON.read_text(encoding="utf-8"))
    ]


def running(sandbox_id: str, **overrides: Any) -> Any:
    return list_item(sandbox_id, version=IMAGE_VERSION, **overrides)


def running_info(sandbox_id: str, **overrides: Any) -> Any:
    return sandbox_info(sandbox_id, version=IMAGE_VERSION, **overrides)


def stub_identity(stubs: Stubs, arn: str = USER_ARN) -> None:
    stubs.sts.add_response(
        "get_caller_identity", {"UserId": "AID", "Account": ACCOUNT_ID, "Arn": arn}, {}
    )


def stub_managed(stubs: Stubs, *, present: bool = True) -> None:
    items = [{"imageArn": BASE_IMAGE_ARN, "createdAt": T0}] if present else []
    stubs.microvms.add_response("list_managed_microvm_images", {"items": items}, {})
    if present:
        stubs.microvms.add_response(
            "list_managed_microvm_image_versions",
            {
                "items": [
                    {"imageArn": BASE_IMAGE_ARN, "imageVersion": "0", "createdAt": T0},
                    {
                        "imageArn": BASE_IMAGE_ARN,
                        "imageVersion": "1",
                        "createdAt": datetime(2026, 9, 2, tzinfo=UTC),
                    },
                ]
            },
            {"imageIdentifier": BASE_IMAGE_ARN},
        )


def stub_quotas(stubs: Stubs, overrides: dict[str, float] | None = None) -> None:
    quotas = measured_quotas()
    for quota in quotas:
        if overrides and quota["QuotaCode"] in overrides:
            quota["Value"] = overrides[quota["QuotaCode"]]
    stubs.quotas.add_response("list_service_quotas", {"Quotas": quotas}, {"ServiceCode": "lambda"})


def simulation_groups(bucket: str | None) -> list[tuple[list[str], list[str]]]:
    connector = f"arn:aws:lambda:{REGION}:aws:network-connector:aws-network-connector:*"
    groups = [
        (list(_checks.IMAGE_ACTIONS), [IMAGE_ARN]),
        (list(_checks.WILDCARD_ACTIONS), []),
        (list(_checks.CONNECTOR_ACTIONS), [connector]),
    ]
    if bucket:
        groups.append(
            (list(_checks.BUCKET_OBJECT_ACTIONS), [f"arn:aws:s3:::{bucket}/rayito/images/*"])
        )
        groups.append((list(_checks.BUCKET_ACTIONS), [f"arn:aws:s3:::{bucket}"]))
    return groups


def stub_simulation(
    stubs: Stubs,
    *,
    source_arn: str = USER_ARN,
    bucket: str | None = BUCKET,
    decisions: dict[str, str] | None = None,
    scp_denied: set[str] | None = None,
) -> None:
    for actions, resources in simulation_groups(bucket):
        expected: dict[str, Any] = {"PolicySourceArn": source_arn, "ActionNames": actions}
        if resources:
            expected["ResourceArns"] = resources
        results = []
        for action in actions:
            result: dict[str, Any] = {
                "EvalActionName": action,
                "EvalDecision": (decisions or {}).get(action, "allowed"),
                "EvalResourceName": resources[0] if resources else "*",
            }
            if scp_denied and action in scp_denied:
                result["OrganizationsDecisionDetail"] = {"AllowedByOrganizations": False}
            results.append(result)
        stubs.iam.add_response(
            "simulate_principal_policy", {"EvaluationResults": results}, expected
        )


def stub_bucket(stubs: Stubs, *, region: str | None = REGION) -> None:
    response: dict[str, Any] = {} if region is None else {"BucketRegion": region}
    stubs.s3.add_response("head_bucket", response, {"Bucket": BUCKET})


def build_item(version: str) -> dict[str, Any]:
    return {
        "imageArn": IMAGE_ARN,
        "imageVersion": version,
        "buildId": "build-1",
        "buildState": "SUCCESSFUL",
        "architecture": "ARM_64",
        "chipset": "GRAVITON",
        "chipsetGeneration": "3",
        "createdAt": T0,
    }


def stub_image_gate(
    stubs: Stubs,
    *,
    active: str | None = "3.0",
    failed: str | None = None,
    image_state: str = "UPDATED",
    version_state: str = "SUCCESSFUL",
    status: str = "ACTIVE",
) -> None:
    image = image_summary(state=image_state, active=active, failed=failed)
    stubs.microvms.add_response("get_microvm_image", image, {"imageIdentifier": IMAGE_ARN})
    if active is None:
        return
    stubs.microvms.add_response("get_microvm_image", image, {"imageIdentifier": IMAGE_ARN})
    stubs.microvms.add_response(
        "get_microvm_image_version",
        {
            **version_item(int(float(active)), state=version_state, status=status),
            "stateReason": "ok",
        },
        {"imageIdentifier": IMAGE_ARN, "imageVersion": active},
    )
    stubs.microvms.add_response(
        "list_microvm_image_builds",
        {"items": [build_item(active)]},
        {"imageIdentifier": IMAGE_ARN, "imageVersion": active},
    )
    stubs.microvms.add_response(
        "get_microvm_image_build",
        {**build_item(active), "snapshotBuild": {"memorySnapshotSizeInBytes": 900}},
        {"imageIdentifier": IMAGE_ARN, "imageVersion": active, "buildId": "build-1"},
    )


def stub_image_missing(stubs: Stubs) -> None:
    stubs.microvms.add_client_error(
        "get_microvm_image",
        service_error_code="ResourceNotFoundException",
        service_message="not found",
        expected_params={"imageIdentifier": IMAGE_ARN},
    )


def stub_all_ok(stubs: Stubs) -> None:
    stub_identity(stubs)
    stub_managed(stubs)
    stub_quotas(stubs)
    stub_simulation(stubs)
    stub_bucket(stubs)
    stub_image_gate(stubs)


def next_minor(version: str) -> str:
    major, minor, _patch = version.split(".")
    return f"{major}.{int(minor) + 1}.0"


def health_response(**overrides: Any) -> health_pb2.HealthResponse:
    fields: dict[str, Any] = {
        "agent_ready": True,
        "kernel_ready": True,
        "agent_version": SDK_VERSION,
        "uptime_ms": 1234,
        "sandbox_id": "microvm-a",
        "resume_generation": 0,
        "imds_blocked": False,
        "hook_anomalies": 0,
    }
    fields.update(overrides)
    return health_pb2.HealthResponse(**fields)


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Sustituye `probe_health` por una respuesta fija y registra las sondas."""
    state: dict[str, Any] = {"response": health_response(), "calls": []}

    def fake_probe(plane: Any, info: Any, transport: Any, timeout: float) -> Any:
        state["calls"].append((info.sandbox_id, timeout))
        plane.create_auth_token(info.sandbox_id, ())
        response = state["response"]
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(_checks, "probe_health", fake_probe)
    return state


def run(runner: CliRunner, clients: Clients, *args: str) -> tuple[int, dict[str, Any], str]:
    result = runner.invoke(app, ["--json", "doctor", "--bucket", BUCKET, *args], obj=clients)
    document = json.loads(result.stdout) if result.stdout.strip() else {}
    return result.exit_code, document, result.stderr


def statuses(document: dict[str, Any]) -> dict[str, str]:
    return {check["name"]: check["status"] for check in document["checks"]}


def check(document: dict[str, Any], name: str) -> dict[str, Any]:
    return next(item for item in document["checks"] if item["name"] == name)


def test_quota_defaults_codes_exist_in_measured_json() -> None:
    by_code = {quota["QuotaCode"]: quota for quota in measured_quotas()}
    for code, (short_name, default) in _checks.QUOTA_DEFAULTS.items():
        assert code in by_code, f"{code} ({short_name}) no está en quotas.json"
        assert "microvm" in by_code[code]["QuotaName"].lower()
        assert by_code[code]["Adjustable"] is True
        assert by_code[code]["Value"] >= default
    assert _checks.quota_default("L-CD1C0CC4", "us-east-1") == 1024.0
    assert _checks.quota_default("L-CD1C0CC4", "eu-west-1") == 400.0
    assert _checks.quota_default("L-72E0D058", "us-east-1") == 10.0
    assert _checks.quota_default("L-535CA9B6", "us-east-1") == 5.0


def test_fresh_account_without_an_image(
    runner: CliRunner, clients: Clients, stubbed_clients: Stubs
) -> None:
    stub_identity(stubbed_clients)
    stub_managed(stubbed_clients)
    stub_quotas(stubbed_clients)
    stub_simulation(stubbed_clients)
    stub_bucket(stubbed_clients)
    stub_image_missing(stubbed_clients)
    code, document, _ = run(runner, clients)
    assert code == 1
    assert statuses(document) == {
        "credentials": "OK",
        "managed-images": "OK",
        "quotas": "OK",
        "iam-simulation": "OK",
        "bucket": "OK",
        "image-gate": "FAIL",
        "sandboxes": "OK",
        "token": "SKIP",
        "agent": "SKIP",
        "compatibility": "SKIP",
    }
    assert "rayito image publish" in check(document, "image-gate")["summary"]
    assert document["compatibility"][0]["sdk_series"] == "0.1"
    assert document["exit_code"] == 1 and document["launched_sandbox_id"] is None
    assert document["account"] == ACCOUNT_ID and document["principal_kind"] == "user"
    assert document["region"] == REGION and document["rayito"]
    assert check(document, "managed-images")["details"]["newest_version"]["imageVersion"] == "1"
    assert [c["name"] for c in document["checks"]] == list(_checks.CHECK_NAMES)


def test_reduced_quotas_and_implicit_deny_are_warnings(
    runner: CliRunner, clients: Clients, stubbed_clients: Stubs
) -> None:
    stub_identity(stubbed_clients)
    stub_managed(stubbed_clients)
    stub_quotas(stubbed_clients, {"L-535CA9B6": 1.0})
    stub_simulation(stubbed_clients, decisions={"lambda:SuspendMicrovm": "implicitDeny"})
    stub_bucket(stubbed_clients)
    stub_image_gate(stubbed_clients)
    code, document, _ = run(runner, clients)
    assert code == 0
    quotas = check(document, "quotas")
    assert quotas["status"] == "WARN" and "RunMicrovm rate 1 < 5" in quotas["summary"]
    simulation = check(document, "iam-simulation")
    assert simulation["status"] == "WARN" and "lambda:SuspendMicrovm" in simulation["summary"]
    assert "orientativa" in simulation["summary"]
    assert statuses(document)["image-gate"] == "OK"


def test_human_output_lists_every_check_and_the_table(
    runner: CliRunner, clients: Clients, stubbed_clients: Stubs
) -> None:
    stub_all_ok(stubbed_clients)
    result = runner.invoke(app, ["doctor", "--bucket", BUCKET], obj=clients)
    assert result.exit_code == 0, result.stderr
    for name in _checks.CHECK_NAMES:
        assert name in result.stdout
    assert "OK   credentials" in result.stdout
    assert "SKIP token" in result.stdout
    assert "no comprobado" in result.stdout
    assert "rayito doctor: 7 OK, 0 WARN, 0 FAIL, 3 SKIP" in result.stdout


def test_credentials_statuses(runner: CliRunner, clients: Clients, stubbed_clients: Stubs) -> None:
    stubbed_clients.sts.add_client_error(
        "get_caller_identity", service_error_code="ExpiredToken", service_message="expired"
    )
    stub_managed(stubbed_clients)
    stub_quotas(stubbed_clients)
    stub_bucket(stubbed_clients)
    stub_image_gate(stubbed_clients)
    code, document, _ = run(runner, clients)
    assert code == 1
    assert check(document, "credentials")["status"] == "FAIL"
    assert "ExpiredToken" in check(document, "credentials")["summary"]
    assert check(document, "iam-simulation")["status"] == "SKIP"


def test_credentials_warns_outside_supported_regions(
    runner: CliRunner, clients: Clients, stubbed_clients: Stubs
) -> None:
    clients.region = "sa-east-1"
    stub_identity(stubbed_clients)
    result = run_check(_checks.CHECKS[0], clients, DoctorContext())
    assert result.status == "WARN" and "sa-east-1" in result.summary


def test_managed_images_statuses(clients: Clients, stubbed_clients: Stubs) -> None:
    stub_managed(stubbed_clients, present=False)
    absent = run_check(_checks.CHECKS[1], clients, DoctorContext())
    assert absent.status == "WARN" and "al2023-1" in absent.summary
    stubbed_clients.microvms.add_client_error(
        "list_managed_microvm_images",
        service_error_code="AccessDeniedException",
        service_message="denied",
    )
    denied = run_check(_checks.CHECKS[1], clients, DoctorContext())
    assert denied.status == "FAIL" and "ListManagedMicrovmImages denegado" in denied.summary
    assert denied.details["action"] == "lambda:ListManagedMicrovmImages"
    assert denied.details["operation"] == "ListManagedMicrovmImages"


def test_quotas_skip_when_denied_and_warn_when_missing(
    clients: Clients, stubbed_clients: Stubs
) -> None:
    stubbed_clients.quotas.add_client_error(
        "list_service_quotas", service_error_code="AccessDeniedException", service_message="no"
    )
    denied = run_check(_checks.CHECKS[2], clients, DoctorContext())
    assert denied.status == "SKIP" and denied.details["action"] == "servicequotas:ListServiceQuotas"
    quotas = [q for q in measured_quotas() if q["QuotaCode"] != "L-F8BECE9C"]
    stubbed_clients.quotas.add_response(
        "list_service_quotas", {"Quotas": quotas}, {"ServiceCode": "lambda"}
    )
    missing = run_check(_checks.CHECKS[2], clients, DoctorContext())
    assert missing.status == "WARN" and "L-F8BECE9C" in missing.summary


def identity_context(arn: str = USER_ARN, bucket: str | None = BUCKET) -> DoctorContext:
    ctx = DoctorContext(bucket=bucket)
    ctx.account = ACCOUNT_ID
    ctx.principal_arn = arn
    ctx.principal_kind = _checks.principal_kind(arn)
    return ctx


def test_iam_simulation_statuses(clients: Clients, stubbed_clients: Stubs) -> None:
    spec = _checks.CHECKS[3]
    stub_simulation(stubbed_clients, decisions={"lambda:RunMicrovm": "explicitDeny"})
    explicit = run_check(spec, clients, identity_context())
    assert explicit.status == "FAIL" and "lambda:RunMicrovm" in explicit.summary
    stub_simulation(stubbed_clients, scp_denied={"s3:PutObject"})
    by_scp = run_check(spec, clients, identity_context())
    assert by_scp.status == "FAIL" and "s3:PutObject" in by_scp.summary
    assert by_scp.details["denied_by_organizations"] == ["s3:PutObject"]
    root = run_check(spec, clients, identity_context(f"arn:aws:iam::{ACCOUNT_ID}:root"))
    assert root.status == "SKIP" and "root" in root.summary
    stubbed_clients.iam.add_client_error(
        "get_role", service_error_code="AccessDeniedException", service_message="no"
    )
    no_get_role = run_check(spec, clients, identity_context(ROLE_SESSION_ARN))
    assert (
        no_get_role.status == "SKIP"
        and no_get_role.details["action"] == "iam:SimulatePrincipalPolicy"
    )
    stub_simulation(stubbed_clients, bucket=None)
    without_bucket = run_check(spec, clients, identity_context(bucket=None))
    assert without_bucket.status == "OK"
    assert len(without_bucket.details["results"]) == len(_checks.IMAGE_ACTIONS) + len(
        _checks.WILDCARD_ACTIONS
    ) + len(_checks.CONNECTOR_ACTIONS)


def test_assumed_role_uses_get_role_arn_with_path(clients: Clients, stubbed_clients: Stubs) -> None:
    stubbed_clients.iam.add_response(
        "get_role",
        {
            "Role": {
                "Path": "/aws-reserved/sso.amazonaws.com/",
                "RoleName": "AWSReservedSSO_Admin_abc",
                "RoleId": "AROAEXAMPLEROLEID0001",
                "Arn": ROLE_ARN,
                "CreateDate": T0,
            }
        },
        {"RoleName": "AWSReservedSSO_Admin_abc"},
    )
    stub_simulation(stubbed_clients, source_arn=ROLE_ARN)
    result = run_check(_checks.CHECKS[3], clients, identity_context(ROLE_SESSION_ARN))
    assert result.status == "OK"
    assert result.details["policy_source_arn"] == ROLE_ARN
    assert _checks.principal_kind(ROLE_SESSION_ARN) == "assumed-role"
    assert (
        _checks.principal_kind(f"arn:aws:sts::{ACCOUNT_ID}:federated-user/bob") == "federated-user"
    )
    assert _checks.principal_kind("garbage") == "other"


def test_bucket_statuses(clients: Clients, stubbed_clients: Stubs) -> None:
    spec = _checks.CHECKS[4]
    assert spec.denied_action == "s3:ListBucket"
    assert run_check(spec, clients, DoctorContext(bucket=None)).status == "SKIP"
    stub_bucket(stubbed_clients)
    same = run_check(spec, clients, DoctorContext(bucket=BUCKET))
    assert same.status == "OK" and same.details["bucket_region"] == REGION
    stub_bucket(stubbed_clients, region="eu-west-1")
    other = run_check(spec, clients, DoctorContext(bucket=BUCKET))
    assert other.status == "WARN" and "eu-west-1" in other.summary
    stub_bucket(stubbed_clients, region=None)
    unknown = run_check(spec, clients, DoctorContext(bucket=BUCKET))
    assert unknown.status == "WARN" and "x-amz-bucket-region" in unknown.summary
    stubbed_clients.s3.add_client_error(
        "head_bucket", service_error_code="403", service_message="Forbidden", http_status_code=403
    )
    forbidden = run_check(spec, clients, DoctorContext(bucket=BUCKET))
    assert forbidden.status == "FAIL" and "HeadBucket 403" in forbidden.summary
    assert "s3:ListBucket" in forbidden.summary and "s3:PutObject" in forbidden.summary
    assert forbidden.details["operation"] == "HeadBucket"


def test_image_gate_statuses(clients: Clients, stubbed_clients: Stubs) -> None:
    spec = _checks.CHECKS[5]
    stub_image_gate(stubbed_clients, failed="2.0")
    lingering = run_check(spec, clients, DoctorContext())
    assert lingering.status == "WARN" and "2.0" in lingering.summary
    stub_image_gate(stubbed_clients, version_state="SUCCESSFUL", status="INACTIVE")
    inactive = run_check(spec, clients, DoctorContext())
    assert inactive.status == "FAIL" and "INACTIVE" in inactive.summary
    stub_image_gate(stubbed_clients, active=None)
    no_version = run_check(spec, clients, DoctorContext())
    assert no_version.status == "FAIL" and "sin versión activa" in no_version.summary
    stub_image_gate(stubbed_clients, active="2.0")
    pinned = run_check(spec, clients, DoctorContext(template_version="2.0"))
    assert pinned.status == "OK" and pinned.details["version"] == "2.0"


def test_sandboxes_statuses(clients: Clients, fake_plane: FakeControlPlane) -> None:
    spec = _checks.CHECKS[6]
    assert run_check(spec, clients, DoctorContext()).status == "OK"
    fake_plane.items = [list_item("microvm-a"), list_item("microvm-b", "SUSPENDED")]
    fake_plane.infos["microvm-a"] = sandbox_info("microvm-a")
    ctx = DoctorContext()
    warn = run_check(spec, clients, ctx)
    assert warn.status == "WARN" and warn.details["running_ids"] == ["microvm-a"]
    assert ctx.target_sandbox is not None and ctx.target_sandbox.sandbox_id == "microvm-a"
    fake_plane.items = [list_item(f"microvm-{n}") for n in range(11)]
    fake_plane.infos["microvm-10"] = sandbox_info("microvm-10")
    assert run_check(spec, clients, DoctorContext()).status == "FAIL"


def test_token_and_agent_against_the_newest_running_sandbox(
    runner: CliRunner,
    clients: Clients,
    stubbed_clients: Stubs,
    fake_plane: FakeControlPlane,
    probe: dict[str, Any],
) -> None:
    stub_all_ok(stubbed_clients)
    fake_plane.items = [
        running("microvm-old", started_at=datetime(2026, 9, 15, 10, tzinfo=UTC)),
        running("microvm-new", started_at=datetime(2026, 9, 15, 12, tzinfo=UTC)),
    ]
    fake_plane.infos["microvm-new"] = running_info("microvm-new")
    code, document, _ = run(runner, clients)
    assert code == 0, document
    assert statuses(document)["sandboxes"] == "WARN"
    token = check(document, "token")
    assert token["status"] == "OK" and token["details"]["sandbox_id"] == "microvm-new"
    assert token["details"]["port"] == 8080 and token["details"]["ttl_minutes"] == 60
    agent = check(document, "agent")
    assert agent["status"] == "OK" and agent["details"]["agent_version"] == SDK_VERSION
    assert check(document, "compatibility")["status"] == "OK"
    assert probe["calls"] == [("microvm-new", 5.0)]
    assert len(fake_plane.tokens) == 1 and fake_plane.tokens[0][0] == "microvm-new"


def test_agent_warnings_and_failures(
    runner: CliRunner,
    clients: Clients,
    stubbed_clients: Stubs,
    fake_plane: FakeControlPlane,
    probe: dict[str, Any],
) -> None:
    fake_plane.items = [running("microvm-a")]
    fake_plane.infos["microvm-a"] = running_info("microvm-a")
    stub_all_ok(stubbed_clients)
    probe["response"] = health_response(kernel_ready=False, hook_anomalies=2)
    code, document, _ = run(runner, clients)
    assert code == 0
    agent = check(document, "agent")
    assert agent["status"] == "WARN"
    assert "kernel_ready" in agent["summary"] and "hook_anomalies=2" in agent["summary"]
    stub_all_ok(stubbed_clients)
    probe["response"] = SandboxException("Health no respondió (UNAVAILABLE)")
    code, document, _ = run(runner, clients)
    assert code == 1
    assert check(document, "agent")["status"] == "FAIL"
    assert check(document, "compatibility")["status"] == "SKIP"


def test_token_denied_is_fail(
    runner: CliRunner,
    clients: Clients,
    stubbed_clients: Stubs,
    fake_plane: FakeControlPlane,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rayito.exceptions import AuthenticationException

    fake_plane.items = [running("microvm-a")]
    fake_plane.infos["microvm-a"] = running_info("microvm-a")

    def denied(sandbox_id: str, ports: Any) -> str:
        raise AuthenticationException("not authorized", aws_code="AccessDeniedException")

    monkeypatch.setattr(fake_plane, "create_auth_token", denied)
    stub_all_ok(stubbed_clients)
    code, document, _ = run(runner, clients)
    assert code == 1
    token = check(document, "token")
    assert (
        token["status"] == "FAIL" and token["details"]["action"] == "lambda:CreateMicrovmAuthToken"
    )
    assert check(document, "agent")["status"] == "SKIP"


def test_compatibility_statuses(
    runner: CliRunner,
    clients: Clients,
    stubbed_clients: Stubs,
    fake_plane: FakeControlPlane,
    probe: dict[str, Any],
) -> None:
    fake_plane.items = [list_item("microvm-a", version="1.0")]
    fake_plane.infos["microvm-a"] = sandbox_info("microvm-a", version="1.0")
    stub_all_ok(stubbed_clients)
    code, document, _ = run(runner, clients)
    assert code == 0
    first_build = check(document, "compatibility")
    assert first_build["status"] == "OK" and "imagen 1.0" in first_build["summary"]
    assert first_build["details"]["image_version"] == "1.0"
    stub_all_ok(stubbed_clients)
    probe["response"] = health_response(agent_version="0.0.9")
    code, document, _ = run(runner, clients)
    assert code == 1
    old_agent = check(document, "compatibility")
    assert old_agent["status"] == "FAIL" and "0.0.9" in old_agent["summary"]
    fake_plane.items = [running("microvm-a")]
    fake_plane.infos["microvm-a"] = running_info("microvm-a")
    stub_all_ok(stubbed_clients)
    probe["response"] = health_response(agent_version=next_minor(SDK_VERSION))
    code, document, _ = run(runner, clients)
    assert code == 0
    assert check(document, "compatibility")["status"] == "WARN"


class FakeSandbox:
    def __init__(self, info: Any, *, health: Any) -> None:
        self.info = info
        self.sandbox_id = info.sandbox_id
        self._health = health
        self.killed = 0

    def get_health(self) -> Any:
        if isinstance(self._health, Exception):
            raise self._health
        return self._health

    def kill(self) -> bool:
        self.killed += 1
        return True


def test_launch_is_killed_whatever_happens(
    runner: CliRunner, clients: Clients, stubbed_clients: Stubs
) -> None:
    from rayito._sandbox_base import health_from_proto

    launched: list[FakeSandbox] = []
    captured: dict[str, Any] = {}

    def factory(template: str, **kwargs: Any) -> Any:
        captured.update(kwargs, template=template)
        sandbox = FakeSandbox(running_info("microvm-launched"), health=SandboxException("boom"))
        launched.append(sandbox)
        return sandbox

    clients.sandbox_factory = factory
    stub_all_ok(stubbed_clients)
    code, document, _ = run(runner, clients, "--launch")
    assert code == 1
    assert check(document, "token")["status"] == "OK"
    assert (
        check(document, "agent")["status"] == "FAIL"
        and "boom" in check(document, "agent")["summary"]
    )
    assert document["launched_sandbox_id"] == "microvm-launched"
    assert launched[0].killed == 1
    assert captured["timeout"] == 300 and captured["idle"] is None
    assert captured["logging"] == "disabled" and captured["metadata"] == {"rayito": "doctor"}
    assert (
        captured["template"] == "rayito-base" and captured["control_plane"] is clients.control_plane
    )

    def healthy(template: str, **kwargs: Any) -> Any:
        sandbox = FakeSandbox(
            running_info("microvm-launched"), health=health_from_proto(health_response())
        )
        launched.append(sandbox)
        return sandbox

    clients.sandbox_factory = healthy
    stub_all_ok(stubbed_clients)
    code, document, _ = run(runner, clients, "--launch")
    assert code == 0, document
    assert statuses(document)["agent"] == "OK" and statuses(document)["compatibility"] == "OK"
    assert check(document, "token")["details"]["source"] == "create()"
    assert launched[1].killed == 1


def test_launch_failure_is_a_token_fail(
    runner: CliRunner, clients: Clients, stubbed_clients: Stubs
) -> None:
    def factory(template: str, **kwargs: Any) -> Any:
        raise SandboxException("ServiceQuotaExceededException")

    clients.sandbox_factory = factory
    stub_all_ok(stubbed_clients)
    code, document, _ = run(runner, clients, "--launch")
    assert code == 1
    assert check(document, "token")["status"] == "FAIL"
    assert document["launched_sandbox_id"] is None


@pytest.fixture
def fake_rayd() -> Iterator[RaydEndpoint]:
    server, endpoint = start_fake_rayd(FakeRayd(agent_version=SDK_VERSION))
    try:
        yield endpoint
    finally:
        server.stop(grace=None)


def test_doctor_uses_the_probe(
    runner: CliRunner,
    clients: Clients,
    stubbed_clients: Stubs,
    fake_plane: FakeControlPlane,
    fake_rayd: RaydEndpoint,
) -> None:
    transport = TrackingTransport.for_loopback()
    clients.transport = cast(TransportSettings, transport)
    fake_plane.items = [list_item("microvm-a")]
    fake_plane.infos["microvm-a"] = running_info(
        "microvm-a", endpoint=f"{fake_rayd.host}:{fake_rayd.port}"
    )
    stub_all_ok(stubbed_clients)
    code, document, _ = run(runner, clients)
    assert code == 0, document
    assert check(document, "agent")["details"]["agent_version"] == SDK_VERSION
    assert fake_plane.tokens == [("microvm-a", (PortSpec.single(8080),))]
    assert len(fake_rayd.servicer.health_calls) == 1
    assert transport.open_count == 1 and transport.all_closed


def test_exception_in_one_check_does_not_stop_the_rest(
    runner: CliRunner, clients: Clients, stubbed_clients: Stubs, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(clients_: Any, ctx: Any) -> CheckResult:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(
        _checks,
        "PRE_LAUNCH_CHECKS",
        (CheckSpec("credentials", broken, "sts:GetCallerIdentity"), *_checks.PRE_LAUNCH_CHECKS[1:]),
    )
    monkeypatch.setattr("rayito.cli.doctor.PRE_LAUNCH_CHECKS", _checks.PRE_LAUNCH_CHECKS)
    stub_managed(stubbed_clients)
    stub_quotas(stubbed_clients)
    stub_bucket(stubbed_clients)
    stub_image_gate(stubbed_clients)
    code, document, _ = run(runner, clients)
    assert code == 1
    assert check(document, "credentials")["status"] == "FAIL"
    assert "RuntimeError: kaboom" in check(document, "credentials")["summary"]
    assert len(document["checks"]) == 10
    assert statuses(document)["image-gate"] == "OK"


def test_run_check_translates_every_status_kind() -> None:
    for status in STATUSES:
        assert status in _checks.CheckStatus.__args__  # type: ignore[attr-defined]
    denied = _checks.denied(_checks.CHECKS[2], "no")
    assert denied.status == "SKIP" and denied.details["error"] == "AccessDeniedException"
