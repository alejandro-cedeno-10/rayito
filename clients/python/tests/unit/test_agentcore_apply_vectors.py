"""Las decisiones al aplicar `--target agentcore`
(`testdata/agentcore-deploy/apply-vectors.json`): el servicio sobre dobles de los
puertos, con la acción de la pila, la imagen y el runtime, o el error con sus
campos. Más el despliegue sin cambios, la imagen nueva y el runtime nuevo."""

from __future__ import annotations

import json
from typing import Any

import pytest

from rayito._agentcore._domain import runtime_log_group_for
from rayito._agentcore._models import ImageObservation, RuntimeDetails
from rayito._agentcore._service import AgentCoreDeployer
from rayito._stacks._condition import StackObservation
from rayito.exceptions import AgentCoreException
from tests.unit import fake_agentcore as fake

VECTORS: dict[str, Any] = json.loads((fake.DATA / "apply-vectors.json").read_text("utf-8"))
CASES: list[dict[str, Any]] = VECTORS["cases"]


def cloud_for(item: dict[str, Any]) -> fake.FakeCloud:
    stack = item["stack"]
    observation = (
        None
        if stack is None
        else StackObservation(fake.STACK_NAME, stack["status"], dict(stack["outputs"]), {})
    )
    image = item["image"]
    cloud = fake.cloud(
        stack=observation,
        image=None
        if image is None
        else ImageObservation(image["exists"], image.get("sizeBytes", 0)),
        listing=fake.listing_from(item["runtime"]),
        details=fake.details_from(item["runtimeDetails"]),
    )
    runtime = cloud.runtimes
    runtime.conflict_on_create = item.get("conflictOnCreate", False)
    if runtime.listing is not None and runtime.details is None:
        outputs = (stack or {"outputs": {}})["outputs"]
        desired = item["desired"]
        runtime.details = RuntimeDetails(
            container_uri=f"{outputs['RepositoryUri']}:{desired['imageTag']}",
            role_arn=outputs["RuntimeRoleArn"],
            network_mode="PUBLIC",
            server_protocol="HTTP",
            environment_variables=desired["environmentVariables"],
            idle_runtime_session_timeout=desired["idleRuntimeSessionTimeout"],
            max_lifetime=desired["maxLifetime"],
            require_mmds_v2=True,
        )
    return cloud


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_the_apply_vectors(case: dict[str, Any]) -> None:
    item = case["input"]
    cloud = cloud_for(item)
    desired = item["desired"]
    plan = fake.make_plan(
        image_tag=desired["imageTag"],
        environment=desired["environmentVariables"],
        idle=desired["idleRuntimeSessionTimeout"],
        maximum=desired["maxLifetime"],
    )
    deployer = AgentCoreDeployer(cloud.gateways, clock=cloud.clock, sleep=cloud.clock.sleep)
    request = fake.make_request(build_image=item["buildImage"])
    expected = case["expected"]
    if "error" in expected:
        with pytest.raises(AgentCoreException) as raised:
            deployer.apply(plan, request)
        error = expected["error"]
        assert raised.value.code == error["code"]
        for key, value in error.items():
            if key != "code":
                assert raised.value.fields[key] == value
        return
    deployment = deployer.apply(plan, request)
    actions = {step.resource: step.action for step in deployment.steps}
    assert actions == expected


def test_a_first_deploy_builds_once_creates_and_enforces_mmds_v2() -> None:
    cloud = fake.cloud()
    deployer = AgentCoreDeployer(cloud.gateways, clock=cloud.clock, sleep=cloud.clock.sleep)
    deployment = deployer.apply(fake.make_plan(), fake.make_request())
    names = cloud.recorder.names()
    assert names.count("create_runtime") == 1 and names.count("update_runtime") == 1
    assert names.index("create_runtime") < names.index("update_runtime")
    assert cloud.runtimes.specs[0] == cloud.runtimes.specs[1]
    assert cloud.runtimes._mmds is True
    assert deployment.status == "READY"
    assert deployment.log_group == runtime_log_group_for(fake.RUNTIME_ID)
    assert deployment.image_uri.endswith(f":{fake.IMAGE_TAG}")
    assert deployment.stack_name == fake.STACK_NAME


