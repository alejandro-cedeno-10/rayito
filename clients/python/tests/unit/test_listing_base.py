"""Núcleo puro del listado paginado (design D8): el token opaco con sus
vectores dorados (los mismos que comprueba el SDK de TypeScript), los
filtros, el `PageWalk` que reanuda por identidad y el `OrderedWalk`."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest

from rayito._listing_base import (
    EXHAUSTED_MESSAGE,
    FIRST_PAGE,
    DecodedToken,
    KeyCursor,
    ListFilters,
    OrderedWalk,
    PageCursor,
    PageRequest,
    PageWalk,
    canonical_json,
    decode_next_token,
    encode_next_token,
    item_digest,
    listing_request,
    next_token_for,
    resume_cursors,
    started_at_ms,
    validate_limit,
    validate_order,
)
from rayito._models import MicrovmListPage, SandboxListItem
from rayito.exceptions import InvalidArgumentException

GOLDEN_IMAGE = "arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base"
GOLDEN_FIRST_ID = "microvm-00000000-0000-0000-0000-000000000001"
GOLDEN_SECOND_ID = "microvm-00000000-0000-0000-0000-000000000002"
GOLDEN_METADATA_FINGERPRINT = "8a197ac8111983d7"
GOLDEN_DIGEST = "0559e532996f"
GOLDEN_PAGE_TOKEN = (
    "eyJhIjpudWxsLCJmIjoiOGExOTdhYzgxMTE5ODNkNyIsInMiOlsiMDU1OWU1MzI5OTZmIl0sInYiOjF9"
)
GOLDEN_ORDER_FINGERPRINT = "1f1b123b1b744c3a"
GOLDEN_KEY_TOKEN = (
    "eyJmIjoiMWYxYjEyM2IxYjc0NGMzYSIsImsiOlsxNzkwMDAwMDAwMDAwLCJtaWNyb3ZtLTAwMDAwMDAwLTAwMD"
    "AtMDAwMC0wMDAwLTAwMDAwMDAwMDAwMiJdLCJ2IjoxfQ"
)
BASE_TIME = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
FINGERPRINT = "0123456789abcdef"


def filters(
    *,
    states: tuple[str, ...] | None = None,
    started_after_ms: int | None = None,
    metadata: tuple[tuple[str, str], ...] | None = None,
    order: str | None = None,
) -> ListFilters:
    return ListFilters(
        image_arn=GOLDEN_IMAGE,
        image_version=None,
        states=states,
        started_after_ms=started_after_ms,
        metadata=metadata,
        order=validate_order(order),
    )


def item(sandbox_id: str, state: str = "RUNNING", *, seconds: float = 0.0) -> SandboxListItem:
    return SandboxListItem(
        sandbox_id=sandbox_id,
        state=state,
        template=GOLDEN_IMAGE,
        template_version="1",
        started_at=BASE_TIME + timedelta(seconds=seconds),
    )


def raw_token(document: object) -> str:
    return base64.urlsafe_b64encode(canonical_json(document)).rstrip(b"=").decode("ascii")


def more(walk: PageWalk | OrderedWalk) -> bool:
    return walk.has_more


def drain(walk: PageWalk, pages: dict[str | None, MicrovmListPage]) -> list[str]:
    served: list[str] = []
    while True:
        request = walk.page_to_fetch()
        if request is not None:
            walk.accept_page(pages[request.aws_token])
            continue
        raw = walk.next_raw()
        if raw is None:
            return served
        served.append(raw.sandbox_id)


# ------------------------------------------------------------- golden vectors


def test_golden_vector_of_a_page_cursor_with_metadata() -> None:
    golden = filters(metadata=(("run", "ñ1"),))
    assert golden.fingerprint() == GOLDEN_METADATA_FINGERPRINT
    assert item_digest(GOLDEN_FIRST_ID) == GOLDEN_DIGEST
    cursor = PageCursor(aws_token=None, consumed=frozenset({GOLDEN_DIGEST}))
    assert encode_next_token(GOLDEN_METADATA_FINGERPRINT, cursor) == GOLDEN_PAGE_TOKEN
    assert decode_next_token(GOLDEN_PAGE_TOKEN) == DecodedToken(GOLDEN_METADATA_FINGERPRINT, cursor)


def test_golden_vector_of_a_key_cursor_with_order() -> None:
    golden = filters(order="desc")
    assert golden.fingerprint() == GOLDEN_ORDER_FINGERPRINT
    cursor = KeyCursor(started_at_ms=1_790_000_000_000, sandbox_id=GOLDEN_SECOND_ID)
    assert encode_next_token(GOLDEN_ORDER_FINGERPRINT, cursor) == GOLDEN_KEY_TOKEN
    assert decode_next_token(GOLDEN_KEY_TOKEN) == DecodedToken(GOLDEN_ORDER_FINGERPRINT, cursor)


def test_canonical_json_sorts_keys_without_spaces_or_ascii_escapes() -> None:
    assert canonical_json({"b": 1, "a": ["ñ", None]}) == '{"a":["ñ",null],"b":1}'.encode()


def test_page_cursor_round_trips_with_an_aws_token_and_sorted_digests() -> None:
    cursor = PageCursor(
        aws_token="aws/next+token==", consumed=frozenset({"bbbbbbbbbbbb", "a" * 12})
    )
    token = encode_next_token(FINGERPRINT, cursor)
    assert "=" not in token and "+" not in token and "/" not in token
    document = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    assert document == {
        "v": 1,
        "f": FINGERPRINT,
        "a": "aws/next+token==",
        "s": ["a" * 12, "bbbbbbbbbbbb"],
    }
    assert decode_next_token(token) == DecodedToken(FINGERPRINT, cursor)


def test_fingerprint_changes_with_every_filter_and_never_carries_values() -> None:
    base = filters()
    variants = [
        filters(states=("RUNNING",)),
        filters(started_after_ms=1),
        filters(metadata=(("env", "ci"),)),
        filters(order="asc"),
        filters(order="desc"),
    ]
    fingerprints = {base.fingerprint(), *(variant.fingerprint() for variant in variants)}
    assert len(fingerprints) == 1 + len(variants)
    assert all(len(value) == 16 for value in fingerprints)


# ------------------------------------------------------------ malformed tokens

VALID_PAGE = {"v": 1, "f": FINGERPRINT, "a": None, "s": []}
VALID_KEY = {"v": 1, "f": FINGERPRINT, "k": [1, GOLDEN_FIRST_ID]}


@pytest.mark.parametrize(
    "token",
    [
        "%%%",
        "",
        "a" * 8193,
        "bm90IGpzb24",
        raw_token(["not", "an", "object"]),
        raw_token({**VALID_PAGE, "v": 2}),
        raw_token({**VALID_PAGE, "v": True}),
        raw_token({**VALID_PAGE, "f": "0123456789ABCDEF"}),
        raw_token({**VALID_PAGE, "f": "0123"}),
        raw_token({"v": 1, "f": FINGERPRINT}),
        raw_token({**VALID_PAGE, "k": [1, GOLDEN_FIRST_ID]}),
        raw_token({**VALID_PAGE, "a": 5}),
        raw_token({**VALID_PAGE, "s": "abc"}),
        raw_token({**VALID_PAGE, "s": ["xyz"]}),
        raw_token({**VALID_PAGE, "s": ["0559E532996F"]}),
        raw_token({**VALID_PAGE, "s": [f"{index:012x}" for index in range(51)]}),
        raw_token({**VALID_PAGE, "extra": 1}),
        raw_token({**VALID_KEY, "k": [1]}),
        raw_token({**VALID_KEY, "k": ["1", GOLDEN_FIRST_ID]}),
        raw_token({**VALID_KEY, "k": [True, GOLDEN_FIRST_ID]}),
        raw_token({**VALID_KEY, "k": [1, ""]}),
        raw_token({**VALID_KEY, "k": [1.5, GOLDEN_FIRST_ID]}),
    ],
)
def test_malformed_tokens_are_rejected_without_echoing_them(token: str) -> None:
    with pytest.raises(InvalidArgumentException) as excinfo:
        decode_next_token(token)
    assert str(excinfo.value) == "next_token inválido"
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__


def test_fifty_digests_and_an_integer_key_are_accepted() -> None:
    digests = [f"{index:012x}" for index in range(50)]
    decoded = decode_next_token(raw_token({**VALID_PAGE, "s": digests}))
    assert isinstance(decoded.cursor, PageCursor) and len(decoded.cursor.consumed) == 50
    assert decode_next_token(raw_token(VALID_KEY)).cursor == KeyCursor(1, GOLDEN_FIRST_ID)


def test_non_string_tokens_are_rejected() -> None:
    with pytest.raises(InvalidArgumentException, match="next_token inválido"):
        decode_next_token(123)  # type: ignore[arg-type]


# --------------------------------------------------------------------- filters


def test_default_states_drop_terminating_and_terminated() -> None:
    default = filters()
    assert default.accepts(item("a", "RUNNING"))
    assert default.accepts(item("b", "SUSPENDED"))
    assert not default.accepts(item("c", "TERMINATING"))
    assert not default.accepts(item("d", "TERMINATED"))


def test_explicit_states_are_a_membership_test() -> None:
    suspended = filters(states=("SUSPENDED", "SUSPENDING"))
    assert suspended.accepts(item("a", "SUSPENDED"))
    assert not suspended.accepts(item("b", "RUNNING"))
    assert filters(states=("TERMINATED",)).accepts(item("c", "TERMINATED"))


def test_metadata_filters_only_running_items() -> None:
    by_metadata = filters(metadata=(("env", "ci"),))
    assert by_metadata.accepts(item("a", "RUNNING"))
    assert not by_metadata.accepts(item("b", "SUSPENDED"))


def test_started_after_is_inclusive_at_the_millisecond() -> None:
    boundary = started_at_ms(item("a", seconds=15))
    window = filters(started_after_ms=boundary)
    assert window.accepts(item("a", seconds=15))
    assert window.accepts(item("b", seconds=20))
    assert not window.accepts(item("c", seconds=14.999))


# ------------------------------------------------------------------- page walk


def test_page_walk_serves_every_page_once_and_resets_consumed_on_the_next_page() -> None:
    pages = {
        None: MicrovmListPage(items=(item("a"), item("b")), next_token="t2"),
        "t2": MicrovmListPage(items=(item("c"),), next_token=None),
    }
    walk = PageWalk(FIRST_PAGE)
    assert more(walk)
    assert walk.page_to_fetch() == PageRequest(aws_token=None)
    walk.accept_page(pages[None])
    assert walk.page_to_fetch() is None
    first = walk.next_raw()
    assert first is not None and first.sandbox_id == "a"
    assert walk.cursor() == PageCursor(None, frozenset({item_digest("a")}))
    second = walk.next_raw()
    assert second is not None and second.sandbox_id == "b"
    assert more(walk)
    assert walk.page_to_fetch() == PageRequest(aws_token="t2")
    assert walk.cursor() == PageCursor("t2", frozenset())
    walk.accept_page(pages["t2"])
    third = walk.next_raw()
    assert third is not None and third.sandbox_id == "c"
    assert not more(walk)
    assert walk.next_raw() is None
    assert walk.page_to_fetch() is None


def test_page_walk_resumes_by_identity_and_tolerates_an_inserted_item() -> None:
    cursor = PageCursor(aws_token=None, consumed=frozenset({item_digest("a")}))
    pages: dict[str | None, MicrovmListPage] = {
        None: MicrovmListPage(items=(item("new"), item("a"), item("b")), next_token=None)
    }
    assert drain(PageWalk(cursor), pages) == ["new", "b"]


def test_page_walk_resumes_on_the_cursor_page_token() -> None:
    walk = PageWalk(PageCursor(aws_token="t2", consumed=frozenset()))
    assert walk.page_to_fetch() == PageRequest(aws_token="t2")


def test_page_walk_moves_past_a_fully_consumed_page() -> None:
    cursor = PageCursor(aws_token=None, consumed=frozenset({item_digest("a")}))
    pages = {
        None: MicrovmListPage(items=(item("a"),), next_token="t2"),
        "t2": MicrovmListPage(items=(item("b"),), next_token=None),
    }
    assert drain(PageWalk(cursor), pages) == ["b"]


def test_an_empty_last_page_exhausts_the_walk() -> None:
    walk = PageWalk(FIRST_PAGE)
    walk.page_to_fetch()
    walk.accept_page(MicrovmListPage(items=(), next_token=None))
    assert not walk.has_more
    assert walk.page_to_fetch() is None
    assert walk.next_raw() is None


# ---------------------------------------------------------------- ordered walk


def test_ordered_walk_sorts_ascending_and_descending_with_an_id_tie_break() -> None:
    items = [
        item("c", seconds=30),
        item("a", seconds=10),
        item("z", seconds=20),
        item("b", seconds=20),
    ]
    ascending = OrderedWalk(items, "asc", None).take(None)
    assert [entry.sandbox_id for entry in ascending] == ["a", "b", "z", "c"]
    descending = OrderedWalk(items, "desc", None).take(None)
    assert [entry.sandbox_id for entry in descending] == ["c", "z", "b", "a"]


def test_ordered_walk_serves_slices_and_reports_its_key_cursor() -> None:
    walk = OrderedWalk(
        [item("a", seconds=10), item("b", seconds=20), item("c", seconds=30)], "asc", None
    )
    assert walk.cursor() is None
    assert [entry.sandbox_id for entry in walk.take(2)] == ["a", "b"]
    assert more(walk)
    assert walk.cursor() == KeyCursor(started_at_ms(item("b", seconds=20)), "b")
    assert [entry.sandbox_id for entry in walk.take(2)] == ["c"]
    assert not more(walk)
    assert walk.take(2) == []


def test_ordered_walk_skips_up_to_the_key_in_either_direction() -> None:
    items = [
        item("a", seconds=10),
        item("b", seconds=20),
        item("c", seconds=30),
        item("n", seconds=20),
    ]
    after_b = KeyCursor(started_at_ms(item("b", seconds=20)), "b")
    ascending = OrderedWalk(items, "asc", after_b).take(None)
    assert [entry.sandbox_id for entry in ascending] == ["n", "c"]
    descending = OrderedWalk(items, "desc", after_b).take(None)
    assert [entry.sandbox_id for entry in descending] == ["a"]
    assert OrderedWalk(items, "asc", after_b).cursor() == after_b


# ------------------------------------------------------------ request building


def test_validate_order_and_limit() -> None:
    assert validate_order(None) is None
    assert validate_order("asc") == "asc"
    assert validate_order("desc") == "desc"
    with pytest.raises(InvalidArgumentException, match="se esperaba 'asc' o 'desc'"):
        validate_order("ASC")
    assert validate_limit(None) is None
    assert validate_limit(3) == 3
    for bad in (0, -1, True, 1.5, "2"):
        with pytest.raises(InvalidArgumentException, match="limit"):
            validate_limit(bad)  # type: ignore[arg-type]


def build(**overrides: object) -> object:
    arguments: dict[str, object] = {
        "template": None,
        "template_version": None,
        "states": None,
        "metadata": None,
        "started_after": None,
        "order": None,
        "limit": None,
        "next_token": None,
    }
    arguments.update(overrides)
    return listing_request(**arguments)  # type: ignore[arg-type]


def test_listing_request_validates_everything_before_any_call() -> None:
    with pytest.raises(InvalidArgumentException, match="RUNNING"):
        build(metadata={"env": "ci"}, states=["SUSPENDED"])
    with pytest.raises(InvalidArgumentException, match="metadata"):
        build(metadata={"": "x"})
    with pytest.raises(InvalidArgumentException, match="started_after"):
        build(started_after=datetime(1969, 12, 31, tzinfo=UTC))
    with pytest.raises(InvalidArgumentException, match="next_token inválido"):
        build(next_token="%%%")
    with pytest.raises(InvalidArgumentException, match="limit"):
        build(limit=0)
    with pytest.raises(InvalidArgumentException, match="order"):
        build(order="sideways")


def test_listing_request_rejects_a_cursor_form_that_does_not_match_the_order() -> None:
    key_token = encode_next_token(FINGERPRINT, KeyCursor(1, "x"))
    page_token = encode_next_token(FINGERPRINT, FIRST_PAGE)
    with pytest.raises(InvalidArgumentException, match="next_token inválido"):
        build(next_token=key_token)
    with pytest.raises(InvalidArgumentException, match="next_token inválido"):
        build(next_token=page_token, order="asc")


def test_listing_request_builds_canonical_filters() -> None:
    request = listing_request(
        template="rayito-base",
        template_version="3",
        states=["SUSPENDED", "RUNNING"],
        metadata=None,
        started_after=BASE_TIME,
        order="asc",
        limit=2,
        next_token=None,
    )
    built = request.filters(GOLDEN_IMAGE)
    assert built == ListFilters(
        image_arn=GOLDEN_IMAGE,
        image_version="3",
        states=("RUNNING", "SUSPENDED"),
        started_after_ms=int(BASE_TIME.timestamp() * 1000),
        metadata=None,
        order="asc",
    )
    by_metadata = listing_request(
        template=None,
        template_version=None,
        states=None,
        metadata={"run": "2", "env": "ci"},
        started_after=None,
        order=None,
        limit=None,
        next_token=None,
    )
    assert by_metadata.filters(None).metadata == (("env", "ci"), ("run", "2"))
    assert by_metadata.filters(None).states is None


def test_resume_cursors_check_the_fingerprint() -> None:
    wanted = filters()
    assert resume_cursors(None, wanted) == (FIRST_PAGE, None)
    page = PageCursor("t", frozenset())
    assert resume_cursors(DecodedToken(wanted.fingerprint(), page), wanted) == (page, None)
    key = KeyCursor(5, "x")
    ordered = filters(order="asc")
    assert resume_cursors(DecodedToken(ordered.fingerprint(), key), ordered) == (FIRST_PAGE, key)
    with pytest.raises(InvalidArgumentException, match="no corresponde a estos filtros"):
        resume_cursors(DecodedToken(ordered.fingerprint(), key), filters(order="desc"))


def test_next_token_for_encodes_only_a_known_cursor() -> None:
    assert next_token_for(FINGERPRINT, None) is None
    token = next_token_for(FINGERPRINT, FIRST_PAGE)
    assert token is not None
    assert decode_next_token(token) == DecodedToken(FINGERPRINT, FIRST_PAGE)
    assert EXHAUSTED_MESSAGE == "no quedan páginas: has_next es False"
