"""`index=DynamoDbIndex(...)` en el SDK síncrono (M14) contra el plano de
control falso, el `rayd` falso y un DynamoDB falso: `create()` escribe una
fila condicional, un `PutItem` que falla termina el VM (o sólo avisa), el
pool la escribe al rellenar, y `list()`/`paginate()` con `metadata` e índice
devuelven sandboxes `SUSPENDED` sin ningún `get-microvm`, token ni `Health`.
Sin `index=` no se construye ningún cliente `dynamodb`."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import boto3
import pytest

from rayito import DynamoDbIndex, IndexWriteException, Sandbox, SandboxIndexException, SandboxPool
from rayito.exceptions import InvalidArgumentException

from .conftest import IMAGE_ARN, TrackingTransport
from .fake_control_plane import FakeControlPlane
from .fake_dynamodb import TABLE, fake_index
from .log_capture import capture_logs

SECRET_LOOKING_VALUE = "metadata-value-never-at-info"
PROBE_OPERATIONS = ("GetMicrovm", "CreateMicrovmAuthToken")


@pytest.fixture
def plane() -> Iterator[FakeControlPlane]:
    fake = FakeControlPlane()
    try:
        yield fake
    finally:
        fake.close()


@pytest.fixture
def transport() -> TrackingTransport:
    return TrackingTransport.for_loopback()


@pytest.fixture
def no_aws_region(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """Sin región en el entorno ni en ficheros de configuración de AWS."""
    for name in ("AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials"))


def create(plane: FakeControlPlane, transport: TrackingTransport, **kwargs: Any) -> Sandbox:
    return Sandbox.create(
        IMAGE_ARN,
        idle=None,
        control_plane=plane,
        transport=transport,
        ready_timeout=10,
        **kwargs,
    )


def probe_calls(plane: FakeControlPlane, since: int) -> list[str]:
    return [call.operation for call in plane.calls[since:] if call.operation in PROBE_OPERATIONS]


def test_create_writes_one_conditional_row(
    plane: FakeControlPlane, transport: TrackingTransport
) -> None:
    index, api, _ = fake_index()
    sandbox = create(plane, transport, metadata={"user": "42"}, index=index)
    sandbox.close()
    (put,) = api.calls("put_item")
    assert put["ConditionExpression"] == "attribute_not_exists(pk)"
    item = put["Item"]
    assert item["pk"] == {"S": sandbox.sandbox_id}
    assert item["metadata"] == {"M": {"user": {"S": "42"}}}
    assert item["image_arn"] == {"S": IMAGE_ARN}
    assert sandbox.access_token not in str(item)


def test_a_failed_put_terminates_the_vm_and_raises(
    plane: FakeControlPlane, transport: TrackingTransport
) -> None:
    index, api, _ = fake_index()
    api.put_error = "AccessDeniedException"
    with pytest.raises(IndexWriteException) as excinfo:
        create(plane, transport, metadata={"user": "42"}, index=index)
    assert excinfo.value.aws_code == "AccessDeniedException"
    assert [call.operation for call in plane.calls_to("TerminateMicrovm")] == ["TerminateMicrovm"]
    assert plane.calls_to("CreateMicrovmAuthToken") == []
    assert plane.live_ids == []


def test_keep_on_failure_keeps_the_vm_but_still_raises(
    plane: FakeControlPlane, transport: TrackingTransport
) -> None:
    index, api, _ = fake_index()
    api.put_error = "ResourceNotFoundException"
    with pytest.raises(IndexWriteException):
        create(plane, transport, index=index, keep_on_failure=True)
    assert plane.calls_to("TerminateMicrovm") == []
    assert len(plane.live_ids) == 1


def test_warn_policy_logs_and_returns_the_sandbox(
    plane: FakeControlPlane, transport: TrackingTransport
) -> None:
    index, api, _ = fake_index(on_write_failure="warn")
    api.put_error = "AccessDeniedException"
    with capture_logs("rayito") as logs:
        sandbox = create(plane, transport, metadata={"user": SECRET_LOOKING_VALUE}, index=index)
    sandbox.close()
    warnings = [r for r in logs.records if r.levelno == logging.WARNING]
    assert any("índice" in r.getMessage() for r in warnings)
    assert SECRET_LOOKING_VALUE not in logs.text()
    assert plane.calls_to("TerminateMicrovm") == []


def test_create_with_pool_and_index_is_rejected() -> None:
    index, _, session = fake_index()
    pool = SandboxPool.__new__(SandboxPool)
    with pytest.raises(InvalidArgumentException, match="PoolConfig"):
        Sandbox.create(pool=pool, index=index)
    assert session.built == []


def test_list_with_index_returns_suspended_items_without_probing(
    plane: FakeControlPlane, transport: TrackingTransport
) -> None:
    index, api, _ = fake_index()
    created = [create(plane, transport, metadata={"user": "42"}, index=index) for _ in range(3)]
    other = create(plane, transport, metadata={"user": "7"}, index=index)
    for sandbox in [*created, other]:
        sandbox.close()
    for sandbox in [*created[:2], other]:
        plane.set_state(sandbox.sandbox_id, "SUSPENDED")
    since = len(plane.calls)
    channels = len(transport.opened)

    with capture_logs("rayito") as logs:
        found = list(
            Sandbox.list(
                metadata={"user": "42"}, states=["SUSPENDED"], index=index, control_plane=plane
            )
        )

    assert sorted(item.sandbox_id for item in found) == sorted(s.sandbox_id for s in created[:2])
    assert {item.state for item in found} == {"SUSPENDED"}
    assert all(item.metadata == {"user": "42"} for item in found)
    assert probe_calls(plane, since) == []
    assert len(transport.opened) == channels
    (batch,) = api.calls("batch_get_item")
    asked = {key["pk"]["S"] for key in batch["RequestItems"]["rayito-sandboxes"]["Keys"]}
    assert asked == {created[0].sandbox_id, created[1].sandbox_id, other.sandbox_id}
    assert "42" not in " ".join(r.getMessage() for r in logs.records if r.levelno >= logging.INFO)


def test_sandboxes_created_without_the_index_are_excluded(
    plane: FakeControlPlane, transport: TrackingTransport
) -> None:
    index, _, _ = fake_index()
    indexed = create(plane, transport, metadata={"user": "42"}, index=index)
    plain = create(plane, transport, metadata={"user": "42"})
    indexed.close()
    plain.close()
    found = list(Sandbox.list(metadata={"user": "42"}, index=index, control_plane=plane))
    assert [item.sandbox_id for item in found] == [indexed.sandbox_id]


def test_list_with_index_but_without_metadata_never_touches_dynamodb(
    plane: FakeControlPlane, transport: TrackingTransport
) -> None:
    index, api, session = fake_index()
    create(plane, transport).close()
    found = list(Sandbox.list(index=index, control_plane=plane))
    assert len(found) == 1
    assert api.requests == []
    assert session.built == []


def test_paginate_with_index_resumes_from_its_token(
    plane: FakeControlPlane, transport: TrackingTransport
) -> None:
    index, _, _ = fake_index()
    ids = []
    for _ in range(3):
        sandbox = create(plane, transport, metadata={"user": "42"}, index=index)
        sandbox.close()
        plane.set_state(sandbox.sandbox_id, "SUSPENDED")
        ids.append(sandbox.sandbox_id)
    first = Sandbox.paginate(metadata={"user": "42"}, index=index, limit=2, control_plane=plane)
    page = [item.sandbox_id for item in first.next_items()]
    assert first.has_next and first.next_token is not None
    second = Sandbox.paginate(
        metadata={"user": "42"},
        index=index,
        limit=2,
        next_token=first.next_token,
        control_plane=plane,
    )
    rest = [item.sandbox_id for item in second.next_items()]
    assert sorted(page + rest) == sorted(ids)
    with pytest.raises(InvalidArgumentException, match="next_token"):
        Sandbox.paginate(
            metadata={"user": "42"}, limit=2, next_token=first.next_token, control_plane=plane
        ).next_items()


def test_read_failures_surface_instead_of_a_partial_list(
    plane: FakeControlPlane, transport: TrackingTransport
) -> None:
    index, api, _ = fake_index()
    create(plane, transport, metadata={"user": "42"}, index=index).close()
    api.batch_error = "ResourceNotFoundException"
    with pytest.raises(SandboxIndexException, match="no existe"):
        list(Sandbox.list(metadata={"user": "42"}, index=index, control_plane=plane))


def test_kill_never_touches_the_index(
    plane: FakeControlPlane, transport: TrackingTransport
) -> None:
    index, api, _ = fake_index()
    sandbox = create(plane, transport, metadata={"user": "42"}, index=index)
    sandbox.kill()
    assert [name for name, _ in api.requests] == ["put_item"]


def test_without_index_no_dynamodb_client_is_ever_built(
    plane: FakeControlPlane, transport: TrackingTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[str] = []
    real_client = boto3.session.Session.client

    def spy(self: boto3.session.Session, service: str, *args: Any, **kwargs: Any) -> Any:
        built.append(service)
        return real_client(self, service, *args, **kwargs)

    monkeypatch.setattr(boto3.session.Session, "client", spy)
    sandbox = create(plane, transport, metadata={"user": "42"})
    list(Sandbox.list(control_plane=plane))
    sandbox.kill()
    assert "dynamodb" not in built


@pytest.mark.usefixtures("no_aws_region")
def test_an_index_without_a_region_fails_before_run_microvm(
    plane: FakeControlPlane, transport: TrackingTransport
) -> None:
    index = DynamoDbIndex(TABLE)
    with pytest.raises(InvalidArgumentException, match="región del índice"):
        create(plane, transport, metadata={"user": "42"}, index=index)
    assert "RunMicrovm" not in [call.operation for call in plane.calls]
