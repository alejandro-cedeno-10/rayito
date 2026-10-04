"""`rayito._stacks._service.OptionalStacks` sobre un `StackProvisioner`
falso (M15 foundations): nunca se invoca implícitamente, un componente sin
plantilla lanza antes de tocar el provisioner, create/update/blocked siguen
`plan_deploy`, y los parámetros se validan antes de llamar a AWS."""

from __future__ import annotations

import pytest

from rayito._stacks._model import StackComponent, StackStatus
from rayito._stacks._packaging import load_template
from rayito._stacks._registry import COMPONENTS, component_by_name
from rayito._stacks._service import OptionalStacks
from rayito._stacks._service_async import AsyncOptionalStacks
from rayito.exceptions import InvalidArgumentException, StackException, UnimplementedError

from .fake_stacks import FakeStackProvisioner


def test_components_lists_the_full_catalog_with_no_provisioner_call() -> None:
    fake = FakeStackProvisioner()
    stacks = OptionalStacks(provisioner=fake)
    names = {c.name for c in stacks.components()}
    assert names == {c.name for c in COMPONENTS}
    assert fake.calls == []


def test_deploying_an_unsupported_component_raises_before_touching_the_provisioner() -> None:
    fake = FakeStackProvisioner()
    stacks = OptionalStacks(provisioner=fake)
    # `s3-mounts` is real since `m15-s3-mounts`; `efs-volumes` is still a
    # stub (`supported=False`) and makes the same point.
    with pytest.raises(UnimplementedError, match="efs-volumes"):
        stacks.deploy("efs-volumes")
    assert fake.calls == []


def test_destroying_an_unsupported_component_also_raises_first() -> None:
    fake = FakeStackProvisioner()
    stacks = OptionalStacks(provisioner=fake)
    with pytest.raises(UnimplementedError):
        stacks.destroy("efs-volumes")
    assert fake.calls == []


def test_an_unknown_component_name_is_invalid_argument() -> None:
    fake = FakeStackProvisioner()
    stacks = OptionalStacks(provisioner=fake)
    with pytest.raises(InvalidArgumentException, match="metadata-index"):
        stacks.status("not-a-real-component")
    assert fake.calls == []


def test_deploy_creates_a_stack_that_does_not_exist_yet() -> None:
    fake = FakeStackProvisioner()
    stacks = OptionalStacks(provisioner=fake)
    status = stacks.deploy("metadata-index")
    assert status.state == "CREATE_COMPLETE"
    assert [call[0] for call in fake.calls] == ["describe", "create", "wait", "describe"]


def test_deploy_updates_an_existing_stack() -> None:
    fake = FakeStackProvisioner(
        stacks={
            "rayito-metadata-index": StackStatus(
                name="rayito-metadata-index", state="CREATE_COMPLETE"
            )
        }
    )
    stacks = OptionalStacks(provisioner=fake)
    status = stacks.deploy("metadata-index")
    assert status.state == "UPDATE_COMPLETE"
    assert [call[0] for call in fake.calls] == ["describe", "update", "wait", "describe"]


def test_deploy_is_blocked_on_a_rollback_complete_stack() -> None:
    fake = FakeStackProvisioner(
        stacks={
            "rayito-metadata-index": StackStatus(
                name="rayito-metadata-index", state="ROLLBACK_COMPLETE"
            )
        }
    )
    stacks = OptionalStacks(provisioner=fake)
    with pytest.raises(StackException) as excinfo:
        stacks.deploy("metadata-index")
    assert excinfo.value.code == "blocked"
    assert [call[0] for call in fake.calls] == ["describe"]


def test_deploy_without_wait_skips_the_wait_call() -> None:
    fake = FakeStackProvisioner()
    stacks = OptionalStacks(provisioner=fake)
    stacks.deploy("metadata-index", wait=False)
    assert "wait" not in [call[0] for call in fake.calls]


def test_unknown_parameters_are_rejected_before_any_call() -> None:
    fake = FakeStackProvisioner()
    stacks = OptionalStacks(provisioner=fake)
    with pytest.raises(InvalidArgumentException, match="TotallyMadeUp"):
        stacks.deploy("metadata-index", parameters={"TotallyMadeUp": "x"})
    assert fake.calls == []


def test_deploy_passes_a_custom_stack_name_through() -> None:
    fake = FakeStackProvisioner()
    stacks = OptionalStacks(provisioner=fake)
    stacks.deploy("metadata-index", stack_name="mi-indice")
    assert fake.calls[0] == ("describe", "mi-indice")


def test_destroy_deletes_and_waits_by_default() -> None:
    fake = FakeStackProvisioner(
        stacks={
            "rayito-metadata-index": StackStatus(
                name="rayito-metadata-index", state="CREATE_COMPLETE"
            )
        }
    )
    stacks = OptionalStacks(provisioner=fake)
    stacks.destroy("metadata-index")
    assert [call[0] for call in fake.calls] == ["delete", "wait"]


