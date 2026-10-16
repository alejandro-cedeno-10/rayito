"""`deploy_agent()`, `deploy_agent_async()` y `plan_deploy()` (`_deploy_facade.py`,
`_deploy_plan.py`, `_aws_resolve.py`) sobre dobles: el orden de resolución de
cada valor contra los vectores compartidos con TypeScript
(`testdata/agent-deploy/`), que nada se cree sin pedirlo y que `plan_deploy()`
no llame a AWS. Ninguna prueba llama a AWS."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from rayito._agent._aws_resolve import (
    BUCKET_ENV_VAR,
    GATEWAY_SECRET_ENV_VARS,
    cdk_bucket_name,
    discover_bucket,
    resolve_bucket,
    resolve_token,
    secret_name_for,
    secret_value_from_env,
)
from rayito._agent._deploy_facade import (
    DeployPorts,
    deploy_agent,
    deploy_with,
    deploy_with_async,
    plan_deploy,
)
from rayito._agent._deploy_plan import (
    AGENT_SOURCE_DEFAULT_EXCLUDES,
    AgentDeployOptions,
    AgentDeployStep,
    estimate_cost,
)
from rayito._agent._detect_domain import DetectOverrides
from rayito._agent._fleet import AgentFleet
from rayito._agent._fleet_async import AsyncAgentFleet
from rayito._agent._project_domain import AgentProject
from rayito._agent._scaling_domain import ScalingOptions
from rayito._agent._template import AgentTemplate, AsyncAgentTemplate
from rayito._payload import generate_access_token
from rayito._sandbox_base import ACCESS_TOKEN_ENV_VAR
from rayito._templates._models import BuildInfo
from rayito.exceptions import InvalidArgumentException
from tests.unit.fake_agent_fleet import FleetFlavor
from tests.unit.fake_agent_project import project

VECTORS = Path(__file__).parents[4] / "testdata" / "agent-deploy"
RESOLVE = json.loads((VECTORS / "resolve.json").read_text(encoding="utf-8"))
SECRET_ENV = json.loads((VECTORS / "secret-env.json").read_text(encoding="utf-8"))
BUCKET = "amzn-s3-demo-bucket"
INFO = BuildInfo(template_id="arn", build_id="3.0/abc", alias="support-bot")


@dataclass
class FakeAws:
    """AWS de doble: lo que existe y lo que se le preguntó."""

    images: set[str] = field(default_factory=lambda: {"rayito-base-caps"})
    buckets: set[str] = field(default_factory=set)
    calls: list[str] = field(default_factory=list)
    resolved_region: str = "us-east-1"

    def account_id(self) -> str:
        self.calls.append("account")
        return "123456789012"

    def bucket_exists(self, bucket: str) -> bool:
        self.calls.append("bucket")
        return bucket in self.buckets

    def image_exists(self, name: str) -> bool:
        self.calls.append(f"image:{name}")
        return name in self.images


@dataclass
class FakeStore:
    existing: set[str] = field(default_factory=lambda: {"bedrock-key"})
    created: list[tuple[str, str]] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)

    def exists(self, name: str) -> bool:
        self.checked.append(name)
        return name in self.existing

    def create(self, name: str, value: str, *, metadata: Mapping[str, str] | None = None) -> None:
        self.created.append((name, value))
        self.existing.add(name)


class Harness:
    """Los puertos de doble y las dos formas del despliegue con una API
    síncrona."""

    def __init__(self, name: str, monkeypatch: pytest.MonkeyPatch) -> None:
        self.flav = FleetFlavor(name)
        self.aws = FakeAws()
        self.store = FakeStore()
        self.environ: dict[str, str] = {BUCKET_ENV_VAR: BUCKET}
        self.builds: list[dict[str, Any]] = []
        self.steps: list[AgentDeployStep] = []

        def build_sync(template: AgentTemplate, **kwargs: Any) -> BuildInfo:
            self.builds.append({"template": template, **kwargs})
            return INFO

        async def build_async(template: AsyncAgentTemplate, **kwargs: Any) -> BuildInfo:
            self.builds.append({"template": template, **kwargs})
            return INFO

        monkeypatch.setattr(AgentTemplate, "build", build_sync)
        monkeypatch.setattr(AsyncAgentTemplate, "build", build_async)

    def ports(self, the_project: AgentProject) -> DeployPorts:
        service = self.flav.service(the_project)
        return DeployPorts(
            aws=self.aws,
            store=self.store,
            environ=self.environ,
            service_kwargs={
                "sandbox_class": self.flav.classes,
                "transport_factory": service._transport_factory,
                "sleep": service._sleep,
                "clock": service._clock,
            },
            fleet_kwargs={
                "directory": self.flav.classes_directory(),
                "pool_factory": self.flav._pool_factory,
                "transport_factory": service._transport_factory,
                "clock": self.flav.clock,
                "sleep": self.flav.clock.asleep
                if self.flav.asynchronous
                else self.flav.clock.sleep,
            },
        )

    def deploy(
        self, the_project: AgentProject | None = None, *, options: AgentDeployOptions | None = None
    ) -> Any:
        the_project = the_project or project()
        chosen = options or AgentDeployOptions(on_step=self.steps.append)
        if options is not None and options.on_step is None:
            chosen = dataclasses.replace(options, on_step=self.steps.append)
        ports = self.ports(the_project)
        if self.flav.asynchronous:
            return asyncio.run(deploy_with_async(ports, project=the_project, options=chosen))
        return deploy_with(ports, project=the_project, options=chosen)

    @property
    def created_vms(self) -> int:
        return len(self.flav.classes.created)


@pytest.fixture(params=["sync", "async"])
def harness(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(request.param, monkeypatch)


# ------------------------------------------------------------ shared vectors


@pytest.mark.parametrize("case", RESOLVE["buckets"], ids=lambda case: case["name"])
def test_the_bucket_order_matches_the_shared_vectors(case: dict[str, Any]) -> None:
    environ = {BUCKET_ENV_VAR: case["environment"]} if case["environment"] else {}
    if case["expected"] is None:
        with pytest.raises(InvalidArgumentException, match="no crea buckets"):
            resolve_bucket(
                case["explicit"], case["projectFile"], environ, lambda: case["discovered"]
            )
        return

    choice = resolve_bucket(
        case["explicit"], case["projectFile"], environ, lambda: case["discovered"]
    )

    assert {"bucket": choice.bucket, "source": choice.source} == case["expected"]


def test_discovery_is_not_called_when_something_else_decides() -> None:
    def fail() -> str:
        raise AssertionError("no debía descubrir")

    assert resolve_bucket(BUCKET, None, {}, fail).bucket == BUCKET
    assert resolve_bucket(None, "from-file", {}, fail).bucket == "from-file"
    assert resolve_bucket(None, None, {BUCKET_ENV_VAR: "from-env"}, fail).bucket == "from-env"


def test_secret_names_follow_the_convention() -> None:
    for case in RESOLVE["secretNames"]:
        assert secret_name_for(case["project"], case["gateway"]) == case["name"]


@pytest.mark.parametrize(
    "case", RESOLVE["secretValues"], ids=lambda c: f"{c['gateway']}-{c['header']}"
)
def test_secret_values_come_from_the_standard_variable(case: dict[str, Any]) -> None:
    assert (
        secret_value_from_env(case["gateway"], case["header"], case["environment"])
        == (case["expected"])
    )


def test_the_provider_variable_table_is_the_shared_one() -> None:
    assert dict(GATEWAY_SECRET_ENV_VARS) == SECRET_ENV["gateways"]


def test_the_cdk_bucket_name_is_the_bootstrap_one() -> None:
    for case in RESOLVE["cdkBuckets"]:
        expected = case["template"].format(
            qualifier=case["qualifier"], account=case["account"], region=case["region"]
        )
        assert cdk_bucket_name(case["account"], case["region"]) == expected


def test_discovery_checks_the_cdk_bucket_and_never_creates_it() -> None:
    aws = FakeAws(buckets={cdk_bucket_name("123456789012", "us-east-1")})

    assert discover_bucket(aws, "us-east-1") == cdk_bucket_name("123456789012", "us-east-1")
    assert discover_bucket(FakeAws(), "us-east-1") is None


@pytest.mark.parametrize("case", RESOLVE["costs"], ids=lambda case: str(case["memoryMib"]))
def test_the_cost_estimate_matches_the_shared_vectors(case: dict[str, Any]) -> None:
    raw = case["scaling"]
    scaling = ScalingOptions(min_vms=raw["minVms"], max_vms=raw["maxVms"], warm_vms=raw["warmVms"])

    cost = estimate_cost(case["memoryMib"], scaling)

    assert round(cost.running_usd_per_hour, 6) == case["runningUsdPerHour"]
    assert round(cost.parked_usd_per_day, 6) == case["parkedUsdPerDay"]
    assert round(cost.image_build_usd, 6) == case["imageBuildUsd"]
    assert round(cost.image_storage_usd_per_week, 6) == case["imageStorageUsdPerWeek"]


def test_the_default_source_excludes_are_the_shared_list() -> None:
    assert list(AGENT_SOURCE_DEFAULT_EXCLUDES) == RESOLVE["sourceExcludes"]


def test_a_deploy_never_ships_the_token_or_the_environment_files(harness: Harness) -> None:
    harness.deploy(project(exclude=("dist", ".git")))

    template = harness.builds[0]["template"]
    assert ".rayito" in template.app_exclude
    assert ".env" in template.app_exclude
    assert template.app_exclude.count(".git") == 1
    assert "dist" in template.app_exclude


def test_the_token_is_the_argument_then_the_environment_then_a_new_one() -> None:
    explicit = generate_access_token()
    from_env = generate_access_token()

    assert resolve_token(explicit, {ACCESS_TOKEN_ENV_VAR: from_env}) == explicit
    assert resolve_token(None, {ACCESS_TOKEN_ENV_VAR: from_env}) == from_env
    assert resolve_token(None, {}) != resolve_token(None, {})


# ---------------------------------------------------------------- options


@pytest.mark.parametrize(
    "options",
    [
        {"bucket": ""},
        {"access_token": "corto"},
        {"secret_names": {"bedrock": ""}},
        {"create_secret_from_env": "si"},
        {"scaling": {"min_vms": 1}},
        {"build_timeout_seconds": 0},
        {"build_image": "no"},
    ],
)
def test_deploy_options_reject_invalid_values(options: dict[str, Any]) -> None:
    with pytest.raises(InvalidArgumentException):
        AgentDeployOptions(**options)


def test_deploy_options_default_to_nothing_that_costs() -> None:
    options = AgentDeployOptions()

    assert not options.create_secret_from_env
    assert options.base_publisher is None
    assert options.session_index is None
    assert options.build_image is None


# ------------------------------------------------------------- plan_deploy


def test_plan_deploy_calls_nothing_and_reports_the_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "rayito._aws.LazyClient.get", lambda self: pytest.fail("plan_deploy llamó a AWS")
    )
    the_project = project(memory_mib=4096)

    plan = plan_deploy(
        project=the_project,
        options=AgentDeployOptions(
            bucket=BUCKET, secret_names={"bedrock": "otra"}, scaling=ScalingOptions(max_vms=2)
        ),
        region="us-east-1",
    )

    assert plan.name == "support-bot"
    assert plan.memory_mib == 4096
    assert plan.region == "us-east-1"
    assert plan.scaling.max_vms == 2
    assert plan.bucket_source == "argument"
    assert [(item.gateway, item.source) for item in plan.secrets] == [("bedrock", "override")]
    assert plan.cost.running_usd_per_hour > 0
    assert any("memoria" in note for note in plan.notes)


def test_building_the_options_and_a_fleet_calls_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("se llamó a AWS al construir")

    monkeypatch.setattr("rayito._aws.LazyClient.get", refuse)
    monkeypatch.setattr("boto3.session.Session.client", refuse)

    AgentDeployOptions(create_secret_from_env=False)
    ScalingOptions(min_vms=1, max_vms=3, warm_vms=1)
    AgentFleet(project(), access_token=generate_access_token())
    AsyncAgentFleet(project(), access_token=generate_access_token())


def test_plan_deploy_names_where_each_value_would_come_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(BUCKET_ENV_VAR, BUCKET)
    monkeypatch.delenv(ACCESS_TOKEN_ENV_VAR, raising=False)

    plan = plan_deploy(project=project())

    assert plan.bucket_source == "environment"
    assert plan.token_source == "generated"
    assert plan.secrets[0].source == "project"


def test_plan_deploy_without_build_has_no_memory_note() -> None:
    plan = plan_deploy(project=project(), options=AgentDeployOptions(build_image=False))

    assert plan.notes == ()


def test_plan_deploy_uses_the_detector_when_the_project_has_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Any] = []

    def detect(path: Any, overrides: Any) -> tuple[AgentProject, object]:
        seen.append((str(path), overrides))
        return project(), {"framework": "langgraph"}

    monkeypatch.setattr(AgentProject, "detect", staticmethod(detect), raising=False)
    overrides = DetectOverrides(framework="langgraph")

    plan = plan_deploy("./mi-agente", overrides=overrides)

    assert seen == [("./mi-agente", overrides)]
    assert plan.detection == {"framework": "langgraph"}


def test_plan_deploy_falls_back_to_the_project_file(tmp_path: Path) -> None:
    (tmp_path / "rayito-agent.json").write_text(json.dumps(project().to_dict()))

    plan = plan_deploy(tmp_path)

    assert plan.name == "support-bot"
    assert plan.detection is None


# ---------------------------------------------------------------- deploy


def test_a_deploy_builds_once_and_starts_min_vms_with_the_resolved_token(
    harness: Harness,
) -> None:
    token = generate_access_token()
    harness.environ[ACCESS_TOKEN_ENV_VAR] = token

    fleet = harness.deploy(options=AgentDeployOptions(scaling=ScalingOptions(min_vms=2, max_vms=2)))

    assert len(harness.builds) == 1
    assert harness.builds[0]["bucket"] == BUCKET
    assert harness.builds[0]["template"].name == "support-bot"
    assert len(fleet.sandbox_ids) == 2
    assert fleet.access_token == token
    assert [step.kind for step in harness.steps] == ["bucket", "image", "fleet"]
    assert harness.steps[0].detail == f"environment:{BUCKET}"
    assert harness.steps[1].detail == "3.0"


def test_a_deploy_without_a_token_makes_one_and_the_fleet_returns_it(harness: Harness) -> None:
    fleet = harness.deploy()

    assert fleet.access_token
    assert all(
        args["access_token"] == fleet.access_token for _, args in harness.flav.classes.created
    )


def test_a_missing_base_image_stops_before_anything_is_created(harness: Harness) -> None:
    harness.aws.images.clear()

    with pytest.raises(InvalidArgumentException, match="rayito image publish"):
        harness.deploy()

    assert harness.builds == []
    assert harness.created_vms == 0
    assert harness.store.checked == []


def test_a_missing_base_image_is_published_only_with_a_publisher(harness: Harness) -> None:
    harness.aws.images.clear()
    published: list[bool] = []

    def publish() -> None:
        published.append(True)
        harness.aws.images.add("rayito-base-caps")

    harness.deploy(options=AgentDeployOptions(base_publisher=publish))

    assert published == [True]
    assert harness.steps[0] == AgentDeployStep("base_image", "published")


def test_a_publisher_that_does_not_publish_is_an_error(harness: Harness) -> None:
    harness.aws.images.clear()

    with pytest.raises(InvalidArgumentException, match="sigue sin existir"):
        harness.deploy(options=AgentDeployOptions(base_publisher=lambda: None))


def test_ensure_base_image_false_never_publishes(harness: Harness) -> None:
    harness.aws.images.clear()
    called: list[bool] = []

    with pytest.raises(InvalidArgumentException):
        harness.deploy(
            options=AgentDeployOptions(
                base_publisher=lambda: called.append(True), ensure_base_image=False
            )
        )

    assert called == []


def test_a_missing_secret_stops_before_the_build_and_names_the_options(harness: Harness) -> None:
    harness.store.existing.clear()

    with pytest.raises(InvalidArgumentException) as raised:
        harness.deploy()

    message = str(raised.value)
    assert "--secret bedrock=" in message
    assert "--create-secret" in message
    assert "AWS_BEARER_TOKEN_BEDROCK" in message
    assert "bedrock-key" not in message
    assert harness.builds == []
    assert harness.created_vms == 0


def test_create_secret_from_env_stores_the_value_and_never_prints_it(harness: Harness) -> None:
    harness.store.existing.clear()
    harness.environ["AWS_BEARER_TOKEN_BEDROCK"] = "valor-de-prueba"

    harness.deploy(options=AgentDeployOptions(create_secret_from_env=True))

    assert harness.store.created == [("bedrock-key", "Bearer valor-de-prueba")]
    assert AgentDeployStep("secret_created", "bedrock") in harness.steps
    assert all("valor-de-prueba" not in step.detail for step in harness.steps)


def test_create_secret_from_env_needs_the_variable(harness: Harness) -> None:
    harness.store.existing.clear()

    with pytest.raises(InvalidArgumentException, match="AWS_BEARER_TOKEN_BEDROCK"):
        harness.deploy(options=AgentDeployOptions(create_secret_from_env=True))

    assert harness.store.created == []


def test_secret_names_replace_the_name_of_the_project(harness: Harness) -> None:
    harness.store.existing = {"mi-propia-clave"}

    harness.deploy(options=AgentDeployOptions(secret_names={"bedrock": "mi-propia-clave"}))

    assert harness.store.checked == ["mi-propia-clave"]
    created = harness.flav.classes.created[0][1]
    assert created["gateways"]["bedrock"].headers["authorization"] == "mi-propia-clave"


def test_secret_names_for_an_unknown_gateway_fail(harness: Harness) -> None:
    with pytest.raises(InvalidArgumentException, match="pasarelas que el proyecto no tiene"):
        harness.deploy(options=AgentDeployOptions(secret_names={"openai": "x"}))


def test_without_a_bucket_an_existing_image_is_reused(harness: Harness) -> None:
    harness.environ.pop(BUCKET_ENV_VAR)
    harness.aws.images.add("support-bot")

    harness.deploy()

    assert harness.builds == []
    assert harness.created_vms == 1
    assert "bucket" not in [step.kind for step in harness.steps]


def test_without_a_bucket_and_without_an_image_it_fails_before_creating(
    harness: Harness,
) -> None:
    harness.environ.pop(BUCKET_ENV_VAR)

    with pytest.raises(InvalidArgumentException, match="no crea buckets"):
        harness.deploy()

    assert harness.created_vms == 0


def test_build_image_true_requires_a_bucket_even_if_the_image_exists(harness: Harness) -> None:
    harness.environ.pop(BUCKET_ENV_VAR)
    harness.aws.images.add("support-bot")

    with pytest.raises(InvalidArgumentException, match="no crea buckets"):
        harness.deploy(options=AgentDeployOptions(build_image=True))


def test_build_image_false_skips_the_build_and_the_base_check(harness: Harness) -> None:
    harness.aws.images.clear()
    harness.environ.pop(BUCKET_ENV_VAR)

    harness.deploy(options=AgentDeployOptions(build_image=False))

    assert harness.builds == []
    assert not any(call.startswith("image:") for call in harness.aws.calls)


def test_the_discovered_bucket_is_used_and_reported(harness: Harness) -> None:
    harness.environ.pop(BUCKET_ENV_VAR)
    discovered = cdk_bucket_name("123456789012", "us-east-1")
    harness.aws.buckets.add(discovered)

    harness.deploy()

    assert harness.builds[0]["bucket"] == discovered
    assert harness.steps[0].detail == f"discovered:{discovered}"


def test_the_base_image_version_and_timeout_reach_the_build(harness: Harness) -> None:
    harness.deploy(options=AgentDeployOptions(base_image_version="7", build_timeout_seconds=42))

    assert harness.builds[0]["timeout"] == 42
    assert harness.builds[0]["template"].base_version == "7"


def test_warm_slots_are_waited_for_and_reported(harness: Harness) -> None:
    scaling = ScalingOptions(min_vms=1, max_vms=3, warm_vms=1)

    fleet = harness.deploy(options=AgentDeployOptions(scaling=scaling))

    assert harness.steps[-1] == AgentDeployStep("warm", "ready")
    assert fleet.scaling == scaling


def test_keep_on_failure_reaches_the_vm_creation(harness: Harness) -> None:
    harness.deploy(options=AgentDeployOptions(keep_on_failure=True))

    assert harness.flav.classes.created[0][1]["access_token"]


def test_the_public_function_builds_its_own_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[DeployPorts] = []

    def fake_deploy_with(ports: DeployPorts, *args: Any, **kwargs: Any) -> str:
        seen.append(ports)
        return "flota"

    monkeypatch.setattr("rayito._agent._deploy_facade.deploy_with", fake_deploy_with)

    result: Any = deploy_agent(project=project())
    assert result == "flota"
    assert seen[0].aws is None
    assert seen[0].store is None
