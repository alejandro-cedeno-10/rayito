"""`AsyncSandbox.agent`: igual que `sandbox_sync/agent.py` sobre `asyncio`
(`ai-agent-core`, design.md §4). Cancelar la tarea que itera un
`AsyncAgentStream` (o salir de su `async with` por una `CancelledError`) llama a
`await stream.abort()` desde `__aexit__`."""

from __future__ import annotations

import asyncio
import logging
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Any, cast

from rayito._agent._domain import DEFAULT_AGENT_RUNTIME, AgentLimits, AgentSpec
from rayito._agent._events import AgentEvent, AgentFailed, AgentResult, Done
from rayito._agent._runtime import AgentRuntime, RuntimeState
from rayito._agent._runtimes import resolve_runtime
from rayito._agent._stream_base import (
    AGENT_RUN_TAG,
    AGENT_STOP_TREE_TIMEOUT_SECONDS,
    AgentSandbox,
    ConfigCache,
    LimitTracker,
    LineBuffer,
    build_run_request,
    gateway_urls_for,
    is_sdk_limit_failure,
    stop_tree_command,
)
from rayito._agent._telemetry import done_attributes, failure_attributes, start_attributes
from rayito._limits import DEFAULT_AGENT_WORKDIR
from rayito._models import WriteEntry
from rayito.exceptions import SandboxException, TimeoutException

if TYPE_CHECKING:
    from rayito.sandbox_async.commands import AsyncCommandHandle

logger = logging.getLogger("rayito.agent")


class AsyncAgent:
    """Espejo asíncrono de `sandbox_sync.agent.Agent` (`ai-agent-core`,
    ADR-025): mismo bloque "Coste y activación" que su docstring
    (`run()`/`stream()`/`prepare()` llaman al modelo a través de una
    pasarela ya creada; ningún recurso ni opción nueva de Rayito)."""

    def __init__(self, sandbox: AgentSandbox) -> None:
        self._sandbox = sandbox
        self._config_cache = ConfigCache()

    async def run(
        self,
        prompt: str,
        *,
        spec: AgentSpec,
        runtime: str | AgentRuntime = DEFAULT_AGENT_RUNTIME,
        session_id: str | None = None,
        model: str | None = None,
        limits: AgentLimits | None = None,
        workdir: str = DEFAULT_AGENT_WORKDIR,
        attach: bool | str = "auto",
        reasoning: bool = False,
    ) -> AgentResult:
        stream = await self.stream(
            prompt,
            spec=spec,
            runtime=runtime,
            session_id=session_id,
            model=model,
            limits=limits,
            workdir=workdir,
            attach=attach,
            reasoning=reasoning,
        )
        try:
            return await stream.result()
        finally:
            await stream.aclose()

    async def stream(
        self,
        prompt: str,
        *,
        spec: AgentSpec,
        runtime: str | AgentRuntime = DEFAULT_AGENT_RUNTIME,
        session_id: str | None = None,
        model: str | None = None,
        limits: AgentLimits | None = None,
        workdir: str = DEFAULT_AGENT_WORKDIR,
        attach: bool | str = "auto",
        reasoning: bool = False,
    ) -> AsyncAgentStream:
        limits = limits or AgentLimits()
        rt = resolve_runtime(runtime)
        gateway_urls = gateway_urls_for(spec, self._sandbox.gateways)
        config = rt.build_config(spec, gateway_urls=gateway_urls, workdir=workdir)
        if self._config_cache.needs_write(rt.name, config):
            await self._sandbox.files.write_files(
                [WriteEntry(f.path, f.data, f.mode) for f in config.files]
            )
            self._config_cache.mark_applied(rt.name, config)
        request = build_run_request(
            spec=spec,
            prompt=prompt,
            workdir=workdir,
            session_id=session_id,
            model=model,
            reasoning=reasoning,
            attach=attach,
        )
        run_command = rt.command(request)
        span_context = self._sandbox._instrumentation.span(
            "rayito.agent.run", start_attributes(spec, runtime_name=rt.name)
        )
        span = span_context.__enter__()
        try:
            handle = await self._sandbox.commands.run(
                run_command.script,
                background=True,
                envs=run_command.envs,
                stdin=True,
                timeout=limits.timeout_seconds,
                max_output_bytes=limits.max_output_bytes,
                tag=AGENT_RUN_TAG,
            )
            await handle.send_stdin(run_command.stdin)
            await handle.close_stdin()
        except Exception:
            span_context.__exit__(None, None, None)
            raise
        return AsyncAgentStream(
            sandbox=self._sandbox,
            handle=handle,
            runtime=rt,
            state=rt.new_state(),
            limits=limits,
            session_id=session_id,
            span_context=span_context,
            span=span,
            attached=attach is True or attach == "auto",
        )

    async def prepare(
        self, *, runtime: str | AgentRuntime = DEFAULT_AGENT_RUNTIME, serve: bool = False
    ) -> None:
        rt = resolve_runtime(runtime)
        for step in rt.warmup_steps(serve=serve):
            handle = await self._sandbox.commands.run(
                step.cmd,
                background=True,
                timeout=None if step.background else step.timeout_seconds,
                tag=step.tag,
            )
            handle.disconnect()


