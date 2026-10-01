"""El componente `sizes-guard` (m15-sizes-catalog) ya no es un stub: tiene
plantilla (`infra/sizes-guard.yaml`), es `CAPABILITY_IAM` y exige
`ImageArns` antes de cualquier llamada al provisioner."""

from __future__ import annotations

import pytest

from rayito._stacks._registry import component_by_name
from rayito._stacks._service import OptionalStacks
from rayito._stacks.components import sizes_guard
from rayito.exceptions import InvalidArgumentException

from .fake_stacks import FakeStackProvisioner


def test_sizes_guard_is_supported_and_registered() -> None:
    component = component_by_name("sizes-guard")
    assert component is sizes_guard.COMPONENT
    assert component.supported


def test_sizes_guard_declares_capability_iam() -> None:
    assert sizes_guard.COMPONENT.capabilities == ("CAPABILITY_IAM",)


def test_sizes_guard_declares_image_arns_as_required() -> None:
    (parameter,) = sizes_guard.COMPONENT.parameters
    assert parameter.name == "ImageArns"
    assert parameter.required
    assert parameter.default is None


def test_deploy_without_image_arns_is_rejected_before_any_call() -> None:
    fake = FakeStackProvisioner()
    stacks = OptionalStacks(provisioner=fake)
    with pytest.raises(InvalidArgumentException, match="ImageArns"):
        stacks.deploy("sizes-guard")
    assert fake.calls == []


def test_deploy_with_image_arns_creates_the_stack() -> None:
    fake = FakeStackProvisioner()
    stacks = OptionalStacks(provisioner=fake)
    status = stacks.deploy(
        "sizes-guard",
        parameters={
            "ImageArns": (
                "arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base,"
                "arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base-4gb"
            )
        },
    )
    assert status.state == "CREATE_COMPLETE"
    assert [call[0] for call in fake.calls] == ["describe", "create", "wait", "describe"]
