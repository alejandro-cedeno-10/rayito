"""Wire format between ``rayd`` and the sidecar: JSON lines over stdio, v1.

Requests arrive on stdin as ``{"id": <u64 >= 1>, "op": "...", ...}``; events
leave on stdout as ``{"event": "...", ...}``. Both codecs (this module and
``rayd-core::code::protocol``) agree byte for byte on
``tests/fixtures/protocol_v1.jsonl``: compact separators, top-level keys in
the order fixed by ``EVENT_FIELDS``/``REQUEST_FIELDS``, nested objects with
sorted keys, non-ASCII left unescaped.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Final, TypedDict

PROTOCOL_VERSION: Final = 1
MAX_STREAM_CHUNK_BYTES: Final = 64 * 1024
MAX_MIME_VALUE_BYTES: Final = 8 * 1024 * 1024
MAX_RESULT_BYTES: Final = 12 * 1024 * 1024
MAX_EVENT_LINE_BYTES: Final = 15 * 1024 * 1024
OMITTED_MIME: Final = "rayito/omitted"
PLAIN_TEXT_MIME: Final = "text/plain"

OPS: Final = frozenset(
    {
        "ping",
        "create_context",
        "execute",
        "interrupt",
        "destroy_context",
        "restart_context",
        "list_contexts",
        "reseed",
        "quiesce",
        "resume",
    }
)
EVENTS: Final = frozenset(
    {"ready", "reply", "started", "stdout", "stderr", "result", "error", "end", "kernel_died"}
)
ERROR_CODES: Final = ("not_found", "invalid_argument", "kernel_dead", "busy", "internal")

SYNTHETIC_TIMEOUT: Final = "ExecutionTimeout"
SYNTHETIC_KERNEL_DIED: Final = "KernelDied"
SYNTHETIC_KERNEL_RESTARTED: Final = "KernelRestarted"
SYNTHETIC_CONTEXT_DESTROYED: Final = "ContextDestroyed"
SYNTHETIC_ABORTED: Final = "ExecutionAborted"
SYNTHETIC_OUTPUT_TRUNCATED: Final = "OutputTruncated"

REQUEST_FIELDS: Final[dict[str, tuple[str, ...]]] = {
    "ping": (),
    "create_context": ("context_id", "language", "cwd", "envs"),
    "execute": ("context_id", "execution_id", "code", "envs"),
    "interrupt": ("context_id", "execution_id"),
    "destroy_context": ("context_id",),
    "restart_context": ("context_id", "envs"),
    "list_contexts": (),
    "reseed": (),
    "quiesce": (),
    "resume": (),
}
EVENT_FIELDS: Final[dict[str, tuple[str, ...]]] = {
    "ready": ("v", "default_context_id", "kernel_pid", "warmup_ms", "languages"),
    "reply": ("id", "ok", "payload", "error"),
    "started": ("id", "execution_id", "execution_count"),
    "stdout": ("id", "execution_id", "text", "timestamp_unix_ns"),
    "stderr": ("id", "execution_id", "text", "timestamp_unix_ns"),
    "result": ("id", "execution_id", "is_main_result", "mime"),
    "error": ("id", "execution_id", "name", "value", "traceback"),
    "end": ("id", "execution_id", "execution_count"),
    "kernel_died": ("context_id", "exit_code", "execution_id"),
}

_ANSI: Final = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class ProtocolError(ValueError):
    """A line that is not a well-formed v1 request."""


class Request(TypedDict, total=False):
    id: int
    op: str
    context_id: str
    execution_id: str
    code: str
    language: str
    cwd: str
    envs: dict[str, str]


class ReplyError(TypedDict):
    code: str
    message: str


Event = dict[str, Any]


def decode_request(line: str) -> Request:
    try:
        raw = json.loads(line)
    except ValueError as error:
        raise ProtocolError("request line is not JSON") from error
    if not isinstance(raw, dict):
        raise ProtocolError("request is not a JSON object")
    request_id = raw.get("id")
    if not isinstance(request_id, int) or isinstance(request_id, bool) or request_id < 1:
        raise ProtocolError("request field `id` must be an integer >= 1")
    op = raw.get("op")
    if not isinstance(op, str):
        raise ProtocolError("request field `op` must be a string")
    request: Request = {"id": request_id, "op": op}
    for field in ("context_id", "execution_id", "code", "language", "cwd"):
        value = raw.get(field)
        if value is not None:
            if not isinstance(value, str):
                raise ProtocolError(f"request field `{field}` must be a string")
            request[field] = value
    envs = raw.get("envs")
    if envs is not None:
        if not isinstance(envs, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in envs.items()
        ):
            raise ProtocolError("request field `envs` must be an object of strings")
        request["envs"] = dict(envs)
    return request


def encode_request(request: Mapping[str, Any]) -> str:
    op = str(request["op"])
    ordered: dict[str, Any] = {"id": request["id"], "op": op}
    for field in REQUEST_FIELDS.get(op, ()):
        if field in request:
            ordered[field] = _canonical(request[field])
    return json.dumps(ordered, separators=(",", ":"), ensure_ascii=False)


def encode_event(event: Mapping[str, Any]) -> str:
    name = str(event["event"])
    ordered: dict[str, Any] = {"event": name}
    for field in EVENT_FIELDS[name]:
        if field in event:
            ordered[field] = _canonical(event[field])
    return json.dumps(ordered, separators=(",", ":"), ensure_ascii=False)


def _canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _canonical(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, list | tuple):
        return [_canonical(item) for item in value]
    return value


def split_text_chunks(text: str, max_bytes: int = MAX_STREAM_CHUNK_BYTES) -> list[str]:
    """Cut ``text`` into pieces of at most ``max_bytes`` UTF-8 bytes, never
    inside a multi-byte sequence."""
    data = text.encode("utf-8", errors="replace")
    if len(data) <= max_bytes:
        return [text] if text else []
    chunks: list[str] = []
    start = 0
    while start < len(data):
        end = min(start + max_bytes, len(data))
        while end < len(data) and end > start and (data[end] & 0xC0) == 0x80:
            end -= 1
        chunks.append(data[start:end].decode("utf-8", errors="replace"))
        start = end
    return chunks


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def strip_plain_text_ansi(mime: Mapping[str, str]) -> dict[str, str]:
    """A copy of a serialised bundle whose ``text/plain`` has ANSI escape
    sequences removed; every other entry is untouched."""
    return {
        key: strip_ansi(value) if key == PLAIN_TEXT_MIME else value for key, value in mime.items()
    }


def serialise_mime(
    bundle: Mapping[str, Any],
    max_bytes: int = MAX_MIME_VALUE_BYTES,
    max_total_bytes: int = MAX_RESULT_BYTES,
) -> dict[str, str]:
    """Every mime entry as a string: ``str`` verbatim, anything else as JSON
    (numpy scalars through ``.item()``). A value above ``max_bytes`` is
    replaced by a note under ``rayito/omitted``; then, while the bundle as a
    whole exceeds ``max_total_bytes`` (one ``result`` is one line and
    ``rayd`` reads lines up to 16 MiB), the largest remaining value goes
    the same way."""
    serialised: dict[str, str] = {}
    sizes: dict[str, int] = {}
    omitted: list[str] = []
    for mime, value in bundle.items():
        text = value if isinstance(value, str) else json.dumps(value, default=_json_default)
        size = len(text.encode("utf-8", errors="replace"))
        if size > max_bytes:
            omitted.append(f"{mime}: {size} bytes")
        else:
            serialised[str(mime)] = text
            sizes[str(mime)] = size
    while sizes and sum(sizes.values()) > max_total_bytes:
        largest = max(sizes, key=sizes.__getitem__)
        omitted.append(f"{largest}: {sizes.pop(largest)} bytes")
        del serialised[largest]
    if omitted:
        serialised[OMITTED_MIME] = ", ".join(omitted)
    return serialised


def encoded_line_bytes(line: str) -> int:
    return len(line.encode("utf-8", errors="replace"))


def omit_payload(event: Mapping[str, Any], line_bytes: int) -> Event:
    """The same ``result`` or ``error`` with its payload replaced by a note,
    for a line that would not fit ``rayd``'s reader (JSON escaping can grow
    a bundle past ``MAX_RESULT_BYTES``, a traceback has no cap of its own).
    ``stdout``/``stderr`` chunks and replies are bounded by construction and
    come back unchanged."""
    name = str(event.get("event"))
    note = f"{name}: {line_bytes} bytes"
    if name == "result":
        return {**event, "mime": {OMITTED_MIME: note}}
    if name == "error":
        return {**event, "value": f"output omitted ({note})", "traceback": []}
    return dict(event)


def _json_default(value: Any) -> Any:
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return item()
        except (TypeError, ValueError):
            pass
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        try:
            return tolist()
        except (TypeError, ValueError):
            pass
    return str(value)


def reply_ok(request_id: int, payload: Mapping[str, Any]) -> Event:
    return {"event": "reply", "id": request_id, "ok": True, "payload": dict(payload)}


def reply_error(request_id: int, code: str, message: str) -> Event:
    if code not in ERROR_CODES:
        code = "internal"
    return {
        "event": "reply",
        "id": request_id,
        "ok": False,
        "error": {"code": code, "message": message},
    }
