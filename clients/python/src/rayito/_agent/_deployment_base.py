"""Lo que comparten `AgentDeployment` y `AsyncAgentDeployment` (ADR-028, D6),
sin E/S: las etiquetas con las que `deploy()` marca el sandbox, el cuerpo de
una invocación, el mapeo de los códigos HTTP del contrato
`rayito.agent-service/1` a excepciones y la decodificación de la respuesta
JSON y de los mensajes SSE a `AgentResult` y `AgentEvent`.

Los mensajes de error van en español y nunca llevan el prompt, la URL, las
cabeceras ni el token.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from rayito._agent._events import (
    AGENT_FAILURE_REASONS,
    AgentEvent,
    AgentFailed,
    AgentResult,
    Done,
    StepStarted,
    Text,
    TokenUsage,
    ToolCall,
    failure_message,
)
from rayito._agent._protocol_v1 import ProtocolV1State, parse_v1_line
from rayito._agent._sse import SseMessage
from rayito._agent._stream_base import require_prompt_or_input
from rayito.exceptions import (
    AgentException,
    AuthenticationException,
    InvalidArgumentException,
    SandboxException,
    TimeoutException,
    UnimplementedError,
)

#: Etiquetas del sandbox de un despliegue (`Sandbox.create(metadata=)`):
#: `connect()` las lee para reconstruir el cliente en cualquier proceso. Van
#: en el `runHookPayload`, así que nunca llevan datos del usuario.
AGENT_DEPLOYMENT_LABEL: Final = "rayito.agent.deployment"
AGENT_DEPLOYMENT_PORT_LABEL: Final = "rayito.agent.port"
#: Sólo con una app `raw` (MCP u otro protocolo expuesto sin traducir).
AGENT_DEPLOYMENT_PROTOCOL_LABEL: Final = "rayito.agent.protocol"
RAW_PROTOCOL: Final = "raw"
JSON_CONTENT_TYPE: Final = "application/json"
EVENT_STREAM_CONTENT_TYPE: Final = "text/event-stream"
_USAGE_KEYS: Final = ("input", "output", "reasoning", "cache_read", "cache_write")
HTTP_OK: Final = 200
HTTP_BAD_REQUEST: Final = 400
HTTP_UNAUTHORIZED: Final = 401
HTTP_FORBIDDEN: Final = 403
HTTP_PAYLOAD_TOO_LARGE: Final = 413
HTTP_TOO_MANY_REQUESTS: Final = 429
HTTP_UNAVAILABLE: Final = 503
#: `Healthy` / `HealthyBusy` de `GET /ping` (contrato de AgentCore).
PING_BUSY: Final = "HealthyBusy"
PING_HEALTHY: Final = "Healthy"
DeploymentState = Literal["healthy", "busy", "unavailable"]
#: Un `400` a una petición con `input`: el host desplegado es anterior a la
#: entrada estructurada (ADR-033 D3) y no conoce la clave.
HOST_TOO_OLD_MESSAGE: Final = (
    "el agente desplegado no acepta input: vuelve a desplegarlo con esta versión de Rayito"
)
OUTPUT_KEY: Final = "output"


@dataclass(frozen=True)
class DeploymentStatus:
    """El estado de un despliegue según `GET /ping`: `healthy` (listo),
    `busy` (con el cupo de ejecuciones lleno) o `unavailable` (el host no
    contesta o aún no está listo)."""

    state: DeploymentState
    ready: bool
    busy: bool
    port: int
    project: str | None


def invocation_body(
    prompt: str | None,
    *,
    session_id: str | None,
    model: str | None,
    reasoning: bool,
    input: Mapping[str, Any] | None = None,
) -> bytes:
    """El cuerpo JSON de `POST /invocations`: exactamente uno de `prompt` o
    `input` (`require_prompt_or_input`) y sólo lo demás que se pidió."""
    checked_prompt, checked_input = require_prompt_or_input(prompt, input)
    payload: dict[str, object] = (
        {"prompt": checked_prompt} if checked_input is None else {"input": dict(checked_input)}
    )
    if session_id is not None:
        payload["session_id"] = session_id
    if model is not None:
        payload["model"] = model
    if reasoning:
        payload["reasoning"] = True
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def invocation_headers(proxy_headers: Mapping[str, str], *, accept: str) -> dict[str, str]:
    """Las cabeceras de una petición: las del proxy de AWS (JWE vigente) más
    el tipo del cuerpo y lo que se acepta."""
    return {**proxy_headers, "Content-Type": JSON_CONTENT_TYPE, "Accept": accept}


def http_error(status: int, *, attempt: int = 1, sent_input: bool = False) -> Exception:
    """La excepción de un código HTTP del contrato. `403` es del proxy de
    AWS (JWE ausente o caducado): `proxy_rejected=True`, que el llamante
    reintenta una vez tras reacuñar el token; `401` es el token del
    contenedor, que no existe en un MicroVM. Un `400` a una petición con
    `input` (`sent_input`) es un host anterior a la entrada estructurada."""
    if status == HTTP_BAD_REQUEST and sent_input:
        return InvalidArgumentException(HOST_TOO_OLD_MESSAGE)
    if status == HTTP_TOO_MANY_REQUESTS:
        return AgentException(failure_message("busy"), reason="busy")
    if status in (HTTP_FORBIDDEN, HTTP_UNAUTHORIZED):
        return AuthenticationException(
            "el despliegue rechazó las credenciales de la petición",
            proxy_rejected=status == HTTP_FORBIDDEN,
        )
    if status == HTTP_UNAVAILABLE:
        return TimeoutException("el agente desplegado aún no está listo")
    if status in (HTTP_BAD_REQUEST, HTTP_PAYLOAD_TOO_LARGE):
        return InvalidArgumentException(
            "el agente desplegado rechazó la petición: cuerpo inválido o demasiado grande"
        )
    return SandboxException(
        f"el agente desplegado respondió con un estado inesperado ({status})", status_code=status
    )


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _usage(raw: object) -> TokenUsage:
    source = raw if isinstance(raw, Mapping) else {}
    return TokenUsage(**{key: _count(source.get(key)) for key in _USAGE_KEYS})


def _failure(payload: Mapping[str, object], *, session_id: str | None) -> AgentFailed:
    reason = payload.get("reason")
    detail = payload.get("detail_code")
    return AgentFailed(
        reason=reason if reason in AGENT_FAILURE_REASONS else "protocol_error",  # type: ignore[arg-type]
        detail_code=detail if isinstance(detail, str) else None,
        session_id=session_id,
    )


def result_from_body(body: bytes) -> AgentResult:
    """El `AgentResult` de la respuesta JSON de `POST /invocations`. Un
    `failed` es `AgentException` (como `sbx.agent.run()`); un cuerpo que no
    es del contrato es `SandboxException`."""
    try:
        payload = json.loads(body)
    except ValueError:
        payload = None
    if not isinstance(payload, Mapping) or payload.get("v") != 1:
        raise SandboxException("el agente desplegado respondió fuera del contrato del servicio")
    session_id = payload.get("session_id")
    session = session_id if isinstance(session_id, str) else None
    usage = _usage(payload.get("usage"))
    reported = payload.get("usage_reported", True) is not False
    failed = payload.get("failed")
    if isinstance(failed, Mapping):
        raise _failure(failed, session_id=session).to_exception(usage)
    text = payload.get("text")
    steps = payload.get("steps")
    return AgentResult(
        session_id=session or "",
        text=text if isinstance(text, str) else "",
        steps=steps if isinstance(steps, int) and not isinstance(steps, bool) else 0,
        usage=usage,
        exit_code=0,
        usage_reported=reported,
        output=output_of(payload),
    )


def output_of(payload: Mapping[str, object]) -> Mapping[str, Any] | None:
    """La salida estructurada (`output`) de un cuerpo o de un evento `done`,
    si es un objeto; `None` si no la hay."""
    output = payload.get(OUTPUT_KEY)
    return output if isinstance(output, Mapping) else None


@dataclass
class EventDecoder:
    """Mensajes SSE a `AgentEvent`. `session`, `done` y `agent_failed` se
    leen aquí (el servicio puede cortar con motivos que el decodificador v1
    del runner no admite, como `timeout`); el resto lo traduce
    `parse_v1_line`. Un mensaje desconocido se ignora y se cuenta."""

    state: ProtocolV1State = field(default_factory=ProtocolV1State)
    session_id: str | None = None

    def feed(self, message: SseMessage) -> list[AgentEvent]:
        try:
            payload = json.loads(message.data)
        except ValueError:
            self.state.ignored_lines += 1
            return []
        if not isinstance(payload, Mapping):
            self.state.ignored_lines += 1
            return []
        kind = payload.get("type")
        session_id = payload.get("session_id")
        if isinstance(session_id, str) and session_id:
            self.session_id = session_id
        if kind == "session":
            return []
        if kind == "done":
            return [
                Done(
                    session_id=self.session_id or "",
                    exit_code=0,
                    usage=self.state.usage,
                    usage_reported=self.state.usage_reported,
                    output=output_of(payload),
                )
            ]
        if kind == "agent_failed":
            return [_failure(payload, session_id=self.session_id)]
        return list(parse_v1_line(message.data.encode("utf-8"), self.state))


@dataclass
class ResultBuilder:
    """Acumula los eventos de un stream en el `AgentResult` final."""

    text: str = ""
    steps: int = 0
    tool_calls: list[ToolCall] = field(default_factory=list)
    final: Done | AgentFailed | None = None

    def add(self, event: AgentEvent) -> None:
        if isinstance(event, Text):
            self.text = event.text
        elif isinstance(event, StepStarted):
            self.steps += 1
        elif isinstance(event, ToolCall):
            self.tool_calls.append(event)
        elif isinstance(event, Done | AgentFailed):
            self.final = event

    def result(self, *, dropped_lines: int = 0) -> AgentResult:
        """El resultado, o la `AgentException` si el stream acabó en
        `AgentFailed` (o sin terminal: `protocol_error`)."""
        final = self.final
        if isinstance(final, Done):
            return AgentResult(
                session_id=final.session_id,
                text=self.text,
                steps=self.steps,
                usage=final.usage,
                exit_code=final.exit_code,
                tool_calls=tuple(self.tool_calls),
                dropped_lines=dropped_lines,
                usage_reported=final.usage_reported,
                output=final.output,
            )
        failed = final or AgentFailed(reason="protocol_error")
        raise failed.to_exception()


def status_from_ping(
    status: int, body: bytes, *, port: int, project: str | None
) -> DeploymentStatus:
    """El `DeploymentStatus` de la respuesta de `GET /ping`."""
    if status != HTTP_OK:
        return DeploymentStatus("unavailable", False, False, port, project)
    try:
        payload = json.loads(body)
    except ValueError:
        payload = None
    busy = isinstance(payload, Mapping) and payload.get("status") == PING_BUSY
    healthy = isinstance(payload, Mapping) and payload.get("status") in (PING_HEALTHY, PING_BUSY)
    if not healthy:
        return DeploymentStatus("unavailable", False, False, port, project)
    return DeploymentStatus("busy" if busy else "healthy", True, busy, port, project)


@dataclass(frozen=True)
class DeploymentLabels:
    """Lo que `connect()` lee de las etiquetas del sandbox."""

    project: str
    port: int
    raw: bool


def labels_for(project: str, port: int, *, raw: bool) -> dict[str, str]:
    labels = {AGENT_DEPLOYMENT_LABEL: project, AGENT_DEPLOYMENT_PORT_LABEL: str(port)}
    if raw:
        labels[AGENT_DEPLOYMENT_PROTOCOL_LABEL] = RAW_PROTOCOL
    return labels


def parse_labels(metadata: Mapping[str, str] | None) -> DeploymentLabels:
    """Las etiquetas de un despliegue, o `InvalidArgumentException` si el
    sandbox no lo es."""
    values = metadata or {}
    project = values.get(AGENT_DEPLOYMENT_LABEL)
    port_text = values.get(AGENT_DEPLOYMENT_PORT_LABEL)
    if project is None or port_text is None or not port_text.isdigit():
        raise InvalidArgumentException("el sandbox no es un despliegue de agente de Rayito")
    return DeploymentLabels(
        project=project,
        port=int(port_text),
        raw=values.get(AGENT_DEPLOYMENT_PROTOCOL_LABEL) == RAW_PROTOCOL,
    )


def require_invocable(*, raw: bool) -> None:
    """`invoke()` y `stream()` no existen para una app `raw`: sólo se
    expone su `url`."""
    if raw:
        raise UnimplementedError(
            "AgentDeployment.invoke",
            "una app con protocol='raw' sólo se expone: usa url y headers con tu cliente",
            "https://rayito.dev/guias/despliega-tu-agente",
        )