def test_destroy_without_wait_skips_the_wait_call() -> None:
    fake = FakeStackProvisioner()
    stacks = OptionalStacks(provisioner=fake)
    stacks.destroy("metadata-index", wait=False)
    assert [call[0] for call in fake.calls] == ["delete"]


def test_status_is_a_plain_describe() -> None:
    fake = FakeStackProvisioner(
        stacks={
            "rayito-secrets-access": StackStatus(
                name="rayito-secrets-access", state="CREATE_COMPLETE"
            )
        }
    )
    stacks = OptionalStacks(provisioner=fake)
    status = stacks.status("secrets-access")
    assert status is not None
    assert status.state == "CREATE_COMPLETE"


def test_component_by_name_is_none_for_an_unknown_name() -> None:
    assert component_by_name("not-a-component") is None
    assert component_by_name("metadata-index") is not None


@pytest.mark.asyncio
async def test_async_optional_stacks_mirrors_the_sync_service() -> None:
    fake = FakeStackProvisioner()
    stacks = AsyncOptionalStacks(provisioner=fake)
    assert {c.name for c in stacks.components()} == {c.name for c in COMPONENTS}
    status = await stacks.deploy("metadata-index")
    assert status.state == "CREATE_COMPLETE"
    await stacks.destroy("metadata-index")
    assert "delete" in [call[0] for call in fake.calls]


#: Prefijo de tipo de todo recurso IAM de CloudFormation; una plantilla que
#: crea uno exige `CAPABILITY_IAM` (o `CAPABILITY_NAMED_IAM`) en
#: `CreateStack`/`UpdateStack`, o falla con `InsufficientCapabilitiesException`.
IAM_RESOURCE_TYPE_PREFIX = "AWS::IAM::"


@pytest.mark.parametrize(
    "component", [c for c in COMPONENTS if c.supported], ids=lambda component: component.name
)
def test_every_template_that_creates_iam_resources_declares_a_capability(
    component: StackComponent,
) -> None:
    if IAM_RESOURCE_TYPE_PREFIX in load_template(component):
        assert component.capabilities, f"{component.name} crea IAM sin declarar CAPABILITY_IAM"


# ------------------------------------------- artifact bucket as a parameter


class _RecordingProvisioner(FakeStackProvisioner):
    """Records the parameters `create()` received (the shared fake does not)."""

    created_parameters: dict[str, str] | None = None

    def create(self, component, *, stack_name, template_body, parameters, tags):  # type: ignore[no-untyped-def]
        self.created_parameters = dict(parameters)
        super().create(
            component,
            stack_name=stack_name,
            template_body=template_body,
            parameters=parameters,
            tags=tags,
        )


def test_events_webhooks_gets_its_bucket_parameter_from_artifact_bucket() -> None:
    """`rayito stack deploy events-webhooks --artifact-bucket B` (no
    `--param ArtifactBucket=B`) found in the 0.6 AWS acceptance: the
    template's `ArtifactBucket` is the bucket the code was uploaded to."""
    provisioner = _RecordingProvisioner()
    OptionalStacks(provisioner=provisioner).deploy(
        "events-webhooks", parameters={"LogGroupName": "/rayito/x"}, artifact_bucket="bucket-a"
    )
    assert provisioner.created_parameters is not None
    assert provisioner.created_parameters["ArtifactBucket"] == "bucket-a"
    assert provisioner.created_parameters["ArtifactS3Key"]
    assert ("put_artifact", "bucket-a", provisioner.created_parameters["ArtifactS3Key"]) in (
        provisioner.calls
    )


def test_a_conflicting_bucket_parameter_is_rejected_before_any_upload() -> None:
    provisioner = FakeStackProvisioner()
    with pytest.raises(InvalidArgumentException, match="ArtifactBucket"):
        OptionalStacks(provisioner=provisioner).deploy(
            "events-webhooks",
            parameters={"LogGroupName": "/rayito/x", "ArtifactBucket": "bucket-b"},
            artifact_bucket="bucket-a",
        )
    assert provisioner.calls == []


def test_a_component_with_artifacts_still_needs_artifact_bucket() -> None:
    with pytest.raises(InvalidArgumentException, match="artifact_bucket"):
        OptionalStacks(provisioner=FakeStackProvisioner()).deploy(
            "events-webhooks", parameters={"LogGroupName": "/rayito/x"}
        )


# ------------------------------- redeploy keeps the settings already deployed


def _redeploy(
    component: str, first: dict[str, str], second: dict[str, str]
) -> FakeStackProvisioner:
    """Deploys `component` with `first`, then again with only `second`."""
    fake = FakeStackProvisioner()
    stacks = OptionalStacks(provisioner=fake)
    stacks.deploy(component, parameters=first)
    stacks.deploy(component, parameters=second)
    return fake


