"""JSON lines on stderr, one object per line: ``{"level", "msg", <fields>}``.

``rayd`` parses these lines and re-emits only the allowlisted fields through
its own logger, so a field outside ``ALLOWED_FIELDS`` is dropped here first.
``msg`` values are fixed literals; nothing that comes from a cell (code,
output, mime payloads, envs, cwd, tracebacks) ever becomes a field value:
lengths and counts travel instead (``code_len``, ``text_len``, ``chunks``).
"""

from __future__ import annotations

import json
import sys
from typing import Any, Final, TextIO

ALLOWED_FIELDS: Final = frozenset(
    {
        "op",
        "id",
        "context_id",
        "execution_id",
        "execution_count",
        "events",
        "results",
        "mime_types",
        "timeout_ms",
        "outcome",
        "kernel_pid",
        "attempt",
        "backoff_ms",
        "exit_code",
        "warmup_ms",
        "restart_ms",
        "duration_ms",
        "bytes",
        "chunks",
        "contexts",
        "reseeded",
        "deferred",
        "failed",
        "skipped",
        "language",
        "languages",
        "code_len",
        "text_len",
        "reason",
        "state",
        "queued",
    }
)
LEVELS: Final = ("debug", "info", "warn", "error")


class SidecarLogger:
    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream if stream is not None else sys.stderr

    def log(self, level: str, msg: str, **fields: Any) -> None:
        if level not in LEVELS:
            level = "info"
        record: dict[str, Any] = {"level": level, "msg": msg}
        for key, value in fields.items():
            if key in ALLOWED_FIELDS and value is not None:
                record[key] = _plain(value)
        self._stream.write(json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n")
        self._stream.flush()

    def debug(self, msg: str, **fields: Any) -> None:
        self.log("debug", msg, **fields)

    def info(self, msg: str, **fields: Any) -> None:
        self.log("info", msg, **fields)

    def warn(self, msg: str, **fields: Any) -> None:
        self.log("warn", msg, **fields)

    def error(self, msg: str, **fields: Any) -> None:
        self.log("error", msg, **fields)


def _plain(value: Any) -> Any:
    if isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    return str(value)
