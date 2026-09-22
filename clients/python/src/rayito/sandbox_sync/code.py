"""`Sandbox.run_code` y los contextos de código: `CodeService` sobre `grpc`.

`Execute` se abre en el canal de unarios y se consume en el hilo que llama,
como los comandos en foreground; los cuatro RPC de contextos son unarios en
el mismo canal. Un error del kernel (`ZeroDivisionError`, `ExecutionTimeout`,
`KernelDied`...) es dato en `Execution.error`; sólo los fallos de transporte,
de argumentos y del protocolo son excepciones. Si el stream se corta después
de `started` (un `/suspend`, el proxy), el SDK espera al agente y sigue la
misma ejecución con `Reattach(context_id, execution_id, from_seq)`; un corte
antes de `started` no se reintenta para no correr la celda dos veces.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import grpc

from rayito._code_base import (
    CONTEXT_REQUEST_TIMEOUT_SECONDS,
    DEFAULT_CODE_TIMEOUT_SECONDS,
    ContextLike,
    ErrorCallback,
    ExecutionBuilder,
    ResultCallback,
    StdoutCallback,
    build_create_context_request,
    build_execute_request,
    context_from_proto,
    execute_deadline,
    fallback_context,
    language_default_context_id,
    reattach_failure,
    require_context_id,
    resolve_context_id,
)
from rayito._models import CodeContext, Execution
from rayito._process_base import deadline_at, remaining_deadline
from rayito._sandbox_base import GateRetry, ReconnectBudget
from rayito.exceptions import NotFoundException, SandboxException, TimeoutException
from rayito.v1 import code_pb2, code_pb2_grpc

if TYPE_CHECKING:
    from rayito.sandbox_sync.main import Sandbox

logger = logging.getLogger("rayito.code")

CODE_STUB = code_pb2_grpc.CodeServiceStub


class CodeClient:
    """`CodeService` del sandbox; la superficie pública vive en `Sandbox`."""

    def __init__(self, sandbox: Sandbox) -> None:
        self._sandbox = sandbox

    def run_code(
        self,
        code: str,
        *,
        language: str | None = None,
        context: ContextLike | None = None,
        on_stdout: StdoutCallback | None = None,
        on_stderr: StdoutCallback | None = None,
        on_result: ResultCallback | None = None,
        on_error: ErrorCallback | None = None,
        envs: Mapping[str, str] | None = None,
        timeout: float | None = DEFAULT_CODE_TIMEOUT_SECONDS,
        request_timeout: float | None = None,
    ) -> Execution:
        """Ejecuta `code` en el kernel del contexto (el `default` si se omite)
        o, con `language`, en el contexto por defecto de ese kernel
        (`default-bash`, sólo en `rayito-base-poly`), que el agente crea en la
        primera celda; `javascript` es un nombre reservado sin kernel en
        ninguna imagen (`UNIMPLEMENTED`). `language` y `context` son
        excluyentes.

        `timeout` lo impone el agente: al vencer interrumpe la celda y, si el
        kernel no queda idle en 5 s, reinicia el contexto; en ambos casos la
        `Execution` trae `error.name == "ExecutionTimeout"`. `None` o `0` lo
        desactivan. El deadline gRPC del stream es `timeout + 15 s` salvo que
        `request_timeout` lo sustituya. `envs` sólo viven durante esta celda.
        Cerrar el stream antes del `end` (Ctrl-C, excepción en un callback)
        hace que el agente interrumpa la ejecución.
        """
        context_id = resolve_context_id(context)
        request = build_execute_request(
            code, context_id=context_id, language=language, envs=envs, timeout=timeout
        )
        deadline = execute_deadline(timeout) if request_timeout is None else request_timeout
        builder = ExecutionBuilder(
            context_id=context_id or language_default_context_id(request),
            on_stdout=on_stdout,
            on_stderr=on_stderr,
            on_result=on_result,
            on_error=on_error,
        )
        call, first = self._sandbox._open_stream(
            lambda stub: stub.Execute(request, timeout=deadline),
            service=CODE_STUB,
            stream=False,
            reconnect=False,
        )
        return self._consume(call, first, builder, deadline_at(deadline, time.monotonic))

    def create_context(
        self,
        *,
        cwd: str | None = None,
        language: str | None = None,
        envs: Mapping[str, str] | None = None,
        request_timeout: float | None = None,
    ) -> CodeContext:
        """Arranca un kernel nuevo (≈ segundos; deadline de 90 s por defecto).
        `language` es `python` (por defecto), `bash` o `javascript` (alias
        `js`); el kernel `bash` sólo existe en la variante de imagen
        `rayito-base-poly` (`InvalidArgumentException` con `grpc_code`
        `UNIMPLEMENTED` en las demás) y `javascript` es un nombre reservado
        sin kernel en ninguna imagen (`UNIMPLEMENTED` en todas). `cwd` debe
        existir en el sandbox;
        `envs` forman parte del entorno del kernel. Como máximo 8 contextos
        por sandbox."""
        request = build_create_context_request(language=language, cwd=cwd, envs=envs)
        response = self._sandbox._code_call(
            lambda stub, timeout: stub.CreateContext(request, timeout=timeout),
            request_timeout,
            default_timeout=CONTEXT_REQUEST_TIMEOUT_SECONDS,
        )
        context_id = str(response.context_id)
        for listed in self.list_contexts(request_timeout=request_timeout):
            if listed.id == context_id:
                return listed
        return fallback_context(context_id, language=language, cwd=cwd)

    def list_contexts(self, *, request_timeout: float | None = None) -> list[CodeContext]:
        """El contexto `default` primero, después por orden de creación."""
        response = self._sandbox._code_call(
            lambda stub, timeout: stub.ListContexts(
                code_pb2.ListContextsRequest(), timeout=timeout
            ),
            request_timeout,
        )
        return [context_from_proto(info) for info in response.contexts]

    def remove_context(self, context: ContextLike, *, request_timeout: float | None = None) -> None:
        """Mata el kernel; las ejecuciones en curso terminan con
        `ContextDestroyed`. El `default` no se puede borrar
        (`InvalidArgumentException`); un id desconocido es `NotFoundException`."""
        request = code_pb2.DestroyContextRequest(context_id=require_context_id(context))
        self._sandbox._code_call(
            lambda stub, timeout: stub.DestroyContext(request, timeout=timeout), request_timeout
        )

    def restart_context(
        self, context: ContextLike, *, request_timeout: float | None = None
    ) -> None:
        """Kernel nuevo con el mismo id: el estado se pierde y las ejecuciones
        en curso terminan con `KernelRestarted`. Deadline de 90 s por defecto."""
        request = code_pb2.RestartContextRequest(context_id=require_context_id(context))
        self._sandbox._code_call(
            lambda stub, timeout: stub.RestartContext(request, timeout=timeout),
            request_timeout,
            default_timeout=CONTEXT_REQUEST_TIMEOUT_SECONDS,
        )

    def _consume(
        self, call: Any, first: Any, builder: ExecutionBuilder, deadline_at: float | None
    ) -> Execution:
        """Alimenta el builder hasta el `end`; si se sale antes (fallo, callback
        que lanza) cancela el stream para que el agente interrumpa la celda."""
        feed = ExecutionFeed(self, call, first, builder, deadline_at)
        try:
            feed.run()
        finally:
            if not feed.finished:
                feed.call.cancel()
        return builder.finish()


class ExecutionFeed:
    """El bucle que alimenta un `ExecutionBuilder` desde `Execute` y, tras un
    corte reconectable posterior a `started`, desde `Reattach`."""

    def __init__(
        self,
        client: CodeClient,
        call: Any,
        first: Any,
        builder: ExecutionBuilder,
        deadline_at: float | None,
    ) -> None:
        self._client = client
        self.call = call
        self._first = first
        self._builder = builder
        self._deadline_at = deadline_at
        self._generation = client._sandbox.resume_generation
        self._budget = ReconnectBudget()
        self.finished = False

    def run(self) -> None:
        while not self.finished:
            cut = self._feed_until_cut()
            if cut is None:
                return
            self._reattach(cut)

    def _feed_until_cut(self) -> grpc.RpcError | None:
        """Consume el stream actual; devuelve el corte reconectable que lo
        terminó (tras `started`) o `None` si terminó por sí mismo."""
        try:
            if self._first is not None and self._builder.feed(self._first):
                self.finished = True
                return None
            self._first = None
            for event in self.call:
                if self._builder.feed(event):
                    self.finished = True
                    return None
        except grpc.RpcError as exc:
            if self._builder.started and self._client._sandbox._is_reconnectable(exc):
                return exc
            raise self._client._sandbox._stream_failure(exc) from exc
        return None

    def _reattach(self, reason: grpc.RpcError) -> None:
        sandbox = self._client._sandbox
        outcome = sandbox._reconnect(
            reason, self._generation, wake=sandbox._foreground_stream_wakes()
        )
        if not outcome.resumed:
            raise sandbox._reconnect_error(outcome, reason) from reason
        self._generation = outcome.resume_generation
        if not self._budget.allows(outcome):
            raise sandbox._stream_failure(reason) from reason
        request = self._builder.reattach_request()
        try:
            self.call, self._first = self._reattach_through_the_gate(request)
        except NotFoundException as exc:
            raise reattach_failure(exc) from exc
        self._builder.reattached += 1
        logger.info(
            "ejecución %s continuada con Reattach desde el seq %s (resume_generation %s)",
            self._builder.execution_id,
            request.from_seq,
            self._generation,
        )

    def _reattach_through_the_gate(self, request: code_pb2.ReattachRequest) -> tuple[Any, Any]:
        """`Reattach` rechazado por el phase gate (`UNAVAILABLE suspending`,
        el gate sigue cerrado tras un `/suspend` que nadie checkpointeó) se
        reintenta con backoff dentro de `reconnect_timeout`, como
        `Connect`."""
        sandbox = self._client._sandbox
        retry = GateRetry(sandbox._reconnect_timeout)
        while True:
            try:
                return sandbox._open_stream(
                    lambda stub: stub.Reattach(request, timeout=self._remaining_deadline()),
                    service=CODE_STUB,
                    stream=False,
                )
            except SandboxException as exc:
                delay = retry.retry_delay(exc)
                if delay is None:
                    raise
                logger.info(
                    "ejecución %s: el gate del agente sigue cerrado (%s); reintento",
                    self._builder.execution_id,
                    exc,
                )
                time.sleep(delay)

    def _remaining_deadline(self) -> float | None:
        remaining = remaining_deadline(self._deadline_at, time.monotonic)
        if remaining is not None and remaining <= 0.0:
            raise TimeoutException("el deadline de la ejecución venció durante la reconexión")
        return remaining
