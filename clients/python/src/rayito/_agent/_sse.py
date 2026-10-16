"""Lector incremental de Server-Sent Events (ADR-028, D6): bytes que llegan
a trozos del cuerpo de `POST /invocations` con `Accept: text/event-stream`
salen como mensajes `(event, data)`. Puro (sin E/S) y compartido por el
cliente síncrono y el asíncrono de `AgentDeployment`.

Sigue la sección 9.2 del estándar HTML (WHATWG, "Server-sent events"): las
líneas acaban en `\\n`, `\\r` o `\\r\\n`; `data:` se acumula con saltos de
línea; `event:` fija el tipo (por defecto `message`); un comentario (`:`) y
los campos `id`/`retry` se ignoran; una línea en blanco cierra el mensaje.
Un mensaje incompleto al cerrar el stream se descarta, como manda el
estándar.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

DEFAULT_EVENT: Final = "message"


@dataclass(frozen=True)
class SseMessage:
    """Un mensaje SSE completo."""

    event: str
    data: str


class SseParser:
    """Alimenta con `feed(chunk)`; cada llamada devuelve los mensajes que el
    trozo completó. Guarda en bytes la línea a medias, así que un carácter
    UTF-8 partido entre dos trozos se decodifica bien."""

    def __init__(self) -> None:
        self._partial = b""
        self._event = ""
        self._data: list[str] = []
        self._after_cr = False

    def feed(self, chunk: bytes) -> list[SseMessage]:
        data = chunk
        if self._after_cr and data.startswith(b"\n"):
            data = data[1:]
        if data:
            self._after_cr = data.endswith(b"\r")
        lines = (self._partial + data).replace(b"\r\n", b"\n").replace(b"\r", b"\n").split(b"\n")
        self._partial = lines.pop()
        messages: list[SseMessage] = []
        for line in lines:
            message = self._line(line.decode("utf-8", errors="replace"))
            if message is not None:
                messages.append(message)
        return messages

    def _line(self, line: str) -> SseMessage | None:
        if not line:
            return self._dispatch()
        if line.startswith(":"):
            return None
        name, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if name == "event":
            self._event = value
        elif name == "data":
            self._data.append(value)
        return None

    def _dispatch(self) -> SseMessage | None:
        if not self._data:
            self._event = ""
            return None
        message = SseMessage(event=self._event or DEFAULT_EVENT, data="\n".join(self._data))
        self._event = ""
        self._data = []
        return message
