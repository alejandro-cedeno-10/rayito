"""Lo que comparten `AgentCoreFleet` y `AsyncAgentCoreFleet` (ADR-031), sin
E/S: el cuerpo y el tipo de cada invocación, el id de sesión que ve quien llama
y el adaptador de la respuesta de `InvokeAgentRuntime` al lector de SSE que ya
usan los despliegues en MicroVMs.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import AsyncIterator, Iterator
from typing import Final

from rayito._agent._deployment_base import (
    EVENT_STREAM_CONTENT_TYPE,
    HTTP_OK,
    JSON_CONTENT_TYPE,
    invocation_body,
)
from rayito._agent._events import AgentEvent, AgentFailed, AgentResult, Done
from rayito._agent._stream_base import require_prompt
from rayito._agentcore._domain import agentcore_session_id
from rayito._agentcore._ports import InvokeResponse

ACCEPT_JSON: Final = JSON_CONTENT_TYPE
ACCEPT_EVENT_STREAM: Final = EVENT_STREAM_CONTENT_TYPE


@dataclasses.dataclass(frozen=True)
class InvocationRequest:
    """Una invocación lista para enviar: el id de sesión de AgentCore (el que
    se devuelve al llamante), el cuerpo, sin `session_id` (el id va sólo en la
    cabecera) y lo que se acepta."""

    session_id: str
    body: bytes
    accept: str


def invocation_request(
    prompt: str,
    session_id: str | None,
    model: str | None,
    reasoning: bool,
    *,
    streaming: bool,
) -> InvocationRequest:
    return InvocationRequest(
        session_id=agentcore_session_id(session_id),
        body=invocation_body(
            require_prompt(prompt), session_id=None, model=model, reasoning=reasoning
        ),
        accept=ACCEPT_EVENT_STREAM if streaming else ACCEPT_JSON,
    )


def with_session_id(result: AgentResult, session_id: str) -> AgentResult:
    """El resultado con el id de sesión que se usó: pasarlo de nuevo continúa
    la misma conversación."""
    return dataclasses.replace(result, session_id=session_id)


def event_with_session_id(event: AgentEvent, session_id: str) -> AgentEvent:
    """El evento con el id de sesión que se usó, si el evento lleva uno."""
    if isinstance(event, Done | AgentFailed):
        return dataclasses.replace(event, session_id=session_id)
    return event


class ChunkedResponse:
    """La respuesta de `InvokeAgentRuntime` con la forma de
    `DeploymentResponse`, para reutilizar `AgentDeploymentStream`: el estado es
    siempre 200 (AgentCore ya tradujo cualquier error a una excepción)."""

    status = HTTP_OK

    def __init__(self, response: InvokeResponse) -> None:
        self._response = response

    def read(self) -> bytes:
        return b"".join(self._response.chunks)

    def chunks(self) -> Iterator[bytes]:
        return self._response.chunks

    def close(self) -> None:
        self._response.close()


class AsyncChunkedResponse:
    """Lo mismo para `AsyncAgentDeploymentStream`: cada trozo se lee en un hilo
    para no bloquear el bucle de eventos."""

    status = HTTP_OK

    def __init__(self, response: InvokeResponse) -> None:
        self._response = response

    async def read(self) -> bytes:
        return await asyncio.to_thread(lambda: b"".join(self._response.chunks))

    def chunks(self) -> AsyncIterator[bytes]:
        return self._chunks()

    async def _chunks(self) -> AsyncIterator[bytes]:
        iterator = iter(self._response.chunks)
        sentinel = object()
        while True:
            chunk = await asyncio.to_thread(next, iterator, sentinel)
            if chunk is sentinel:
                return
            assert isinstance(chunk, bytes)
            yield chunk

    async def close(self) -> None:
        await asyncio.to_thread(self._response.close)
