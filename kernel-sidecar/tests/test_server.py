"""The dispatcher and the context queue machine on any host, against a fake
kernel context that scripts a few cells (``sleep``, ``print``, ``die``)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

import pytest

from rayito_kernel_sidecar import server as server_module
from rayito_kernel_sidecar.executions import Emit, ExecutionOutcome
from rayito_kernel_sidecar.kernels import ContextBase, ExecutionSlot
from rayito_kernel_sidecar.logging import SidecarLogger
from rayito_kernel_sidecar.server import ServerOptions, SidecarServer


class MemoryTransport:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue[str | None] = asyncio.Queue()
        self.outgoing: asyncio.Queue[str] = asyncio.Queue()

    async def read_line(self) -> str | None:
        return await self.incoming.get()

    async def write_line(self, line: str) -> None:
        await self.outgoing.put(line)

    async def next_event(self, timeout: float = 2.0) -> dict[str, Any]:
        line = await asyncio.wait_for(self.outgoing.get(), timeout)
        return json.loads(line)  # type: ignore[no-any-return]

    async def events_until(self, predicate: Any, timeout: float = 2.0) -> list[dict[str, Any]]:
        seen: list[dict[str, Any]] = []
        while True:
            event = await self.next_event(timeout)
            seen.append(event)
            if predicate(event):
                return seen


class FakeKernelContext(ContextBase):
    started_pids = 1000

    def __init__(
        self,
        context_id: str,
        language: str,
        cwd: str,
        envs: Mapping[str, str],
        logger: SidecarLogger,
        **kw: Any,
    ) -> None:
        super().__init__(context_id, language, cwd, envs, logger, **kw)
        self.starts = 0
        self.interrupts = 0
        self.stops = 0
        self.reseeds = 0
        self._abort: asyncio.Queue[str] = asyncio.Queue()
        self._interrupted = asyncio.Event()

    async def _start_kernel(self) -> None:
        await asyncio.sleep(0.05)
        FakeKernelContext.started_pids += 1
        self.kernel_pid = FakeKernelContext.started_pids
        self.starts += 1
        self._abort = asyncio.Queue()

    async def _stop_kernel(self) -> None:
        self.stops += 1
        self.kernel_pid = None

    async def _interrupt_kernel(self) -> None:
        self.interrupts += 1
        self._interrupted.set()

    async def _abort_running(self, reason: str) -> None:
        self._abort.put_nowait(reason)

    async def _run_cell(
        self, slot: ExecutionSlot, code: str, envs: Mapping[str, str], emit: Emit
    ) -> None:
        base = {"id": slot.request_id, "execution_id": slot.execution_id}
        await emit({"event": "started", **base, "execution_count": self.starts})
        if code.startswith("sleep"):
            seconds = float(code.split()[1])
            self._interrupted.clear()
            waiter = asyncio.ensure_future(self._interrupted.wait())
            aborter = asyncio.ensure_future(self._abort.get())
            done, _ = await asyncio.wait(
                {waiter, aborter}, timeout=seconds, return_when=asyncio.FIRST_COMPLETED
            )
            if aborter in done:
                waiter.cancel()
                reason = aborter.result()
                await emit(
                    {
                        "event": "error",
                        **base,
                        "name": reason,
                        "value": "cancelled",
                        "traceback": [],
                    }
                )
                await emit({"event": "end", **base, "execution_count": 0})
                return
            aborter.cancel()
            if waiter in done:
                await emit(
                    {
                        "event": "error",
                        **base,
                        "name": "KeyboardInterrupt",
                        "value": "",
                        "traceback": ["KeyboardInterrupt"],
                    }
                )
            else:
                waiter.cancel()
        elif code == "die":
            await emit(
                {
                    "event": "error",
                    **base,
                    "name": "KernelDied",
                    "value": "kernel process exited (code 3)",
                    "traceback": [],
                }
            )
            await emit({"event": "end", **base, "execution_count": 0})
            self.kernel_died(3)
            return
        elif code.startswith("print"):
            await emit({"event": "stdout", **base, "text": "hola\n", "timestamp_unix_ns": 1})
        else:
            await emit(
                {"event": "result", **base, "is_main_result": True, "mime": {"text/plain": code}}
            )
        await emit({"event": "end", **base, "execution_count": self.starts})

    async def _run_silent_cell(self, code: str) -> ExecutionOutcome:
        self.reseeds += 1
        return ExecutionOutcome(ended_by="kernel")

    async def _probe_kernel(self, timeout: float) -> bool:
        return True


class Harness:
    def __init__(self, transport: MemoryTransport, server: SidecarServer) -> None:
        self.transport = transport
        self.server = server
        self.task: asyncio.Task[None] | None = None

    def context(self, context_id: str = "default") -> FakeKernelContext:
        context = self.server.contexts[context_id]
        assert isinstance(context, FakeKernelContext)
        return context

    async def send(self, request_id: int, op: str, **fields: Any) -> None:
        await self.transport.incoming.put(json.dumps({"id": request_id, "op": op, **fields}))

    async def reply(self, request_id: int, timeout: float = 2.0) -> dict[str, Any]:
        events = await self.transport.events_until(
            lambda e: e.get("event") == "reply" and e.get("id") == request_id, timeout
        )
        return events[-1]

    async def collect(self, request_id: int, timeout: float = 2.0) -> list[dict[str, Any]]:
        events = await self.transport.events_until(
            lambda e: (
                e.get("id") == request_id and (e.get("event") == "end" or e.get("event") == "reply")
            ),
            timeout,
        )
        return [event for event in events if event.get("id") == request_id]

    async def stop(self) -> None:
        self.server.stop()
        if self.task is not None:
            await asyncio.wait_for(self.task, 2.0)


LANGUAGES_OF_THE_FAKE = ("python", "bash")


async def start_fake_server(languages: tuple[str, ...]) -> tuple[Harness, dict[str, Any]]:
    transport = MemoryTransport()
    server: SidecarServer | None = None

    def factory(context_id: str, language: str, cwd: str, envs: Mapping[str, str]) -> ContextBase:
        assert server is not None
        return FakeKernelContext(
            context_id, language, cwd, envs, SidecarLogger(), on_kernel_died=server.on_kernel_died
        )

    server = SidecarServer(
        transport,
        factory,
        SidecarLogger(),
        ServerOptions(context_ready_timeout=1.0, languages=languages),
    )
    harness = Harness(transport, server)
    harness.task = asyncio.create_task(server.run())
    ready = await transport.next_event()
    return harness, ready


@pytest.fixture
async def harness() -> Any:
    harness, ready = await start_fake_server(LANGUAGES_OF_THE_FAKE)
    assert ready["event"] == "ready"
    assert ready["default_context_id"] == "default"
    assert ready["v"] == 1
    assert ready["languages"] == ["bash", "python"]
    yield harness
    await harness.stop()


@pytest.fixture
async def python_only_harness() -> Any:
    harness, ready = await start_fake_server(())
    assert ready["languages"] == ["python"]
    yield harness
    await harness.stop()


async def test_ready_is_emitted_once_and_ping_reports_the_default(harness: Harness) -> None:
    await harness.send(1, "ping")
    reply = await harness.reply(1)
    assert reply["ok"] is True
    assert reply["payload"] == {"kernel_ready": True, "contexts": 1}
    assert harness.transport.outgoing.empty()


async def test_unknown_op_is_invalid_argument(harness: Harness) -> None:
    await harness.send(2, "frobnicate")
    reply = await harness.reply(2)
    assert reply["ok"] is False
    assert reply["error"]["code"] == "invalid_argument"


async def test_execute_events_carry_the_request_id_and_execution_id(harness: Harness) -> None:
    await harness.send(3, "execute", context_id="default", execution_id="exec-a", code="42")
    events = await harness.collect(3)
    assert [e["event"] for e in events] == ["started", "result", "end"]
    assert all(e["execution_id"] == "exec-a" for e in events)
    assert events[1]["mime"] == {"text/plain": "42"}


async def test_execute_on_unknown_context_is_not_found(harness: Harness) -> None:
    await harness.send(4, "execute", context_id="nope", execution_id="exec-b", code="1")
    reply = await harness.reply(4)
    assert reply["ok"] is False
    assert reply["error"]["code"] == "not_found"


async def test_executions_queue_fifo_on_one_context(harness: Harness) -> None:
    await harness.send(5, "execute", context_id="default", execution_id="exec-1", code="sleep 0.3")
    await harness.send(6, "execute", context_id="default", execution_id="exec-2", code="1")
    events = await harness.transport.events_until(
        lambda e: e.get("id") == 6 and e["event"] == "end"
    )
    order = [(e["id"], e["event"]) for e in events]
    assert order.index((5, "end")) < order.index((6, "started"))


async def test_interrupt_of_a_queued_execution_ends_it_without_error(harness: Harness) -> None:
    await harness.send(7, "execute", context_id="default", execution_id="exec-1", code="sleep 0.5")
    await harness.send(8, "execute", context_id="default", execution_id="exec-2", code="1")
    await harness.transport.events_until(lambda e: e.get("id") == 7 and e["event"] == "started")
    await asyncio.sleep(0.05)
    assert harness.context().queued_execution_ids() == ["exec-2"]
    await harness.send(9, "interrupt", context_id="default", execution_id="exec-2")
    events = await harness.transport.events_until(
        lambda e: e.get("id") == 8 and e["event"] == "end"
    )
    assert [e["event"] for e in events if e.get("id") == 8] == ["end"]
    assert events[-1]["execution_count"] == 0
    assert harness.context().interrupts == 0


async def test_interrupt_of_the_running_execution_signals_the_kernel(harness: Harness) -> None:
    await harness.send(10, "execute", context_id="default", execution_id="exec-1", code="sleep 5")
    await harness.transport.events_until(lambda e: e.get("id") == 10 and e["event"] == "started")
    await harness.send(11, "interrupt", context_id="default", execution_id="exec-1")
    events = await harness.collect(10)
    assert harness.context().interrupts == 1
    assert [e["event"] for e in events] == ["error", "end"]
    assert events[0]["name"] == "KeyboardInterrupt"


async def test_restart_cancels_running_and_queued_with_kernel_restarted(harness: Harness) -> None:
    await harness.send(12, "execute", context_id="default", execution_id="exec-1", code="sleep 5")
    await harness.send(13, "execute", context_id="default", execution_id="exec-2", code="1")
    await harness.transport.events_until(lambda e: e.get("id") == 12 and e["event"] == "started")
    await asyncio.sleep(0.05)
    previous_pid = harness.context().kernel_pid
    await harness.send(14, "restart_context", context_id="default", envs={"M4": "1"})
    events = await harness.transport.events_until(lambda e: e.get("id") == 14)
    by_id = {}
    for event in events:
        by_id.setdefault(event.get("id"), []).append(event["event"])
    assert by_id[12] == ["error", "end"] or by_id[12] == ["started", "error", "end"]
    assert by_id[13] == ["error", "end"]
    assert [e["name"] for e in events if e["event"] == "error"] == ["KernelRestarted"] * 2
    assert events.index(next(e for e in events if e.get("id") == 14)) == len(events) - 1
    assert events[-1]["ok"] is True
    assert events[-1]["payload"]["kernel_pid"] != previous_pid
    assert harness.context().envs == {"M4": "1"}
    await harness.send(15, "execute", context_id="default", execution_id="exec-3", code="2")
    assert [e["event"] for e in await harness.collect(15)] == ["started", "result", "end"]


async def test_destroy_cancels_in_flight_with_context_destroyed(harness: Harness) -> None:
    await harness.send(16, "create_context", context_id="ctx-1", cwd="/tmp", envs={})
    reply = await harness.reply(16)
    assert reply["ok"] and reply["payload"]["kernel_pid"] > 0
    await harness.send(17, "execute", context_id="ctx-1", execution_id="exec-1", code="sleep 5")
    await harness.transport.events_until(lambda e: e.get("id") == 17 and e["event"] == "started")
    await harness.send(18, "destroy_context", context_id="ctx-1")
    events = await harness.transport.events_until(lambda e: e.get("id") == 18)
    assert [e["event"] for e in events if e.get("id") == 17] == ["error", "end"]
    assert next(e for e in events if e["event"] == "error")["name"] == "ContextDestroyed"
    assert events[-1]["ok"] is True
    await harness.send(19, "list_contexts")
    reply = await harness.reply(19)
    assert [c["context_id"] for c in reply["payload"]["contexts"]] == ["default"]
    await harness.send(20, "destroy_context", context_id="ctx-1")
    assert (await harness.reply(20))["error"]["code"] == "not_found"


async def test_list_contexts_reports_default_first_with_fields(harness: Harness) -> None:
    await harness.send(21, "create_context", context_id="ctx-2", cwd="/srv", envs={"A": "1"})
    await harness.reply(21)
    await harness.send(22, "list_contexts")
    contexts = (await harness.reply(22))["payload"]["contexts"]
    assert [c["context_id"] for c in contexts] == ["default", "ctx-2"]
    assert contexts[1] == {
        "context_id": "ctx-2",
        "language": "python",
        "cwd": "/srv",
        "kernel_pid": harness.context("ctx-2").kernel_pid,
        "state": "ready",
    }
    assert harness.context("ctx-2").envs == {"A": "1"}


async def test_create_context_refuses_duplicates_and_the_cap(harness: Harness) -> None:
    await harness.send(23, "create_context", context_id="default", cwd="/", envs={})
    assert (await harness.reply(23))["error"]["code"] == "invalid_argument"
    for index in range(7):
        await harness.send(30 + index, "create_context", context_id=f"c{index}", cwd="/", envs={})
        assert (await harness.reply(30 + index))["ok"]
    await harness.send(40, "create_context", context_id="c-over", cwd="/", envs={})
    assert (await harness.reply(40))["error"]["code"] == "invalid_argument"


async def test_kernel_death_ends_the_cell_and_the_context_recovers(harness: Harness) -> None:
    await harness.send(41, "execute", context_id="default", execution_id="exec-1", code="die")
    events = await harness.transport.events_until(lambda e: e.get("event") == "kernel_died")
    assert [e["event"] for e in events if e.get("id") == 41] == ["started", "error", "end"]
    assert events[-1] == {
        "event": "kernel_died",
        "context_id": "default",
        "exit_code": 3,
        "execution_id": "exec-1",
    }
    await harness.send(42, "execute", context_id="default", execution_id="exec-2", code="3")
    assert [e["event"] for e in await harness.collect(42)] == ["started", "result", "end"]
    assert harness.context().starts == 2


async def test_reseed_and_resume_cover_every_context(harness: Harness) -> None:
    await harness.send(43, "create_context", context_id="ctx-3", cwd="/", envs={})
    await harness.reply(43)
    await harness.send(44, "reseed")
    assert (await harness.reply(44))["payload"] == {
        "reseeded": ["default", "ctx-3"],
        "deferred": [],
        "failed": [],
        "skipped": [],
    }
    assert harness.context().reseeds == 1
    await harness.send(45, "resume")
    assert (await harness.reply(45))["payload"] == {
        "contexts": [
            {"context_id": "default", "alive": True},
            {"context_id": "ctx-3", "alive": True},
        ]
    }
    await harness.send(46, "quiesce")
    assert (await harness.reply(46))["ok"] is True


async def test_concurrent_restart_is_busy(harness: Harness) -> None:
    context = harness.context()
    first = asyncio.create_task(context.restart({}))
    await asyncio.sleep(0)
    await harness.send(47, "restart_context", context_id="default", envs={})
    assert (await harness.reply(47))["error"]["code"] == "busy"
    await first


async def test_an_event_line_above_the_limit_leaves_with_its_payload_omitted(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server_module, "MAX_EVENT_LINE_BYTES", 200)
    await harness.send(49, "execute", context_id="default", execution_id="exec-big", code="x" * 300)
    events = await harness.collect(49)
    assert [e["event"] for e in events] == ["started", "result", "end"]
    assert list(events[1]["mime"]) == ["rayito/omitted"]
    note = events[1]["mime"]["rayito/omitted"]
    assert note.startswith("result: ")
    assert int(note.removeprefix("result: ").removesuffix(" bytes")) > 300


async def test_malformed_lines_are_ignored(harness: Harness) -> None:
    await harness.transport.incoming.put("not json")
    await harness.send(48, "ping")
    assert (await harness.reply(48))["ok"] is True


async def test_reseed_defers_a_busy_context_and_reseeds_it_after_the_cell(
    harness: Harness,
) -> None:
    await harness.send(50, "execute", context_id="default", execution_id="exec-50", code="sleep 1")
    await harness.transport.events_until(lambda e: e.get("event") == "started")
    assert harness.context().reseed_plan() == "busy"
    await harness.send(51, "reseed")
    reply = await harness.reply(51, timeout=0.5)
    assert reply["payload"] == {
        "reseeded": [],
        "deferred": ["default"],
        "failed": [],
        "skipped": [],
    }
    assert harness.context().reseeds == 0, "the deferred reseed waits for the cell"
    await harness.send(52, "interrupt", context_id="default", execution_id="exec-50")
    await harness.reply(52)
    await harness.transport.events_until(lambda e: e.get("event") == "end" and e.get("id") == 50)
    for _ in range(20):
        if harness.context().reseeds == 1:
            break
        await asyncio.sleep(0.05)
    assert harness.context().reseeds == 1
    assert harness.context().reseed_plan() == "idle"


async def test_reseed_reports_a_restarting_context_as_failed(harness: Harness) -> None:
    context = harness.context()
    restart = asyncio.create_task(context.restart({}))
    await asyncio.sleep(0)
    assert context.reseed_plan() == "unavailable"
    await harness.send(53, "reseed")
    assert (await harness.reply(53))["payload"] == {
        "reseeded": [],
        "deferred": [],
        "failed": ["default"],
        "skipped": [],
    }
    await restart
    assert context.reseed_plan() == "idle"


async def test_create_context_with_a_language_and_list_reports_it(harness: Harness) -> None:
    await harness.send(60, "create_context", context_id="default-bash", language="bash", cwd="/")
    reply = await harness.reply(60)
    assert reply["ok"] and reply["payload"]["kernel_pid"] > 0
    assert harness.context("default-bash").language == "bash"
    assert harness.context("default-bash").starts == 1
    await harness.send(61, "list_contexts")
    contexts = (await harness.reply(61))["payload"]["contexts"]
    assert [(c["context_id"], c["language"]) for c in contexts] == [
        ("default", "python"),
        ("default-bash", "bash"),
    ]
    await harness.send(62, "create_context", context_id="ctx-py", cwd="/")
    await harness.reply(62)
    assert harness.context("ctx-py").language == "python"


async def test_unknown_and_unavailable_languages_are_refused(harness: Harness) -> None:
    await harness.send(63, "create_context", context_id="ctx-r", language="r", cwd="/")
    reply = await harness.reply(63)
    assert reply["ok"] is False
    assert reply["error"] == {"code": "invalid_argument", "message": "unknown language"}
    await harness.send(64, "create_context", context_id="ctx-js", language="javascript", cwd="/")
    reply = await harness.reply(64)
    assert reply["error"] == {"code": "invalid_argument", "message": "language not installed"}
    await harness.send(71, "create_context", context_id="ctx-ts", language="typescript", cwd="/")
    reply = await harness.reply(71)
    assert reply["error"] == {"code": "invalid_argument", "message": "language not installed"}
    await harness.send(72, "create_context", context_id="ctx-alias", language="ts", cwd="/")
    reply = await harness.reply(72)
    assert reply["error"] == {"code": "invalid_argument", "message": "unknown language"}
    await harness.send(65, "list_contexts")
    contexts = (await harness.reply(65))["payload"]["contexts"]
    assert [c["context_id"] for c in contexts] == ["default"]


async def test_python_only_sidecar_refuses_bash(python_only_harness: Harness) -> None:
    harness = python_only_harness
    await harness.send(66, "create_context", context_id="default-bash", language="bash", cwd="/")
    reply = await harness.reply(66)
    assert reply["error"] == {"code": "invalid_argument", "message": "language not installed"}
    await harness.send(67, "create_context", context_id="ctx-1", language="python", cwd="/")
    assert (await harness.reply(67))["ok"] is True


async def test_reseed_skips_non_python_contexts(harness: Harness) -> None:
    await harness.send(68, "create_context", context_id="default-bash", language="bash", cwd="/")
    await harness.reply(68)
    await harness.send(69, "reseed")
    assert (await harness.reply(69))["payload"] == {
        "reseeded": ["default"],
        "deferred": [],
        "failed": [],
        "skipped": ["default-bash"],
    }
    assert harness.context("default-bash").reseeds == 0
    await harness.send(70, "resume")
    assert (await harness.reply(70))["payload"] == {
        "contexts": [
            {"context_id": "default", "alive": True},
            {"context_id": "default-bash", "alive": True},
        ]
    }
