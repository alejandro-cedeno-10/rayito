"""El shim de E2B con el índice de metadatos (M14): `SandboxQuery(metadata=,
state=[PAUSED])` con la extensión `index=` (o `E2B(index=)`) devuelve los
sandboxes en pausa sin sondearlos; sin índice sigue siendo
`UnimplementedError`, ahora con la pista de `index=`."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from rayito import Sandbox as NativeSandbox
from rayito.e2b import (
    E2B,
    AsyncSandbox,
    Sandbox,
    SandboxQuery,
    SandboxState,
    UnimplementedError,
)
from rayito.e2b._compat import states_for

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
def indexed(plane: FakeControlPlane) -> tuple[Any, list[str], list[str]]:
    """Tres sandboxes con `user=42` creados con el índice: dos en pausa."""
    index, _, _ = fake_index()
    transport = TrackingTransport.for_loopback()
    ids = []
    for _ in range(3):
        native = NativeSandbox.create(
            IMAGE_ARN,
            idle=None,
            metadata={"user": "42"},
            index=index,
            control_plane=plane,
            transport=transport,
            ready_timeout=10,
        )
        native.close()
        ids.append(native.sandbox_id)
    for sandbox_id in ids[:2]:
        plane.set_state(sandbox_id, "SUSPENDED")
    return index, ids[:2], ids[2:]


def probes(plane: FakeControlPlane, since: int) -> list[str]:
    return [call.operation for call in plane.calls[since:] if call.operation in PROBE_OPERATIONS]


def test_paused_query_with_index_returns_paused_sandboxes(
    plane: FakeControlPlane, indexed: tuple[Any, list[str], list[str]]
) -> None:
    index, paused, _ = indexed
    since = len(plane.calls)
    query = SandboxQuery(metadata={"user": "42"}, state=[SandboxState.PAUSED])
    items = Sandbox.list(query=query, index=index, control_plane=plane).next_items()
    assert sorted(item.sandbox_id for item in items) == sorted(paused)
    assert {item.state for item in items} == {SandboxState.PAUSED}
    assert all(item.metadata == {"user": "42"} for item in items)
    assert probes(plane, since) == []


def test_running_query_with_index_does_not_probe(
    plane: FakeControlPlane, indexed: tuple[Any, list[str], list[str]]
) -> None:
    index, _, running = indexed
    since = len(plane.calls)
    query = SandboxQuery(metadata={"user": "42"}, state=[SandboxState.RUNNING])
    items = Sandbox.list(query=query, index=index, control_plane=plane).next_items()
    assert [item.sandbox_id for item in items] == running
    assert probes(plane, since) == []


def test_bound_client_index_is_used_by_list(
    plane: FakeControlPlane, indexed: tuple[Any, list[str], list[str]]
) -> None:
    index, paused, _ = indexed
    client = E2B(control_plane=plane, index=index)
    query = SandboxQuery(metadata={"user": "42"}, state=[SandboxState.PAUSED])
    items = client.Sandbox.list(query=query).next_items()
    assert sorted(item.sandbox_id for item in items) == sorted(paused)


async def test_async_paused_query_with_index(
    plane: FakeControlPlane, indexed: tuple[Any, list[str], list[str]]
) -> None:
    index, paused, _ = indexed
    query = SandboxQuery(metadata={"user": "42"}, state=[SandboxState.PAUSED])
    paginator = await AsyncSandbox.list(query=query, index=index, control_plane=plane)
    items = await paginator.next_items()
    assert sorted(item.sandbox_id for item in items) == sorted(paused)


def test_without_index_the_paused_query_names_the_option() -> None:
    query = SandboxQuery(metadata={"user": "42"}, state=[SandboxState.PAUSED])
    with pytest.raises(UnimplementedError) as excinfo:
        Sandbox.list(query=query)
    assert "index=DynamoDbIndex" in str(excinfo.value)
    assert "optional-features" in str(excinfo.value)


def test_states_for_with_index_keeps_the_e2b_state_mapping() -> None:
    query = SandboxQuery(metadata={"a": "1"})
    assert states_for([SandboxState.PAUSED], query, indexed=True) == ("SUSPENDING", "SUSPENDED")
    assert states_for(None, query, indexed=True) is None
    assert states_for([SandboxState.RUNNING], query, indexed=True) == ("PENDING", "RUNNING")
