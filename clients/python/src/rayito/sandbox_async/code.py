"""`AsyncSandbox.run_code` y los contextos de código: la misma superficie que
`sandbox_sync.code` sobre `grpc.aio`. El stream de `Execute` se lee con
`read()`; los callbacks son callables síncronos (paridad con E2B: una
corrutina como callback no se espera)."""

from __future__ import annotations

import asyncio
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
from rayito._process_base import STREAM_EOF, deadline_at, remaining_deadline
from rayito._sandbox_base import GateRetry, ReconnectBudget
from rayito.exceptions import NotFoundException, SandboxException, TimeoutException
from rayito.v1 import code_pb2, code_pb2_grpc

if TYPE_CHECKING:
    from rayito.sandbox_async.main import AsyncSandbox

logger = logging.getLogger("rayito.code")

CODE_STUB = code_pb2_grpc.CodeServiceStub


class AsyncCodeClient:
    """`CodeService` del sandbox como corrutinas; la superficie pública vive
    en `AsyncSandbox`."""

    def __init__(self, sandbox: AsyncSandbox) -> None:
        self._sandbox = sandbox

    async def run_code(
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
        """Misma semántica que `CodeClient.run_code`; los callbacks corren en el loop."""
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
        call, first = await self._sandbox._open_stream(
            lambda stub: stub.Execute(request, timeout=deadline),
            service=CODE_STUB,
            stream=False,
            reconnect=False,
        )
        return await self._consume(call, first, builder, deadline_at(deadline, time.monotonic))

    async def create_context(
        self,
        *,
        cwd: str | None = None,
        language: str | None = None,
        envs: Mapping[str, str] | None = None,
        request_timeout: float | None = None,
    ) -> CodeContext:
        request = build_create_context_request(language=language, cwd=cwd, envs=envs)
        response = await self._sandbox._code_call(
            lambda stub, timeout: stub.CreateContext(request, timeout=timeout),
            request_timeout,
            default_timeout=CONTEXT_REQUEST_TIMEOUT_SECONDS,
        )
        context_id = str(response.context_id)
        for listed in await self.list_contexts(request_timeout=request_timeout):
            if listed.id == context_id:
                return listed
        return fallback_context(context_id, language=language, cwd=cwd)

    async def list_contexts(self, *, request_timeout: float | None = None) -> list[CodeContext]:
        response = await self._sandbox._code_call(
            lambda stub, timeout: stub.ListContexts(
                code_pb2.ListContextsRequest(), timeout=timeout
            ),
            request_timeout,
        )
        return [context_from_proto(info) for info in response.contexts]

    async def remove_context(
        self, context: ContextLike, *, request_timeout: float | None = None
    ) -> None:
        request = code_pb2.DestroyContextRequest(context_id=require_context_id(context))
        await self._sandbox._code_call(
            lambda stub, timeout: stub.DestroyContext(request, timeout=timeout), request_timeout
        )

    async def restart_context(
        self, context: ContextLike, *, request_timeout: float | None = None
    ) -> None:
        request = code_pb2.RestartContextRequest(context_id=require_context_id(context))
        await self._sandbox._code_call(
            lambda stub, timeout: stub.RestartContext(request, timeout=timeout),
            request_timeout,
            default_timeout=CONTEXT_REQUEST_TIMEOUT_SECONDS,
        )

    async def _consume(
        self, call: Any, first: Any, builder: ExecutionBuilder, deadline_at: float | None
    ) -> Execution:
        feed = AsyncExecutionFeed(self, call, first, builder, deadline_at)
        try:
            await feed.run()
        finally:
            if not feed.finished:
                feed.call.cancel()
        return builder.finish()


class AsyncExecutionFeed:
    """`ExecutionFeed` sobre `grpc.aio`: `Execute` y, tras un corte
    reconectable posterior a `started`, `Reattach`."""

    def __init__(
        self,
        client: AsyncCodeClient,
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

    async def run(self) -> None:
        while not self.finished:
            cut = await self._feed_until_cut()
            if cut is None:
                return
            await self._reattach(cut)

    async def _feed_until_cut(self) -> grpc.RpcError | None:
        try:
            if self._first is not None and self._builder.feed(self._first):
                self.finished = True
                return None
            self._first = None
            while True:
                event = await self.call.read()
                if event is STREAM_EOF:
                    return None
                if self._builder.feed(event):
                    self.finished = True
                    return None
        except grpc.RpcError as exc:
            if self._builder.started and self._client._sandbox._is_reconnectable(exc):
                return exc
            raise await self._client._sandbox._stream_failure(exc) from exc

    async def _reattach(self, reason: grpc.RpcError) -> None:
        sandbox = self._client._sandbox
        outcome = await sandbox._reconnect(
            reason, self._generation, wake=sandbox._foreground_stream_wakes()
        )
        if not outcome.resumed:
            raise sandbox._reconnect_error(outcome, reason) from reason
        self._generation = outcome.resume_generation
        if not self._budget.allows(outcome):
            raise await sandbox._stream_failure(reason) from reason
        request = self._builder.reattach_request()
        try:
            self.call, self._first = await self._reattach_through_the_gate(request)
        except NotFoundException as exc:
            raise reattach_failure(exc) from exc
        self._builder.reattached += 1
        logger.info(
            "ejecución %s continuada con Reattach desde el seq %s (resume_generation %s)",
            self._builder.execution_id,
            request.from_seq,
            self._generation,
        )

    async def _reattach_through_the_gate(
        self, request: code_pb2.ReattachRequest
    ) -> tuple[Any, Any]:
        """Como la versión síncrona: un `Reattach` rechazado por el phase
        gate se reintenta con backoff dentro de `reconnect_timeout`."""
        sandbox = self._client._sandbox
        retry = GateRetry(sandbox._reconnect_timeout)
        while True:
            try:
                return await sandbox._open_stream(
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
                await asyncio.sleep(delay)

    def _remaining_deadline(self) -> float | None:
        remaining = remaining_deadline(self._deadline_at, time.monotonic)
        if remaining is not None and remaining <= 0.0:
            raise TimeoutException("el deadline de la ejecución venció durante la reconexión")
        return remaining
