"""One ``execute`` op against a live kernel, mapped to protocol events the way
E2B's ``messaging.py`` maps Jupyter messages (design D5).

The kernel's iopub and shell messages reach ``run_execution`` through the
context's inbox, already tagged with their channel; the inbox also carries
two sentinels the context injects: ``("died", exit_code)`` when the kernel
process went away and ``("abort", reason)`` when the context is being
restarted or destroyed under a running cell.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from rayito_kernel_sidecar.protocol import (
    SYNTHETIC_ABORTED,
    SYNTHETIC_KERNEL_DIED,
    Event,
    serialise_mime,
    split_text_chunks,
    strip_ansi,
    strip_plain_text_ansi,
)

Emit = Callable[[Event], Awaitable[None]]
InboxItem = tuple[str, Any]

KIND_IOPUB = "iopub"
KIND_SHELL = "shell"
KIND_DIED = "died"
KIND_ABORT = "abort"
CHANNEL_KINDS: Final = frozenset({KIND_IOPUB, KIND_SHELL})

INBOX_CAPACITY: Final = 64


class Inbox:
    """The FIFO between the channel pumps and the running cell.

    Channel messages take a slot and wait for one when none is free, so a
    cell whose events cannot leave the sidecar (``rayd`` parked on a stalled
    client, a chatty cell outpacing the network) stops draining the kernel's
    sockets and ZMQ's high-water marks hold the rest, instead of messages
    piling up in this process. The context's sentinels never wait: a
    restart or a dead kernel reaches the cell even when every slot is taken.
    """

    def __init__(self, capacity: int = INBOX_CAPACITY) -> None:
        self._items: asyncio.Queue[InboxItem] = asyncio.Queue()
        self._slots = asyncio.Semaphore(capacity)

    def qsize(self) -> int:
        return self._items.qsize()

    async def put(self, item: InboxItem) -> None:
        """A channel message; waits for a free slot."""
        await self._slots.acquire()
        self._items.put_nowait(item)

    def put_sentinel(self, item: InboxItem) -> None:
        """``died`` or ``abort``; never waits."""
        self._items.put_nowait(item)

    async def get(self) -> InboxItem:
        item = await self._items.get()
        if item[0] in CHANNEL_KINDS:
            self._slots.release()
        return item


class KernelIo(Protocol):
    """What an execution needs from a context: a way to submit code and the
    inbox where the channel pumps and the context's sentinels deliver."""

    @property
    def inbox(self) -> Inbox: ...

    def submit(self, code: str, *, silent: bool, store_history: bool) -> str: ...


@dataclass
class ExecutionOutcome:
    """How a cell ended: ``kernel`` (normal reply), ``died`` or ``abort``."""

    ended_by: str
    execution_count: int = 0
    detail: Any = None


@dataclass
class _CellState:
    msg_id: str
    reply_seen: bool = False
    idle_seen: bool = False
    execution_count: int = 0
    started_count: int = 0
    started_emitted: bool = False
    events: int = 0
    results: int = 0
    mime_types: set[str] = field(default_factory=set)

    def complete(self) -> bool:
        return self.reply_seen and self.idle_seen


def set_envs_cell(envs: Mapping[str, str]) -> str:
    keys = sorted(envs)
    values = {key: envs[key] for key in keys}
    return (
        "import os as _o\n"
        f"_p = {{k: _o.environ.get(k) for k in {keys!r}}}\n"
        f"_o.environ.update({values!r})\n"
        "del _o\n"
    )


RESTORE_ENVS_CELL = (
    "import os as _o\n"
    "for _k, _v in _p.items():\n"
    "    if _v is None:\n"
    "        _o.environ.pop(_k, None)\n"
    "    else:\n"
    "        _o.environ[_k] = _v\n"
    "del _o, _p, _k, _v\n"
)