class AsyncAgentStream:
    """Espejo asíncrono de `sandbox_sync.agent.AgentStream`: implementa
    `AsyncIterable[AgentEvent]`. Cancelar la tarea que corre `__anext__`
    (o un `async with` del que se sale por cancelación) dispara `abort()`."""

    def __init__(
        self,
        *,
        sandbox: AgentSandbox,
        handle: AsyncCommandHandle,
        runtime: AgentRuntime,
        state: RuntimeState,
        limits: AgentLimits,
        session_id: str | None,
        span_context: AbstractContextManager[Any],
        span: Any,
        attached: bool,
    ) -> None:
        self._sandbox = sandbox
        self._handle = handle
        self._handle_iter = handle.__aiter__()
        self._runtime = runtime
        self._state = state
        self._limits = limits
        self._tracker = LimitTracker(limits, session_id=session_id)
        self._buffer = LineBuffer()
        self._queue: list[AgentEvent] = []
        self._final: Done | AgentFailed | None = None
        self._closed = False
        self._abort_requested = False
        self._stopped = False
        self._span_context = span_context
        self._span = span
        self._attached = attached

    @property
    def session_id(self) -> str | None:
        return self._tracker.session_id

    @property
    def dropped_lines(self) -> int:
        return self._buffer.dropped

    async def __aenter__(self) -> AsyncAgentStream:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if exc_info[0] is asyncio.CancelledError:
            await self.abort()
        await self.aclose()

    def __aiter__(self) -> AsyncAgentStream:
        return self

    async def __anext__(self) -> AgentEvent:
        while not self._queue:
            if self._final is not None:
                raise StopAsyncIteration
            await self._pump()
        return self._queue.pop(0)

    async def abort(self) -> None:
        """Le da al adaptador la oportunidad de pedirle al runtime que pare
        con elegancia (`abort_command`, por ejemplo `POST /session/<id>/abort`)
        y luego mata el proceso."""
        if self._abort_requested:
            return
        self._abort_requested = True
        await self._stop()

    async def _stop(self) -> None:
        """Para el runtime y todo lo que lanzó: el `abort_command` con
        elegancia del adaptador, `stop_tree_command` y el `kill()` del
        handle. Sirve a `abort()` y a un límite del SDK (`max_steps`,
        `token_budget`), que sin esto dejaría al runtime trabajando (y
        gastando tokens) en segundo plano."""
        if self._stopped:
            return
        self._stopped = True
        command = self._runtime.abort_command(self._state)
        if command:
            try:
                await self._sandbox.commands.run(command, timeout=self._limits.timeout_seconds)
            except SandboxException:
                logger.warning("no se pudo abortar con elegancia la sesión del agente")
        try:
            await self._sandbox.commands.run(
                stop_tree_command(self._handle.pid), timeout=AGENT_STOP_TREE_TIMEOUT_SECONDS
            )
        except SandboxException:
            logger.warning("no se pudieron parar los procesos lanzados por el agente")
        await self._handle.kill()

    async def result(self) -> AgentResult:
        async for _ in self:
            pass
        final = self._final
        if isinstance(final, AgentFailed):
            self._record_failure(final)
            raise final.to_exception(self._tracker.usage)
        assert isinstance(final, Done)
        self._record_done(final)
        return AgentResult(
            session_id=final.session_id,
            text=self._tracker.last_text,
            steps=self._tracker.steps,
            usage=self._tracker.usage,
            exit_code=final.exit_code,
            tool_calls=tuple(self._tracker.tool_calls),
            dropped_lines=self._buffer.dropped,
        )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._handle.disconnect()
        self._span_context.__exit__(None, None, None)

    async def _pump(self) -> None:
        try:
            chunk = await self._handle_iter.__anext__()
        except StopAsyncIteration:
            await self._finish()
            return
        except TimeoutException:
            self._enqueue_final(AgentFailed(reason="timeout", session_id=self._tracker.session_id))
            return
        stdout, _stderr, _pty = chunk
        for line in self._buffer.feed(stdout or ""):
            for raw_event in self._runtime.parse_line(line, self._state):
                event = self._tracker.track(raw_event)
                self._queue.append(event)
                if isinstance(event, Done | AgentFailed):
                    self._final = event
                    if is_sdk_limit_failure(event):
                        await self._stop()
                        await self._drain()
                    return

    async def _finish(self) -> None:
        if self._abort_requested:
            self._enqueue_final(
                AgentFailed(
                    reason="aborted",
                    exit_code=self._handle.exit_code,
                    session_id=self._tracker.session_id,
                )
            )
            return
        if await self._ended_by_timeout():
            self._enqueue_final(AgentFailed(reason="timeout", session_id=self._tracker.session_id))
            return
        exit_code = self._handle.exit_code if self._handle.exit_code is not None else -1
        final = self._runtime.finish(self._state, exit_code)
        self._enqueue_final(cast("Done | AgentFailed", self._tracker.track(final)))

    async def _ended_by_timeout(self) -> bool:
        """Si `rayd` terminó el proceso por `AgentLimits.timeout_seconds`.
        Iterar el handle no lo dice (el stream acaba con el `EndEvent` sin
        lanzar); `wait()`, sobre el stream ya consumido, lo convierte en
        `TimeoutException` sin otra llamada."""
        try:
            await self._handle.wait()
        except TimeoutException:
            return True
        except SandboxException:
            return False
        return False

    async def _drain(self) -> None:
        """Consume el handle hasta su `EndEvent` tras pararlo por un límite:
        `rayd` sólo lo manda cuando el proceso ya no existe, así que al
        volver el cerrojo de ejecución está libre y el siguiente `run()` no
        encuentra el runtime `busy` (un proceso con hilos tarda en morir del
        todo tras el `SIGKILL`)."""
        try:
            async for _ in self._handle_iter:
                pass
        except SandboxException:
            logger.warning("no se pudo esperar al final del runtime parado")

    def _enqueue_final(self, event: Done | AgentFailed) -> None:
        self._queue.append(event)
        self._final = event

    def _record_done(self, done: Done) -> None:
        attrs = done_attributes(done, steps=self._tracker.steps, attached=self._attached)
        for key, value in attrs.items():
            self._span.set_attribute(key, value)

    def _record_failure(self, failed: AgentFailed) -> None:
        for key, value in failure_attributes(failed).items():
            self._span.set_attribute(key, value)
