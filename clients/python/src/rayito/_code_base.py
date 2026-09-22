"""Núcleo puro de `run_code` y los contextos de código, compartido por
`Sandbox` y `AsyncSandbox`: validación, construcción de requests, aritmética
de deadlines, conversión de protos y el `ExecutionBuilder` que convierte el
stream de `Execute` en una `Execution`. Sin I/O.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from typing import Any, Final

from rayito._charts import parse_chart
from rayito._models import (
    CodeContext,
    Execution,
    ExecutionError,
    Logs,
    OutputMessage,
    Result,
)
from rayito._payload import DEFAULT_WORKDIR, validated_envs
from rayito._process_base import timeout_to_ms
from rayito.exceptions import InvalidArgumentException, SandboxException
from rayito.v1 import code_pb2

DEFAULT_CODE_TIMEOUT_SECONDS: Final = 300.0
CODE_STREAM_GRACE_SECONDS: Final = 15.0
CONTEXT_REQUEST_TIMEOUT_SECONDS: Final = 90.0
MAX_CODE_BYTES: Final = 1_048_576
MAIN_RESULT_MIME: Final = "text/plain"
DEFAULT_CONTEXT_ID: Final = "default"
DEFAULT_LANGUAGE: Final = "python"
SUPPORTED_LANGUAGES: Final = frozenset({DEFAULT_LANGUAGE, "bash", "javascript"})
LANGUAGE_ALIASES: Final[dict[str, str]] = {"js": "javascript"}

RESULT_MIME_FIELDS: Final[tuple[tuple[str, str], ...]] = (
    ("text", "text/plain"),
    ("html", "text/html"),
    ("markdown", "text/markdown"),
    ("svg", "image/svg+xml"),
    ("png", "image/png"),
    ("jpeg", "image/jpeg"),
    ("pdf", "application/pdf"),
    ("latex", "text/latex"),
    ("javascript", "application/javascript"),
    ("json", "application/json"),
    ("data", "e2b/data"),
    ("chart", "e2b/chart"),
)
PARSED_JSON_FIELDS: Final = frozenset({"json", "data"})

StdoutCallback = Callable[[OutputMessage], None]
ResultCallback = Callable[[Result], None]
ErrorCallback = Callable[[ExecutionError], None]
ContextLike = CodeContext | str


def validate_code(code: str) -> str:
    if not isinstance(code, str):
        raise InvalidArgumentException(f"code debe ser str, recibido {type(code).__name__}")
    size = len(code.encode("utf-8"))
    if size > MAX_CODE_BYTES:
        raise InvalidArgumentException(
            f"code ocupa {size} bytes y el máximo es {MAX_CODE_BYTES} (1 MiB); escribe el "
            "código en un fichero con files.write() y ejecútalo desde allí"
        )
    return code


def resolve_context_id(context: ContextLike | None) -> str | None:
    """`None` es el contexto por defecto del agente (se omite en el request)."""
    return None if context is None else require_context_id(context)


def require_context_id(context: ContextLike | None) -> str:
    if isinstance(context, CodeContext):
        context = context.id
    if not isinstance(context, str) or not context:
        raise InvalidArgumentException(f"context inválido: {context!r}")
    return context


def normalize_language(language: str | None) -> str | None:
    """El nombre canónico del kernel (`python`, `bash`, `javascript`) sin
    distinguir mayúsculas y con el alias `js`; `None` o `""` es "no enviar"
    (el contexto indicado o el de Python)."""
    if language is None or language == "":
        return None
    if not isinstance(language, str):
        raise InvalidArgumentException(
            f"language debe ser str o None, recibido {type(language).__name__}"
        )
    lowered = language.lower()
    canonical = LANGUAGE_ALIASES.get(lowered, lowered)
    if canonical not in SUPPORTED_LANGUAGES:
        raise InvalidArgumentException(
            f"language debe ser uno de {', '.join(sorted(SUPPORTED_LANGUAGES))} (o None), "
            f"recibido {language!r}"
        )
    return canonical


def validate_language(language: str | None) -> str:
    """Como `normalize_language`, con `python` como valor por defecto (lo que
    `create_code_context` envía)."""
    return normalize_language(language) or DEFAULT_LANGUAGE


def validate_cwd(cwd: str | None) -> str | None:
    if cwd is None or cwd == "":
        return None
    if not isinstance(cwd, str) or "\x00" in cwd or not cwd.startswith("/"):
        raise InvalidArgumentException(f"cwd debe ser una ruta absoluta, recibido {cwd!r}")
    return cwd


def build_execute_request(
    code: str,
    *,
    context_id: str | None = None,
    language: str | None = None,
    envs: Mapping[str, str] | None = None,
    timeout: float | None = DEFAULT_CODE_TIMEOUT_SECONDS,
) -> code_pb2.ExecuteRequest:
    """`timeout_ms` lo impone el agente (`interrupt` al vencer, reinicio del
    contexto 5 s después); `None`/`0` es sin límite. `language` sólo viaja
    cuando se pide (selecciona el contexto por defecto de ese kernel) y es
    excluyente con `context_id`, como en E2B."""
    canonical = normalize_language(language)
    if context_id and canonical is not None:
        raise InvalidArgumentException("language y context son excluyentes")
    request = code_pb2.ExecuteRequest(code=validate_code(code), timeout_ms=timeout_to_ms(timeout))
    if context_id:
        request.context_id = context_id
    if canonical is not None:
        request.language = canonical
    if envs:
        request.envs.update(validated_envs(envs))
    return request


def build_create_context_request(
    *,
    language: str | None = None,
    cwd: str | None = None,
    envs: Mapping[str, str] | None = None,
) -> code_pb2.CreateContextRequest:
    request = code_pb2.CreateContextRequest(language=validate_language(language))
    validated_cwd = validate_cwd(cwd)
    if validated_cwd is not None:
        request.cwd = validated_cwd
    if envs:
        request.envs.update(validated_envs(envs))
    return request


def execute_deadline(timeout: float | None) -> float | None:
    """Deadline gRPC de `Execute`: `timeout + 15 s`, para que el `end` del
    servidor (interrupción, reinicio) llegue antes que el `DEADLINE_EXCEEDED`."""
    if timeout is None or timeout_to_ms(timeout) == 0:
        return None
    return timeout + CODE_STREAM_GRACE_SECONDS


EXECUTION_ID_PATTERN: Final = re.compile(r"^exec-[0-9a-f]{16}$")


def build_reattach_request(
    context_id: str, execution_id: str, from_seq: int
) -> code_pb2.ReattachRequest:
    """`Reattach{context_id, execution_id, from_seq}`; `from_seq = 0` sólo
    entrega eventos nuevos, `N` reenvía los retenidos con `seq >= N`."""
    if not EXECUTION_ID_PATTERN.match(execution_id):
        raise InvalidArgumentException(f"execution_id inválido: {execution_id!r}")
    if isinstance(from_seq, bool) or not isinstance(from_seq, int) or from_seq < 0:
        raise InvalidArgumentException(f"from_seq debe ser un entero >= 0, recibido {from_seq!r}")
    return code_pb2.ReattachRequest(
        context_id=require_context_id(context_id), execution_id=execution_id, from_seq=from_seq
    )


def reattach_failure(exc: Exception) -> SandboxException:
    """Un `NOT_FOUND` (la ejecución terminó hace más de 30 s de reloj corrido)
    o un `OUT_OF_RANGE` (el ring descartó lo que faltaba) al reenganchar una
    celda: el resultado ya no se puede reconstruir."""
    return SandboxException(
        f"no se pudo reenganchar la ejecución tras la reconexión: {exc}; la celda corrió "
        "pero su salida se perdió"
    )


def parse_json_or_raw(document: str) -> Any:
    try:
        return json.loads(document)
    except ValueError:
        return document


def result_from_proto(result: code_pb2.ExecutionResult) -> Result:
    raw: dict[str, str] = {}
    fields: dict[str, Any] = {}
    for name, mime in RESULT_MIME_FIELDS:
        if not result.HasField(name):
            continue
        value = str(getattr(result, name))
        raw[mime] = value
        fields[name] = _parsed_result_field(name, value)
    extra = {str(mime): str(value) for mime, value in result.extra.items()}
    raw.update(extra)
    return Result(**fields, is_main_result=bool(result.is_main_result), extra=extra, raw=raw)


def _parsed_result_field(name: str, value: str) -> Any:
    if name == "chart":
        return parse_chart(value)
    if name in PARSED_JSON_FIELDS:
        return parse_json_or_raw(value)
    return value


def error_from_proto(error: code_pb2.ExecutionError) -> ExecutionError:
    return ExecutionError(
        name=str(error.name),
        value=str(error.value),
        traceback="\n".join(str(line) for line in error.traceback),
    )


def context_from_proto(info: code_pb2.ContextInfo) -> CodeContext:
    return CodeContext(
        id=str(info.context_id),
        language=str(info.language) or DEFAULT_LANGUAGE,
        cwd=str(info.cwd) or DEFAULT_WORKDIR,
    )


def language_default_context_id(request: code_pb2.ExecuteRequest) -> str | None:
    """El id del contexto por defecto que el agente elige para el `language`
    del request (`default` para Python), o `None` si no viaja `language`;
    es el `context_id` que `Reattach` necesita tras una reconexión."""
    if not request.HasField("language"):
        return None
    language = str(request.language)
    if language == DEFAULT_LANGUAGE:
        return DEFAULT_CONTEXT_ID
    return f"{DEFAULT_CONTEXT_ID}-{language}"


def fallback_context(context_id: str, *, language: str | None, cwd: str | None) -> CodeContext:
    """Lo que `create_code_context` devuelve si `ListContexts` no lista el
    contexto recién creado (no debería ocurrir)."""
    return CodeContext(
        id=context_id, language=validate_language(language), cwd=cwd or DEFAULT_WORKDIR
    )


class ExecutionBuilder:
    """Consume los `ExecuteEvent` de una ejecución y construye la `Execution`.

    `feed` devuelve `True` en el `end`. Los `keepalive` se ignoran; los
    callbacks reciben `OutputMessage`, `Result` y `ExecutionError` y una
    excepción suya se propaga al caller (como en E2B). Un segundo `started` o
    cualquier evento después del `end` es una violación de protocolo
    (`SandboxException`).
    """

    def __init__(
        self,
        *,
        context_id: str | None = None,
        on_stdout: StdoutCallback | None = None,
        on_stderr: StdoutCallback | None = None,
        on_result: ResultCallback | None = None,
        on_error: ErrorCallback | None = None,
    ) -> None:
        self._on_stdout = on_stdout
        self._on_stderr = on_stderr
        self._on_result = on_result
        self._on_error = on_error
        self.execution = Execution(results=[], logs=Logs(), error=None, execution_count=None)
        self.context_id = context_id or DEFAULT_CONTEXT_ID
        self.execution_id: str | None = None
        self.last_seq = 0
        self.reattached = 0
        self.started = False
        self.ended = False

    def feed(self, event: code_pb2.ExecuteEvent) -> bool:
        kind = str(event.WhichOneof("event") or "")
        if kind == "keepalive":
            return False
        if self.ended:
            raise SandboxException(
                f"protocol violation: llegó {kind!r} después del end de la ejecución"
            )
        handler = self._handlers().get(kind)
        if handler is None:
            raise SandboxException(f"protocol violation: evento desconocido {kind!r}")
        handler(event)
        self.last_seq = max(self.last_seq, int(event.seq))
        return self.ended

    def reattach_request(self) -> code_pb2.ReattachRequest:
        """El `Reattach` que continúa esta ejecución sin huecos; sólo tiene
        sentido tras `started` (antes no hay `execution_id`)."""
        if self.execution_id is None:
            raise SandboxException("no se puede reenganchar una ejecución sin started")
        return build_reattach_request(self.context_id, self.execution_id, self.last_seq + 1)

    def finish(self) -> Execution:
        if not self.ended:
            raise SandboxException("el stream de Execute terminó sin ExecutionEnd")
        return self.execution

    def _handlers(self) -> dict[str, Callable[[code_pb2.ExecuteEvent], None]]:
        return {
            "started": self._on_started,
            "stdout": self._on_stdout_event,
            "stderr": self._on_stderr_event,
            "result": self._on_result_event,
            "error": self._on_error_event,
            "end": self._on_end,
        }

    def _on_started(self, event: code_pb2.ExecuteEvent) -> None:
        if self.started:
            raise SandboxException("protocol violation: segundo started en la misma ejecución")
        self.started = True
        self.execution_id = str(event.started.execution_id)
        self.execution.execution_count = int(event.started.execution_count)

    def _on_stdout_event(self, event: code_pb2.ExecuteEvent) -> None:
        message = OutputMessage(
            line=str(event.stdout.text), timestamp=int(event.stdout.timestamp_unix_ns), error=False
        )
        self.execution.logs.stdout.append(message.line)
        if self._on_stdout is not None:
            self._on_stdout(message)

    def _on_stderr_event(self, event: code_pb2.ExecuteEvent) -> None:
        message = OutputMessage(
            line=str(event.stderr.text), timestamp=int(event.stderr.timestamp_unix_ns), error=True
        )
        self.execution.logs.stderr.append(message.line)
        if self._on_stderr is not None:
            self._on_stderr(message)

    def _on_result_event(self, event: code_pb2.ExecuteEvent) -> None:
        result = result_from_proto(event.result)
        self.execution.results.append(result)
        if self._on_result is not None:
            self._on_result(result)

    def _on_error_event(self, event: code_pb2.ExecuteEvent) -> None:
        error = error_from_proto(event.error)
        self.execution.error = error
        if self._on_error is not None:
            self._on_error(error)

    def _on_end(self, event: code_pb2.ExecuteEvent) -> None:
        self.ended = True
        count = int(event.end.execution_count)
        if count > 0:
            self.execution.execution_count = count
