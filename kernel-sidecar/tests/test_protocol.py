"""Codec tests on any host: the golden lines, chunking, ANSI, mime bundles."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rayito_kernel_sidecar.protocol import (
    EVENT_FIELDS,
    EVENTS,
    MAX_EVENT_LINE_BYTES,
    MAX_RESULT_BYTES,
    OMITTED_MIME,
    OPS,
    REQUEST_FIELDS,
    ProtocolError,
    decode_request,
    encode_event,
    encode_request,
    encoded_line_bytes,
    omit_payload,
    reply_error,
    reply_ok,
    serialise_mime,
    split_text_chunks,
    strip_ansi,
)

FIXTURE = Path(__file__).parent / "fixtures" / "protocol_v1.jsonl"


def golden_lines() -> list[str]:
    return [line for line in FIXTURE.read_text(encoding="utf-8").splitlines() if line]


def test_fixture_covers_every_op_and_event() -> None:
    lines = [json.loads(line) for line in golden_lines()]
    ops = {line["op"] for line in lines if "op" in line}
    events = {line["event"] for line in lines if "event" in line}
    assert ops == OPS
    assert events == EVENTS
    assert set(REQUEST_FIELDS) == OPS
    assert set(EVENT_FIELDS) == EVENTS


@pytest.mark.parametrize("line", [line for line in golden_lines() if '"op"' in line])
def test_request_lines_round_trip(line: str) -> None:
    request = decode_request(line)
    assert request["id"] >= 1
    assert request["op"] in OPS
    assert encode_request(request) == line


@pytest.mark.parametrize("line", [line for line in golden_lines() if '"event"' in line])
def test_event_lines_round_trip(line: str) -> None:
    event = json.loads(line)
    assert event["event"] in EVENTS
    assert encode_event(event) == line


def test_encode_event_orders_keys_and_sorts_nested_objects() -> None:
    event = {
        "mime": {"text/plain": "1", "e2b/data": "{}"},
        "execution_id": "exec-1",
        "is_main_result": False,
        "id": 9,
        "event": "result",
        "ignored": "x",
    }
    assert (
        encode_event(event)
        == '{"event":"result","id":9,"execution_id":"exec-1","is_main_result":false,'
        '"mime":{"e2b/data":"{}","text/plain":"1"}}'
    )


def test_decode_request_rejects_malformed_lines() -> None:
    with pytest.raises(ProtocolError):
        decode_request("not json")
    with pytest.raises(ProtocolError):
        decode_request("[1]")
    with pytest.raises(ProtocolError):
        decode_request('{"op":"ping"}')
    with pytest.raises(ProtocolError):
        decode_request('{"id":0,"op":"ping"}')
    with pytest.raises(ProtocolError):
        decode_request('{"id":true,"op":"ping"}')
    with pytest.raises(ProtocolError):
        decode_request('{"id":1}')
    with pytest.raises(ProtocolError):
        decode_request('{"id":1,"op":"execute","code":7}')
    with pytest.raises(ProtocolError):
        decode_request('{"id":1,"op":"execute","envs":{"A":1}}')


def test_decode_request_ignores_unknown_fields_and_keeps_unknown_ops() -> None:
    request = decode_request('{"id":5,"op":"frobnicate","future":true}')
    assert request == {"id": 5, "op": "frobnicate"}


def test_create_context_language_is_decoded_and_ordered() -> None:
    request = decode_request(
        '{"id":2,"op":"create_context","cwd":"/tmp","language":"bash","context_id":"default-bash"}'
    )
    assert request["language"] == "bash"
    assert encode_request(request) == (
        '{"id":2,"op":"create_context","context_id":"default-bash","language":"bash","cwd":"/tmp"}'
    )
    assert "language" not in decode_request('{"id":2,"op":"create_context","context_id":"c"}')
    with pytest.raises(ProtocolError):
        decode_request('{"id":2,"op":"create_context","language":7}')


def test_ready_languages_and_reseed_skipped_are_encoded_in_order() -> None:
    ready = {
        "languages": ["python", "bash"],
        "warmup_ms": 1,
        "kernel_pid": 2,
        "default_context_id": "default",
        "v": 1,
        "event": "ready",
    }
    assert encode_event(ready) == (
        '{"event":"ready","v":1,"default_context_id":"default","kernel_pid":2,"warmup_ms":1,'
        '"languages":["python","bash"]}'
    )
    reply = reply_ok(9, {"skipped": ["default-bash"], "reseeded": [], "failed": [], "deferred": []})
    assert encode_event(reply) == (
        '{"event":"reply","id":9,"ok":true,"payload":{"deferred":[],"failed":[],"reseeded":[],'
        '"skipped":["default-bash"]}}'
    )


def test_split_text_chunks_respects_utf8_boundaries() -> None:
    assert split_text_chunks("") == []
    assert split_text_chunks("abc", 2) == ["ab", "c"]
    text = "ñ" * 5
    chunks = split_text_chunks(text, 3)
    assert "".join(chunks) == text
    assert all(len(chunk.encode()) <= 3 for chunk in chunks)
    assert chunks == ["ñ", "ñ", "ñ", "ñ", "ñ"]
    big = "x" * 70_000
    assert [len(chunk) for chunk in split_text_chunks(big)] == [65_536, 70_000 - 65_536]


def test_strip_ansi() -> None:
    assert strip_ansi("\x1b[0;31mZeroDivisionError\x1b[0m: x") == "ZeroDivisionError: x"
    assert strip_ansi("plain") == "plain"


class _Scalar:
    def item(self) -> int:
        return 7


def test_serialise_mime_keeps_strings_and_dumps_the_rest() -> None:
    bundle = {"text/plain": "42", "e2b/data": {"a": [_Scalar(), 2]}, "application/json": [1]}
    assert serialise_mime(bundle) == {
        "text/plain": "42",
        "e2b/data": '{"a": [7, 2]}',
        "application/json": "[1]",
    }


def test_serialise_mime_omits_oversized_values() -> None:
    bundle = {"image/png": "p" * 20, "text/plain": "ok"}
    assert serialise_mime(bundle, max_bytes=10) == {
        "text/plain": "ok",
        OMITTED_MIME: "image/png: 20 bytes",
    }


def test_serialise_mime_drops_the_largest_values_until_the_bundle_fits() -> None:
    bundle = {"a": "x" * 10, "b": "y" * 12, "c": "z" * 8}
    assert serialise_mime(bundle, max_bytes=100, max_total_bytes=25) == {
        "a": "x" * 10,
        "c": "z" * 8,
        OMITTED_MIME: "b: 12 bytes",
    }
    assert serialise_mime(bundle, max_bytes=100, max_total_bytes=9) == {
        "c": "z" * 8,
        OMITTED_MIME: "b: 12 bytes, a: 10 bytes",
    }


def test_two_seven_mib_values_never_leave_as_one_line() -> None:
    seven_mib = 7 * 1024 * 1024
    bundle = {"image/png": "p" * seven_mib, "image/jpeg": "j" * seven_mib}
    mime = serialise_mime(bundle)
    assert set(mime) == {"image/jpeg", OMITTED_MIME}
    assert mime[OMITTED_MIME] == f"image/png: {seven_mib} bytes"
    assert sum(len(v) for k, v in mime.items() if k != OMITTED_MIME) <= MAX_RESULT_BYTES
    line = encode_event(
        {"event": "result", "id": 1, "execution_id": "e", "is_main_result": True, "mime": mime}
    )
    assert encoded_line_bytes(line) < MAX_EVENT_LINE_BYTES


def test_omit_payload_replaces_result_and_error_payloads_only() -> None:
    result = {"event": "result", "id": 1, "execution_id": "e", "is_main_result": True, "mime": {}}
    assert omit_payload(result, 99) == {**result, "mime": {OMITTED_MIME: "result: 99 bytes"}}
    error = {
        "event": "error",
        "id": 1,
        "execution_id": "e",
        "name": "E",
        "value": "v",
        "traceback": ["t"],
    }
    assert omit_payload(error, 7) == {
        **error,
        "value": "output omitted (error: 7 bytes)",
        "traceback": [],
    }
    stdout = {"event": "stdout", "id": 1, "execution_id": "e", "text": "x", "timestamp_unix_ns": 1}
    assert omit_payload(stdout, 5) == stdout


def test_replies() -> None:
    assert encode_reply(reply_ok(1, {"b": 1, "a": 2})) == (
        '{"event":"reply","id":1,"ok":true,"payload":{"a":2,"b":1}}'
    )
    assert encode_reply(reply_error(2, "not_found", "nope")) == (
        '{"event":"reply","id":2,"ok":false,"error":{"code":"not_found","message":"nope"}}'
    )
    assert json.loads(encode_reply(reply_error(3, "made-up", "x")))["error"]["code"] == "internal"


def encode_reply(event: dict[str, object]) -> str:
    return encode_event(event)
