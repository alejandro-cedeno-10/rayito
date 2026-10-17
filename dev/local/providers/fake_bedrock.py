"""Bedrock Runtime falso para los tests `local` de los agentes de ejemplo
(`testdata/agent-samples/`, `make local-providers-up`).

Contesta `POST /model/{modelId}/converse` (JSON) y
`POST /model/{modelId}/converse-stream` (flujo de eventos de AWS, el formato
binario `application/vnd.amazon.eventstream`), que es lo que llaman boto3,
`@langchain/aws`, Strands y el Vercel AI SDK. El modelo es un guion mínimo y
determinista, sin red ni claves:

- Si el último mensaje trae el resultado de una herramienta, contesta con él
  (`The answer is 42.`).
- Si el usuario pide una suma (`17 plus 25`) y la petición declara una
  herramienta `add`, pide llamarla con esos dos números.
- Si el texto del usuario lleva `slow:<segundos>`, espera ese tiempo antes de
  contestar (para probar la concurrencia, la cola y el 429).
- En cualquier otro caso contesta `REPLY_TEXT`.

Sólo biblioteca estándar. La parte pura (`turn_for`, `converse_response`,
`stream_events`, `encode_event`) no toca la red y la prueban
`scripts/tests/test_fake_bedrock.py`; `fake_upstream.py` la sirve por HTTPS.
"""

from __future__ import annotations

import json
import re
import struct
import zlib
from dataclasses import dataclass
from typing import Any, Final

REPLY_TEXT: Final = "rayito-fake-ok"
INPUT_TOKENS: Final = 7
OUTPUT_TOKENS: Final = 3
LATENCY_MS: Final = 5
ADD_TOOL: Final = "add"
#: Cotas de los operandos y del espacio entre ellos: con ellas la búsqueda no
#: es polinómica con una entrada hecha de dígitos.
MAX_OPERAND_DIGITS: Final = 9
MAX_SPACES: Final = 4
_DIGITS: Final = rf"\d{{1,{MAX_OPERAND_DIGITS}}}"
_OPERAND: Final = rf"(-?{_DIGITS})"
_GAP: Final = rf"\s{{0,{MAX_SPACES}}}"
SUM_PATTERN: Final = re.compile(rf"{_OPERAND}{_GAP}(?:plus|\+){_GAP}{_OPERAND}", re.IGNORECASE)
SLOW_PATTERN: Final = re.compile(rf"slow:({_DIGITS}(?:\.{_DIGITS})?)")
TOOL_USE_ID: Final = "tooluse_rayito_local"
CONVERSE_SUFFIX: Final = "/converse"
STREAM_SUFFIX: Final = "/converse-stream"
EVENT_STREAM_TYPE: Final = "application/vnd.amazon.eventstream"
STOP_END_TURN: Final = "end_turn"
STOP_TOOL_USE: Final = "tool_use"
HEADER_STRING_TYPE: Final = 7
PRELUDE_BYTES: Final = 12
CRC_BYTES: Final = 4


@dataclass(frozen=True)
class Turn:
    """Lo que el modelo falso contesta: texto, o una llamada a `add`."""

    text: str = ""
    tool_name: str | None = None
    tool_input: dict[str, int] | None = None
    delay_seconds: float = 0.0

    @property
    def stop_reason(self) -> str:
        return STOP_TOOL_USE if self.tool_name else STOP_END_TURN


