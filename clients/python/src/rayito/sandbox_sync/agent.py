"""`Sandbox.agent`: `sbx.agent.run/stream/prepare` (`ai-agent-core`,
design.md §4). Aplica la configuración del runtime con `files.write_files`
(una vez por sha), lanza el script del runtime con `commands.run` en
background con stdin y trocea su stdout en eventos, imponiendo
`AgentLimits` sobre ellos. `sbx.agent` en sí es perezoso: construirlo
(`Sandbox.agent`, una propiedad) no manda ningún RPC; el primero en
costar algo es el primer `run()`/`stream()`.
"""

from __future__ import annotations

import logging
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Any, cast

from rayito._agent._domain import DEFAULT_AGENT_RUNTIME, AgentLimits, AgentSpec
from rayito._agent._events import AgentEvent, AgentFailed, AgentResult, Done
from rayito._agent._runtime import AgentRuntime, RuntimeState
from rayito._agent._runtimes import resolve_runtime
from rayito._agent._stream_base import (
    AGENT_RUN_TAG,
    AgentSandbox,
    ConfigCache,
    LimitTracker,
    LineBuffer,
    build_run_request,
    gateway_urls_for,
    is_sdk_limit_failure,
)
from rayito._agent._telemetry import done_attributes, failure_attributes, start_attributes
from rayito._limits import DEFAULT_AGENT_WORKDIR
from rayito._models import WriteEntry
from rayito.exceptions import SandboxException, TimeoutException

if TYPE_CHECKING:
    from rayito.sandbox_sync.commands import CommandHandle

logger = logging.getLogger("rayito.agent")