async def run_execution(
    io: KernelIo,
    request_id: int,
    execution_id: str,
    code: str,
    envs: Mapping[str, str],
    emit: Emit,
    *,
    strip_plain_text_ansi: bool = False,
) -> ExecutionOutcome:
    """Runs one user cell (with its silent env set/restore cells around it
    when ``envs`` is non-empty) and emits ``started``, ``stdout``/``stderr``,
    ``result``, ``error`` and exactly one ``end``. ``strip_plain_text_ansi``
    removes ANSI escapes from the ``text/plain`` of every ``result`` bundle,
    for kernels whose inspector colours results (Deno, AWS_API_NOTES.md
    Q61); stream text is left alone because colours there are the user's."""
    if envs:
        silent = await run_silent(io, set_envs_cell(envs))
        if silent.ended_by != "kernel":
            await _emit_synthetic_end(
                request_id, execution_id, silent.ended_by, emit, silent.detail, False
            )
            return silent
    outcome = await _run_user_cell(
        io, request_id, execution_id, code, emit, strips_plain_text_ansi=strip_plain_text_ansi
    )
    if envs and outcome.ended_by == "kernel":
        await run_silent(io, RESTORE_ENVS_CELL)
    return outcome


async def run_silent(io: KernelIo, code: str) -> ExecutionOutcome:
    """A cell with ``silent=True``: no ``execute_input``, no history, no
    execution count; only its reply and idle status are awaited."""
    msg_id = io.submit(code, silent=True, store_history=False)
    state = _CellState(msg_id=msg_id)
    while True:
        kind, payload = await io.inbox.get()
        if kind == KIND_DIED:
            return ExecutionOutcome(ended_by=KIND_DIED, detail=payload)
        if kind == KIND_ABORT:
            return ExecutionOutcome(ended_by=str(payload))
        if _parent_id(payload) != msg_id:
            continue
        msg_type = payload.get("msg_type")
        if kind == KIND_SHELL and msg_type == "execute_reply":
            state.reply_seen = True
        elif kind == KIND_IOPUB and msg_type == "status":
            state.idle_seen = payload.get("content", {}).get("execution_state") == "idle"
        if state.complete():
            return ExecutionOutcome(ended_by="kernel")


async def _run_user_cell(
    io: KernelIo,
    request_id: int,
    execution_id: str,
    code: str,
    emit: Emit,
    *,
    strips_plain_text_ansi: bool,
) -> ExecutionOutcome:
    msg_id = io.submit(code, silent=False, store_history=True)
    state = _CellState(msg_id=msg_id)
    while True:
        kind, payload = await io.inbox.get()
        if kind == KIND_DIED:
            await _emit_synthetic_end(
                request_id, execution_id, KIND_DIED, emit, payload, state.started_emitted
            )
            return ExecutionOutcome(ended_by=KIND_DIED)
        if kind == KIND_ABORT:
            await _emit_synthetic_end(
                request_id, execution_id, str(payload), emit, None, state.started_emitted
            )
            return ExecutionOutcome(ended_by=str(payload))
        if _parent_id(payload) != msg_id:
            continue
        if kind == KIND_SHELL:
            await _on_shell(state, payload, request_id, execution_id, emit)
        else:
            await _on_iopub(state, payload, request_id, execution_id, emit, strips_plain_text_ansi)
        if state.complete():
            count = state.execution_count or state.started_count
            if not state.started_emitted:
                await _emit_started(state, request_id, execution_id, count, emit)
            await emit(
                {
                    "event": "end",
                    "id": request_id,
                    "execution_id": execution_id,
                    "execution_count": count,
                }
            )
            return ExecutionOutcome(ended_by="kernel", execution_count=count)


async def _on_shell(
    state: _CellState, msg: Mapping[str, Any], request_id: int, execution_id: str, emit: Emit
) -> None:
    if msg.get("msg_type") != "execute_reply":
        return
    content = msg.get("content", {})
    state.reply_seen = True
    count = content.get("execution_count")
    if isinstance(count, int):
        state.execution_count = count
    if content.get("status") in ("abort", "aborted"):
        await emit(
            {
                "event": "error",
                "id": request_id,
                "execution_id": execution_id,
                "name": SYNTHETIC_ABORTED,
                "value": "the kernel aborted the request before running it",
                "traceback": [],
            }
        )