def test_the_source_zip_is_uploaded_before_the_build_and_deleted_after_it() -> None:
    cloud = fake.cloud()
    AgentCoreDeployer(cloud.gateways, clock=cloud.clock, sleep=cloud.clock.sleep).apply(
        fake.make_plan(), fake.make_request()
    )
    names = cloud.recorder.names()
    assert names.index("put_object") < names.index("start_build") < names.index("delete_object")
    assert cloud.bucket.objects == {}
    key = fake.make_plan().image_tag
    assert cloud.recorder.calls[names.index("put_object")][2].endswith(f"/{key}.zip")


def test_a_redeploy_without_changes_reports_three_reuses_and_writes_nothing() -> None:
    created = fake.cloud()
    plan = fake.make_plan()
    AgentCoreDeployer(created.gateways, clock=created.clock, sleep=created.clock.sleep).apply(
        plan, fake.make_request()
    )
    created.stack.changed = False
    created.recorder.calls.clear()
    created.runtimes.specs.clear()
    deployment = AgentCoreDeployer(
        created.gateways, clock=created.clock, sleep=created.clock.sleep
    ).apply(plan, fake.make_request())
    assert [step.action for step in deployment.steps] == ["reuse", "reuse", "reuse"]
    names = created.recorder.names()
    for write in ("put_object", "start_build", "create_runtime", "update_runtime"):
        assert write not in names


def test_a_concurrent_create_lists_again_and_updates_instead_of_failing() -> None:
    cloud = fake.cloud(stack=_ready_stack())
    cloud.runtimes.conflict_on_create = True
    cloud.registry.image = ImageObservation(True, fake.BUILT_IMAGE_BYTES)
    deployment = AgentCoreDeployer(
        cloud.gateways, clock=cloud.clock, sleep=cloud.clock.sleep
    ).apply(fake.make_plan(), fake.make_request())
    assert deployment.steps[2].action == "update"
    assert cloud.recorder.count("create_runtime") == 1
    assert cloud.recorder.count("update_runtime") == 1


def test_a_failed_build_deletes_the_zip_and_never_touches_the_runtime() -> None:
    cloud = fake.cloud()
    cloud.builder.statuses = ["IN_PROGRESS", "FAILED"]
    deployer = AgentCoreDeployer(cloud.gateways, clock=cloud.clock, sleep=cloud.clock.sleep)
    with pytest.raises(AgentCoreException) as raised:
        deployer.apply(fake.make_plan(), fake.make_request())
    assert raised.value.code == "image_build_failed"
    assert raised.value.fields["buildId"] == "build-1"
    assert raised.value.fields["logGroup"] == fake.DEFAULT_OUTPUTS["BuildLogGroupName"]
    names = cloud.recorder.names()
    assert "delete_object" in names
    assert not {"create_runtime", "update_runtime"} & set(names)


def test_a_build_that_outlives_its_timeout_is_image_build_timeout() -> None:
    cloud = fake.cloud()
    cloud.builder.statuses = ["IN_PROGRESS"]
    deployer = AgentCoreDeployer(cloud.gateways, clock=cloud.clock, sleep=cloud.clock.sleep)
    with pytest.raises(AgentCoreException) as raised:
        deployer.apply(fake.make_plan(), fake.make_request())
    assert raised.value.code == "image_build_timeout"
    assert "delete_object" in cloud.recorder.names()


def test_a_bucket_in_another_region_is_bucket_region_before_any_write() -> None:
    cloud = fake.cloud()
    cloud.bucket.regions = {fake.BUCKET: "eu-west-1"}
    deployer = AgentCoreDeployer(cloud.gateways, clock=cloud.clock, sleep=cloud.clock.sleep)
    with pytest.raises(AgentCoreException) as raised:
        deployer.apply(fake.make_plan(), fake.make_request())
    assert raised.value.code == "bucket_region"
    assert "create_stack" not in cloud.recorder.names()