class Agent:
    """El agente de IA de este sandbox (`ai-agent-core`, ADR-025). Una
    instancia por `Sandbox`, creada en su constructor (como
    `.commands`/`.files`): tocarla no hace ninguna llamada; el coste
    empieza con el primer `run()`/`stream()`.

    Coste y activación
    -------------------
    Activa: llamar a `run()`, `stream()` o `prepare()`. No hace falta
        ninguna opción nueva: basta con usar `sbx.agent`.
    Recursos y llamadas AWS: ninguno nuevo de Rayito. El propio runtime
        llama al modelo (Bedrock `Converse`/`ConverseStream`, o la API que
        exponga la pasarela) a través de la pasarela ya configurada con
        `gateways=` en `create()`; sin ella, `run()`/`stream()` fallan con
        `InvalidArgumentException` antes de cualquier llamada. El sandbox
        ya factura por segundo, con o sin agente.
    Coste aproximado: depende del modelo y de los pasos, no de Rayito.
        Guía con precios de lista (us-east-1, consultados 2026-10-06,
        https://aws.amazon.com/bedrock/pricing y
        https://aws.amazon.com/lambda/pricing/): 10 pasos de Haiku 4.5
        regional (15k tokens de entrada + 400 de salida por paso) ≈ $0,187
        sin caché / $0,083 con caché de prompts; 5 minutos de MicroVM de
        2 GB ≈ $0,0119. El modelo cuesta entre 7x y 21x la VM (`cost.md`,
        "Coste de un agente").
    IAM: ninguno adicional a lo que ya pide la pasarela (`SecretGateway`,
        `secretsmanager:GetSecretValue`).
    Cómo apagarla: no llames a `sbx.agent.run()`/`stream()`/`prepare()`.
    Ejemplo:
        model_id = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
        with Sandbox.create(
            "rayito-agent",
            allow_internet_access=False,
            gateways={"bedrock": bedrock_gateway(
                "bedrock-key", region="us-east-1", models=[model_id],
            )},
        ) as sbx:
            result = sbx.agent.run(
                "lista los ficheros de /home/user",
                spec=AgentSpec(model=AgentModel(
                    provider="bedrock", id=model_id,
                    gateway="bedrock", region="us-east-1",
                )),
            )
    """

    def __init__(self, sandbox: AgentSandbox) -> None:
        self._sandbox = sandbox
        self._config_cache = ConfigCache()

    def run(
        self,
        prompt: str,
        *,
        spec: AgentSpec,
        runtime: str | AgentRuntime = DEFAULT_AGENT_RUNTIME,
        session_id: str | None = None,
        model: str | None = None,
        limits: AgentLimits | None = None,
        workdir: str = DEFAULT_AGENT_WORKDIR,
        reasoning: bool = False,
    ) -> AgentResult:
        """Corre el agente hasta que termina y devuelve su `AgentResult`.
        Una ejecución fallida lanza `AgentException` (el último evento era
        `AgentFailed`); un fallo de sandbox o de transporte (`SandboxException`,
        `TimeoutException`) se propaga tal cual."""
        with self.stream(
            prompt,
            spec=spec,
            runtime=runtime,
            session_id=session_id,
            model=model,
            limits=limits,
            workdir=workdir,
            reasoning=reasoning,
        ) as stream:
            return stream.result()

    def stream(
        self,
        prompt: str,
        *,
        spec: AgentSpec,
        runtime: str | AgentRuntime = DEFAULT_AGENT_RUNTIME,
        session_id: str | None = None,
        model: str | None = None,
        limits: AgentLimits | None = None,
        workdir: str = DEFAULT_AGENT_WORKDIR,
        reasoning: bool = False,
    ) -> AgentStream:
        """Como `run()`, pero devuelve un `AgentStream` iterable que nunca
        lanza por un fallo del agente: el último evento es `Done` o
        `AgentFailed`. Un fallo de sandbox o de transporte sí se propaga,
        al construir el stream o al iterarlo."""
        limits = limits or AgentLimits()
        rt = resolve_runtime(runtime)
        gateway_urls = gateway_urls_for(spec, self._sandbox.gateways)
        config = rt.build_config(spec, gateway_urls=gateway_urls, workdir=workdir)
        if self._config_cache.needs_write(rt.name, config):
            self._sandbox.files.write_files(
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
        )
        run_command = rt.command(request)
        span_context = self._sandbox._instrumentation.span(
            "rayito.agent.run", start_attributes(spec, runtime_name=rt.name)
        )
        span = span_context.__enter__()
        try:
            handle = self._sandbox.commands.run(
                run_command.script,
                background=True,
                envs=run_command.envs,
                stdin=True,
                timeout=limits.timeout_seconds,
                max_output_bytes=limits.max_output_bytes,
                tag=AGENT_RUN_TAG,
                kill_tree=True,
            )
            handle.send_stdin(run_command.stdin)
            handle.close_stdin()
        except Exception:
            span_context.__exit__(None, None, None)
            raise
        return AgentStream(
            sandbox=self._sandbox,
            handle=handle,
            runtime=rt,
            state=rt.new_state(),
            limits=limits,
            session_id=session_id,
            span_context=span_context,
            span=span,
        )

    def prepare(self, *, runtime: str | AgentRuntime = DEFAULT_AGENT_RUNTIME) -> None:
        """Dispara los pasos de calentamiento del runtime (`warmup_steps`)
        en segundo plano y vuelve enseguida: no espera a ninguno. Un paso
        con `background=True` corre sin plazo: es un proceso que debe seguir
        vivo y moriría al agotarlo."""
        rt = resolve_runtime(runtime)
        for step in rt.warmup_steps():
            handle = self._sandbox.commands.run(
                step.cmd,
                background=True,
                timeout=None if step.background else step.timeout_seconds,
                tag=step.tag,
            )
            handle.disconnect()


