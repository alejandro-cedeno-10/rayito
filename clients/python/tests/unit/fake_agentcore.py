"""Dobles en memoria de los puertos de `--target agentcore`
(`rayito._agentcore._ports`) y los datos compartidos de
`testdata/agentcore-deploy/`: la cuenta, la pila, el repositorio, CodeBuild, el
control de AgentCore y los logs, con un registro común de las llamadas."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

from rayito._agentcore._domain import (
    runtime_description_for,
    runtime_log_group_for,
)
from rayito._agentcore._models import (
    AgentCoreApplyRequest,
    AgentCoreDeployPlan,
    AgentCoreLifecycle,
    AgentCoreStep,
    BuildObservation,
    ImageObservation,
    RuntimeDetails,
    RuntimeListing,
    RuntimeRef,
    RuntimeSpec,
)
from rayito._agentcore._ports import (
    AgentCoreGateways,
    InvokeResponse,
    RuntimeAlreadyExists,
    RuntimeDescription,
    RuntimeNotFound,
    StackDeployment,
)
from rayito._stacks._condition import StackObservation

DATA: Final = Path(__file__).resolve().parents[4] / "testdata" / "agentcore-deploy"
CONTRACT: Final[dict[str, Any]] = json.loads((DATA / "contract.json").read_text(encoding="utf-8"))
ACCOUNT: Final = "123456789012"
REGION: Final = "us-east-1"
BUCKET: Final = "amzn-s3-demo-bucket"
AGENT: Final = "support-bot"
RUNTIME_NAME: Final = "rayito_support_bot"
RUNTIME_ID: Final = "rayito_support_bot-a1b2c3d4e5"
STACK_NAME: Final = "rayito-agentcore-support-bot"
REPOSITORY_NAME: Final = "rayito/agentcore/support-bot"
IMAGE_TAG: Final = "src-0123456789abcdef"
BUILT_IMAGE_BYTES: Final = 800_000_000
DEFAULT_OUTPUTS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "RepositoryName": REPOSITORY_NAME,
        "RepositoryUri": f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/{REPOSITORY_NAME}",
        "RepositoryArn": f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/{REPOSITORY_NAME}",
        "BuildProjectName": STACK_NAME,
        "BuildLogGroupName": f"/aws/codebuild/{STACK_NAME}",
        "RuntimeRoleArn": f"arn:aws:iam::{ACCOUNT}:role/{STACK_NAME}-RuntimeRole-ABCDEFGHIJKL",
    }
)
Call = tuple[str, ...]


@dataclass
class Recorder:
    calls: list[Call] = field(default_factory=list)

    def record(self, *call: str) -> None:
        self.calls.append(call)

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]

    def count(self, name: str) -> int:
        return self.names().count(name)


@dataclass
class FakeClock:
    now: float = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class FakeIdentity:
    recorder: Recorder
    account: str = ACCOUNT

    def account_id(self) -> str:
        self.recorder.record("account")
        return self.account


@dataclass
class FakeBucket:
    recorder: Recorder
    discovered: str | None = BUCKET
    regions: dict[str, str] = field(default_factory=lambda: {BUCKET: REGION})
    objects: dict[str, bytes] = field(default_factory=dict)

    def discover(self) -> str | None:
        self.recorder.record("discover")
        return self.discovered

    def region_of(self, bucket: str) -> str | None:
        self.recorder.record("head_bucket", bucket)
        return self.regions.get(bucket)

    def put(self, bucket: str, key: str, data: bytes) -> None:
        self.recorder.record("put_object", bucket, key)
        self.objects[f"{bucket}/{key}"] = data

    def delete(self, bucket: str, key: str) -> None:
        self.recorder.record("delete_object", bucket, key)
        self.objects.pop(f"{bucket}/{key}", None)


@dataclass
class FakeStack:
    recorder: Recorder
    stack: StackObservation
    changed: bool = True
    created_outputs: Mapping[str, str] = field(default_factory=lambda: dict(DEFAULT_OUTPUTS))
    parameters_seen: list[Mapping[str, str]] = field(default_factory=list)
    destroyed: bool = False

    def observe(self) -> StackObservation:
        self.recorder.record("describe_stack")
        return self.stack

    def deploy(self, parameters: Mapping[str, str]) -> StackDeployment:
        self.parameters_seen.append(dict(parameters))
        creating = self.stack.status is None
        self.recorder.record("create_stack" if creating else "update_stack")
        outputs = dict(self.created_outputs) if creating else dict(self.stack.outputs)
        status = "CREATE_COMPLETE" if creating else str(self.stack.status)
        self.stack = StackObservation(self.stack.name, status, outputs, dict(parameters))
        return StackDeployment(self.stack, self.changed or creating)

    def wait(self) -> StackObservation:
        self.recorder.record("wait_stack")
        self.stack = replace(self.stack, status="UPDATE_COMPLETE")
        return self.stack

    def destroy(self) -> None:
        self.recorder.record("delete_stack")
        self.destroyed = True
        self.stack = StackObservation(self.stack.name, None, {}, {})


@dataclass
class FakeRegistry:
    recorder: Recorder
    image: ImageObservation = field(default_factory=lambda: ImageObservation(False))
    built: bool = False

    def describe(self, repository: str, image_tag: str) -> ImageObservation:
        self.recorder.record("describe_images", image_tag)
        if self.built:
            return ImageObservation(True, BUILT_IMAGE_BYTES)
        return self.image


@dataclass
class FakeBuilder:
    recorder: Recorder
    registry: FakeRegistry
    statuses: list[str] = field(default_factory=lambda: ["IN_PROGRESS", "SUCCEEDED"])
    log_group: str = DEFAULT_OUTPUTS["BuildLogGroupName"]
    polls: int = 0

    def start(
        self, project: str, bucket: str, key: str, image_tag: str, base_image: str
    ) -> BuildObservation:
        self.recorder.record("start_build", project, key, image_tag, base_image)
        return BuildObservation("build-1", "IN_PROGRESS", self.log_group)

    def get(self, build_id: str) -> BuildObservation:
        self.recorder.record("get_build", build_id)
        status = self.statuses[min(self.polls, len(self.statuses) - 1)]
        self.polls += 1
        if status == "SUCCEEDED":
            self.registry.built = True
        return BuildObservation(build_id, status, self.log_group, phase="BUILD")


@dataclass
class FakeRuntimes:
    """El control de AgentCore: un runtime opcional (más los de otros) que pasa
    de `CREATING` o `UPDATING` a `READY` en la primera lectura."""

    recorder: Recorder
    listing: RuntimeListing | None = None
    details: RuntimeDetails | None = None
    others: list[RuntimeListing] = field(default_factory=list)
    conflict_on_create: bool = False
    specs: list[RuntimeSpec] = field(default_factory=list)
    base_versions: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    ready_after_polls: int = 1
    _polls: int = 0
    _last_spec: RuntimeSpec | None = None
    _mmds: bool = False

    def list_runtimes(self) -> tuple[RuntimeListing, ...]:
        self.recorder.record("list_runtimes")
        own = [] if self.listing is None else [self.listing]
        return tuple([*own, *self.others])

    def get_runtime(self, runtime_id: str) -> RuntimeDescription:
        self.recorder.record("get_runtime", runtime_id)
        if self.listing is None or self.listing.runtime_id != runtime_id:
            raise RuntimeNotFound
        if self.listing.status in {"CREATING", "UPDATING"}:
            self._polls += 1
            if self._polls >= self.ready_after_polls:
                self.listing = replace(self.listing, status="READY")
        self.details = self._details_from_spec() or self.details
        if self.details is None:
            raise RuntimeNotFound
        return RuntimeDescription(self.listing, self.details)

    def _details_from_spec(self) -> RuntimeDetails | None:
        spec = self._last_spec
        if spec is None:
            return None
        return RuntimeDetails(
            container_uri=spec.container_uri,
            role_arn=spec.role_arn,
            network_mode="PUBLIC",
            server_protocol="HTTP",
            environment_variables=dict(spec.environment_variables),
            idle_runtime_session_timeout=spec.idle_runtime_session_timeout,
            max_lifetime=spec.max_lifetime,
            require_mmds_v2=self._mmds,
        )

    def create_runtime(self, spec: RuntimeSpec) -> RuntimeRef:
        self.recorder.record("create_runtime", spec.name)
        if self.conflict_on_create:
            self._polls = 0
            self.listing = RuntimeListing(
                RUNTIME_ID, _arn(RUNTIME_ID), spec.name, "1", "CREATING", spec.description
            )
            self.details = RuntimeDetails(
                container_uri="",
                role_arn="",
                network_mode="PUBLIC",
                server_protocol="HTTP",
                environment_variables={},
                idle_runtime_session_timeout=0,
                max_lifetime=0,
                require_mmds_v2=False,
            )
            raise RuntimeAlreadyExists
        self.specs.append(spec)
        self._last_spec = spec
        self._mmds = False
        self._polls = 0
        self.listing = RuntimeListing(
            RUNTIME_ID, _arn(RUNTIME_ID), spec.name, "1", "CREATING", spec.description
        )
        return RuntimeRef(RUNTIME_ID, _arn(RUNTIME_ID), "1", "CREATING")

    def update_runtime(self, runtime_id: str, spec: RuntimeSpec, base_version: str) -> RuntimeRef:
        self.recorder.record("update_runtime", runtime_id)
        self.base_versions.append(base_version)
        assert self.listing is not None
        assert self.listing.status not in {"CREATING", "UPDATING"}, (
            "UpdateAgentRuntime sobre un runtime que no está READY"
        )
        self.specs.append(spec)
        self._last_spec = spec
        self._mmds = True
        self._polls = 0
        version = str(int(self.listing.version) + 1)
        self.listing = replace(self.listing, version=version, status="UPDATING")
        return RuntimeRef(runtime_id, "", version, "UPDATING")

    def delete_runtime(self, runtime_id: str) -> None:
        self.recorder.record("delete_runtime", runtime_id)
        self.deleted.append(runtime_id)

    def runtime_exists(self, runtime_id: str) -> bool:
        self.recorder.record("runtime_exists", runtime_id)
        if runtime_id in self.deleted:
            self._polls += 1
            if self._polls >= self.ready_after_polls:
                self.listing = None
                return False
        return self.listing is not None


@dataclass
class FakeLogs:
    recorder: Recorder
    existing: set[str] = field(default_factory=set)

    def delete_log_group(self, log_group: str) -> bool:
        self.recorder.record("delete_log_group", log_group)
        return log_group in self.existing


def _arn(runtime_id: str) -> str:
    return f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/{runtime_id}"


@dataclass
class FakeCloud:
    """Los ocho dobles de un `AgentCoreGateways` con su registro común."""

    recorder: Recorder
    identity: FakeIdentity
    bucket: FakeBucket
    stack: FakeStack
    registry: FakeRegistry
    builder: FakeBuilder
    runtimes: FakeRuntimes
    logs: FakeLogs
    clock: FakeClock

    @property
    def gateways(self) -> AgentCoreGateways:
        return AgentCoreGateways(
            region=REGION,
            identity=self.identity,
            bucket=self.bucket,
            stack=self.stack,
            registry=self.registry,
            builder=self.builder,
            runtimes=self.runtimes,
            logs=self.logs,
        )


def cloud(
    *,
    stack: StackObservation | None = None,
    image: ImageObservation | None = None,
    listing: RuntimeListing | None = None,
    details: RuntimeDetails | None = None,
) -> FakeCloud:
    recorder = Recorder()
    registry = FakeRegistry(recorder, image or ImageObservation(False))
    return FakeCloud(
        recorder=recorder,
        identity=FakeIdentity(recorder),
        bucket=FakeBucket(recorder),
        stack=FakeStack(recorder, stack or StackObservation(STACK_NAME, None, {}, {})),
        registry=registry,
        builder=FakeBuilder(recorder, registry),
        runtimes=FakeRuntimes(recorder, listing, details),
        logs=FakeLogs(recorder, {runtime_log_group_for(RUNTIME_ID)}),
        clock=FakeClock(),
    )


def make_plan(
    *,
    image_tag: str = IMAGE_TAG,
    environment: Mapping[str, str] | None = None,
    idle: int = 900,
    maximum: int = 28800,
    bucket: str | None = BUCKET,
    models: tuple[str, ...] = ("us.anthropic.claude-haiku-4-5-20251001-v1:0",),
    secrets: tuple[str, ...] = (),
) -> AgentCoreDeployPlan:
    return AgentCoreDeployPlan(
        name=AGENT,
        region=REGION,
        runtime_name=RUNTIME_NAME,
        stack_name=STACK_NAME,
        repository_name=REPOSITORY_NAME,
        build_project_name=STACK_NAME,
        image_tag=image_tag,
        bucket=bucket,
        bucket_source="argument",
        network="PUBLIC",
        bedrock_models=models,
        readable_secrets=secrets,
        environment=MappingProxyType(
            dict(environment or {"AWS_DEFAULT_REGION": REGION, "AWS_REGION": REGION})
        ),
        lifecycle=AgentCoreLifecycle(idle, maximum),
        tags=MappingProxyType({}),
        steps=(
            AgentCoreStep("stack", STACK_NAME, "planned"),
            AgentCoreStep("image", REPOSITORY_NAME, "planned"),
            AgentCoreStep("runtime", RUNTIME_NAME, "planned"),
        ),
        security=("noGateway",),
        warnings=(),
        cost=(),
    )


def make_request(
    *,
    build_image: bool | None = None,
    tree: Mapping[str, bytes] | None = None,
    on_progress: Callable[[str, str], None] | None = None,
    on_build_logs: Callable[[str], None] | None = None,
) -> AgentCoreApplyRequest:
    return AgentCoreApplyRequest(
        tree=tree or {"Dockerfile": b"FROM scratch\n"},
        model_region=REGION,
        build_image=build_image,
        build_timeout_seconds=1800.0,
        on_progress=on_progress,
        on_build_logs=on_build_logs,
    )


def listing_from(item: Mapping[str, Any] | None) -> RuntimeListing | None:
    if item is None:
        return None
    return RuntimeListing(
        runtime_id=item["agentRuntimeId"],
        arn=_arn(item["agentRuntimeId"]),
        name=item["agentRuntimeName"],
        version=item["agentRuntimeVersion"],
        status=item["status"],
        description=item["description"],
    )


def details_from(item: Mapping[str, Any] | None) -> RuntimeDetails | None:
    if item is None:
        return None
    return RuntimeDetails(
        container_uri=item["containerUri"],
        role_arn=item["roleArn"],
        network_mode=item["networkMode"],
        server_protocol=item["serverProtocol"],
        environment_variables=dict(item["environmentVariables"]),
        idle_runtime_session_timeout=item["idleRuntimeSessionTimeout"],
        max_lifetime=item["maxLifetime"],
        require_mmds_v2=item["requireMMDSV2"],
    )


def runtime_description(name: str = AGENT) -> str:
    return runtime_description_for(name)


@dataclass
class FakeInvoker:
    """`RuntimeInvoker` que responde lo que se le diga y recuerda cada
    llamada: (tipo, ARN, id de sesión, cuerpo, `accept`)."""

    body: bytes
    calls: list[tuple[str, str, str, bytes, str]] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)
    closed: int = 0
    failure: Exception | None = None
    chunk_size: int = 64

    def _response(self) -> InvokeResponse:
        chunks = [
            self.body[start : start + self.chunk_size]
            for start in range(0, len(self.body), self.chunk_size)
        ]

        def close() -> None:
            self.closed += 1

        return InvokeResponse(iter(chunks), close)

    def invoke(
        self, runtime_arn: str, session_id: str, payload: bytes, accept: str
    ) -> InvokeResponse:
        self.calls.append(("invoke", runtime_arn, session_id, payload, accept))
        if self.failure is not None:
            raise self.failure
        return self._response()

    def stream(
        self, runtime_arn: str, session_id: str, payload: bytes, accept: str
    ) -> InvokeResponse:
        self.calls.append(("stream", runtime_arn, session_id, payload, accept))
        if self.failure is not None:
            raise self.failure
        return self._response()

    def stop_session(self, runtime_arn: str, session_id: str) -> bool:
        self.stopped.append(session_id)
        return True
