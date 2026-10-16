"""El lector SSE del cliente de despliegues (`_agent/_sse.py`)."""

from __future__ import annotations

import pytest

from rayito._agent._sse import SseMessage, SseParser

STREAM = (
    b": comentario\r\n"
    b'event: step_started\r\ndata: {"index": 1}\r\n\r\n'
    b"data: linea 1\ndata: linea 2\n\n"
    b"id: 7\nretry: 100\nevent: done\ndata: " + '{"ñ": 1}'.encode() + b"\r\r"
)
EXPECTED = [
    SseMessage("step_started", '{"index": 1}'),
    SseMessage("message", "linea 1\nlinea 2"),
    SseMessage("done", '{"ñ": 1}'),
]


def _parse_all(chunks: list[bytes]) -> list[SseMessage]:
    parser = SseParser()
    return [message for chunk in chunks for message in parser.feed(chunk)]


def test_parses_a_whole_stream() -> None:
    assert _parse_all([STREAM]) == EXPECTED


@pytest.mark.parametrize("size", [1, 2, 3, 5, 7, 11])
def test_the_chunking_never_changes_the_messages(size: int) -> None:
    chunks = [STREAM[i : i + size] for i in range(0, len(STREAM), size)]
    assert _parse_all(chunks) == EXPECTED


def test_a_split_utf8_character_is_decoded_whole() -> None:
    data = "event: text\ndata: ñandú\n\n".encode()
    cut = data.index("ñ".encode()) + 1
    assert _parse_all([data[:cut], data[cut:]]) == [SseMessage("text", "ñandú")]


def test_an_incomplete_message_is_dropped() -> None:
    assert _parse_all([b"event: done\ndata: {}"]) == []
    assert _parse_all([b"data: a\n\ndata: b"]) == [SseMessage("message", "a")]


def test_an_empty_data_block_dispatches_nothing_and_resets_the_event() -> None:
    messages = _parse_all([b"event: x\n\ndata: y\n\n"])
    assert messages == [SseMessage("message", "y")]


def test_a_crlf_split_between_chunks_is_one_line_end() -> None:
    assert _parse_all([b"data: a\r", b"\n\r", b"\n"]) == [SseMessage("message", "a")]
