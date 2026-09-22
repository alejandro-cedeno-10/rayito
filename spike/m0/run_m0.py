"""Rayito M0 runbook: measures AWS_API_NOTES.md section 16 against a real account.

Python port of the former run_m0.sh (which needed jq/zip/bc). Every AWS
parameter name used here appears literally in docs/aws-api/model_summary.md.

Run:
    uv run --with "boto3>=1.43.82" --with requests --with "httpx[http2]" \
        python spike/m0/run_m0.py <iam|image|run|probe|suspend|resume|streams|fleet|cleanup|all>

Environment: AWS_PROFILE, AWS_REGION and RAYITO_BUCKET (required; an existing
bucket in AWS_REGION that receives the probe zip), RAYITO_IMAGE_NAME,
RAYITO_STACK_NAME, RAYITO_LOG_GROUP, RAYITO_SUSPEND_SECONDS.

Each step appends raw API output to spike/m0/out/ and one summary line per
question id to spike/m0/out/results.jsonl; step state (ARNs, ids, token) lives
in spike/m0/out/state.json so steps can be re-run one at a time. spike/m0/out/
is git-ignored: it carries the account, ARNs, endpoints and MicroVM ids of
whoever runs it, so it is regenerated locally and never committed.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import io
import json
import os
import statistics
import sys
import threading
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import boto3
import requests
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError, WaiterError

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "out"
HOOK_PREFIX = "/aws/lambda-microvms/runtime/v1/"
APP_PORT = 8080
HOOK_PORT = 9000
LOOPBACK_PORT = 3000
PROXY_AUTH_HEADER = "X-aws-proxy-auth"
PROXY_PORT_HEADER = "X-aws-proxy-port"
PROXY_FORCE_H2_HEADER = "X-aws-proxy-force-h2"
TOKEN_REFRESH_AFTER_SECONDS = 50 * 60
IMAGE_HOOKS = {
    "port": HOOK_PORT,
    "microvmImageHooks": {
        "ready": "ENABLED",
        "readyTimeoutInSeconds": 900,
        "validate": "ENABLED",
        "validateTimeoutInSeconds": 600,
    },
    "microvmHooks": {
        "run": "ENABLED",
        "runTimeoutInSeconds": 30,
        "resume": "ENABLED",
        "resumeTimeoutInSeconds": 30,
        "suspend": "ENABLED",
        "suspendTimeoutInSeconds": 30,
        "terminate": "ENABLED",
        "terminateTimeoutInSeconds": 10,
    },
}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, round(fraction * (len(ordered) - 1))))
    return ordered[rank]


def summarize_latencies(values: list[float]) -> dict[str, Any]:
    return {
        "n": len(values),
        "p50": percentile(values, 0.5),
        "p95": percentile(values, 0.95),
        "min": min(values) if values else None,
        "max": max(values) if values else None,
        "mean": statistics.fmean(values) if values else None,
    }


def error_code(exc: ClientError) -> str:
    return exc.response.get("Error", {}).get("Code", "Unknown")


def error_summary(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {})
        return {
            "exception": error_code(exc),
            "http_status": exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode"),
            "message": str(error.get("Message", ""))[:300],
        }
    return {"exception": type(exc).__name__, "message": str(exc)[:300]}


def without_metadata(response: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in response.items() if key != "ResponseMetadata"}


@dataclass(frozen=True)
class Settings:
    region: str
    bucket: str
    image_name: str
    stack_name: str
    log_group: str
    suspend_seconds: int

    @classmethod
    def from_env(cls) -> Settings:
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        if not region:
            raise SystemExit("set AWS_REGION")
        bucket = os.environ.get("RAYITO_BUCKET")
        if not bucket:
            raise SystemExit("set RAYITO_BUCKET (an existing S3 bucket in AWS_REGION for the probe zip)")
        image_name = os.environ.get("RAYITO_IMAGE_NAME", "rayito-m0-probe")
        return cls(
            region=region,
            bucket=bucket,
            image_name=image_name,
            stack_name=os.environ.get("RAYITO_STACK_NAME", "rayito-m0-iam"),
            log_group=os.environ.get("RAYITO_LOG_GROUP", f"/rayito/{image_name}"),
            suspend_seconds=int(os.environ.get("RAYITO_SUSPEND_SECONDS", "300")),
        )

    @property
    def base_image_arn(self) -> str:
        return f"arn:aws:lambda:{self.region}:aws:microvm-image:al2023-1"

    def managed_connector(self, name: str) -> str:
        return f"arn:aws:lambda:{self.region}:aws:network-connector:aws-network-connector:{name}"

    @property
    def default_log_group_candidates(self) -> list[str]:
        return [self.log_group, f"/aws/lambda-microvms/{self.image_name}", f"/aws/lambda/microvms/{self.image_name}"]


class Journal:
    """Appends question summaries to results.jsonl and keeps step state in state.json."""

    def __init__(self, out_dir: Path) -> None:
        self.out_dir = out_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / ".gitignore").write_text("state.json\n", encoding="utf-8")
        self.results_path = out_dir / "results.jsonl"
        self.state_path = out_dir / "state.json"

    def record(self, question: str, **fields: Any) -> None:
        line = {"q": question, "at": utc_now(), **fields}
        with self.results_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, default=str) + "\n")
        print(f"[{question}] {json.dumps(fields, default=str)[:600]}", flush=True)

    def dump(self, name: str, payload: Any) -> Path:
        path = self.out_dir / name
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return path

    def _state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {}
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def save(self, key: str, value: Any) -> None:
        state = self._state()
        state[key] = value
        self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def load(self, key: str, default: Any = None) -> Any:
        return self._state().get(key, default)

    def require(self, key: str, step: str) -> Any:
        value = self.load(key)
        if value in (None, ""):
            raise SystemExit(f"state '{key}' missing: run the '{step}' step first")
        return value


class Aws:
    """boto3 clients; the variants exist only to observe raw server behaviour."""

    def __init__(self, region: str) -> None:
        session = boto3.session.Session(region_name=region)
        self.microvms = session.client("lambda-microvms")
        self.microvms_unvalidated = session.client("lambda-microvms", config=BotoConfig(parameter_validation=False))
        self.microvms_no_retry = session.client(
            "lambda-microvms", config=BotoConfig(retries={"total_max_attempts": 1, "mode": "standard"})
        )
        self.cloudformation = session.client("cloudformation")
        self.s3 = session.client("s3")
        self.quotas = session.client("service-quotas")
        self.logs = session.client("logs")
        self.account_id: str = session.client("sts").get_caller_identity()["Account"]


class VmClient:
    """HTTP client for one MicroVM endpoint through the AWS proxy."""

    def __init__(self, host: str, token: str) -> None:
        self.host = host
        self.token = token

    def url(self, path: str) -> str:
        return f"https://{self.host}{path}"

    def headers(self, port: int = APP_PORT, **extra: str) -> dict[str, str]:
        return {PROXY_AUTH_HEADER: self.token, PROXY_PORT_HEADER: str(port), **extra}

    def get(self, path: str, port: int = APP_PORT, timeout: float = 30, **kwargs: Any) -> requests.Response:
        return requests.get(self.url(path), headers=self.headers(port), timeout=timeout, **kwargs)

    def post(self, path: str, port: int = APP_PORT, timeout: float = 30, data: bytes | None = None) -> requests.Response:
        return requests.post(self.url(path), headers=self.headers(port), timeout=timeout, data=data)

    def get_json(self, path: str, port: int = APP_PORT, timeout: float = 30) -> Any:
        response = self.get(path, port, timeout)
        try:
            return response.json()
        except ValueError:
            return {"http_status": response.status_code, "text": response.text[:300]}

    def health_status(self, timeout: float = 5) -> int | str:
        try:
            return self.get("/health", timeout=timeout).status_code
        except requests.RequestException as exc:
            return type(exc).__name__

    def wait_healthy(self, timeout_s: float = 180, interval: float = 0.5) -> float:
        started = time.perf_counter()
        last: int | str = "not-tried"
        while time.perf_counter() - started < timeout_s:
            last = self.health_status()
            if last == 200:
                return time.perf_counter() - started
            time.sleep(interval)
        raise TimeoutError(f"{self.host} never answered 200 on /health (last: {last})")


@dataclass
class Context:
    settings: Settings
    aws: Aws
    journal: Journal

    @property
    def image_arn(self) -> str:
        return f"arn:aws:lambda:{self.settings.region}:{self.aws.account_id}:microvm-image:{self.settings.image_name}"

    def mint_token(self, microvm_id: str, minutes: int, allowed_ports: list[dict[str, Any]]) -> str:
        response = self.aws.microvms.create_microvm_auth_token(
            microvmIdentifier=microvm_id, expirationInMinutes=minutes, allowedPorts=allowed_ports
        )
        parts = response["authToken"]
        self.journal.save("token_keys", sorted(parts))
        return parts.get(PROXY_AUTH_HEADER) or next(iter(parts.values()))

    def vm(self) -> VmClient:
        host = self.journal.require("host", "run")
        minted_at = float(self.journal.load("token_minted_at", 0))
        if time.time() - minted_at > TOKEN_REFRESH_AFTER_SECONDS:
            self.refresh_token()
        return VmClient(host, self.journal.require("token", "run"))

    def refresh_token(self) -> None:
        token = self.mint_token(self.journal.require("microvm_id", "run"), 60, [{"allPorts": {}}])
        self.journal.save("token", token)
        self.journal.save("token_minted_at", time.time())

    def microvm_state(self, microvm_id: str) -> str:
        return self.aws.microvms.get_microvm(microvmIdentifier=microvm_id)["state"]

    def wait_state(self, microvm_id: str, wanted: str, timeout_s: float = 180, interval: float = 0.5) -> float:
        started = time.perf_counter()
        state = ""
        while time.perf_counter() - started < timeout_s:
            state = self.microvm_state(microvm_id)
            if state == wanted:
                return time.perf_counter() - started
            time.sleep(interval)
        raise TimeoutError(f"{microvm_id} is {state}, expected {wanted}")

    def terminate(self, microvm_id: str) -> dict[str, Any]:
        try:
            self.aws.microvms.terminate_microvm(microvmIdentifier=microvm_id)
            return {"terminated": microvm_id}
        except ClientError as exc:
            return {"terminate_failed": microvm_id, **error_summary(exc)}


# ---------------------------------------------------------------------- iam --


def deploy_stack(ctx: Context) -> dict[str, str]:
    cloudformation = ctx.aws.cloudformation
    request = {
        "StackName": ctx.settings.stack_name,
        "TemplateBody": (HERE / "iam.yaml").read_text(encoding="utf-8"),
        "Capabilities": ["CAPABILITY_NAMED_IAM"],
        "Parameters": [
            {"ParameterKey": "ArtifactBucket", "ParameterValue": ctx.settings.bucket},
            {"ParameterKey": "LogGroupPrefix", "ParameterValue": "/rayito"},
        ],
    }
    waiter_name = "stack_create_complete"
    try:
        cloudformation.create_stack(**request)
    except ClientError as exc:
        if error_code(exc) != "AlreadyExistsException":
            raise
        waiter_name = "stack_update_complete"
        if not start_update(cloudformation, request):
            waiter_name = ""
    if waiter_name:
        wait_for_stack(cloudformation, waiter_name, ctx)
    outputs = cloudformation.describe_stacks(StackName=ctx.settings.stack_name)["Stacks"][0].get("Outputs", [])
    ctx.journal.dump("iam.outputs.json", outputs)
    return {item["OutputKey"]: item["OutputValue"] for item in outputs}


def start_update(cloudformation: Any, request: dict[str, Any]) -> bool:
    try:
        cloudformation.update_stack(**request)
        return True
    except ClientError as exc:
        if "No updates are to be performed" in str(exc):
            print("stack unchanged", flush=True)
            return False
        raise


def wait_for_stack(cloudformation: Any, waiter_name: str, ctx: Context) -> None:
    try:
        cloudformation.get_waiter(waiter_name).wait(
            StackName=ctx.settings.stack_name, WaiterConfig={"Delay": 5, "MaxAttempts": 120}
        )
    except WaiterError:
        events = cloudformation.describe_stack_events(StackName=ctx.settings.stack_name)["StackEvents"]
        ctx.journal.dump("iam.events.json", events[:40])
        raise SystemExit(f"stack {ctx.settings.stack_name} failed; see out/iam.events.json")


def microvm_quotas(ctx: Context) -> dict[str, float]:
    paginator = ctx.aws.quotas.get_paginator("list_service_quotas")
    quotas = [
        quota
        for page in paginator.paginate(ServiceCode="lambda")
        for quota in page["Quotas"]
        if "microvm" in quota["QuotaName"].lower()
    ]
    ctx.journal.dump("quotas.json", quotas)
    return {quota["QuotaName"]: quota["Value"] for quota in quotas}


def step_iam(ctx: Context) -> None:
    outputs = deploy_stack(ctx)
    ctx.journal.save("build_role_arn", outputs["BuildRoleArn"])
    ctx.journal.save("execution_role_arn", outputs["ExecutionRoleArn"])
    ctx.journal.record("IAM", stack=ctx.settings.stack_name, build_role=outputs["BuildRoleArn"], execution_role=outputs["ExecutionRoleArn"])
    ctx.journal.record("Q9", quotas=microvm_quotas(ctx))


# -------------------------------------------------------------------- image --


def build_artifact_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in ("Dockerfile", "probe.py"):
            archive.write(HERE / "image" / name, arcname=name)
    return buffer.getvalue()


def upload_artifact(ctx: Context) -> str:
    payload = build_artifact_zip()
    key = f"rayito/m0/probe-{sha256_hex(payload)[:12]}.zip"
    ctx.aws.s3.put_object(Bucket=ctx.settings.bucket, Key=key, Body=payload)
    (ctx.journal.out_dir / "probe.zip").write_bytes(payload)
    print(f"uploaded s3://{ctx.settings.bucket}/{key} ({len(payload)} bytes)", flush=True)
    return f"s3://{ctx.settings.bucket}/{key}"


def image_exists(ctx: Context) -> bool:
    ctx.journal.record("IMAGE_IDENTIFIER", get_by_name=lookup_image(ctx, ctx.settings.image_name), get_by_arn=lookup_image(ctx, ctx.image_arn))
    return lookup_image(ctx, ctx.image_arn) == "found"


def lookup_image(ctx: Context, identifier: str) -> str:
    try:
        ctx.aws.microvms.get_microvm_image(imageIdentifier=identifier)
        return "found"
    except ClientError as exc:
        if error_code(exc) in ("ResourceNotFoundException", "ValidationException"):
            return f"{error_code(exc)}: {exc.response['Error'].get('Message', '')[:80]}"
        raise


def submit_image_build(ctx: Context, artifact_uri: str, environment: dict[str, str] | None = None) -> dict[str, Any]:
    common = {
        "baseImageArn": ctx.settings.base_image_arn,
        "buildRoleArn": ctx.journal.require("build_role_arn", "iam"),
        "codeArtifact": {"uri": artifact_uri},
        "resources": [{"minimumMemoryInMiB": 2048}],
        "cpuConfigurations": [{"architecture": "ARM_64"}],
        "hooks": IMAGE_HOOKS,
        "logging": {"cloudWatch": {"logGroup": ctx.settings.log_group}},
        "description": f"M0 probe {utc_now()}",
    }
    if environment:
        common["environmentVariables"] = environment
    if image_exists(ctx):
        response = ctx.aws.microvms.update_microvm_image(imageIdentifier=ctx.image_arn, **common)
        ctx.journal.dump("image.update.json", without_metadata(response))
    else:
        response = ctx.aws.microvms.create_microvm_image(name=ctx.settings.image_name, **common)
        ctx.journal.dump("image.create.json", without_metadata(response))
    return without_metadata(response)


def wait_for_version(ctx: Context, image_arn: str, version: str, timeout_s: float = 1800) -> dict[str, Any]:
    started = time.perf_counter()
    while True:
        detail = without_metadata(ctx.aws.microvms.get_microvm_image_version(imageIdentifier=image_arn, imageVersion=version))
        image_state = ctx.aws.microvms.get_microvm_image(imageIdentifier=image_arn)["state"]
        print(f"  image={image_state} version={version} state={detail['state']} status={detail['status']} ({time.perf_counter() - started:.0f}s)", flush=True)
        if detail["state"] in ("SUCCESSFUL", "FAILED") or time.perf_counter() - started > timeout_s:
            detail["imageState"] = image_state
            ctx.journal.dump("image.version.json", detail)
            return detail
        time.sleep(10)


def fetch_build(ctx: Context, image_arn: str, version: str) -> dict[str, Any]:
    builds = ctx.aws.microvms.list_microvm_image_builds(imageIdentifier=image_arn, imageVersion=version)["items"]
    ctx.journal.dump("image.builds.json", builds)
    if not builds:
        return {}
    build = without_metadata(
        ctx.aws.microvms.get_microvm_image_build(imageIdentifier=image_arn, imageVersion=version, buildId=builds[0]["buildId"])
    )
    ctx.journal.dump("image.build.json", build)
    return build


def dump_recent_logs(ctx: Context, file_name: str, streams_per_group: int = 5) -> list[str]:
    found: list[str] = []
    lines: list[str] = []
    for group in ctx.settings.default_log_group_candidates:
        try:
            streams = ctx.aws.logs.describe_log_streams(
                logGroupName=group, orderBy="LastEventTime", descending=True, limit=streams_per_group
            )["logStreams"]
        except ClientError as exc:
            if error_code(exc) != "ResourceNotFoundException":
                raise
            continue
        found.append(group)
        for stream in streams:
            events = ctx.aws.logs.get_log_events(logGroupName=group, logStreamName=stream["logStreamName"], limit=400)["events"]
            lines.append(f"===== {group} / {stream['logStreamName']} ({len(events)} events)")
            lines.extend(f"{event['timestamp']} {event['message'].rstrip()}" for event in events)
    (ctx.journal.out_dir / file_name).write_text("\n".join(lines), encoding="utf-8")
    return found


def step_image(ctx: Context) -> None:
    artifact_uri = upload_artifact(ctx)
    started = time.perf_counter()
    response = submit_image_build(ctx, artifact_uri)
    image_arn, version = response["imageArn"], response["imageVersion"]
    ctx.journal.save("image_arn", image_arn)
    print(f"building {image_arn} version {version} ...", flush=True)
    detail = wait_for_version(ctx, image_arn, version)
    build_seconds = time.perf_counter() - started
    build = fetch_build(ctx, image_arn, version)
    if detail["state"] != "SUCCESSFUL":
        groups = dump_recent_logs(ctx, "build.logs.txt")
        ctx.journal.record("Q21", build_failed=True, version_state=detail["state"], state_reason=detail.get("stateReason"), build_state_reason=build.get("stateReason"), log_groups_with_build_logs=groups)
        raise SystemExit(f"image build failed: {detail.get('stateReason')} / {build.get('stateReason')} (logs in out/build.logs.txt)")
    ctx.journal.save("image_version", version)
    ctx.journal.record(
        "Q21",
        build_seconds=round(build_seconds, 1),
        image_state=detail["imageState"],
        version_state=detail["state"],
        version_status=detail["status"],
        build_state=build.get("buildState"),
        chipset_generation=build.get("chipsetGeneration"),
        snapshot=build.get("snapshotBuild", {}),
    )


# ---------------------------------------------------------------------- run --


def run_request(ctx: Context, payload: str) -> dict[str, Any]:
    return {
        "imageIdentifier": ctx.journal.require("image_arn", "image"),
        "executionRoleArn": ctx.journal.require("execution_role_arn", "iam"),
        "ingressNetworkConnectors": [ctx.settings.managed_connector("ALL_INGRESS")],
        "egressNetworkConnectors": [ctx.settings.managed_connector("INTERNET_EGRESS")],
        "idlePolicy": {"autoResumeEnabled": True, "maxIdleDurationSeconds": 120, "suspendedDurationSeconds": 3600},
        "maximumDurationInSeconds": 7200,
        "runHookPayload": payload,
        "logging": {"cloudWatch": {"logGroup": ctx.settings.log_group}},
    }


def normalize_endpoint(endpoint: str) -> tuple[str, bool]:
    has_scheme = "://" in endpoint
    host = endpoint.split("://", 1)[1] if has_scheme else endpoint
    return host.rstrip("/"), has_scheme


def probe_payload_limit(ctx: Context, image_arn: str) -> dict[str, Any]:
    big = "a" * 4097
    try:
        response = ctx.aws.microvms_unvalidated.run_microvm(
            imageIdentifier=image_arn, runHookPayload=big, maximumDurationInSeconds=60
        )
    except ClientError as exc:
        return {"payload_4097": "rejected", **error_summary(exc)}
    ctx.terminate(response["microvmId"])
    return {"payload_4097": "accepted", "microvm_id": response["microvmId"]}


def probe_token_ttl(ctx: Context, microvm_id: str) -> dict[str, Any]:
    try:
        ctx.aws.microvms.create_microvm_auth_token(microvmIdentifier=microvm_id, expirationInMinutes=61, allowedPorts=[{"port": APP_PORT}])
    except ClientError as exc:
        return {"61_minutes": "rejected", **error_summary(exc)}
    return {"61_minutes": "accepted"}


def step_run(ctx: Context) -> None:
    payload = json.dumps({"v": 1, "token_sha256": sha256_hex(os.urandom(32)), "note": "m0"})
    request = run_request(ctx, payload)
    ctx.journal.dump("run.request.json", request)
    started = time.perf_counter()
    response = without_metadata(ctx.aws.microvms.run_microvm(**request))
    api_seconds = time.perf_counter() - started
    ctx.journal.dump("run.response.json", response)
    host, has_scheme = normalize_endpoint(response["endpoint"])
    ctx.journal.save("microvm_id", response["microvmId"])
    ctx.journal.save("endpoint", response["endpoint"])
    ctx.journal.save("host", host)
    ctx.journal.save("payload_sha256", sha256_hex(payload.encode()))
    ctx.journal.record("Q14", microvm_id=response["microvmId"], state_at_return=response["state"])
    ctx.journal.record("Q23", endpoint=response["endpoint"], has_scheme=has_scheme)
    ctx.refresh_token()
    ctx.journal.record("TOKEN_SHAPE", auth_token_keys=ctx.journal.load("token_keys"))
    first_200 = ctx.vm().wait_healthy() + api_seconds
    ctx.journal.record(
        "Q2",
        sample="main",
        run_api_seconds=round(api_seconds, 3),
        first_200_seconds=round(first_200, 3),
        state_after=ctx.microvm_state(response["microvmId"]),
    )
    ctx.journal.record("Q11", **probe_payload_limit(ctx, request["imageIdentifier"]))
    ctx.journal.record("TOKEN_TTL", **probe_token_ttl(ctx, response["microvmId"]))


# -------------------------------------------------------------------- probe --


def hook_summary(hooks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "hook": hook.get("hook"),
            "client": hook.get("client"),
            "http_version": hook.get("http_version"),
            "body_len": hook.get("body_len"),
            "user_agent": hook.get("headers", {}).get("User-Agent"),
            "header_names": sorted(hook.get("headers", {})),
        }
        for hook in hooks
    ]


def creds_summary(creds: dict[str, Any]) -> dict[str, Any]:
    return {
        "roles": creds.get("roles"),
        "creds": creds.get("creds"),
        "error": creds.get("error"),
        "env": creds.get("env"),
        "as": creds.get("as"),
    }


def probe_loopback_listener(vm: VmClient) -> dict[str, Any]:
    try:
        response = vm.get("/", port=LOOPBACK_PORT, timeout=15)
        return {"http_status": response.status_code, "body": response.text[:200]}
    except requests.RequestException as exc:
        return {"error": f"{type(exc).__name__}: {exc}"[:200]}


def probe_hooks_via_proxy(vm: VmClient) -> dict[str, Any]:
    try:
        response = vm.post(f"{HOOK_PREFIX}ready", port=HOOK_PORT, timeout=15)
        return {"post_ready_via_proxy_http": response.status_code, "body": response.text[:200]}
    except requests.RequestException as exc:
        return {"error": f"{type(exc).__name__}: {exc}"[:200]}


def probe_bandwidth(vm: VmClient, size: int = 16 * 1024 * 1024) -> dict[str, Any]:
    started = time.perf_counter()
    response = vm.get(f"/big?bytes={size}", timeout=120)
    seconds = time.perf_counter() - started
    return {"bytes": len(response.content), "seconds": round(seconds, 2), "MB_per_s": round(len(response.content) / seconds / 1e6, 2), "http_status": response.status_code}


def probe_http2(vm: VmClient) -> dict[str, Any]:
    try:
        import httpx
    except ImportError:
        return {"skipped": "httpx[http2] not installed (add --with 'httpx[http2]')"}
    result: dict[str, Any] = {}
    for label, extra in (("with_force_h2", {PROXY_FORCE_H2_HEADER: "true"}), ("without_force_h2", {})):
        try:
            with httpx.Client(http2=True, timeout=20) as client:
                response = client.get(vm.url("/echo"), headers=vm.headers(APP_PORT, **extra))
                body = response.json() if response.headers.get("content-type", "").startswith("application/json") else response.text[:200]
                result[label] = {"client_http_version": response.http_version, "status": response.status_code, "app_saw": body}
        except Exception as exc:  # noqa: BLE001 - the failure mode is the measurement
            result[label] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    return result


def step_probe(ctx: Context) -> None:
    vm = ctx.vm()
    journal = ctx.journal
    hooks = vm.get_json("/hooks")
    journal.dump("hooks.before.json", hooks)
    journal.record("HOOKS", phase="before", hooks=hook_summary(hooks))
    echo = vm.get_json("/echo")
    journal.dump("echo.json", echo)
    journal.record("PROXY_HEADERS", request_version=echo.get("request_version"), client=echo.get("client"), headers=echo.get("headers"))
    payload = vm.get_json("/run-payload")
    journal.record("RUN_PAYLOAD", received=payload, sha256_matches=payload.get("payload_sha256") == journal.load("payload_sha256"))
    identity = vm.get_json("/id")
    journal.dump("id.json", identity)
    journal.record("Q20", uid=identity.get("uid"), cap_eff=identity.get("cap_eff"), cgroup_writable=identity.get("cgroup_writable"), tools=identity.get("tools"), platform=identity.get("platform"), iptables=identity.get("iptables_list"), disk_root=identity.get("disk_root"), dockerenv=identity.get("dockerenv"))
    env = vm.get_json("/env")
    journal.dump("env.json", env)
    journal.record("ENV", aws=env.get("aws"))
    creds = vm.get_json("/creds")
    journal.dump("creds.before.json", creds)
    journal.record("Q1", phase="before", **creds_summary(creds))
    creds_uid = vm.get_json("/creds?as_uid=1000")
    journal.dump("creds.uid1000.before.json", creds_uid)
    journal.record("Q20", imds_as_uid1000=creds_summary(creds_uid))
    rand = vm.get_json("/rand")
    journal.save("rand_vm1", rand)
    journal.record("RNG", vm="vm1", **rand)
    vm.get_json("/bg/start")
    vm.get_json("/sleep/start?seconds=120")
    journal.record("KERNEL", info=vm.get_json("/kernel/info"), x_before=vm.get_json("/kernel/exec?code=x").get("results"))
    vm.get_json("/tcp/loopback/start")
    vm.get_json("/unix/start")
    journal.record("Q6", phase="before", outbound_connect=vm.get_json("/tcp/outbound/connect?host=example.com&port=80"), outbound_send=vm.get_json("/tcp/outbound/send"))
    vm.get_json(f"/listen127/start?port={LOOPBACK_PORT}")
    journal.record("Q18", **probe_loopback_listener(vm))
    journal.record("HOOKS_EXPOSED", **probe_hooks_via_proxy(vm))
    journal.record("BANDWIDTH", **probe_bandwidth(vm))
    journal.record("Q17", **probe_http2(vm))


# ----------------------------------------------------------- suspend/resume --


def step_suspend(ctx: Context) -> None:
    vm = ctx.vm()
    microvm_id = ctx.journal.require("microvm_id", "run")
    ctx.journal.dump("time.before.json", vm.get_json("/time"))
    started = time.perf_counter()
    ctx.aws.microvms.suspend_microvm(microvmIdentifier=microvm_id)
    seconds = ctx.wait_state(microvm_id, "SUSPENDED")
    ctx.journal.record("SUSPEND", seconds_to_SUSPENDED=round(time.perf_counter() - started, 2), poll_seconds=round(seconds, 2))
    ctx.journal.save("suspended_at", time.time())
    print(f"suspended; sleeping {ctx.settings.suspend_seconds}s so credentials and clocks drift ...", flush=True)
    time.sleep(ctx.settings.suspend_seconds)


def kernel_after_resume(vm: VmClient) -> dict[str, Any]:
    return {
        "alive": vm.get_json("/kernel/alive"),
        "x_existing_client": vm.get_json("/kernel/exec?code=x").get("results"),
        "x_fresh_client": vm.get_json("/kernel/fresh?code=x").get("results"),
    }


def probe_auto_resume(ctx: Context, vm: VmClient, microvm_id: str) -> dict[str, Any]:
    ctx.aws.microvms.suspend_microvm(microvmIdentifier=microvm_id)
    ctx.wait_state(microvm_id, "SUSPENDED")
    time.sleep(20)
    started = time.perf_counter()
    try:
        response = vm.get("/health", timeout=90)
        outcome: dict[str, Any] = {"http": response.status_code}
    except requests.RequestException as exc:
        outcome = {"error": f"{type(exc).__name__}: {exc}"[:200]}
    outcome["auto_resume_first_request_seconds"] = round(time.perf_counter() - started, 2)
    outcome["state_after"] = ctx.microvm_state(microvm_id)
    return outcome


def step_resume(ctx: Context) -> None:
    vm = ctx.vm()
    journal = ctx.journal
    microvm_id = journal.require("microvm_id", "run")
    started = time.perf_counter()
    ctx.aws.microvms.resume_microvm(microvmIdentifier=microvm_id)
    api_seconds = time.perf_counter() - started
    first_200 = vm.wait_healthy(interval=0.3) + api_seconds
    journal.record("Q4", resume_api_seconds=round(api_seconds, 3), resume_api_to_first_200_seconds=round(first_200, 3), token_minted_before_suspend_still_valid=True)
    clock = vm.get_json("/time")
    journal.dump("time.after.json", clock)
    journal.record("Q19", resume_delta=clock.get("resume_delta"), utc_in_vm=clock.get("utc"), utc_real=utc_now(), wall_minus_real_seconds=round(clock["wall"] - time.time(), 2) if "wall" in clock else None)
    creds = vm.get_json("/creds")
    journal.dump("creds.after.json", creds)
    journal.record("Q1", phase="after", **creds_summary(creds))
    creds_uid = vm.get_json("/creds?as_uid=1000")
    journal.dump("creds.uid1000.after.json", creds_uid)
    journal.record("Q1", phase="after_uid1000", **creds_summary(creds_uid))
    bg = vm.get_json("/bg/status")
    journal.dump("bg.after.json", bg)
    journal.record("Q5", bg={key: bg.get(key) for key in ("alive", "pipe_lines", "file_lines", "max_gap_between_ticks")}, sleep=vm.get_json("/sleep/status"))
    journal.record("Q7", **kernel_after_resume(vm))
    journal.record("Q6", phase="after", loopback=vm.get_json("/tcp/loopback/send"), unix=vm.get_json("/unix/send"), outbound=vm.get_json("/tcp/outbound/send"))
    hooks = vm.get_json("/hooks")
    journal.dump("hooks.after.json", hooks)
    journal.record("HOOKS", phase="after", hooks=hook_summary(hooks))
    journal.record("Q4", **probe_auto_resume(ctx, vm, microvm_id))


# ------------------------------------------------------------------ streams --


def consume_stream(vm: VmClient, path: str, outcome: dict[str, Any], read_timeout: float = 60) -> None:
    started = time.perf_counter()
    ticks = 0
    try:
        with requests.get(vm.url(path), headers=vm.headers(), stream=True, timeout=(10, read_timeout)) as response:
            outcome["http"] = response.status_code
            for line in response.iter_lines():
                if line.startswith(b"data:"):
                    ticks += 1
                    outcome["last_tick_at"] = round(time.perf_counter() - started, 1)
            outcome["ended"] = "server closed"
    except requests.RequestException as exc:
        outcome["ended"] = f"{type(exc).__name__}: {exc}"[:200]
    outcome["ticks"] = ticks
    outcome["seconds"] = round(time.perf_counter() - started, 1)


def probe_idle_with_open_stream(ctx: Context, vm: VmClient, microvm_id: str, polls: int = 26) -> dict[str, Any]:
    outcome: dict[str, Any] = {}
    worker = threading.Thread(target=consume_stream, args=(vm, "/stream?seconds=600", outcome), daemon=True)
    worker.start()
    timeline: list[str] = []
    for _ in range(polls):
        time.sleep(10)
        timeline.append(f"{time.strftime('%H:%M:%S')}={ctx.microvm_state(microvm_id)}")
    worker.join(timeout=70)
    return {"stream": outcome, "states": timeline}


def probe_connection_cap(vm: VmClient, connections: int = 9) -> dict[str, Any]:
    def one() -> str:
        try:
            with requests.Session() as session:
                response = session.get(vm.url("/stream?seconds=30"), headers=vm.headers(), stream=True, timeout=(10, 60))
                for _ in response.iter_lines():
                    pass
                return str(response.status_code)
        except requests.RequestException as exc:
            return type(exc).__name__

    with concurrent.futures.ThreadPoolExecutor(max_workers=connections) as pool:
        codes = list(pool.map(lambda _: one(), range(connections)))
    return {"connections": connections, "codes": {code: codes.count(code) for code in set(codes)}}


def probe_token_expiry_with_open_stream(ctx: Context, microvm_id: str) -> dict[str, Any]:
    short = VmClient(ctx.journal.require("host", "run"), ctx.mint_token(microvm_id, 1, [{"port": APP_PORT}]))
    outcome: dict[str, Any] = {}
    consume_stream(short, "/stream?seconds=150", outcome, read_timeout=30)
    return outcome


def step_streams(ctx: Context) -> None:
    vm = ctx.vm()
    microvm_id = ctx.journal.require("microvm_id", "run")
    ctx.journal.record("Q15", **probe_idle_with_open_stream(ctx, vm, microvm_id))
    ctx.journal.record("Q15", state_before_reconnect=ctx.microvm_state(microvm_id), reconnect_seconds=round(vm.wait_healthy(), 2))
    ctx.journal.record("Q16", **probe_connection_cap(vm))
    ctx.journal.record("TOKEN_EXPIRY_STREAM", **probe_token_expiry_with_open_stream(ctx, microvm_id))


# -------------------------------------------------------------------- fleet --


def fleet_request(ctx: Context, image_arn: str, **extra: Any) -> dict[str, Any]:
    return {
        "imageIdentifier": image_arn,
        "maximumDurationInSeconds": 600,
        "ingressNetworkConnectors": [ctx.settings.managed_connector("ALL_INGRESS")],
        "egressNetworkConnectors": [ctx.settings.managed_connector("INTERNET_EGRESS")],
        **extra,
    }


def launch_and_time(ctx: Context, client: Any, request: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        response = client.run_microvm(**request)
    except ClientError as exc:
        return {"ok": False, **error_summary(exc), "api_seconds": round(time.perf_counter() - started, 3)}
    api_seconds = time.perf_counter() - started
    host, _ = normalize_endpoint(response["endpoint"])
    vm = VmClient(host, ctx.mint_token(response["microvmId"], 10, [{"port": APP_PORT}]))
    try:
        first_200 = vm.wait_healthy() + api_seconds
    except TimeoutError as exc:
        return {"ok": False, "microvm_id": response["microvmId"], "exception": str(exc), "api_seconds": round(api_seconds, 3)}
    return {
        "ok": True,
        "microvm_id": response["microvmId"],
        "image_version": response["imageVersion"],
        "api_seconds": round(api_seconds, 3),
        "first_200_seconds": round(first_200, 3),
        "vm": vm,
    }


def sequential_launches(ctx: Context, image_arn: str, count: int = 20) -> None:
    latencies: list[float] = []
    for index in range(1, count + 1):
        result = launch_and_time(ctx, ctx.aws.microvms, fleet_request(ctx, image_arn))
        result.pop("vm", None)
        ctx.journal.record("Q2", sample=f"seq_{index}", **result)
        if result.get("microvm_id"):
            ctx.terminate(result["microvm_id"])
        if result.get("ok"):
            latencies.append(result["first_200_seconds"])
    ctx.journal.record("Q2", summary=summarize_latencies(latencies))


def parallel_launches(ctx: Context, image_arn: str, count: int = 20) -> None:
    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=count) as pool:
        results = list(pool.map(lambda _: launch_and_time(ctx, ctx.aws.microvms_no_retry, fleet_request(ctx, image_arn)), range(count)))
    elapsed = time.perf_counter() - started
    for result in results:
        result.pop("vm", None)
    ctx.journal.dump("fleet.parallel.json", results)
    ok = [result for result in results if result.get("ok")]
    throttled = [result for result in results if result.get("exception") == "ThrottlingException"]
    others = [result for result in results if not result.get("ok") and result.get("exception") != "ThrottlingException"]
    ctx.journal.record(
        "Q3",
        parallel_ok=len(ok),
        throttled=len(throttled),
        other_failures=[error for error in others][:5],
        wall_seconds=round(elapsed, 2),
        first_200=summarize_latencies([result["first_200_seconds"] for result in ok]),
        api_seconds=summarize_latencies([result["api_seconds"] for result in ok]),
    )
    for result in results:
        if result.get("microvm_id"):
            ctx.terminate(result["microvm_id"])
            time.sleep(0.12)


def clone_rng_check(ctx: Context, image_arn: str) -> None:
    request = fleet_request(ctx, image_arn, executionRoleArn=ctx.journal.require("execution_role_arn", "iam"), maximumDurationInSeconds=300)
    result = launch_and_time(ctx, ctx.aws.microvms, request)
    vm = result.pop("vm", None)
    if not vm:
        ctx.journal.record("RNG", vm="vm2", **result)
        return
    rand = vm.get_json("/rand")
    vm1 = ctx.journal.load("rand_vm1") or {}
    ctx.journal.record("RNG", vm="vm2", **rand, random_at_import_identical=rand.get("random_at_import") == vm1.get("random_at_import"), random_now_identical=rand.get("random_now") == vm1.get("random_now"))
    ctx.journal.save("microvm_id_vm2", result["microvm_id"])
    ctx.journal.record("Q12", vm2_without_logging_param=result["microvm_id"], hooks_seen=vm.get_json("/health").get("hooks_seen"))
    ctx.terminate(result["microvm_id"])


def step_fleet(ctx: Context) -> None:
    image_arn = ctx.journal.require("image_arn", "image")
    sequential_launches(ctx, image_arn)
    parallel_launches(ctx, image_arn)
    clone_rng_check(ctx, image_arn)


# ------------------------------------------------------------ failsuspend --


def watch_after_failed_suspend(ctx: Context, vm: VmClient, microvm_id: str, polls: int = 12) -> dict[str, Any]:
    timeline: list[str] = []
    for _ in range(polls):
        time.sleep(5)
        timeline.append(f"{ctx.microvm_state(microvm_id)}/{vm.health_status()}")
    detail = ctx.aws.microvms.get_microvm(microvmIdentifier=microvm_id)
    try:
        hooks = vm.get_json("/hooks")
    except requests.RequestException as exc:
        hooks = f"{type(exc).__name__}: {exc}"[:200]
    suspend_calls = [hook for hook in hooks if isinstance(hook, dict) and hook.get("hook") == "suspend"] if isinstance(hooks, list) else []
    return {
        "timeline_state/health": timeline,
        "final_state": detail["state"],
        "state_reason": detail.get("stateReason"),
        "suspend_hook_calls": len(suspend_calls),
        "suspend_hook_wall_gaps": [round(b["wall"] - a["wall"], 1) for a, b in zip(suspend_calls, suspend_calls[1:])],
        "hooks_endpoint_answer": hooks if not isinstance(hooks, list) else "list",
    }


def fail_suspend_version(ctx: Context) -> str:
    version = ctx.journal.load("failsuspend_version")
    if version:
        return version
    artifact_uri = upload_artifact(ctx)
    response = submit_image_build(ctx, artifact_uri, environment={"PROBE_FAIL_SUSPEND": "1"})
    version = response["imageVersion"]
    detail = wait_for_version(ctx, ctx.image_arn, version)
    if detail["state"] != "SUCCESSFUL":
        raise SystemExit(f"fail-suspend build failed: {detail.get('stateReason')}")
    ctx.journal.save("failsuspend_version", version)
    return version


def step_failsuspend(ctx: Context) -> None:
    """Q10: a version whose /suspend answers 500, then suspend-microvm and watch."""
    version = fail_suspend_version(ctx)
    request = fleet_request(ctx, ctx.image_arn, imageVersion=version, executionRoleArn=ctx.journal.require("execution_role_arn", "iam"))
    launch = launch_and_time(ctx, ctx.aws.microvms, request)
    vm = launch.pop("vm", None)
    if not vm:
        raise SystemExit(f"fail-suspend launch failed: {launch}")
    microvm_id = launch["microvm_id"]
    try:
        try:
            ctx.aws.microvms.suspend_microvm(microvmIdentifier=microvm_id)
            outcome: dict[str, Any] = {"suspend_api": "accepted"}
        except ClientError as exc:
            outcome = {"suspend_api": error_summary(exc)}
        ctx.journal.record("Q10", version=version, microvm_id=microvm_id, **outcome, **watch_after_failed_suspend(ctx, vm, microvm_id))
    finally:
        ctx.journal.record("CLEANUP", **ctx.terminate(microvm_id))
    deactivated = without_metadata(ctx.aws.microvms.update_microvm_image_version(imageIdentifier=ctx.image_arn, imageVersion=version, status="INACTIVE"))
    ctx.journal.dump("image.version2.inactive.json", deactivated)
    image = ctx.aws.microvms.get_microvm_image(imageIdentifier=ctx.image_arn)
    ctx.journal.record("VERSION_STATUS", version=version, status=deactivated["status"], state=deactivated["state"], latest_active_after=image.get("latestActiveImageVersion"))


# ------------------------------------------------------------ silentstream --


def step_silentstream(ctx: Context) -> None:
    """Q15b: an open stream that sends nothing for 600 s against a 120 s idle policy."""
    request = fleet_request(
        ctx,
        ctx.image_arn,
        executionRoleArn=ctx.journal.require("execution_role_arn", "iam"),
        idlePolicy={"autoResumeEnabled": True, "maxIdleDurationSeconds": 120, "suspendedDurationSeconds": 900},
        maximumDurationInSeconds=1200,
    )
    launch = launch_and_time(ctx, ctx.aws.microvms, request)
    vm = launch.pop("vm", None)
    if not vm:
        raise SystemExit(f"silent-stream launch failed: {launch}")
    microvm_id = launch["microvm_id"]
    try:
        outcome: dict[str, Any] = {}
        worker = threading.Thread(target=consume_stream, args=(vm, "/stream?seconds=600&interval=600", outcome, 400), daemon=True)
        worker.start()
        timeline: list[str] = []
        for _ in range(21):
            time.sleep(10)
            timeline.append(f"{time.strftime('%H:%M:%S')}={ctx.microvm_state(microvm_id)}")
        worker.join(timeout=30)
        ctx.journal.record("Q15", phase="silent_stream", image_version=launch.get("image_version"), stream=outcome, states=timeline, worker_alive=worker.is_alive())
        ctx.journal.record("Q15", phase="silent_stream_reconnect", state=ctx.microvm_state(microvm_id), reconnect_seconds=round(vm.wait_healthy(), 2), state_after=ctx.microvm_state(microvm_id))
    finally:
        ctx.journal.record("CLEANUP", **ctx.terminate(microvm_id))


# ------------------------------------------------------------------ cleanup --


def list_states(ctx: Context, image_arn: str) -> dict[str, int]:
    paginator = ctx.aws.microvms.get_paginator("list_microvms")
    states: dict[str, int] = {}
    for page in paginator.paginate(imageIdentifier=image_arn):
        for item in page["items"]:
            states[item["state"]] = states.get(item["state"], 0) + 1
    return states


def listed_state(ctx: Context, image_arn: str, microvm_id: str) -> str | None:
    paginator = ctx.aws.microvms.get_paginator("list_microvms")
    for page in paginator.paginate(imageIdentifier=image_arn):
        for item in page["items"]:
            if item["microvmId"] == microvm_id:
                return item["state"]
    return None


def terminate_stragglers(ctx: Context, image_arn: str) -> list[str]:
    paginator = ctx.aws.microvms.get_paginator("list_microvms")
    stragglers = [
        item["microvmId"]
        for page in paginator.paginate(imageIdentifier=image_arn)
        for item in page["items"]
        if item["state"] not in ("TERMINATING", "TERMINATED")
    ]
    for microvm_id in stragglers:
        ctx.terminate(microvm_id)
        time.sleep(0.12)
    return stragglers


def log_groups_seen(ctx: Context) -> dict[str, list[str]]:
    seen: dict[str, list[str]] = {}
    for prefix in ("/rayito", "/aws/lambda-microvms", "/aws/lambda/microvms"):
        for page in ctx.aws.logs.get_paginator("describe_log_groups").paginate(logGroupNamePrefix=prefix):
            for group in page["logGroups"]:
                streams = ctx.aws.logs.describe_log_streams(logGroupName=group["logGroupName"], orderBy="LastEventTime", descending=True, limit=10)["logStreams"]
                seen[group["logGroupName"]] = [stream["logStreamName"] for stream in streams]
    return seen


def step_cleanup(ctx: Context) -> None:
    image_arn = ctx.journal.load("image_arn")
    microvm_id = ctx.journal.load("microvm_id")
    if microvm_id:
        ctx.journal.record("CLEANUP", **ctx.terminate(microvm_id))
        time.sleep(5)
        ctx.journal.record("Q13", after_seconds=5, listed_state=listed_state(ctx, image_arn, microvm_id), get=without_metadata_state(ctx, microvm_id))
        time.sleep(60)
        ctx.journal.record("Q13", after_seconds=65, listed_state=listed_state(ctx, image_arn, microvm_id), get=without_metadata_state(ctx, microvm_id))
    if image_arn:
        stragglers = terminate_stragglers(ctx, image_arn)
        ctx.journal.record("CLEANUP", stragglers_terminated=stragglers, list_states=list_states(ctx, image_arn))
    groups = log_groups_seen(ctx)
    ctx.journal.dump("loggroups.json", groups)
    ctx.journal.record("Q12", log_groups={group: streams[:5] for group, streams in groups.items()})


def without_metadata_state(ctx: Context, microvm_id: str) -> dict[str, Any]:
    try:
        response = ctx.aws.microvms.get_microvm(microvmIdentifier=microvm_id)
    except ClientError as exc:
        return error_summary(exc)
    return {key: response.get(key) for key in ("state", "stateReason", "terminatedAt")}


# --------------------------------------------------------------------- main --

STEPS: dict[str, Callable[[Context], None]] = {
    "iam": step_iam,
    "image": step_image,
    "run": step_run,
    "probe": step_probe,
    "suspend": step_suspend,
    "resume": step_resume,
    "streams": step_streams,
    "fleet": step_fleet,
    "cleanup": step_cleanup,
    "failsuspend": step_failsuspend,
    "silentstream": step_silentstream,
}
ALL_STEPS = ("iam", "image", "run", "probe", "suspend", "resume", "streams", "fleet")


def run_all(ctx: Context) -> None:
    try:
        for name in ALL_STEPS:
            print(f"===== {name}", flush=True)
            STEPS[name](ctx)
    finally:
        print("===== cleanup", flush=True)
        step_cleanup(ctx)


def main(argv: list[str]) -> None:
    step_name = argv[1] if len(argv) > 1 else "all"
    if step_name != "all" and step_name not in STEPS:
        raise SystemExit(f"unknown step: {step_name} (choose from {', '.join(STEPS)} or all; failsuspend is a separate pass)")
    settings = Settings.from_env()
    ctx = Context(settings=settings, aws=Aws(settings.region), journal=Journal(OUT_DIR))
    if step_name == "all":
        run_all(ctx)
    else:
        STEPS[step_name](ctx)
    print(f"done: results in {ctx.journal.results_path}", flush=True)


if __name__ == "__main__":
    main(sys.argv)
