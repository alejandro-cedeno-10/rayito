"""Dobles de prueba para `sbx.agent` (`ai-agent-core`, design.md §11):
nunca tocan `grpc` ni AWS. `Agent`/`AgentStream` (y sus espejos async) sólo
necesitan de un `Sandbox` los atributos `commands`, `files`, `gateways` e
`_instrumentation`, así que estos dobles sólo implementan eso.

`FakeAgentRuntime` traduce líneas JSON (`{"event": "step_started", ...}`) a
los eventos de `rayito._agent._events`: lo que un test escribe en
`lines=[...]` es ya el protocolo, no el JSONL real de OpenCode (ese lo prueba
el adaptador que llega con el resto de `ai-agent-core`)."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from rayito._agent._domain import AgentSpec
from rayito._agent._events import (
    AgentEvent,
    AgentFailed,
    Done,
    Reasoning,
    StepFinished,
    StepStarted,
    Text,
    TextDelta,
    TokenUsage,
    ToolCall,
)
from rayito._agent._runtime import (
    RuntimeFile,
    RuntimeFiles,
    RuntimeState,
    TemplateStep,
    WarmupStep,
)
from rayito._otel import NOOP, Instrumentation
from rayito._secret_gateway._domain import GatewayStatus

FAKE_SESSION_ID = "fake-session-1"
FAKE_PID = 4242


def _event_from_json(payload: Mapping[str, Any]) -> AgentEvent:
    kind = payload["event"]
    if kind == "text_delta":
        return TextDelta(payload["text"])
    if kind == "text":
        return Text(payload["text"])
    if kind == "reasoning":
        return Reasoning(payload["text"])
    if kind == "tool_call":
        return ToolCall(
            call_id=payload["call_id"],
            name=payload["name"],
            status=payload["status"],
            input=payload.get("input"),
            output=payload.get("output"),
            output_truncated=payload.get("output_truncated", False),
        )
    if kind == "step_started":
        return StepStarted(payload["index"])
    if kind == "step_finished":
        usage = TokenUsage(**payload.get("usage", {}))
        return StepFinished(payload["index"], usage, payload.get("finish_reason"))
    if kind == "agent_failed":
        return AgentFailed(
            reason=payload["reason"],
            exit_code=payload.get("exit_code"),
            detail_code=payload.get("detail_code"),
            session_id=payload.get("session_id"),
        )
    if kind == "done":
        usage = TokenUsage(**payload.get("usage", {}))
        return Done(payload.get("session_id", FAKE_SESSION_ID), payload["exit_code"], usage)
    raise ValueError(f"evento de prueba desconocido: {kind!r}")


@dataclass
class FakeRuntimeState:
    session_id: str = FAKE_SESSION_ID


@dataclass
class FakeAgentRuntime:
    """Un `AgentRuntime` de doble: `build_config` siempre devuelve el mismo
    sha salvo que el test pase `config_sha`, y `parse_line` decodifica el
    JSON que el test puso en el stdout simulado."""

    name: str = "fake"
    config_sha: str = "sha-1"
    finish_result: Done | AgentFailed | None = None
    build_config_calls: list[Mapping[str, str]] = field(default_factory=list)
    warmup: tuple[WarmupStep, ...] = ()

    def build_config(
        self, spec: AgentSpec, *, gateway_urls: Mapping[str, str], workdir: str
    ) -> RuntimeFiles:
        self.build_config_calls.append(dict(gateway_urls))
        return RuntimeFiles(
            files=(RuntimeFile(f"{workdir}/.rayito/agent/fake.json", b"{}"),),
            config_sha256=self.config_sha,
        )

    def command(self, request: Any) -> Any:
        from rayito._agent._runtime import RunCommand

        return RunCommand(
            script="exec rayito-agent-fake-run",
            envs={"RAYITO_AGENT_FAKE": "1"},
            stdin=request.prompt.encode("utf-8"),
        )

    def new_state(self) -> RuntimeState:
        return FakeRuntimeState()

    def parse_line(self, line: bytes, state: RuntimeState) -> Sequence[AgentEvent]:
        fake_state = cast(FakeRuntimeState, state)
        payload = json.loads(line.decode("utf-8"))
        event = _event_from_json(payload)
        if isinstance(event, Done):
            fake_state.session_id = event.session_id
        return (event,)

    def finish(self, state: RuntimeState, exit_code: int) -> Done | AgentFailed:
        fake_state = cast(FakeRuntimeState, state)
        if self.finish_result is not None:
            return self.finish_result
        if exit_code == 0:
            return Done(fake_state.session_id, exit_code, TokenUsage())
        return AgentFailed(
            reason="runtime_error", exit_code=exit_code, session_id=fake_state.session_id
        )

    def template_steps(self) -> Sequence[TemplateStep]:
        return ()

    def warmup_steps(self) -> Sequence[WarmupStep]:
        return self.warmup


class FakeCommandHandle:
    """Un `CommandHandle` de doble: `lines` son las líneas de stdout (sin
    salto final) que el iterador entrega una por chunk, seguidas del final
    (`exit_code`). `kill()`/`disconnect()` sólo anotan que se llamaron."""

    def __init__(
        self,
        *,
        lines: Iterable[bytes],
        exit_code: int | None = 0,
        raise_on_iterate: Exception | None = None,
        raise_on_wait: Exception | None = None,
    ) -> None:
        self._raise_on_wait = raise_on_wait
        self._chunks = [line + b"\n" for line in lines]
        self._index = 0
        self._exit_code = exit_code
        self._raise_on_iterate = raise_on_iterate
        self.pid = FAKE_PID
        self.killed = False
        self.disconnected = False
        self.stdin: bytes = b""
        self.stdin_closed = False

    @property
    def exit_code(self) -> int | None:
        return self._exit_code

    def send_stdin(self, data: bytes | str) -> None:
        self.stdin = data if isinstance(data, bytes) else data.encode("utf-8")

    def close_stdin(self) -> None:
        self.stdin_closed = True

    def kill(self) -> bool:
        self.killed = True
        return True

    def wait(self) -> None:
        """Como `CommandHandle.wait()` con el stream ya consumido: lanza la
        excepción del `EndEvent` (`raise_on_wait`), si la hay."""
        if self._raise_on_wait is not None:
            raise self._raise_on_wait

    def disconnect(self) -> None:
        self.disconnected = True

    def __iter__(self) -> FakeCommandHandle:
        return self

    def __next__(self) -> tuple[str | None, str | None, bytes | None]:
        if self.killed or self.disconnected:
            raise StopIteration
        if self._index >= len(self._chunks):
            if self._raise_on_iterate is not None:
                raise self._raise_on_iterate
            raise StopIteration
        chunk = self._chunks[self._index]
        self._index += 1
        return (chunk.decode("utf-8"), None, None)


class FakeAsyncCommandHandle(FakeCommandHandle):
    """Espejo async: los mismos datos, consumidos con `async for`/`await`."""

    async def send_stdin(self, data: bytes | str) -> None:  # type: ignore[override]
        super().send_stdin(data)

    async def close_stdin(self) -> None:  # type: ignore[override]
        super().close_stdin()

    async def kill(self) -> bool:  # type: ignore[override]
        return super().kill()

    async def wait(self) -> None:  # type: ignore[override]
        super().wait()

    def __aiter__(self) -> FakeAsyncCommandHandle:
        return self

    async def __anext__(self) -> tuple[str | None, str | None, bytes | None]:
        try:
            return super().__next__()
        except StopIteration:
            raise StopAsyncIteration from None


@dataclass
class _RunCall:
    cmd: str
    background: bool
    envs: Mapping[str, str] | None
    timeout: float | None
    tag: str | None


class FakeCommands:
    """`sandbox.commands` de doble: entrega los `FakeCommandHandle` que el
    test precargó en `handles` (una ejecución en background, en orden) y
    registra cada llamada en `calls`. Una llamada en foreground (el `abort`
    con elegancia) consume `foreground_results` en vez de `handles`."""

    def __init__(
        self,
        *,
        handles: list[FakeCommandHandle] | None = None,
        foreground_results: list[Exception | None] | None = None,
        is_async: bool = False,
    ) -> None:
        self._handles = list(handles or [])
        self._foreground_results = list(foreground_results or [])
        self.calls: list[_RunCall] = []
        self._is_async = is_async

    def run(
        self,
        cmd: str,
        *,
        background: bool = False,
        envs: Mapping[str, str] | None = None,
        stdin: bool = False,
        timeout: float | None = None,
        max_output_bytes: int | None = None,
        tag: str | None = None,
    ) -> Any:
        self.calls.append(_RunCall(cmd, background, envs, timeout, tag))
        if self._is_async:
            return self._async_run(background)
        return self._sync_run(background)

    def _sync_run(self, background: bool) -> Any:
        if not background:
            if self._foreground_results:
                failure = self._foreground_results.pop(0)
                if failure is not None:
                    raise failure
            return None
        return self._handles.pop(0)

    async def _async_run(self, background: bool) -> Any:
        if not background:
            if self._foreground_results:
                failure = self._foreground_results.pop(0)
                if failure is not None:
                    raise failure
            return None
        return self._handles.pop(0)


class FakeFilesystem:
    def __init__(self, *, is_async: bool = False) -> None:
        self.write_files_calls: list[list[Any]] = []
        self._is_async = is_async

    def write_files(self, entries: list[Any]) -> Any:
        self.write_files_calls.append(list(entries))
        if self._is_async:
            return self._noop()
        return []

    async def _noop(self) -> list[Any]:
        return []


class FakeGateways(dict[str, GatewayStatus]):
    """`sbx.gateways`: un `dict` normal basta (`Mapping.keys()`/`__getitem__`)."""


class FakeSandbox:
    """Lo mínimo que `Agent`/`AsyncAgent` tocan de un `Sandbox`."""

    def __init__(
        self,
        *,
        commands: FakeCommands,
        files: FakeFilesystem,
        gateways: Mapping[str, GatewayStatus] | None = None,
    ) -> None:
        self.commands = commands
        self.files = files
        self.gateways = FakeGateways(gateways or {})
        self._instrumentation: Instrumentation = NOOP


def gateway_status(port: int = 18080) -> GatewayStatus:
    return GatewayStatus(port=port)


__all__ = [
    "FAKE_SESSION_ID",
    "FakeAgentRuntime",
    "FakeAsyncCommandHandle",
    "FakeCommandHandle",
    "FakeCommands",
    "FakeFilesystem",
    "FakeGateways",
    "FakeRuntimeState",
    "FakeSandbox",
    "gateway_status",
]