def test_without_a_bucket_the_account_is_searched_and_failing_is_no_bucket() -> None:
    cloud = fake.cloud()
    cloud.bucket.discovered = None
    deployer = AgentCoreDeployer(cloud.gateways, clock=cloud.clock, sleep=cloud.clock.sleep)
    with pytest.raises(AgentCoreException) as raised:
        deployer.apply(fake.make_plan(bucket=None), fake.make_request())
    assert raised.value.code == "no_bucket"
    discovered = fake.cloud()
    deployer = AgentCoreDeployer(
        discovered.gateways, clock=discovered.clock, sleep=discovered.clock.sleep
    )
    deployer.apply(fake.make_plan(bucket=None), fake.make_request())
    assert discovered.recorder.count("discover") == 1
    assert discovered.stack.parameters_seen[0]["ArtifactBucket"] == fake.BUCKET


def test_the_stack_gets_the_role_resources_from_the_plan() -> None:
    cloud = fake.cloud()
    plan = fake.make_plan(models=("amazon.nova-lite-v1:0",), secrets=("rayito/agents/support/db",))
    AgentCoreDeployer(cloud.gateways, clock=cloud.clock, sleep=cloud.clock.sleep).apply(
        plan, fake.make_request()
    )
    parameters = cloud.stack.parameters_seen[0]
    assert parameters["BedrockModelArns"] == (
        f"arn:aws:bedrock:{fake.REGION}::foundation-model/amazon.nova-lite-v1:0"
    )
    assert parameters["ReadableSecretArns"] == (
        f"arn:aws:secretsmanager:{fake.REGION}:{fake.ACCOUNT}:secret:rayito/agents/support/db-??????"
    )
    assert parameters["AgentName"] == fake.AGENT and parameters["RuntimeName"] == fake.RUNTIME_NAME


def test_a_runtime_that_never_becomes_ready_is_runtime_timeout() -> None:
    cloud = fake.cloud()
    cloud.runtimes.ready_after_polls = 10**6
    deployer = AgentCoreDeployer(cloud.gateways, clock=cloud.clock, sleep=cloud.clock.sleep)
    with pytest.raises(AgentCoreException) as raised:
        deployer.apply(fake.make_plan(), fake.make_request())
    assert raised.value.code == "runtime_timeout"
    assert raised.value.fields["status"] == "CREATING"


def test_progress_lines_come_with_their_contract_keys() -> None:
    cloud = fake.cloud()
    seen: list[tuple[str, str]] = []
    AgentCoreDeployer(cloud.gateways, clock=cloud.clock, sleep=cloud.clock.sleep).apply(
        fake.make_plan(),
        fake.make_request(on_progress=lambda key, line: seen.append((key, line))),
    )
    keys = [key for key, _ in seen]
    assert keys[0] == "stackDeploy" and keys[-1] == "runtimeReady"
    assert {"sourceUpload", "buildStart", "imageReady", "sourceDeleted", "runtimeCreate"} <= set(
        keys
    )
    assert all("arn:" not in line for _, line in seen)


def test_build_phases_reach_on_build_logs() -> None:
    cloud = fake.cloud()
    phases: list[str] = []
    AgentCoreDeployer(cloud.gateways, clock=cloud.clock, sleep=cloud.clock.sleep).apply(
        fake.make_plan(), fake.make_request(on_build_logs=phases.append)
    )
    assert phases == ["BUILD"]


def _ready_stack() -> StackObservation:
    return StackObservation(fake.STACK_NAME, "UPDATE_COMPLETE", dict(fake.DEFAULT_OUTPUTS), {})


def test_a_runtime_that_is_not_rayitos_fails_before_the_stack_and_the_build() -> None:
    cloud = fake.cloud(
        listing=fake.listing_from(
            {
                "agentRuntimeId": fake.RUNTIME_ID,
                "agentRuntimeName": fake.RUNTIME_NAME,
                "agentRuntimeVersion": "1",
                "status": "READY",
                "description": "my own agent",
            }
        )
    )
    deployer = AgentCoreDeployer(cloud.gateways, clock=cloud.clock, sleep=cloud.clock.sleep)
    with pytest.raises(AgentCoreException) as raised:
        deployer.apply(fake.make_plan(), fake.make_request())
    assert raised.value.code == "name_conflict"
    assert not {"create_stack", "update_stack", "put_object", "start_build"} & set(
        cloud.recorder.names()
    )
