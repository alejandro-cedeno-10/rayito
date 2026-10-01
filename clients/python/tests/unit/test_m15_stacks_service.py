"""`rayito._stacks._service.OptionalStacks` sobre un `StackProvisioner`
falso (M15 foundations): nunca se invoca implícitamente, un componente sin
plantilla lanza antes de tocar el provisioner, create/update/blocked siguen
`plan_deploy`, y los parámetros se validan antes de llamar a AWS."""

from __future__ import annotations

import pytest

from rayito._stacks._model import StackStatus
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
    with pytest.raises(UnimplementedError, match="s3-mounts"):
        stacks.deploy("s3-mounts")
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
