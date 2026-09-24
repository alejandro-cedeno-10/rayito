"""``run_execution`` over a scripted inbox on any host: the bounded inbox
holds the channel pumps back while the cell's events cannot leave, and the
sentinels still get through."""

from __future__ import annotations

import asyncio
from typing import Any

from rayito_kernel_sidecar.executions import (
    KIND_ABORT,
    KIND_IOPUB,
    KIND_SHELL,
    Inbox,
    InboxItem,
    run_execution,
)

MSG_ID = "msg-1"


def iopub(msg_type: str, **content: Any) -> InboxItem:
    return (
        KIND_IOPUB,
        {"parent_header": {"msg_id": MSG_ID}, "msg_type": msg_type, "content": content},
    )


def shell_reply() -> InboxItem:
    return (
        KIND_SHELL,
        {
            "parent_header": {"msg_id": MSG_ID},
            "msg_type": "execute_reply",
            "content": {"status": "ok", "execution_count": 1},
        },
    )


class ScriptedIo:
    def __init__(self, capacity: int) -> None:
        self._inbox = Inbox(capacity)

    @property
    def inbox(self) -> Inbox:
        return self._inbox

    def submit(self, code: str, *, silent: bool, store_history: bool) -> str:
        return MSG_ID


async def pump(inbox: Inbox, items: list[InboxItem]) -> None:
    for item in items:
        await inbox.put(item)


async def settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


async def test_channel_messages_wait_for_a_slot_and_sentinels_do_not() -> None:
    inbox = Inbox(capacity=4)
    task = asyncio.create_task(pump(inbox, [iopub("stream", name="stdout", text="x")] * 10))
    await settle()
    assert inbox.qsize() == 4
    assert not task.done()
    inbox.put_sentinel((KIND_ABORT, "KernelRestarted"))
    assert inbox.qsize() == 5
    for _ in range(3):
        await inbox.get()
    await settle()
    assert inbox.qsize() == 5
    assert not task.done()
    drained = [await inbox.get() for _ in range(5)]
    assert drained[1][0] == KIND_ABORT
    await settle()
    assert task.done()


async def test_a_blocked_emit_keeps_the_inbox_depth_bounded() -> None:
    io = ScriptedIo(capacity=8)
    gate = asyncio.Event()
    emitted: list[dict[str, Any]] = []

    async def emit(event: dict[str, Any]) -> None:
        emitted.append(event)
        await gate.wait()

    chunks = 100
    script = [
        iopub("execute_input", code="print", execution_count=1),
        *([iopub("stream", name="stdout", text="hola\n")] * chunks),
        shell_reply(),
        iopub("status", execution_state="idle"),
    ]
    pumping = asyncio.create_task(pump(io.inbox, script))
    running = asyncio.create_task(run_execution(io, 7, "exec-7", "print", {}, emit))
    await settle()
    assert [e["event"] for e in emitted] == ["started"]
    assert io.inbox.qsize() <= 8
    assert not pumping.done()
    gate.set()
    outcome = await asyncio.wait_for(running, 5.0)
    await asyncio.wait_for(pumping, 5.0)
    assert outcome.ended_by == "kernel"
    assert [e["event"] for e in emitted[:2]] == ["started", "stdout"]
    assert emitted[-1]["event"] == "end"
    assert sum(e["event"] == "stdout" for e in emitted) == chunks
    assert io.inbox.qsize() == 0


COLOURED_42 = "\x1b[33m42\x1b[39m"
COLOURED_STREAM = "\x1b[31mred\x1b[0m\n"


async def run_scripted(script: list[InboxItem], **options: Any) -> list[dict[str, Any]]:
    io = ScriptedIo(capacity=len(script))
    emitted: list[dict[str, Any]] = []

    async def emit(event: dict[str, Any]) -> None:
        emitted.append(event)

    await pump(io.inbox, script)
    outcome = await asyncio.wait_for(run_execution(io, 1, "exec-1", "x", {}, emit, **options), 5.0)
    assert outcome.ended_by == "kernel"
    return emitted


async def test_plain_text_ansi_stripped_only_when_asked() -> None:
    """Deno colours its ``execute_result`` (AWS_API_NOTES.md Q61); only the
    ``text/plain`` of result bundles is cleaned, stream text is user data."""
    script = [
        iopub("execute_input", code="x", execution_count=1),
        iopub("stream", name="stdout", text=COLOURED_STREAM),
        iopub("display_data", data={"text/plain": COLOURED_42, "text/html": "<b>\x1b[1m</b>"}),
        iopub("execute_result", data={"text/plain": COLOURED_42}, execution_count=1),
        shell_reply(),
        iopub("status", execution_state="idle"),
    ]
    for strip, plain in ((True, "42"), (False, COLOURED_42)):
        events = await run_scripted(script, strip_plain_text_ansi=strip)
        results = [e for e in events if e["event"] == "result"]
        assert [r["mime"]["text/plain"] for r in results] == [plain, plain]
        assert results[0]["mime"]["text/html"] == "<b>\x1b[1m</b>"
        assert [e["text"] for e in events if e["event"] == "stdout"] == [COLOURED_STREAM]
    default = await run_scripted(script)
    main = next(e for e in default if e["event"] == "result" and e["is_main_result"])
    assert main["mime"] == {"text/plain": COLOURED_42}
