"""`CodeService` falso en proceso, con el contrato de `rayd` en M4 y M5.

Interpreta una tabla mínima de celdas (`x = 42`, `x`, `print(x)`, `1/0`,
`plot`, `df`, `stderr`, `many`, `sleep`, `slow <s>`, `print-then-sleep`,
`kernel-die`, `bad-json`, `omitted`, `envs-echo`, `1+1`,
`raise ValueError('boom')`, `echo <texto>`, `console.log('<texto>')`; lo demás
es `ScriptError`) y reproduce lo que el SDK necesita observar: `started`
primero, `seq` desde 1 con `keepalive` a 0, `end` con `execution_count` (0 en
los finales sintéticos), contextos `default` + `ctx-<12 hex>` con tope de 8,
el kernel gate (`UNAVAILABLE` con prefijo `kernel not ready`), el phase gate,
`timeout_ms`/`envs` grabados por ejecución y `x-access-token` en cada RPC.

Desde M7 honra `ExecuteRequest.language` como `rayd`: `language` +
`context_id` es `INVALID_ARGUMENT`; un lenguaje que el fake no "instala"
(`languages`) es `UNIMPLEMENTED` con `rayito-base-poly` en el mensaje; los
contextos por defecto `default-bash`/`default-javascript`/`default-typescript`
nacen en la primera celda de ese lenguaje y `ListContexts` los lista con su
lenguaje; `envs` por ejecución en un contexto no Python es `INVALID_ARGUMENT`.
Desde M9 conoce `typescript` y un nombre desconocido recibe el mensaje nuevo de
`rayd` con los cuatro nombres canónicos.

Desde M5 cada ejecución la corre un "recorder" propio que graba los eventos
en un ring por ejecución, así el stream de `Execute` es un suscriptor más:
`Reattach(context_id, execution_id, from_seq)` reenvía desde el ring
(`NOT_FOUND`/`OUT_OF_RANGE`/`INVALID_ARGUMENT`), `suspend()` corta los
streams vivos con `UNAVAILABLE suspending` sin interrumpir la celda, y un
cliente que cancela el `Execute` original sigue interrumpiéndola.
"""

from __future__ import annotations

import base64
import json
import queue
import re
import secrets
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import grpc

from rayito.v1 import code_pb2, code_pb2_grpc, common_pb2

from .fake_process import require_access_token

DEFAULT_CONTEXT_ID = "default"
DEFAULT_LANGUAGE = "python"
KNOWN_LANGUAGES = frozenset({"python", "bash", "javascript", "typescript"})
INVALID_LANGUAGE_MESSAGE = "language must be one of python, bash, javascript, typescript"
POLY_IMAGE = "rayito-base-poly"
DEFAULT_CWD = "/home/user"
KNOWN_DIRECTORIES = frozenset({"/", "/tmp", "/home/user"})
MAX_CONTEXTS = 8
MAX_CODE_BYTES = 1_048_576
MAX_SUBSCRIBERS = 8
TIMEOUT_CAP_SECONDS = 0.2
SLEEP_MAX_SECONDS = 30.0
STEP_SECONDS = 0.02
KERNEL_GATE_PREFIX = "kernel not ready"
EXECUTION_ID = re.compile(r"^exec-[0-9a-f]{16}$")

