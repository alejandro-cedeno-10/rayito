"""`_build.py` (m15-templates) sobre un `BuildClients` falso: resuelve la
versión base, compone y sube el artefacto, reusa una versión idéntica en
vez de reconstruir, y traduce un build fallido a `BuildException` con el
paso/comando/código de salida del log."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rayito import Template, _images
from rayito._templates import _build, _concurrency
from rayito._templates._build import desired_configuration, image_arn
from rayito.exceptions import (
    BuildException,
    InvalidArgumentException,
    NotFoundException,
    TemplateException,
)

from .fake_templates import FakeBuildClients, make_base_zip

BASE_ARN = "arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base"
BUCKET = "my-artifact-bucket"
BASE_DOCKERFILE = 'FROM scratch\nCOPY rayd /usr/local/bin/rayd\nCMD ["/usr/local/bin/rayd"]\n'
BASE_HOOKS = {"port": 9000}


def _clients_with_base_image(**overrides: object) -> FakeBuildClients:
    clients = FakeBuildClients(**overrides)  # type: ignore[arg-type]
    clients.images[BASE_ARN] = {"state": "CREATED"}
    clients.versions[(BASE_ARN, "1")] = {
        "state": "SUCCESSFUL",
        "status": "ACTIVE",
        "imageVersion": "1",
        "createdAt": 1,
        "baseImageArn": "arn:aws:lambda:us-east-1:aws:microvm-image:al2023-1",
        "baseImageVersion": "1.0",
        "buildRoleArn": "arn:aws:iam::123456789012:role/rayito-build",
        "hooks": BASE_HOOKS,
        "codeArtifact": {"uri": f"s3://{BUCKET}/base.zip"},
    }
    clients.objects[(BUCKET, "base.zip")] = make_base_zip(BASE_DOCKERFILE, {"rayd": b"\x7fELF"})
    return clients


def test_image_arn_passes_through_an_already_resolved_arn() -> None:
    clients = FakeBuildClients()
    assert image_arn(clients, BASE_ARN) == BASE_ARN


def test_image_arn_builds_one_from_region_account_and_name() -> None:
    clients = FakeBuildClients(region_value="eu-west-1", account_id_value="444455556666")
    assert image_arn(clients, "mi-template") == (
        "arn:aws:lambda:eu-west-1:444455556666:microvm-image:mi-template"
    )


def test_building_without_from_base_image_raises_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_build, "_Clients", lambda **_kwargs: FakeBuildClients())
    with pytest.raises(InvalidArgumentException, match="from_base_image"):
        _build.build(Template().pip_install("pandas"), "mi-template", bucket=BUCKET)


def test_building_with_an_unsupported_memory_size_raises_before_any_call() -> None:
    with pytest.raises(InvalidArgumentException, match="memory_mb"):
        _build.build(Template().from_base_image(), "mi-template", bucket=BUCKET, memory_mb=3000)


def test_resolve_base_version_picks_the_newest_active_successful_version() -> None:
    clients = _clients_with_base_image()
    clients.versions[(BASE_ARN, "2")] = {
        **clients.versions[(BASE_ARN, "1")],
        "imageVersion": "2",
        "createdAt": 2,
    }
    resolved = _build.resolve_base_version(clients, BASE_ARN, None)
    assert resolved["imageVersion"] == "2"


def test_resolve_base_version_with_no_active_version_is_not_found() -> None:
    clients = FakeBuildClients()
    with pytest.raises(NotFoundException):
        _build.resolve_base_version(clients, BASE_ARN, None)


def test_a_successful_build_uploads_the_artifact_and_returns_build_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients = _clients_with_base_image()
    monkeypatch.setattr(_build, "_Clients", lambda **_kwargs: clients)
    monkeypatch.setattr("rayito._templates._build.time.sleep", lambda _seconds: None)

    t = Template().from_base_image().pip_install("pandas")
    info = _build.build(t, "mi-template", bucket=BUCKET, context_dir=Path())

    assert info.template_id.endswith(":microvm-image:mi-template")
    assert info.alias == "mi-template"
    assert ("create_microvm_image", "mi-template") in clients.calls
    # the uploaded artifact is content-addressed and reused on a second call.
    first_put_calls = [c for c in clients.calls if c[0] == "put_object"]
    assert len(first_put_calls) == 1


def test_building_twice_with_the_same_configuration_reuses_the_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients = _clients_with_base_image()
    monkeypatch.setattr(_build, "_Clients", lambda **_kwargs: clients)
    monkeypatch.setattr("rayito._templates._build.time.sleep", lambda _seconds: None)
    t = Template().from_base_image().pip_install("pandas")

    _build.build(t, "mi-template", bucket=BUCKET, context_dir=Path())
    clients.calls.clear()
    _build.build(t, "mi-template", bucket=BUCKET, context_dir=Path())

    assert not any(
        call[0] in ("create_microvm_image", "update_microvm_image") for call in clients.calls
    )


def test_force_rebuilds_even_with_an_identical_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients = _clients_with_base_image()
    monkeypatch.setattr(_build, "_Clients", lambda **_kwargs: clients)
    monkeypatch.setattr("rayito._templates._build.time.sleep", lambda _seconds: None)
    t = Template().from_base_image().pip_install("pandas")

    _build.build(t, "mi-template", bucket=BUCKET, context_dir=Path())
    clients.calls.clear()
    _build.build(t, "mi-template", bucket=BUCKET, context_dir=Path(), force=True)

    assert any(call[0] == "update_microvm_image" for call in clients.calls)


def test_a_failed_run_step_raises_build_exception_with_step_and_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients = _clients_with_base_image()
    clients.next_build_version = "9"

    def _fake_create(*, name: str, **request: object) -> dict[str, str]:
        arn = image_arn(clients, name)
        clients.versions[(arn, "9")] = {
            "state": "FAILED",
            "status": "INACTIVE",
            "imageVersion": "9",
            "createdAt": 3,
            "stateReason": "The container image build failed.",
        }
        clients.images[arn] = {"state": "CREATE_FAILED"}
        return {"imageArn": arn, "imageVersion": "9"}

    clients.log_lines = [
        "#4 [2/5] RUN pip install not-a-real-package",
        "#4 ERROR: executor failed running [...]: exit code: 1",
    ]
    monkeypatch.setattr(
        clients.microvms.__class__, "create_microvm_image", lambda self, **kw: _fake_create(**kw)
    )
    monkeypatch.setattr(_build, "_Clients", lambda **_kwargs: clients)
    monkeypatch.setattr("rayito._templates._build.time.sleep", lambda _seconds: None)

    with pytest.raises(BuildException) as excinfo:
        _build.build(
            Template().from_base_image().pip_install("not-a-real-package"),
            "mi-failing-template",
            bucket=BUCKET,
            context_dir=Path(),
        )
    assert excinfo.value.step == 2
    assert excinfo.value.exit_code == 1


def test_a_ready_cmd_server_error_is_classified_as_such(monkeypatch: pytest.MonkeyPatch) -> None:
    clients = _clients_with_base_image()

    def _fake_create(*, name: str, **request: object) -> dict[str, str]:
        arn = image_arn(clients, name)
        clients.versions[(arn, "1")] = {
            "state": "FAILED",
            "status": "INACTIVE",
            "imageVersion": "1",
            "createdAt": 3,
            "stateReason": (
                "Ready hook check failed: the application returned a server error "
                "(HTTP 5xx) response"
            ),
        }
        clients.images[arn] = {"state": "CREATE_FAILED"}
        return {"imageArn": arn, "imageVersion": "1"}

    monkeypatch.setattr(
        clients.microvms.__class__, "create_microvm_image", lambda self, **kw: _fake_create(**kw)
    )
    monkeypatch.setattr(_build, "_Clients", lambda **_kwargs: clients)
    monkeypatch.setattr("rayito._templates._build.time.sleep", lambda _seconds: None)

    with pytest.raises(BuildException) as excinfo:
        _build.build(
            Template()
            .from_base_image()
            .set_start_cmd("python app.py", "curl -f http://localhost:8000"),
            "mi-template",
            bucket=BUCKET,
            context_dir=Path(),
        )
    assert excinfo.value.reason == "ready_server_error"


def test_build_in_background_then_get_build_status_reports_in_progress_then_successful(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients = _clients_with_base_image()
    monkeypatch.setattr(_build, "_Clients", lambda **_kwargs: clients)

    handle = _build.build_in_background(
        Template().from_base_image().pip_install("pandas"),
        "mi-template",
        bucket=BUCKET,
        context_dir=Path(),
    )
    status = _build.get_build_status(handle)
    assert status.state == "SUCCESSFUL"
    assert status.info is not None
    assert status.info.template_id == handle.arn


def test_template_exists_reflects_whether_the_image_was_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients = _clients_with_base_image()
    monkeypatch.setattr(_build, "_Clients", lambda **_kwargs: clients)
    assert _build.template_exists("mi-template") is False
    clients.images[image_arn(clients, "mi-template")] = {"state": "CREATED"}
    assert _build.template_exists("mi-template") is True


def test_desired_configuration_copies_the_base_images_own_managed_base_not_itself() -> None:
    base_version = {
        "imageArn": "arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base",
        "imageVersion": "7",
        "baseImageArn": "arn:aws:lambda:us-east-1:aws:microvm-image:al2023-1",
        "baseImageVersion": "1.0",
        "buildRoleArn": "arn:aws:iam::123456789012:role/rayito-build",
        "hooks": BASE_HOOKS,
    }
    desired = desired_configuration(
        artifact_uri="s3://bucket/key.zip",
        memory_mb=2048,
        base_image_version=base_version,
        log_group="/rayito/x",
    )
    assert desired["baseImageArn"] == "arn:aws:lambda:us-east-1:aws:microvm-image:al2023-1"
    assert desired["baseImageVersion"] == "1.0"


def test_desired_configuration_keeps_the_caps_variants_os_capabilities() -> None:
    """A template over `rayito-base-caps` must stay a caps image: the
    composed configuration inherits `additionalOsCapabilities` (and any
    other configuration key the base version declares)."""
    caps_version = {
        "baseImageArn": "arn:aws:lambda:us-east-1:aws:microvm-image:al2023-1",
        "baseImageVersion": "1.0",
        "buildRoleArn": "arn:aws:iam::123456789012:role/rayito-build",
        "hooks": BASE_HOOKS,
        "cpuConfigurations": [{"architecture": "ARM_64"}],
        "additionalOsCapabilities": ["ALL"],
    }
    desired = desired_configuration(
        artifact_uri="s3://bucket/key.zip",
        memory_mb=2048,
        base_image_version=caps_version,
        log_group="/rayito/x",
    )
    assert desired["additionalOsCapabilities"] == ["ALL"]
    assert desired["cpuConfigurations"] == [{"architecture": "ARM_64"}]
    assert desired["codeArtifact"] == {"uri": "s3://bucket/key.zip"}


def test_a_caps_base_image_builds_a_caps_template(monkeypatch: pytest.MonkeyPatch) -> None:
    clients = _clients_with_base_image()
    clients.versions[(BASE_ARN, "1")]["additionalOsCapabilities"] = ["ALL"]
    monkeypatch.setattr(_build, "_Clients", lambda **_kwargs: clients)

    handle = _build.build_in_background(
        Template().from_base_image(), "mi-template", bucket=BUCKET, context_dir=Path()
    )

    assert clients.versions[(handle.arn, handle.version)]["additionalOsCapabilities"] == ["ALL"]


def test_skip_cache_rebuilds_even_with_an_identical_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients = _clients_with_base_image()
    monkeypatch.setattr(_build, "_Clients", lambda **_kwargs: clients)
    monkeypatch.setattr("rayito._templates._build.time.sleep", lambda _seconds: None)

    _build.build(Template().from_base_image(), "mi-template", bucket=BUCKET, context_dir=Path())
    clients.calls.clear()
    _build.build(
        Template().from_base_image().skip_cache(),
        "mi-template",
        bucket=BUCKET,
        context_dir=Path(),
    )

    assert any(call[0] == "update_microvm_image" for call in clients.calls)


def test_the_aws_build_quota_is_a_build_exception_with_reason_build_quota(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients = _clients_with_base_image(submit_error_code="ServiceQuotaExceededException")
    monkeypatch.setattr(_build, "_Clients", lambda **_kwargs: clients)

    with pytest.raises(BuildException) as excinfo:
        _build.build_in_background(
            Template().from_base_image(), "mi-template", bucket=BUCKET, context_dir=Path()
        )
    assert excinfo.value.reason == "build_quota"


def test_build_holds_its_slot_while_waiting_for_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    clients = _clients_with_base_image()
    monkeypatch.setattr(_build, "_Clients", lambda **_kwargs: clients)
    free_slots_while_waiting: list[int] = []
    real_wait = _images.wait_for_gate

    def _spy(*args: Any, **kwargs: Any) -> Any:
        free_slots_while_waiting.append(_concurrency._SLOTS._value)
        return real_wait(*args, **kwargs)

    monkeypatch.setattr("rayito._templates._build.wait_for_gate", _spy)
    _build.build(Template().from_base_image(), "mi-template", bucket=BUCKET, context_dir=Path())

    assert free_slots_while_waiting == [_concurrency.MAX_CONCURRENT_BUILDS - 1]
    assert _concurrency._SLOTS._value == _concurrency.MAX_CONCURRENT_BUILDS


@pytest.mark.parametrize("name", ["mi-template:v1", "", "con espacio", "x" * 65])
def test_an_invalid_template_name_is_a_template_exception(name: str) -> None:
    with pytest.raises(TemplateException):
        _build.build(Template().from_base_image(), name, bucket=BUCKET)


def test_a_failed_build_message_names_the_template_not_the_arn_or_aws_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients = _clients_with_base_image()

    def _fake_create(*, name: str, **request: object) -> dict[str, str]:
        arn = image_arn(clients, name)
        clients.versions[(arn, "1")] = {
            "state": "FAILED",
            "status": "INACTIVE",
            "imageVersion": "1",
            "createdAt": 3,
            "stateReason": f"Build failed for {arn}",
        }
        clients.images[arn] = {"state": "CREATE_FAILED"}
        return {"imageArn": arn, "imageVersion": "1"}

    monkeypatch.setattr(
        clients.microvms.__class__, "create_microvm_image", lambda self, **kw: _fake_create(**kw)
    )
    monkeypatch.setattr(_build, "_Clients", lambda **_kwargs: clients)

    with pytest.raises(BuildException) as excinfo:
        _build.build(Template().from_base_image(), "mi-template", bucket=BUCKET, context_dir=Path())
    message = str(excinfo.value)
    assert "mi-template" in message
    assert clients.account_id not in message
    assert "Build failed for" not in message


def test_a_missing_base_image_is_not_found_without_the_account_id() -> None:
    clients = FakeBuildClients()
    with pytest.raises(NotFoundException) as excinfo:
        _build.resolve_base_version(clients, "rayito-base", None)
    assert clients.account_id not in str(excinfo.value)