async def _on_iopub(
    state: _CellState,
    msg: Mapping[str, Any],
    request_id: int,
    execution_id: str,
    emit: Emit,
    strips_plain_text_ansi: bool,
) -> None:
    msg_type = msg.get("msg_type")
    content = msg.get("content", {})
    if msg_type == "status":
        if content.get("execution_state") == "idle":
            state.idle_seen = True
    elif msg_type == "execute_input":
        count = content.get("execution_count")
        state.started_count = count if isinstance(count, int) else 0
        await _emit_started(state, request_id, execution_id, state.started_count, emit)
    elif msg_type == "stream":
        name = "stderr" if content.get("name") == "stderr" else "stdout"
        stamp = time.time_ns()
        for chunk in split_text_chunks(str(content.get("text", ""))):
            await emit(
                {
                    "event": name,
                    "id": request_id,
                    "execution_id": execution_id,
                    "text": chunk,
                    "timestamp_unix_ns": stamp,
                }
            )
    elif msg_type in ("display_data", "execute_result", "update_display_data"):
        mime = _result_mime(content.get("data", {}), strips_plain_text_ansi)
        state.results += 1
        state.mime_types.update(mime)
        await emit(
            {
                "event": "result",
                "id": request_id,
                "execution_id": execution_id,
                "is_main_result": msg_type == "execute_result",
                "mime": mime,
            }
        )
    elif msg_type == "error":
        await emit(
            {
                "event": "error",
                "id": request_id,
                "execution_id": execution_id,
                "name": str(content.get("ename", "")),
                "value": str(content.get("evalue", "")),
                "traceback": [strip_ansi(str(line)) for line in content.get("traceback", [])],
            }
        )


def _result_mime(data: Mapping[str, Any], strips_plain_text_ansi: bool) -> dict[str, str]:
    mime = serialise_mime(data)
    return strip_plain_text_ansi(mime) if strips_plain_text_ansi else mime


async def _emit_started(
    state: _CellState, request_id: int, execution_id: str, count: int, emit: Emit
) -> None:
    state.started_emitted = True
    await emit(
        {
            "event": "started",
            "id": request_id,
            "execution_id": execution_id,
            "execution_count": count,
        }
    )


async def _emit_synthetic_end(
    request_id: int,
    execution_id: str,
    reason: str,
    emit: Emit,
    exit_code: Any,
    started_emitted: bool,
) -> None:
    """``error`` + ``end{0}`` for a cell the kernel did not finish; a kernel
    killed by its own cell (``os._exit``) never flushed its ``execute_input``,
    so ``started`` is synthesised first to keep one per execution."""
    if not started_emitted:
        await emit(
            {
                "event": "started",
                "id": request_id,
                "execution_id": execution_id,
                "execution_count": 0,
            }
        )
    if reason == KIND_DIED:
        name = SYNTHETIC_KERNEL_DIED
        value = f"kernel process exited (code {exit_code})"
    else:
        name = reason
        value = _abort_value(reason)
    await emit(
        {
            "event": "error",
            "id": request_id,
            "execution_id": execution_id,
            "name": name,
            "value": value,
            "traceback": [],
        }
    )
    await emit(
        {"event": "end", "id": request_id, "execution_id": execution_id, "execution_count": 0}
    )


def _abort_value(reason: str) -> str:
    if reason == "KernelRestarted":
        return "the context was restarted while the cell was running"
    if reason == "ContextDestroyed":
        return "the context was destroyed while the cell was running"
    return "the execution was cancelled"


def _parent_id(msg: Any) -> str | None:
    if not isinstance(msg, Mapping):
        return None
    parent = msg.get("parent_header")
    if not isinstance(parent, Mapping):
        return None
    msg_id = parent.get("msg_id")
    return str(msg_id) if msg_id is not None else None