ONE_PIXEL_PNG_BASE64 = base64.b64encode(
    bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000d49444154789c63f8ffff3f0005fe02fe0d58d2fe0000000049454e44ae426082"
    )
).decode("ascii")
LINE_CHART = {
    "type": "line",
    "title": "plot",
    "x_label": None,
    "y_label": None,
    "x_unit": None,
    "y_unit": None,
    "x_ticks": [0.0, 1.0, 2.0],
    "x_tick_labels": ["0.0", "1.0", "2.0"],
    "x_scale": "linear",
    "y_ticks": [1.0, 2.0, 3.0],
    "y_tick_labels": ["1.0", "2.0", "3.0"],
    "y_scale": "linear",
    "elements": [{"label": "_child0", "points": [[0.0, 1.0], [1.0, 2.0], [2.0, 3.0]]}],
}
DATAFRAME_TEXT = "   a    b\n0  1  3.5\n1  2  4.5"
DATAFRAME_HTML = "<table><tr><th>a</th><th>b</th></tr><tr><td>1</td><td>3.5</td></tr></table>"
DATAFRAME_DATA = {"a": [1, 2], "b": [3.5, 4.5]}
OMITTED_NOTE = "image/png: 9000000 bytes"
ASSIGNMENT = re.compile(r"^(\w+) = (.+)$")
NAME = re.compile(r"^\w+$")
PRINT = re.compile(r"^print\((\w+)\)$")
ARITHMETIC = re.compile(r"^(\d+) ?([+*]) ?(\d+)$")
RAISE = re.compile(r"^raise (\w+)\('(.*)'\)$")
SLOW = re.compile(r"^slow (\d+(?:\.\d+)?)$")
ECHO = re.compile(r"^echo (.+)$")
CONSOLE_LOG = re.compile(r"^console\.log\('(.*)'\)$")


@dataclass
class FakeContext:
    context_id: str
    cwd: str = DEFAULT_CWD
    language: str = DEFAULT_LANGUAGE
    envs: dict[str, str] = field(default_factory=dict)
    namespace: dict[str, str] = field(default_factory=dict)
    execution_count: int = 0
    kernel_generation: int = 0

    def reset(self) -> None:
        self.namespace.clear()
        self.execution_count = 0
        self.kernel_generation += 1


@dataclass
class FakeExecution:
    """Lo que el `rayd` falso recuerda de cada `Execute`, para las aserciones."""

    context_id: str
    execution_id: str
    code: str
    timeout_ms: int
    envs: dict[str, str]
    interrupted: bool = False


@dataclass
class StreamEnd:
    code: grpc.StatusCode
    message: str


class ReplayOutOfRange(Exception):
    def __init__(self, oldest: int, next_seq: int) -> None:
        super().__init__(f"from_seq fuera de rango: oldest={oldest} next={next_seq}")


Item = code_pb2.ExecuteEvent | StreamEnd


class FakeRun:
    """El "recorder" de una ejecución: ring con `seq`, suscriptores y final."""

    def __init__(self, record: FakeExecution) -> None:
        self.record = record
        self.lock = threading.Lock()
        self.ring: list[code_pb2.ExecuteEvent] = []
        self.next_seq = 1
        self.subscribers: list[queue.Queue[Item]] = []
        self.ended = False
        self.ended_at: float | None = None
        self.detached_origin = False

    def publish(self, event: code_pb2.ExecuteEvent) -> None:
        with self.lock:
            if not event.HasField("keepalive"):
                event.seq = self.next_seq
                self.next_seq += 1
                self.ring.append(event)
            for subscriber in self.subscribers:
                subscriber.put(event)
            if event.HasField("end"):
                self.ended = True
                self.ended_at = time.monotonic()
                self.subscribers = []

    def subscribe(self, from_seq: int) -> tuple[queue.Queue[Item] | None, list[Item]]:
        with self.lock:
            oldest = int(self.ring[0].seq) if self.ring else self.next_seq
            if from_seq > 0 and (from_seq < oldest or from_seq > self.next_seq):
                raise ReplayOutOfRange(oldest, self.next_seq)
            replay: list[Item] = [
                event for event in self.ring if from_seq > 0 and int(event.seq) >= from_seq
            ]
            if self.ended:
                return None, replay
            subscriber: queue.Queue[Item] = queue.Queue()
            self.subscribers.append(subscriber)
            return subscriber, replay

    def unsubscribe(self, subscriber: queue.Queue[Item]) -> None:
        with self.lock:
            if subscriber in self.subscribers:
                self.subscribers.remove(subscriber)

    def suspend(self) -> None:
        """Lo que hace `/suspend`: los streams se cortan con `UNAVAILABLE
        suspending`, el origen queda desacoplado (su cierre no interrumpe) y
        la celda sigue corriendo y grabándose."""
        with self.lock:
            self.detached_origin = True
            for subscriber in self.subscribers:
                subscriber.put(StreamEnd(grpc.StatusCode.UNAVAILABLE, "suspending"))
            self.subscribers = []