def _blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content")
    return (
        [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []
    )


def _user_text(payload: dict[str, Any]) -> str:
    messages = payload.get("messages") or []
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        texts = [str(block["text"]) for block in _blocks(message) if "text" in block]
        if texts:
            return " ".join(texts)
    return ""


def _tool_result_text(payload: dict[str, Any]) -> str | None:
    messages = payload.get("messages") or []
    if not messages:
        return None
    for block in _blocks(messages[-1]):
        result = block.get("toolResult")
        if isinstance(result, dict):
            parts = [str(item["text"]) for item in result.get("content", []) if "text" in item]
            return " ".join(parts)
    return None


def _declared_tools(payload: dict[str, Any]) -> set[str]:
    config = payload.get("toolConfig") or {}
    return {
        str(entry["toolSpec"]["name"])
        for entry in config.get("tools", [])
        if isinstance(entry, dict) and "toolSpec" in entry
    }


def turn_for(payload: dict[str, Any]) -> Turn:
    """El guion del modelo falso para una petición de Converse."""
    text = _user_text(payload)
    slow = SLOW_PATTERN.search(text)
    delay = float(slow.group(1)) if slow else 0.0
    result = _tool_result_text(payload)
    if result is not None:
        return Turn(text=f"The answer is {result}.", delay_seconds=delay)
    asked = SUM_PATTERN.search(text)
    if asked and ADD_TOOL in _declared_tools(payload):
        numbers = {"a": int(asked.group(1)), "b": int(asked.group(2))}
        return Turn(tool_name=ADD_TOOL, tool_input=numbers, delay_seconds=delay)
    return Turn(text=REPLY_TEXT, delay_seconds=delay)


def _usage() -> dict[str, int]:
    return {
        "inputTokens": INPUT_TOKENS,
        "outputTokens": OUTPUT_TOKENS,
        "totalTokens": INPUT_TOKENS + OUTPUT_TOKENS,
    }


def _content(turn: Turn) -> list[dict[str, Any]]:
    if turn.tool_name:
        tool_use = {
            "toolUseId": TOOL_USE_ID,
            "name": turn.tool_name,
            "input": turn.tool_input,
        }
        return [{"toolUse": tool_use}]
    return [{"text": turn.text}]


def converse_response(turn: Turn) -> dict[str, Any]:
    """El cuerpo JSON de `converse`."""
    return {
        "output": {"message": {"role": "assistant", "content": _content(turn)}},
        "stopReason": turn.stop_reason,
        "usage": _usage(),
        "metrics": {"latencyMs": LATENCY_MS},
    }


def stream_events(turn: Turn) -> list[tuple[str, dict[str, Any]]]:
    """Los eventos de `converse-stream`, en orden, como `(tipo, carga)`."""
    events: list[tuple[str, dict[str, Any]]] = [("messageStart", {"role": "assistant"})]
    if turn.tool_name:
        start = {"toolUse": {"toolUseId": TOOL_USE_ID, "name": turn.tool_name}}
        events.append(("contentBlockStart", {"contentBlockIndex": 0, "start": start}))
        delta = {"toolUse": {"input": json.dumps(turn.tool_input)}}
    else:
        delta = {"text": turn.text}
    events.append(("contentBlockDelta", {"contentBlockIndex": 0, "delta": delta}))
    events.append(("contentBlockStop", {"contentBlockIndex": 0}))
    events.append(("messageStop", {"stopReason": turn.stop_reason}))
    events.append(("metadata", {"usage": _usage(), "metrics": {"latencyMs": LATENCY_MS}}))
    return events


def _header(name: str, value: str) -> bytes:
    key, text = name.encode(), value.encode()
    return (
        struct.pack("B", len(key)) + key + struct.pack("!BH", HEADER_STRING_TYPE, len(text)) + text
    )


def encode_event(event_type: str, payload: dict[str, Any]) -> bytes:
    """Un mensaje del flujo de eventos de AWS: prelude con longitudes y CRC,
    cabeceras (`:event-type`, `:content-type`, `:message-type`), cuerpo JSON y
    CRC del mensaje."""
    headers = (
        _header(":event-type", event_type)
        + _header(":content-type", "application/json")
        + _header(":message-type", "event")
    )
    body = json.dumps(payload).encode()
    total = PRELUDE_BYTES + len(headers) + len(body) + CRC_BYTES
    prelude = struct.pack("!II", total, len(headers))
    prelude += struct.pack("!I", zlib.crc32(prelude))
    message = prelude + headers + body
    return message + struct.pack("!I", zlib.crc32(message))


def encode_stream(turn: Turn) -> bytes:
    return b"".join(encode_event(kind, payload) for kind, payload in stream_events(turn))


def model_path_kind(path: str) -> str | None:
    """`converse`, `converse-stream` o `None` si la ruta no es de Bedrock."""
    if not path.startswith("/model/"):
        return None
    if path.endswith(STREAM_SUFFIX):
        return "converse-stream"
    return "converse" if path.endswith(CONVERSE_SUFFIX) else None