class AgentStream:
    """Iterable de `AgentEvent` sobre un `CommandHandle` en background:
    cada chunk de stdout se troceaba en líneas, cada línea se traduce con
    el adaptador del runtime y cada evento pasa por `LimitTracker` antes de
    entregarse. Nunca lanza por un fallo del agente; `result()` sí."""

    def __init__(
        self,
        *,
        sandbox: AgentSandbox,
        handle: CommandHandle,
        runtime: AgentRuntime,
        state: RuntimeState,
        limits: AgentLimits,
        session_id: str | None,
        span_context: AbstractContextManager[Any],
        span: Any,
    ) -> None:
        self._sandbox = sandbox
        self._handle = handle
        self._handle_iter = iter(handle)
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

    @property
    def session_id(self) -> str | None:
        return self._tracker.session_id

    @property
    def dropped_lines(self) -> int:
        return self._buffer.dropped

    def __enter__(self) -> AgentStream:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __iter__(self) -> AgentStream:
        return self

    def __next__(self) -> AgentEvent:
        while not self._queue:
            if self._final is not None:
                raise StopIteration
            self._pump()
        return self._queue.pop(0)

    def abort(self) -> None:
        """Aborta la ejecución: para los procesos que lanzó el runtime y mata
        el proceso. Puede llamarse desde otro hilo mientras otro consume el stream."""
        if self._abort_requested:
            return
        self._abort_requested = True
        self._stop()

    def _stop(self) -> None:
        """Para el runtime y todo lo que lanzó con el `kill()` del handle:
        la ejecución arranca con `kill_tree=True`, así que `rayd` congela y
        mata también lo que la herramienta de shell del agente sacó de su
        grupo (`setsid`, doble fork) antes de matar el runtime. Sirve a
        `abort()` y a un límite del SDK (`max_steps`, `token_budget`), que
        sin esto dejaría al runtime trabajando (y gastando tokens) en
        segundo plano."""
        if self._stopped:
            return
        self._stopped = True
        self._handle.kill()

    def result(self) -> AgentResult:
        """Consume el resto del stream y devuelve su `AgentResult`.
        `AgentFailed` se convierte en `AgentException`."""
        for _ in self:
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

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._handle.disconnect()
        self._span_context.__exit__(None, None, None)

    def _pump(self) -> None:
        try:
            chunk = next(self._handle_iter)
        except StopIteration:
            self._finish()
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
                        self._stop()
                        self._drain()
                    return

    def _finish(self) -> None:
        if self._abort_requested:
            self._enqueue_final(
                AgentFailed(
                    reason="aborted",
                    exit_code=self._handle.exit_code,
                    session_id=self._tracker.session_id,
                )
            )
            return
        if self._ended_by_timeout():
            self._enqueue_final(AgentFailed(reason="timeout", session_id=self._tracker.session_id))
            return
        exit_code = self._handle.exit_code if self._handle.exit_code is not None else -1
        final = self._runtime.finish(self._state, exit_code)
        self._enqueue_final(cast("Done | AgentFailed", self._tracker.track(final)))

    def _ended_by_timeout(self) -> bool:
        """Si `rayd` terminó el proceso por `AgentLimits.timeout_seconds`.
        Iterar el handle no lo dice (el stream acaba con el `EndEvent` sin
        lanzar); `wait()`, sobre el stream ya consumido, lo convierte en
        `TimeoutException` sin otra llamada."""
        try:
            self._handle.wait()
        except TimeoutException:
            return True
        except SandboxException:
            return False
        return False

    def _drain(self) -> None:
        """Consume el handle hasta su `EndEvent` tras pararlo por un límite:
        `rayd` sólo lo manda cuando el proceso ya no existe, así que al
        volver el cerrojo de ejecución está libre y el siguiente `run()` no
        encuentra el runtime `busy` (un proceso con hilos tarda en morir del
        todo tras el `SIGKILL`)."""
        try:
            for _ in self._handle_iter:
                pass
        except SandboxException:
            logger.warning("no se pudo esperar al final del runtime parado")

    def _enqueue_final(self, event: Done | AgentFailed) -> None:
        self._queue.append(event)
        self._final = event

    def _record_done(self, done: Done) -> None:
        attrs = done_attributes(done, steps=self._tracker.steps)
        for key, value in attrs.items():
            self._span.set_attribute(key, value)

    def _record_failure(self, failed: AgentFailed) -> None:
        for key, value in failure_attributes(failed).items():
            self._span.set_attribute(key, value)