@dataclass
class Cell:
    context: FakeContext
    request: code_pb2.ExecuteRequest
    record: FakeExecution
    run: FakeRun

    @property
    def code(self) -> str:
        return str(self.request.code)

    @property
    def interrupted(self) -> bool:
        return self.record.interrupted


def stdout(text: str) -> code_pb2.ExecuteEvent:
    return code_pb2.ExecuteEvent(
        stdout=code_pb2.OutputChunk(text=text, timestamp_unix_ns=time.time_ns())
    )


def stderr(text: str) -> code_pb2.ExecuteEvent:
    return code_pb2.ExecuteEvent(
        stderr=code_pb2.OutputChunk(text=text, timestamp_unix_ns=time.time_ns())
    )


def result(*, is_main_result: bool = False, **mime: Any) -> code_pb2.ExecuteEvent:
    return code_pb2.ExecuteEvent(
        result=code_pb2.ExecutionResult(is_main_result=is_main_result, **mime)
    )


def error(name: str, value: str, traceback: list[str] | None = None) -> code_pb2.ExecuteEvent:
    return code_pb2.ExecuteEvent(
        error=code_pb2.ExecutionError(name=name, value=value, traceback=traceback or [])
    )


def end(execution_count: int) -> code_pb2.ExecuteEvent:
    return code_pb2.ExecuteEvent(end=code_pb2.ExecutionEnd(execution_count=execution_count))


def keepalive() -> code_pb2.ExecuteEvent:
    return code_pb2.ExecuteEvent(keepalive=common_pb2.KeepAlive())


def kernel_traceback(name: str, value: str) -> list[str]:
    return [
        "---------------------------------------------------------------------------",
        f"{name}                                Traceback (most recent call last)",
        f"{name}: {value}",
    ]


def name_error(name: str) -> code_pb2.ExecuteEvent:
    value = f"name '{name}' is not defined"
    return error("NameError", value, kernel_traceback("NameError", value))


def cell_assignment(cell: Cell, match: re.Match[str]) -> Iterator[code_pb2.ExecuteEvent]:
    cell.context.namespace[match.group(1)] = match.group(2)
    return iter(())


def cell_name(cell: Cell, match: re.Match[str]) -> Iterator[code_pb2.ExecuteEvent]:
    value = cell.context.namespace.get(match.group(0))
    yield name_error(match.group(0)) if value is None else result(text=value, is_main_result=True)


def cell_print(cell: Cell, match: re.Match[str]) -> Iterator[code_pb2.ExecuteEvent]:
    value = cell.context.namespace.get(match.group(1))
    yield name_error(match.group(1)) if value is None else stdout(f"{value}\n")


def cell_arithmetic(cell: Cell, match: re.Match[str]) -> Iterator[code_pb2.ExecuteEvent]:
    left, operator, right = int(match.group(1)), match.group(2), int(match.group(3))
    value = left + right if operator == "+" else left * right
    yield result(text=str(value), is_main_result=True)


def cell_raise(cell: Cell, match: re.Match[str]) -> Iterator[code_pb2.ExecuteEvent]:
    name, value = match.group(1), match.group(2)
    yield error(name, value, kernel_traceback(name, value))


def cell_echo(cell: Cell, match: re.Match[str]) -> Iterator[code_pb2.ExecuteEvent]:
    yield stdout(f"{match.group(1)}\n")


def cell_slow(cell: Cell, match: re.Match[str]) -> Iterator[code_pb2.ExecuteEvent]:
    """Una celda que tarda `s` segundos y devuelve `'slow'`; sobrevive a un
    `suspend()` del fake (sigue corriendo y grabándose en el ring)."""
    yield stdout("slow start\n")
    if wait_or_interrupt(cell, float(match.group(1))):
        return
    yield result(text="'slow'", is_main_result=True)


