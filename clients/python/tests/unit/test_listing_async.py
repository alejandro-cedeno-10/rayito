"""`AsyncSandbox.paginate` y `AsyncSandbox.list`: la misma superficie que
la versión síncrona, con las llamadas al plano de control en un hilo y las
sondas de metadatos por `grpc.aio`."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import pytest

from rayito import AsyncSandbox, AsyncSandboxListPaginator
from rayito._models import SandboxInfo, SandboxListItem
from rayito.exceptions import (
    InvalidArgumentException,
    SandboxException,
    SandboxNotFoundException,
)
from rayito.sandbox_sync.pool import LaunchObserver

from .conftest import IMAGE_ARN, TrackingTransport
from .fake_control_plane import FakeControlPlane
from .test_listing_sync import BASE_TIME, OTHER_IMAGE_ARN, listed


@pytest.fixture
def plane() -> Iterator[FakeControlPlane]:
    fake = FakeControlPlane()
    try:
        yield fake
    finally:
        fake.close()


async def walk(paginator: AsyncSandboxListPaginator) -> list[str]:
    served: list[str] = []
    while paginator.has_next:
        served.extend(item.sandbox_id for item in await paginator.next_items())
    return served


async def test_limit_one_walks_every_item_exactly_once(plane: FakeControlPlane) -> None:
    plane.script_pages([listed("a"), listed("b")], [listed("c"), listed("dead", "TERMINATED")])
    paginator = AsyncSandbox.paginate(limit=1, control_plane=plane)
    assert await walk(paginator) == ["a", "b", "c"]
    assert paginator.next_token is None
    with pytest.raises(SandboxException, match="has_next es False"):
        await paginator.next_items()


async def test_a_fresh_paginator_resumes_by_identity(plane: FakeControlPlane) -> None:
    plane.script_pages([listed("a"), listed("b"), listed("c")])
    first = AsyncSandbox.paginate(limit=1, control_plane=plane)
    assert [item.sandbox_id for item in await first.next_items()] == ["a"]
    plane.script_pages([listed("new"), listed("a"), listed("b"), listed("c")])
    resumed = AsyncSandbox.paginate(limit=1, next_token=first.next_token, control_plane=plane)
    assert await walk(resumed) == ["new", "b", "c"]


async def test_order_filters_and_key_resume(plane: FakeControlPlane) -> None:
    plane.script_pages(
        [listed("thirty", seconds=30), listed("ten", "SUSPENDED", seconds=10)],
        [listed("twenty", seconds=20)],
    )
    descending = AsyncSandbox.paginate(order="desc", limit=1, control_plane=plane)
    assert [item.sandbox_id for item in await descending.next_items()] == ["thirty"]
    resumed = AsyncSandbox.paginate(
        order="desc", limit=5, next_token=descending.next_token, control_plane=plane
    )
    assert [item.sandbox_id for item in await resumed.next_items()] == ["twenty", "ten"]
    suspended = AsyncSandbox.paginate(states=["SUSPENDED"], control_plane=plane)
    assert [item.sandbox_id for item in await suspended.next_items()] == ["ten"]
    recent = AsyncSandbox.paginate(
        started_after=BASE_TIME + timedelta(seconds=15), control_plane=plane
    )
    assert [item.sandbox_id for item in await recent.next_items()] == ["thirty", "twenty"]


async def test_metadata_with_a_limit_probes_only_what_it_consumes(
    plane: FakeControlPlane,
) -> None:
    matching = plane.add_listed_sandbox(metadata={"env": "ci"}, started_at=BASE_TIME)
    other = plane.add_listed_sandbox(metadata={"env": "ci"}, started_at=BASE_TIME)
    transport = TrackingTransport.for_loopback()
    paginator = AsyncSandbox.paginate(
        metadata={"env": "ci"}, limit=1, control_plane=plane, transport=transport
    )
    items = await paginator.next_items()
    assert [item.sandbox_id for item in items] == [matching]
    assert items[0].metadata == {"env": "ci"}
    assert plane.microvms[other].rayd.servicer.health_calls == []
    assert transport.open_count == 1 and transport.all_closed


async def test_paginate_validates_before_any_aws_call(plane: FakeControlPlane) -> None:
    with pytest.raises(InvalidArgumentException):
        AsyncSandbox.paginate(limit=-1, control_plane=plane)
    with pytest.raises(InvalidArgumentException):
        AsyncSandbox.paginate(next_token="!!", control_plane=plane)
    assert plane.calls == []


async def test_foreign_token_fails_before_listing(plane: FakeControlPlane) -> None:
    plane.script_pages([listed("a"), listed("b")])
    first = AsyncSandbox.paginate(limit=1, control_plane=plane)
    await first.next_items()
    plane.page_requests.clear()
    foreign = AsyncSandbox.paginate(order="asc", limit=1, control_plane=plane)
    ordered_token = (await foreign.next_items(), foreign.next_token)[1]
    plane.page_requests.clear()
    mismatched = AsyncSandbox.paginate(order="desc", next_token=ordered_token, control_plane=plane)
    with pytest.raises(InvalidArgumentException, match="no corresponde"):
        await mismatched.next_items()
    assert plane.page_requests == []


async def test_a_token_of_another_order_is_refused_before_listing(
    plane: FakeControlPlane,
) -> None:
    plane.script_pages([listed("a", seconds=1), listed("b", seconds=2)])
    unordered = AsyncSandbox.paginate(limit=1, control_plane=plane)
    await unordered.next_items()
    ascending = AsyncSandbox.paginate(order="asc", limit=1, control_plane=plane)
    await ascending.next_items()
    plane.page_requests.clear()
    calls = len(plane.calls)
    with pytest.raises(InvalidArgumentException, match="next_token inválido"):
        AsyncSandbox.paginate(order="asc", next_token=unordered.next_token, control_plane=plane)
    with pytest.raises(InvalidArgumentException, match="next_token inválido"):
        AsyncSandbox.paginate(next_token=ascending.next_token, control_plane=plane)
    assert len(plane.calls) == calls
    reversed_order = AsyncSandbox.paginate(
        order="desc", next_token=ascending.next_token, control_plane=plane
    )
    with pytest.raises(InvalidArgumentException, match="no corresponde a estos filtros"):
        await reversed_order.next_items()
    assert plane.page_requests == []


async def test_list_keeps_its_list_return_type_and_accepts_order(
    plane: FakeControlPlane,
) -> None:
    plane.script_pages([listed("thirty", seconds=30), listed("ten", seconds=10)])
    assert [item.sandbox_id for item in await AsyncSandbox.list(control_plane=plane)] == [
        "thirty",
        "ten",
    ]
    ordered = await AsyncSandbox.list(order="asc", control_plane=plane)
    assert isinstance(ordered, list)
    assert [item.sandbox_id for item in ordered] == ["ten", "thirty"]
    with pytest.raises(InvalidArgumentException):
        await AsyncSandbox.list(order="up", control_plane=plane)  # type: ignore[arg-type]


async def test_states_and_started_after_filter_on_the_client(plane: FakeControlPlane) -> None:
    plane.script_pages(
        [
            listed("thirty", seconds=30),
            listed("ten", "SUSPENDED", seconds=10),
            listed("twenty", seconds=20),
        ]
    )
    suspended = AsyncSandbox.paginate(states=["SUSPENDED"], control_plane=plane)
    assert [item.sandbox_id for item in await suspended.next_items()] == ["ten"]
    recent = AsyncSandbox.paginate(
        started_after=BASE_TIME + timedelta(seconds=15), control_plane=plane
    )
    assert [item.sandbox_id for item in await recent.next_items()] == ["thirty", "twenty"]
    listed_suspended = await AsyncSandbox.list(states=["SUSPENDED"], control_plane=plane)
    assert [item.sandbox_id for item in listed_suspended] == ["ten"]


async def test_template_travels_as_the_server_side_image_filter(plane: FakeControlPlane) -> None:
    plane.script_pages([listed("a")])
    paginator = AsyncSandbox.paginate(
        template="rayito-base-2gb", template_version="1.0", control_plane=plane
    )
    assert await paginator.next_items()
    request = plane.page_requests[0]
    assert (request.image_arn, request.image_version) == (IMAGE_ARN, "1.0")
    plane.page_requests.clear()
    assert await AsyncSandbox.list(template="rayito-base-2gb", control_plane=plane)
    assert plane.page_requests[0].image_arn == IMAGE_ARN


async def test_metadata_listing_skips_suspended_and_non_matching(
    plane: FakeControlPlane,
) -> None:
    wanted = plane.add_listed_sandbox(metadata={"env": "ci", "run": "2"}, started_at=BASE_TIME)
    plane.add_listed_sandbox(metadata={"env": "ci", "run": "1"}, started_at=BASE_TIME)
    paused = plane.add_listed_sandbox(metadata={"env": "ci", "run": "2"}, state="SUSPENDED")
    transport = TrackingTransport.for_loopback()
    listed_items = await AsyncSandbox.list(
        metadata={"run": "2"}, control_plane=plane, transport=transport
    )
    assert [item.sandbox_id for item in listed_items] == [wanted]
    assert plane.microvms[paused].rayd.servicer.health_calls == []
    assert transport.all_closed


async def test_metadata_listing_skips_a_sandbox_that_vanished(
    plane: FakeControlPlane, monkeypatch: pytest.MonkeyPatch
) -> None:
    gone = plane.add_listed_sandbox(metadata={"run": "2"}, started_at=BASE_TIME)
    kept = plane.add_listed_sandbox(metadata={"run": "2"}, started_at=BASE_TIME)
    real_get_microvm = plane.get_microvm

    def vanishing(sandbox_id: str) -> SandboxInfo:
        if sandbox_id == gone:
            raise SandboxNotFoundException(f"{sandbox_id} ya no existe")
        return real_get_microvm(sandbox_id)

    monkeypatch.setattr(plane, "get_microvm", vanishing)
    listed_items = await AsyncSandbox.list(
        metadata={"run": "2"}, control_plane=plane, transport=TrackingTransport.for_loopback()
    )
    assert [item.sandbox_id for item in listed_items] == [kept]


async def test_next_items_past_the_end_raises(plane: FakeControlPlane) -> None:
    plane.script_pages([listed("a")])
    paginator = AsyncSandbox.paginate(control_plane=plane)
    assert [item.sandbox_id for item in await paginator.next_items()] == ["a"]
    assert not paginator.has_next
    with pytest.raises(SandboxException, match="has_next es False"):
        await paginator.next_items()


async def test_list_without_new_kwargs_streams_the_same_pages(plane: FakeControlPlane) -> None:
    plane.script_pages([listed("a"), listed("dead", "TERMINATED")], [listed("b", "SUSPENDED")])
    listing = await AsyncSandbox.list(control_plane=plane)
    assert [item.sandbox_id for item in listing] == ["a", "b"]
    assert [(request.max_results, request.next_token) for request in plane.page_requests] == [
        (50, None),
        (50, "page-1"),
    ]


async def test_items_of_another_image_are_filtered_by_the_server(plane: FakeControlPlane) -> None:
    foreign = SandboxListItem(
        sandbox_id="x",
        state="RUNNING",
        template=OTHER_IMAGE_ARN,
        template_version="1.0",
        started_at=BASE_TIME,
    )
    plane.script_pages([listed("a"), foreign])
    items = await AsyncSandbox.paginate(template=IMAGE_ARN, control_plane=plane).next_items()
    assert [item.sandbox_id for item in items] == ["a"]


async def test_the_pool_launch_observer_forwards_page_requests(plane: FakeControlPlane) -> None:
    plane.script_pages([listed("a")], [listed("b")])
    observed = LaunchObserver(plane, lambda _info: None)
    items = await AsyncSandbox.paginate(control_plane=observed).next_items()
    assert [item.sandbox_id for item in items] == ["a", "b"]
    assert [request.next_token for request in plane.page_requests] == [None, "page-1"]
