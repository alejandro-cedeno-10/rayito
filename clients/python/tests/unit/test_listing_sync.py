"""`Sandbox.paginate` y `Sandbox.list` sobre páginas guionizadas del plano
de control falso (design D9): `limit`, reanudar con `next_token`, `order`,
filtros de cliente, la sonda de metadatos acotada a lo consumido y el
fin del paginador."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from rayito import Sandbox, SandboxListPaginator
from rayito._models import SandboxInfo, SandboxListItem
from rayito.exceptions import (
    InvalidArgumentException,
    SandboxException,
    SandboxNotFoundException,
)
from rayito.sandbox_sync.pool import LaunchObserver

from .conftest import IMAGE_ARN, TrackingTransport
from .fake_control_plane import FakeControlPlane

BASE_TIME = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
OTHER_IMAGE_ARN = "arn:aws:lambda:us-east-1:123456789012:microvm-image:other"


def listed(sandbox_id: str, state: str = "RUNNING", *, seconds: float = 0.0) -> SandboxListItem:
    return SandboxListItem(
        sandbox_id=sandbox_id,
        state=state,
        template=IMAGE_ARN,
        template_version="1.0",
        started_at=BASE_TIME + timedelta(seconds=seconds),
    )


@pytest.fixture
def plane() -> Iterator[FakeControlPlane]:
    fake = FakeControlPlane()
    try:
        yield fake
    finally:
        fake.close()


def walk(paginator: SandboxListPaginator) -> list[list[str]]:
    pages: list[list[str]] = []
    while paginator.has_next:
        pages.append([item.sandbox_id for item in paginator.next_items()])
    return pages


def cursor_state(paginator: SandboxListPaginator) -> tuple[bool, str | None]:
    return paginator.has_next, paginator.next_token


def flat(pages: list[list[str]]) -> list[str]:
    return [sandbox_id for page in pages for sandbox_id in page]


def test_limit_one_walks_every_item_exactly_once(plane: FakeControlPlane) -> None:
    plane.script_pages(
        [listed("a"), listed("b"), listed("c"), listed("dead", "TERMINATED")],
    )
    paginator = Sandbox.paginate(limit=1, control_plane=plane)
    assert cursor_state(paginator) == (True, None)
    pages = walk(paginator)
    assert flat(pages) == ["a", "b", "c"]
    assert all(len(page) <= 1 for page in pages)
    assert cursor_state(paginator) == (False, None)
    assert {request.max_results for request in plane.page_requests} == {50}


def test_a_fresh_paginator_resumes_by_identity_on_the_cursor_page(
    plane: FakeControlPlane,
) -> None:
    plane.script_pages([listed("a"), listed("b"), listed("c"), listed("dead", "TERMINATED")])
    first = Sandbox.paginate(limit=1, control_plane=plane)
    assert [item.sandbox_id for item in first.next_items()] == ["a"]
    token = first.next_token
    assert token is not None and first.has_next

    plane.script_pages(
        [listed("new"), listed("a"), listed("b"), listed("c"), listed("dead", "TERMINATED")],
    )
    plane.page_requests.clear()
    resumed = Sandbox.paginate(limit=1, next_token=token, control_plane=plane)
    assert resumed.next_token == token
    assert flat(walk(resumed)) == ["new", "b", "c"]
    assert plane.page_requests[0].next_token is None


def test_resuming_across_pages_continues_on_the_next_aws_page(plane: FakeControlPlane) -> None:
    plane.script_pages([listed("a"), listed("b")], [listed("c")])
    first = Sandbox.paginate(limit=2, control_plane=plane)
    assert [item.sandbox_id for item in first.next_items()] == ["a", "b"]
    assert first.has_next
    plane.page_requests.clear()
    resumed = Sandbox.paginate(next_token=first.next_token, control_plane=plane)
    assert [item.sandbox_id for item in resumed.next_items()] == ["c"]
    assert [request.next_token for request in plane.page_requests] == [None, "page-1"]
    assert not resumed.has_next


def test_order_sorts_by_started_at_and_resumes_with_a_key_cursor(
    plane: FakeControlPlane,
) -> None:
    plane.script_pages(
        [listed("thirty", seconds=30), listed("ten", seconds=10)], [listed("twenty", seconds=20)]
    )
    ascending = Sandbox.paginate(order="asc", limit=2, control_plane=plane)
    assert [item.sandbox_id for item in ascending.next_items()] == ["ten", "twenty"]
    assert ascending.has_next
    resumed = Sandbox.paginate(
        order="asc", limit=2, next_token=ascending.next_token, control_plane=plane
    )
    assert [item.sandbox_id for item in resumed.next_items()] == ["thirty"]
    assert not resumed.has_next and resumed.next_token is None
    descending = Sandbox.paginate(order="desc", control_plane=plane)
    assert [item.sandbox_id for item in descending.next_items()] == ["thirty", "twenty", "ten"]


def test_states_and_started_after_filter_on_the_client(plane: FakeControlPlane) -> None:
    plane.script_pages(
        [
            listed("thirty", seconds=30),
            listed("ten", "SUSPENDED", seconds=10),
            listed("twenty", seconds=20),
        ]
    )
    suspended = Sandbox.paginate(states=["SUSPENDED"], control_plane=plane)
    assert [item.sandbox_id for item in suspended.next_items()] == ["ten"]
    recent = Sandbox.paginate(started_after=BASE_TIME + timedelta(seconds=15), control_plane=plane)
    assert [item.sandbox_id for item in recent.next_items()] == ["thirty", "twenty"]


def test_template_travels_as_the_server_side_image_filter(plane: FakeControlPlane) -> None:
    plane.script_pages([listed("a")])
    assert Sandbox.paginate(
        template="rayito-base-2gb", template_version="1.0", control_plane=plane
    ).next_items()
    request = plane.page_requests[0]
    assert (request.image_arn, request.image_version) == (IMAGE_ARN, "1.0")


def test_metadata_with_a_limit_probes_only_what_it_consumes(plane: FakeControlPlane) -> None:
    matching = plane.add_listed_sandbox(metadata={"env": "ci"}, started_at=BASE_TIME)
    others = [
        plane.add_listed_sandbox(metadata={"env": "ci"}, started_at=BASE_TIME) for _ in range(3)
    ]
    transport = TrackingTransport.for_loopback()
    paginator = Sandbox.paginate(
        metadata={"env": "ci"}, limit=1, control_plane=plane, transport=transport
    )
    items = paginator.next_items()
    assert [item.sandbox_id for item in items] == [matching]
    assert items[0].metadata == {"env": "ci"}
    assert len(plane.microvms[matching].rayd.servicer.health_calls) == 1
    assert all(plane.microvms[other].rayd.servicer.health_calls == [] for other in others)
    assert transport.open_count == 1 and transport.all_closed
    assert paginator.has_next


def test_metadata_listing_skips_suspended_and_non_matching(plane: FakeControlPlane) -> None:
    wanted = plane.add_listed_sandbox(metadata={"env": "ci", "run": "2"}, started_at=BASE_TIME)
    plane.add_listed_sandbox(metadata={"env": "ci", "run": "1"}, started_at=BASE_TIME)
    paused = plane.add_listed_sandbox(metadata={"env": "ci", "run": "2"}, state="SUSPENDED")
    transport = TrackingTransport.for_loopback()
    listed_items = list(
        Sandbox.list(metadata={"run": "2"}, control_plane=plane, transport=transport)
    )
    assert [item.sandbox_id for item in listed_items] == [wanted]
    assert plane.microvms[paused].rayd.servicer.health_calls == []
    assert transport.all_closed


def test_metadata_listing_skips_a_sandbox_that_vanished(
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
    listed_items = list(
        Sandbox.list(
            metadata={"run": "2"}, control_plane=plane, transport=TrackingTransport.for_loopback()
        )
    )
    assert [item.sandbox_id for item in listed_items] == [kept]


def test_next_items_past_the_end_raises(plane: FakeControlPlane) -> None:
    plane.script_pages([listed("a")])
    paginator = Sandbox.paginate(control_plane=plane)
    assert [item.sandbox_id for item in paginator.next_items()] == ["a"]
    assert not paginator.has_next
    with pytest.raises(SandboxException, match="has_next es False"):
        paginator.next_items()


def test_paginate_validates_before_any_aws_call(plane: FakeControlPlane) -> None:
    for kwargs in (
        {"limit": 0},
        {"order": "sideways"},
        {"metadata": {"a": "1"}, "states": ["SUSPENDED"]},
        {"next_token": "%%%"},
    ):
        with pytest.raises(InvalidArgumentException):
            Sandbox.paginate(control_plane=plane, **kwargs)
    Sandbox.paginate(template="rayito-base-2gb", limit=3, control_plane=plane)
    assert plane.calls == []


def test_a_token_from_other_filters_fails_before_listing(plane: FakeControlPlane) -> None:
    plane.script_pages([listed("a"), listed("b")])
    first = Sandbox.paginate(limit=1, control_plane=plane)
    first.next_items()
    plane.page_requests.clear()
    foreign = Sandbox.paginate(states=["RUNNING"], next_token=first.next_token, control_plane=plane)
    with pytest.raises(InvalidArgumentException, match="no corresponde a estos filtros"):
        foreign.next_items()
    assert plane.page_requests == []


def test_a_token_of_another_order_is_refused_before_listing(plane: FakeControlPlane) -> None:
    plane.script_pages([listed("a", seconds=1), listed("b", seconds=2)])
    unordered = Sandbox.paginate(limit=1, control_plane=plane)
    unordered.next_items()
    ascending = Sandbox.paginate(order="asc", limit=1, control_plane=plane)
    ascending.next_items()
    plane.page_requests.clear()
    calls = len(plane.calls)
    with pytest.raises(InvalidArgumentException, match="next_token inválido"):
        Sandbox.paginate(order="asc", next_token=unordered.next_token, control_plane=plane)
    with pytest.raises(InvalidArgumentException, match="next_token inválido"):
        Sandbox.paginate(next_token=ascending.next_token, control_plane=plane)
    assert len(plane.calls) == calls
    reversed_order = Sandbox.paginate(
        order="desc", next_token=ascending.next_token, control_plane=plane
    )
    with pytest.raises(InvalidArgumentException, match="no corresponde a estos filtros"):
        reversed_order.next_items()
    assert plane.page_requests == []


def test_list_accepts_order_and_started_after(plane: FakeControlPlane) -> None:
    plane.script_pages(
        [listed("thirty", seconds=30), listed("ten", seconds=10)], [listed("twenty", seconds=20)]
    )
    ordered = Sandbox.list(
        order="desc", started_after=BASE_TIME + timedelta(seconds=15), control_plane=plane
    )
    assert [item.sandbox_id for item in ordered] == ["thirty", "twenty"]


def test_list_without_new_kwargs_streams_the_same_pages(plane: FakeControlPlane) -> None:
    plane.script_pages([listed("a"), listed("dead", "TERMINATED")], [listed("b", "SUSPENDED")])
    listing = Sandbox.list(control_plane=plane)
    assert plane.page_requests == []
    assert next(listing).sandbox_id == "a"
    assert len(plane.page_requests) == 1
    assert [item.sandbox_id for item in listing] == ["b"]
    assert [(request.max_results, request.next_token) for request in plane.page_requests] == [
        (50, None),
        (50, "page-1"),
    ]


def test_list_validates_eagerly(plane: FakeControlPlane) -> None:
    with pytest.raises(InvalidArgumentException, match="order"):
        Sandbox.list(order="up", control_plane=plane)  # type: ignore[arg-type]
    assert plane.calls == []


def test_items_of_another_image_are_filtered_by_the_server(plane: FakeControlPlane) -> None:
    foreign = SandboxListItem(
        sandbox_id="x",
        state="RUNNING",
        template=OTHER_IMAGE_ARN,
        template_version="1.0",
        started_at=BASE_TIME,
    )
    plane.script_pages([listed("a"), foreign])
    items = Sandbox.paginate(template=IMAGE_ARN, control_plane=plane).next_items()
    assert [item.sandbox_id for item in items] == ["a"]


def test_the_pool_launch_observer_forwards_page_requests(plane: FakeControlPlane) -> None:
    plane.script_pages([listed("a")], [listed("b")])
    observed = LaunchObserver(plane, lambda _info: None)
    items = Sandbox.paginate(control_plane=observed).next_items()
    assert [item.sandbox_id for item in items] == ["a", "b"]
    assert [request.next_token for request in plane.page_requests] == [None, "page-1"]
