"""`index=DynamoDbIndex(...)` en `AsyncSandbox` (M14): la misma semántica que
el SDK síncrono, con `PutItem`/`BatchGetItem` en un hilo."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from rayito import AsyncSandbox, AsyncSandboxPool, IndexWriteException
from rayito.exceptions import InvalidArgumentException

from .conftest import IMAGE_ARN, TrackingTransport
from .fake_control_plane import FakeControlPlane
from .fake_dynamodb import fake_index

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


async def create(
    plane: FakeControlPlane, transport: TrackingTransport, **kwargs: Any
) -> AsyncSandbox:
    return await AsyncSandbox.create(
        IMAGE_ARN,
        idle=None,
        control_plane=plane,
        transport=transport,
        ready_timeout=10,
        **kwargs,
    )


async def test_create_writes_one_conditional_row(
    plane: FakeControlPlane, transport: TrackingTransport
) -> None:
    index, api, _ = fake_index()
    sandbox = await create(plane, transport, metadata={"user": "42"}, index=index)
    await sandbox.close()
    (put,) = api.calls("put_item")
    assert put["ConditionExpression"] == "attribute_not_exists(pk)"
    assert put["Item"]["pk"] == {"S": sandbox.sandbox_id}


async def test_a_failed_put_terminates_the_vm_and_raises(
    plane: FakeControlPlane, transport: TrackingTransport
) -> None:
    index, api, _ = fake_index()
    api.put_error = "AccessDeniedException"
    with pytest.raises(IndexWriteException):
        await create(plane, transport, index=index)
    assert len(plane.calls_to("TerminateMicrovm")) == 1
    assert plane.live_ids == []


async def test_warn_policy_returns_the_sandbox(
    plane: FakeControlPlane, transport: TrackingTransport
) -> None:
    index, api, _ = fake_index(on_write_failure="warn")
    api.put_error = "AccessDeniedException"
    sandbox = await create(plane, transport, index=index)
    await sandbox.close()
    assert plane.calls_to("TerminateMicrovm") == []


async def test_create_with_pool_and_index_is_rejected() -> None:
    index, _, _ = fake_index()
    pool = AsyncSandboxPool.__new__(AsyncSandboxPool)
    with pytest.raises(InvalidArgumentException, match="PoolConfig"):
        await AsyncSandbox.create(pool=pool, index=index)


async def test_list_and_paginate_with_index_never_probe(
    plane: FakeControlPlane, transport: TrackingTransport
) -> None:
    index, api, _ = fake_index()
    created = [
        await create(plane, transport, metadata={"user": "42"}, index=index) for _ in range(3)
    ]
    for sandbox in created:
        await sandbox.close()
    for sandbox in created[:2]:
        plane.set_state(sandbox.sandbox_id, "SUSPENDED")
    since = len(plane.calls)
    channels = len(transport.opened)

    found = await AsyncSandbox.list(
        metadata={"user": "42"}, states=["SUSPENDED"], index=index, control_plane=plane
    )
    paginator = AsyncSandbox.paginate(
        metadata={"user": "42"}, index=index, limit=1, control_plane=plane
    )
    first = await paginator.next_items()

    assert sorted(item.sandbox_id for item in found) == sorted(s.sandbox_id for s in created[:2])
    assert {item.state for item in found} == {"SUSPENDED"}
    assert len(first) == 1 and paginator.has_next
    assert [c.operation for c in plane.calls[since:] if c.operation in PROBE_OPERATIONS] == []
    assert len(transport.opened) == channels
    assert len(api.calls("batch_get_item")) == 2