def cell_zero_division(cell: Cell) -> Iterator[code_pb2.ExecuteEvent]:
    yield error(
        "ZeroDivisionError",
        "division by zero",
        kernel_traceback("ZeroDivisionError", "division by zero"),
    )


def cell_plot(cell: Cell) -> Iterator[code_pb2.ExecuteEvent]:
    yield keepalive()
    yield result(png=ONE_PIXEL_PNG_BASE64, chart=json.dumps(LINE_CHART))


def cell_dataframe(cell: Cell) -> Iterator[code_pb2.ExecuteEvent]:
    yield result(
        text=DATAFRAME_TEXT,
        html=DATAFRAME_HTML,
        data=json.dumps(DATAFRAME_DATA),
        is_main_result=True,
    )


def cell_stderr(cell: Cell) -> Iterator[code_pb2.ExecuteEvent]:
    yield stderr("warn\n")


def cell_many(cell: Cell) -> Iterator[code_pb2.ExecuteEvent]:
    yield result(text="one")
    yield result(text="two", is_main_result=True)
    yield result(text="three")


def cell_print_then_sleep(cell: Cell) -> Iterator[code_pb2.ExecuteEvent]:
    yield stdout("tick\n")
    yield from cell_sleep(cell)


def wait_or_interrupt(cell: Cell, seconds: float) -> bool:
    """Espera `seconds` en pasos cortos; True si el cliente interrumpió."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if cell.interrupted:
            return True
        time.sleep(STEP_SECONDS)
    return cell.interrupted


def cell_sleep(cell: Cell) -> Iterator[code_pb2.ExecuteEvent]:
    """Espera hasta `min(timeout_ms, 0.2 s)`; sin timeout, hasta 30 s o hasta
    que el cliente cierre el stream (eso es el `interrupt` del agente)."""
    timeout_ms = int(cell.request.timeout_ms)
    wait = min(timeout_ms / 1000, TIMEOUT_CAP_SECONDS) if timeout_ms > 0 else SLEEP_MAX_SECONDS
    if wait_or_interrupt(cell, wait):
        return
    if timeout_ms > 0:
        yield error("ExecutionTimeout", f"execution exceeded {timeout_ms} ms")
        yield end(0)


def cell_kernel_die(cell: Cell) -> Iterator[code_pb2.ExecuteEvent]:
    cell.context.reset()
    yield error("KernelDied", "kernel process exited (code 3)")
    yield end(0)


def cell_bad_json(cell: Cell) -> Iterator[code_pb2.ExecuteEvent]:
    yield result(json="{not json")


def cell_omitted(cell: Cell) -> Iterator[code_pb2.ExecuteEvent]:
    yield code_pb2.ExecuteEvent(
        result=code_pb2.ExecutionResult(extra={"rayito/omitted": OMITTED_NOTE})
    )


def cell_envs_echo(cell: Cell) -> Iterator[code_pb2.ExecuteEvent]:
    yield stdout(json.dumps(dict(cell.request.envs), sort_keys=True) + "\n")


def cell_unknown(cell: Cell) -> Iterator[code_pb2.ExecuteEvent]:
    yield error(
        "ScriptError", "unscripted cell", kernel_traceback("ScriptError", "unscripted cell")
    )


Script = Callable[[Cell], Iterator[code_pb2.ExecuteEvent]]
PatternScript = Callable[[Cell, re.Match[str]], Iterator[code_pb2.ExecuteEvent]]

SCRIPTS: dict[str, Script] = {
    "1/0": cell_zero_division,
    "plot": cell_plot,
    "df": cell_dataframe,
    "stderr": cell_stderr,
    "many": cell_many,
    "sleep": cell_sleep,
    "print-then-sleep": cell_print_then_sleep,
    "kernel-die": cell_kernel_die,
    "bad-json": cell_bad_json,
    "omitted": cell_omitted,
    "envs-echo": cell_envs_echo,
}
PATTERN_SCRIPTS: tuple[tuple[re.Pattern[str], PatternScript], ...] = (
    (ASSIGNMENT, cell_assignment),
    (PRINT, cell_print),
    (ARITHMETIC, cell_arithmetic),
    (RAISE, cell_raise),
    (SLOW, cell_slow),
    (ECHO, cell_echo),
    (CONSOLE_LOG, cell_echo),
    (NAME, cell_name),
)


def run_cell(cell: Cell) -> Iterator[code_pb2.ExecuteEvent]:
    script = SCRIPTS.get(cell.code)
    if script is not None:
        return script(cell)
    for pattern, pattern_script in PATTERN_SCRIPTS:
        match = pattern.match(cell.code)
        if match is not None:
            return pattern_script(cell, match)
    return cell_unknown(cell)


def record_cell(cell: Cell) -> None:
    """El recorder: `started`, la celda y un `end` con el `execution_count`
    salvo que la celda emita el suyo. Una interrupción termina con
    `KeyboardInterrupt` + `end`, como el kernel real."""
    context = cell.context
    context.execution_count += 1
    count = context.execution_count
    cell.run.publish(
        code_pb2.ExecuteEvent(
            started=code_pb2.ExecutionStarted(
                execution_id=cell.record.execution_id, execution_count=count
            )
        )
    )
    for event in run_cell(cell):
        cell.run.publish(event)
        if event.HasField("end"):
            return
    if cell.interrupted:
        cell.run.publish(error("KeyboardInterrupt", ""))
    cell.run.publish(end(count))


def stream_subscriber(
    run: FakeRun,
    subscriber: queue.Queue[Item] | None,
    prelude: list[Item],
    context: grpc.ServicerContext,
    *,
    origin: bool,
) -> Iterator[code_pb2.ExecuteEvent]:
    """Entrega el replay y luego lo que llegue por la cola; un `StreamEnd`
    aborta con su status. Al cerrarse, un stream de origen cuya celda sigue
    corriendo y que no fue desacoplado por un `suspend()` deja `interrupted`."""
    try:
        for event in prelude:
            if isinstance(event, StreamEnd):
                context.abort(event.code, event.message)
            yield event
        if subscriber is None:
            return
        while context.is_active():
            try:
                item = subscriber.get(timeout=STEP_SECONDS)
            except queue.Empty:
                continue
            if isinstance(item, StreamEnd):
                context.abort(item.code, item.message)
            yield item
            if item.HasField("end"):
                return
    finally:
        if subscriber is not None:
            run.unsubscribe(subscriber)
        if origin and not run.ended and not run.detached_origin and not context.is_active():
            run.record.interrupted = True


def context_info(context: FakeContext) -> code_pb2.ContextInfo:
    return code_pb2.ContextInfo(
        context_id=context.context_id, language=context.language, cwd=context.cwd
    )


@dataclass
class FakeCodeService(code_pb2_grpc.CodeServiceServicer):
    """`CodeService` como lo implementa `rayd` en M4/M5 (ver módulo)."""

    token_sha256: str
    phase: str | None = None
    kernel_gate: str | None = None
    retention_seconds: float = 30.0
    languages: frozenset[str] = KNOWN_LANGUAGES
    contexts: dict[str, FakeContext] = field(
        default_factory=lambda: {DEFAULT_CONTEXT_ID: FakeContext(DEFAULT_CONTEXT_ID)}
    )
    executions: list[FakeExecution] = field(default_factory=list)
    runs: dict[str, FakeRun] = field(default_factory=dict)
    execute_requests: list[code_pb2.ExecuteRequest] = field(default_factory=list)
    execute_metadata: list[dict[str, str]] = field(default_factory=list)
    reattach_requests: list[code_pb2.ReattachRequest] = field(default_factory=list)
    create_requests: list[code_pb2.CreateContextRequest] = field(default_factory=list)
    destroy_requests: list[str] = field(default_factory=list)
    restart_requests: list[str] = field(default_factory=list)
    lazy_contexts: list[str] = field(default_factory=list)
    peers: set[str] = field(default_factory=set)
    next_context: int = 1
    lock: threading.Lock = field(default_factory=threading.Lock)

    def CreateContext(
        self, request: code_pb2.CreateContextRequest, context: grpc.ServicerContext
    ) -> code_pb2.CreateContextResponse:
        self._authenticate(context)
        self.create_requests.append(request)
        self._gate_kernel(context)
        language = self._require_language(request.language or DEFAULT_LANGUAGE, context)
        if request.HasField("cwd") and request.cwd not in KNOWN_DIRECTORIES:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "cwd does not exist")
        with self.lock:
            if len(self.contexts) >= MAX_CONTEXTS:
                context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, f"max {MAX_CONTEXTS} contexts")
            context_id = f"ctx-{self.next_context:012x}"
            self.next_context += 1
            self.contexts[context_id] = FakeContext(
                context_id,
                cwd=str(request.cwd) if request.HasField("cwd") else DEFAULT_CWD,
                language=language,
                envs=dict(request.envs),
            )
        return code_pb2.CreateContextResponse(context_id=context_id)

    def Execute(
        self, request: code_pb2.ExecuteRequest, context: grpc.ServicerContext
    ) -> Iterator[code_pb2.ExecuteEvent]:
        self.execute_metadata.append(self._authenticate(context))
        self.execute_requests.append(request)
        self._gate_phase(context)
        self._gate_kernel(context)
        if len(request.code.encode("utf-8")) > MAX_CODE_BYTES:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "code exceeds 1 MiB")
        fake_context = self._execute_context(request, context)
        if request.envs and fake_context.language != DEFAULT_LANGUAGE:
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "envs per execution are only supported on python contexts",
            )
        record = FakeExecution(
            context_id=fake_context.context_id,
            execution_id=f"exec-{secrets.token_hex(8)}",
            code=str(request.code),
            timeout_ms=int(request.timeout_ms),
            envs=dict(request.envs),
        )
        run = FakeRun(record)
        with self.lock:
            self.executions.append(record)
            self.runs[record.execution_id] = run
        subscriber, prelude = run.subscribe(0)
        threading.Thread(
            target=record_cell, args=(Cell(fake_context, request, record, run),), daemon=True
        ).start()
        return stream_subscriber(run, subscriber, prelude, context, origin=True)

    def Reattach(
        self, request: code_pb2.ReattachRequest, context: grpc.ServicerContext
    ) -> Iterator[code_pb2.ExecuteEvent]:
        self._authenticate(context)
        self.reattach_requests.append(request)
        self._gate_phase(context)
        if not EXECUTION_ID.match(request.execution_id):
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "malformed execution_id")
        run = self._retained_run(request, context)
        if len(run.subscribers) >= MAX_SUBSCRIBERS:
            context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, f"max {MAX_SUBSCRIBERS} subscribers")
        try:
            subscriber, prelude = run.subscribe(int(request.from_seq))
        except ReplayOutOfRange as exc:
            context.abort(grpc.StatusCode.OUT_OF_RANGE, str(exc))
        return stream_subscriber(run, subscriber, prelude, context, origin=False)

    def ListContexts(
        self, request: code_pb2.ListContextsRequest, context: grpc.ServicerContext
    ) -> code_pb2.ListContextsResponse:
        self._authenticate(context)
        with self.lock:
            return code_pb2.ListContextsResponse(
                contexts=[context_info(item) for item in self.contexts.values()]
            )

    def DestroyContext(
        self, request: code_pb2.DestroyContextRequest, context: grpc.ServicerContext
    ) -> code_pb2.DestroyContextResponse:
        self._authenticate(context)
        self.destroy_requests.append(str(request.context_id))
        if request.context_id == DEFAULT_CONTEXT_ID:
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, "the default context is protected")
        self._context(request.context_id, context)
        with self.lock:
            del self.contexts[request.context_id]
        return code_pb2.DestroyContextResponse()

    def RestartContext(
        self, request: code_pb2.RestartContextRequest, context: grpc.ServicerContext
    ) -> code_pb2.RestartContextResponse:
        self._authenticate(context)
        self.restart_requests.append(str(request.context_id))
        self._gate_kernel(context)
        self._context(request.context_id, context).reset()
        return code_pb2.RestartContextResponse()

    # --------------------------------------------------------- test controls

    def suspend(self) -> None:
        """Lo que hace `/suspend` con `CodeService`: los `Execute`/`Reattach`
        vivos terminan con `UNAVAILABLE suspending` (sin `end` y sin
        interrumpir la celda) y los nuevos se rechazan igual."""
        with self.lock:
            runs = list(self.runs.values())
        for run in runs:
            if not run.ended:
                run.suspend()
        self.phase = "suspending"

    def resume(self) -> None:
        self.phase = None

    # -------------------------------------------------------------- internals

    def _authenticate(self, context: grpc.ServicerContext) -> dict[str, str]:
        self.peers.add(str(context.peer()))
        return require_access_token(context, self.token_sha256)

    def _gate_phase(self, context: grpc.ServicerContext) -> None:
        if self.phase is not None:
            context.abort(grpc.StatusCode.UNAVAILABLE, self.phase)

    def _gate_kernel(self, context: grpc.ServicerContext) -> None:
        if self.kernel_gate is not None:
            context.abort(grpc.StatusCode.UNAVAILABLE, f"{KERNEL_GATE_PREFIX}: {self.kernel_gate}")

    def _context(self, context_id: str, context: grpc.ServicerContext) -> FakeContext:
        with self.lock:
            found = self.contexts.get(context_id)
        if found is None:
            context.abort(grpc.StatusCode.NOT_FOUND, "context not found")
        return found

    def _require_language(self, language: str, context: grpc.ServicerContext) -> str:
        if language not in KNOWN_LANGUAGES:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, INVALID_LANGUAGE_MESSAGE)
        if language not in self.languages:
            context.abort(
                grpc.StatusCode.UNIMPLEMENTED,
                f"language {language} is not installed in this image; use {POLY_IMAGE}",
            )
        return language

    def _execute_context(
        self, request: code_pb2.ExecuteRequest, context: grpc.ServicerContext
    ) -> FakeContext:
        """La tabla de rutas de `rayd`: `context_id` explícito, `default` para
        Python o el contexto por defecto del lenguaje, creado en la primera
        celda."""
        if request.HasField("language") and request.context_id:
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT, "language cannot be combined with context_id"
            )
        if not request.HasField("language"):
            return self._context(request.context_id or DEFAULT_CONTEXT_ID, context)
        language = self._require_language(request.language, context)
        if language == DEFAULT_LANGUAGE:
            return self._context(DEFAULT_CONTEXT_ID, context)
        context_id = f"{DEFAULT_CONTEXT_ID}-{language}"
        with self.lock:
            found = self.contexts.get(context_id)
            if found is None:
                if len(self.contexts) >= MAX_CONTEXTS:
                    context.abort(
                        grpc.StatusCode.RESOURCE_EXHAUSTED, f"max {MAX_CONTEXTS} contexts"
                    )
                found = FakeContext(context_id, language=language)
                self.contexts[context_id] = found
                self.lazy_contexts.append(context_id)
        return found

    def _retained_run(
        self, request: code_pb2.ReattachRequest, context: grpc.ServicerContext
    ) -> FakeRun:
        with self.lock:
            run = self.runs.get(str(request.execution_id))
        context_id = str(request.context_id) or DEFAULT_CONTEXT_ID
        if run is None or run.record.context_id != context_id or self._expired(run):
            context.abort(grpc.StatusCode.NOT_FOUND, "execution not found")
        return run

    def _expired(self, run: FakeRun) -> bool:
        return run.ended_at is not None and (
            time.monotonic() - run.ended_at >= self.retention_seconds
        )