def test_redeploying_s3_mounts_keeps_prefixes_and_read_only() -> None:
    """Redeploying with only `BucketName` must not widen the policy back to
    `Prefixes='*'` nor turn `ReadOnly` back on."""
    fake = _redeploy(
        "s3-mounts",
        {"BucketName": "b", "Prefixes": "team7/*", "ReadOnly": "false"},
        {"BucketName": "b"},
    )
    assert fake.sent_parameters == {"BucketName": "b"}
    assert fake.sent_keep_previous == ("Prefixes", "ReadOnly")
    deployed = fake.stacks["rayito-s3-mounts"].parameters
    assert deployed["Prefixes"] == "team7/*"
    assert deployed["ReadOnly"] == "false"


def test_redeploying_s3_mounts_without_the_required_bucket_keeps_it() -> None:
    fake = _redeploy("s3-mounts", {"BucketName": "b"}, {"Prefixes": "a/*"})
    assert fake.sent_keep_previous == ("BucketName", "ReadOnly")
    assert fake.stacks["rayito-s3-mounts"].parameters["BucketName"] == "b"


def test_redeploying_metadata_index_keeps_table_name_and_protection() -> None:
    """`TableName` forces a replacement (`UpdateReplacePolicy: Delete`): going
    back to the default would delete the old table and its rows."""
    fake = _redeploy(
        "metadata-index",
        {"TableName": "my-table", "DeletionProtection": "true", "PointInTimeRecovery": "true"},
        {},
    )
    assert fake.sent_parameters == {}
    assert fake.sent_keep_previous == ("DeletionProtection", "PointInTimeRecovery", "TableName")
    assert fake.stacks["rayito-metadata-index"].parameters == {
        "TableName": "my-table",
        "DeletionProtection": "true",
        "PointInTimeRecovery": "true",
    }


def test_redeploying_secrets_access_keeps_the_kms_key() -> None:
    fake = _redeploy("secrets-access", {"KmsKeyArn": "arn:aws:kms:example"}, {})
    assert "KmsKeyArn" in fake.sent_keep_previous
    assert fake.stacks["rayito-secrets-access"].parameters["KmsKeyArn"] == "arn:aws:kms:example"


def test_a_parameter_passed_again_on_update_is_sent_with_its_value() -> None:
    fake = _redeploy("secrets-access", {"KmsKeyArn": "arn:aws:kms:example"}, {"KmsKeyArn": ""})
    assert fake.sent_parameters == {"KmsKeyArn": ""}
    assert fake.sent_keep_previous == ("SecretPrefix",)


def test_a_parameter_the_existing_stack_lacks_gets_its_default_on_update() -> None:
    """A template version that adds a parameter: the old stack does not have
    it, so `UsePreviousValue` would fail; it gets the catalog default."""
    fake = FakeStackProvisioner(
        stacks={
            "rayito-metadata-index": StackStatus(
                name="rayito-metadata-index",
                state="CREATE_COMPLETE",
                parameters={"TableName": "my-table"},
            )
        }
    )
    OptionalStacks(provisioner=fake).deploy("metadata-index")
    assert fake.sent_keep_previous == ("TableName",)
    assert fake.sent_parameters == {"DeletionProtection": "false", "PointInTimeRecovery": "false"}


def test_create_still_fills_the_catalog_defaults() -> None:
    fake = FakeStackProvisioner()
    OptionalStacks(provisioner=fake).deploy("s3-mounts", parameters={"BucketName": "b"})
    assert fake.sent_parameters == {"BucketName": "b", "Prefixes": "*", "ReadOnly": "true"}
    assert fake.sent_keep_previous == ()


def test_create_without_a_required_parameter_is_rejected_before_creating() -> None:
    fake = FakeStackProvisioner()
    with pytest.raises(InvalidArgumentException, match="BucketName"):
        OptionalStacks(provisioner=fake).deploy("s3-mounts")
    assert [call[0] for call in fake.calls] == ["describe"]


def test_parameter_changes_lists_only_what_deploy_would_change() -> None:
    fake = FakeStackProvisioner()
    stacks = OptionalStacks(provisioner=fake)
    stacks.deploy("s3-mounts", parameters={"BucketName": "b", "Prefixes": "team7/*"})
    changes = stacks.parameter_changes("s3-mounts", parameters={"ReadOnly": "false"})
    assert [(c.name, c.before, c.after) for c in changes] == [("ReadOnly", "true", "false")]
    assert fake.calls[-1][0] == "describe"


def test_parameter_changes_on_a_missing_stack_lists_every_value_as_new() -> None:
    changes = OptionalStacks(provisioner=FakeStackProvisioner()).parameter_changes(
        "s3-mounts", parameters={"BucketName": "b"}
    )
    assert {c.name: (c.before, c.after) for c in changes} == {
        "BucketName": (None, "b"),
        "Prefixes": (None, "*"),
        "ReadOnly": (None, "true"),
    }


@pytest.mark.asyncio
async def test_async_parameter_changes_mirrors_the_sync_one() -> None:
    changes = await AsyncOptionalStacks(provisioner=FakeStackProvisioner()).parameter_changes(
        "secrets-access"
    )
    assert {c.name for c in changes} == {"SecretPrefix", "KmsKeyArn"}
