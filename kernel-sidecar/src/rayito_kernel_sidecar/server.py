"""The request dispatcher: reads JSON lines from a transport, runs each op in
its own task and writes events back one line at a time. The transport is
abstract so the dispatcher is tested on any host with in-memory queues and a
fake context; ``StdioTransport`` is what ``rayd`` talks to.
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol

from rayito_kernel_sidecar.kernels import ContextBase, ContextBusy, context_summary
from rayito_kernel_sidecar.languages import PYTHON, language_for
from rayito_kernel_sidecar.logging import SidecarLogger
from rayito_kernel_sidecar.protocol import (
    MAX_EVENT_LINE_BYTES,
    OPS,
    PROTOCOL_VERSION,
    Event,
    ProtocolError,
    Request,
    decode_request,
    encode_event,
    encoded_line_bytes,
    omit_payload,
    reply_error,
    reply_ok,
)

CONTEXT_READY_TIMEOUT_S: Final = 30.0
RESUME_PROBE_TIMEOUT_S: Final = 5.0
RESUME_PROBE_CONCURRENCY: Final = 8
MAX_CONTEXTS: Final = 8

ContextFactory = Callable[[str, str, str, Mapping[str, str]], ContextBase]


class Transport(Protocol):
    async def read_line(self) -> str | None: ...

    async def write_line(self, line: str) -> None: ...


class StdioTransport:
    """stdin/stdout as asyncio pipes; one lock keeps every written line whole
    even when several executions emit at once."""

    def __init__(self) -> None:
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=64 * 1024 * 1024)
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
        transport, protocol = await loop.connect_write_pipe(
            asyncio.streams.FlowControlMixin, sys.stdout
        )
        self._reader = reader
        self._writer = asyncio.StreamWriter(transport, protocol, None, loop)

    async def read_line(self) -> str | None:
        if self._reader is None:
            return None
        line = await self._reader.readline()
        if not line:
            return None
        return line.decode("utf-8", errors="replace").rstrip("\r\n")

    async def write_line(self, line: str) -> None:
        if self._writer is None:
            return
        async with self._lock:
            self._writer.write(line.encode("utf-8") + b"\n")
            await self._writer.drain()


@dataclass
class ServerOptions:
    default_context_id: str = "default"
    default_cwd: str = "/home/user"
    context_ready_timeout: float = CONTEXT_READY_TIMEOUT_S
    resume_probe_timeout: float = RESUME_PROBE_TIMEOUT_S
    max_contexts: int = MAX_CONTEXTS
    languages: tuple[str, ...] = (PYTHON,)


class SidecarServer:
    def __init__(
        self,
        transport: Transport,
        factory: ContextFactory,
        logger: SidecarLogger,
        options: ServerOptions | None = None,
    ) -> None:
        self._transport = transport
        self._factory = factory
        self._logger = logger
        self._options = options or ServerOptions()
        self._contexts: dict[str, ContextBase] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._ready_sent = False
        self._stopped = asyncio.Event()

    @property
    def contexts(self) -> Mapping[str, ContextBase]:
        return self._contexts

    async def emit(self, event: Event) -> None:
        """One event, one line; a line ``rayd`` would refuse as too long
        (a protocol fault that kills the sidecar) leaves with its payload
        omitted instead."""
        line = encode_event(event)
        size = encoded_line_bytes(line)
        if size > MAX_EVENT_LINE_BYTES:
            self._logger.warn(
                "event line too long; payload omitted",
                id=event.get("id"),
                execution_id=event.get("execution_id"),
                bytes=size,
            )
            line = encode_event(omit_payload(event, size))
        await self._transport.write_line(line)

    async def run(self) -> None:
        """Starts the default context, announces ``ready`` and serves stdin
        until EOF; then shuts every kernel down."""
        await self.start_default_context()
        stop = asyncio.ensure_future(self._stopped.wait())
        try:
            while True:
                read = asyncio.ensure_future(self._transport.read_line())
                done, _ = await asyncio.wait({read, stop}, return_when=asyncio.FIRST_COMPLETED)
                if stop in done:
                    read.cancel()
                    break
                line = read.result()
                if line is None:
                    break
                if line.strip():
                    self._spawn(self._handle_line(line))
        finally:
            stop.cancel()
            await self.shutdown()

    async def start_default_context(self) -> None:
        """Starts the Python default kernel and announces ``ready`` with the
        languages this image can serve; no other kernel is started here."""
        started = time.monotonic()
        context = self._factory(
            self._options.default_context_id, PYTHON, self._options.default_cwd, {}
        )
        self._contexts[context.context_id] = context
        await context.start()
        if not self._ready_sent:
            self._ready_sent = True
            await self.emit(
                {
                    "event": "ready",
                    "v": PROTOCOL_VERSION,
                    "default_context_id": context.context_id,
                    "kernel_pid": context.kernel_pid,
                    "warmup_ms": int((time.monotonic() - started) * 1000),
                    "languages": sorted(set(self._options.languages) | {PYTHON}),
                }
            )

    def stop(self) -> None:
        self._stopped.set()

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for context in list(self._contexts.values()):
            try:
                await context.shutdown()
            except Exception as error:
                self._logger.warn(
                    "context shutdown failed",
                    context_id=context.context_id,
                    reason=type(error).__name__,
                )
        self._contexts.clear()

    async def on_kernel_died(
        self, context_id: str, exit_code: int | None, execution_id: str | None
    ) -> None:
        event: Event = {"event": "kernel_died", "context_id": context_id, "exit_code": exit_code}
        if execution_id is not None:
            event["execution_id"] = execution_id
        await self.emit(event)

    def _spawn(self, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _handle_line(self, line: str) -> None:
        try:
            request = decode_request(line)
        except ProtocolError as error:
            self._logger.warn("request rejected", reason=type(error).__name__)
            return
        await self.handle(request)

    async def handle(self, request: Request) -> None:
        request_id = request["id"]
        op = request["op"]
        if op not in OPS:
            await self.emit(reply_error(request_id, "invalid_argument", "unknown op"))
            return
        try:
            await getattr(self, f"_op_{op}")(request)
        except ContextBusy:
            await self.emit(
                reply_error(request_id, "busy", "context is being restarted or destroyed")
            )
        except Exception as error:
            self._logger.error("op failed", op=op, id=request_id, reason=type(error).__name__)
            await self.emit(reply_error(request_id, "internal", type(error).__name__))

    async def _op_ping(self, request: Request) -> None:
        default = self._contexts.get(self._options.default_context_id)
        await self.emit(
            reply_ok(
                request["id"],
                {
                    "kernel_ready": default is not None and default.state == "ready",
                    "contexts": len(self._contexts),
                },
            )
        )

    async def _op_create_context(self, request: Request) -> None:
        request_id = request["id"]
        context_id = request.get("context_id", "")
        cwd = request.get("cwd", "") or self._options.default_cwd
        if not context_id:
            await self.emit(reply_error(request_id, "invalid_argument", "context_id is required"))
            return
        if context_id in self._contexts:
            await self.emit(reply_error(request_id, "invalid_argument", "context already exists"))
            return
        if len(self._contexts) >= self._options.max_contexts:
            await self.emit(reply_error(request_id, "invalid_argument", "context limit reached"))
            return
        language = request.get("language") or PYTHON
        if language_for(language) is None:
            await self.emit(reply_error(request_id, "invalid_argument", "unknown language"))
            return
        if language != PYTHON and language not in self._options.languages:
            await self.emit(reply_error(request_id, "invalid_argument", "language not installed"))
            return
        context = self._factory(context_id, language, cwd, request.get("envs", {}))
        self._contexts[context_id] = context
        try:
            await context.start()
        except Exception as error:
            self._contexts.pop(context_id, None)
            self._logger.error(
                "context start failed", context_id=context_id, reason=type(error).__name__
            )
            await self.emit(reply_error(request_id, "invalid_argument", "kernel failed to start"))
            return
        self._logger.info(
            "context created",
            context_id=context_id,
            language=language,
            kernel_pid=context.kernel_pid,
        )
        await self.emit(reply_ok(request_id, {"kernel_pid": context.kernel_pid}))

    async def _op_execute(self, request: Request) -> None:
        request_id = request["id"]
        execution_id = request.get("execution_id", "")
        context = self._contexts.get(request.get("context_id", ""))
        if context is None:
            await self.emit(reply_error(request_id, "not_found", "unknown context"))
            return
        if not await context.wait_ready(self._options.context_ready_timeout):
            await self.emit(reply_error(request_id, "kernel_dead", "kernel not ready in time"))
            return
        code = request.get("code", "")
        self._logger.debug(
            "execute", id=request_id, context_id=context.context_id, code_len=len(code)
        )
        await context.run(request_id, execution_id, code, request.get("envs", {}), self.emit)

    async def _op_interrupt(self, request: Request) -> None:
        context = self._contexts.get(request.get("context_id", ""))
        if context is not None:
            await context.interrupt(request.get("execution_id"))
        await self.emit(reply_ok(request["id"], {}))

    async def _op_destroy_context(self, request: Request) -> None:
        request_id = request["id"]
        context_id = request.get("context_id", "")
        context = self._contexts.get(context_id)
        if context is None:
            await self.emit(reply_error(request_id, "not_found", "unknown context"))
            return
        await context.destroy()
        self._contexts.pop(context_id, None)
        await self.emit(reply_ok(request_id, {}))

    async def _op_restart_context(self, request: Request) -> None:
        request_id = request["id"]
        context = self._contexts.get(request.get("context_id", ""))
        if context is None:
            await self.emit(reply_error(request_id, "not_found", "unknown context"))
            return
        started = time.monotonic()
        kernel_pid = await context.restart(request.get("envs", {}))
        self._logger.info(
            "context restarted",
            context_id=context.context_id,
            kernel_pid=kernel_pid,
            restart_ms=int((time.monotonic() - started) * 1000),
        )
        await self.emit(reply_ok(request_id, {"kernel_pid": kernel_pid}))

    async def _op_list_contexts(self, request: Request) -> None:
        contexts = [context_summary(context) for context in self._contexts.values()]
        await self.emit(reply_ok(request["id"], {"contexts": contexts}))

    async def _op_reseed(self, request: Request) -> None:
        """Answers at once: idle Python contexts are reseeded inline, busy
        ones get a background reseed that takes its turn after the running
        cell (so a long cell in flight at ``/resume`` never stalls the
        reply), restarting or dead ones are reported as failed, and contexts
        of other languages are listed as skipped (the reseed cell is Python
        source; their RNG state is whatever their kernel keeps)."""
        reseeded: list[str] = []
        deferred: list[str] = []
        failed: list[str] = []
        skipped: list[str] = []
        for context in list(self._contexts.values()):
            if context.language != PYTHON:
                skipped.append(context.context_id)
                continue
            plan = context.reseed_plan()
            if plan == "idle":
                (reseeded if await context.reseed() else failed).append(context.context_id)
            elif plan == "busy":
                self._spawn(self._reseed_deferred(context))
                deferred.append(context.context_id)
            else:
                failed.append(context.context_id)
        self._logger.info(
            "reseeded",
            reseeded=len(reseeded),
            deferred=len(deferred),
            failed=len(failed),
            skipped=len(skipped),
        )
        await self.emit(
            reply_ok(
                request["id"],
                {
                    "reseeded": reseeded,
                    "deferred": deferred,
                    "failed": failed,
                    "skipped": skipped,
                },
            )
        )

    async def _reseed_deferred(self, context: ContextBase) -> None:
        outcome = "reseeded" if await context.reseed() else "failed"
        self._logger.info("reseeded_deferred", context_id=context.context_id, outcome=outcome)

    async def _op_quiesce(self, request: Request) -> None:
        await self.emit(reply_ok(request["id"], {}))

    async def _op_resume(self, request: Request) -> None:
        semaphore = asyncio.Semaphore(RESUME_PROBE_CONCURRENCY)

        async def probe(context: ContextBase) -> Event:
            async with semaphore:
                alive = await context.probe(self._options.resume_probe_timeout)
            return {"context_id": context.context_id, "alive": alive}

        results = await asyncio.gather(*(probe(c) for c in list(self._contexts.values())))
        await self.emit(reply_ok(request["id"], {"contexts": list(results)}))
