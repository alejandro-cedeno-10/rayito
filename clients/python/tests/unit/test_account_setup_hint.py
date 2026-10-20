"""Los errores de una cuenta sin preparar apuntan a `rayito setup` (ADR-030,
D8) sin cambiar de clase, de código ni de causa."""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError

from rayito._agent._aws_resolve import ensure_base_image, resolve_bucket
from rayito._aws import LaunchRequest
from rayito.exceptions import InvalidArgumentException, SandboxNotFoundException
from rayito.sandbox_sync.main import Sandbox

from .conftest import IMAGE_ARN, SANDBOX_ID, StubbedControlPlane

HINT = "rayito setup"


def launch_request() -> LaunchRequest:
    return LaunchRequest(
        image_arn=IMAGE_ARN,
        maximum_duration_seconds=60,
        run_hook_payload="{}",
        client_token="t",
        logging={"disabled": {}},
    )


def test_create_on_an_account_without_the_image_points_to_setup(
    control_plane: StubbedControlPlane,
) -> None:
    control_plane.microvms.add_client_error(
        "run_microvm",
        service_error_code="ResourceNotFoundException",
        service_message="image not found",
        http_status_code=404,
    )
    with pytest.raises(SandboxNotFoundException) as caught:
        Sandbox.create(IMAGE_ARN, control_plane=control_plane.plane)
    error = caught.value
    assert error.aws_code == "ResourceNotFoundException"
    assert error.status_code == 404
    assert str(error).startswith("image not found (")
    assert HINT in str(error)
    assert error.__cause__ is not None
    assert not isinstance(error.__cause__, ClientError)
    assert error.__context__ is None


def test_other_not_found_errors_do_not_get_the_hint(control_plane: StubbedControlPlane) -> None:
    control_plane.microvms.add_client_error(
        "get_microvm",
        service_error_code="ResourceNotFoundException",
        service_message="no such microvm",
        http_status_code=404,
        expected_params={"microvmIdentifier": SANDBOX_ID},
    )
    with pytest.raises(SandboxNotFoundException) as caught:
        control_plane.plane.get_microvm(SANDBOX_ID)
    assert HINT not in str(caught.value)


def test_other_run_microvm_errors_keep_their_message(control_plane: StubbedControlPlane) -> None:
    control_plane.microvms.add_client_error(
        "run_microvm",
        service_error_code="ValidationException",
        service_message="bad request",
        http_status_code=400,
    )
    with pytest.raises(InvalidArgumentException) as caught:
        control_plane.plane.run_microvm(launch_request())
    assert str(caught.value) == "bad request"


def test_the_missing_bucket_error_keeps_its_text_and_adds_the_hint() -> None:
    with pytest.raises(InvalidArgumentException) as caught:
        resolve_bucket(None, None, {}, lambda: None)
    message = str(caught.value)
    assert "Rayito no crea buckets" in message
    assert HINT in message


class NoImage:
    def image_exists(self, name: str) -> bool:
        return False


def test_the_missing_base_image_error_keeps_its_text_and_adds_the_hint() -> None:
    with pytest.raises(InvalidArgumentException) as caught:
        ensure_base_image(NoImage(), "rayito-base-caps", None, ensure=True)  # type: ignore[arg-type]
    message = str(caught.value)
    assert "no existe en esta cuenta y región" in message
    assert HINT in message
