"""`AsyncAgentCoreFleet`: el espejo asíncrono de `AgentCoreFleet` (ADR-031): las
mismas operaciones como corrutinas y `stream()` como contexto asíncrono. Las
llamadas bloqueantes de boto3 corren en un hilo para no detener el bucle de
eventos.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from types import TracebackType
from typing import Any, Self, TypeVar

from rayito._agent._deployment_async import AsyncAgentDeploymentStream
from rayito._agent._deployment_base import result_from_body
from rayito._agent._events import AgentEvent, AgentResult
from rayito._agentcore._aws import (
    AgentCoreClients,
    AgentCoreInvoker,
    deployment_region,
    gateways_for,
)
from rayito._agentcore._domain import (
    agentcore_session_id,
    require_supported_region,
    runtime_name_for,
    stack_name_for,
)
from rayito._agentcore._fleet_base import (
    AgentCoreFleetView,
    AsyncChunkedResponse,
    event_with_session_id,
    invocation_request,
    runtime_not_found,
    with_session_id,
)
from rayito._agentcore._models import (
    AgentCoreDeleteResult,
    AgentCoreStatus,
)
from rayito._agentcore._ports import (
    RuntimeNotFound,
)
from rayito._agentcore._service import AgentCoreDeleter, describe_agent, status_of

T = TypeVar("T")


class AsyncAgentCoreFleetStream:
    """El stream asíncrono de una invocación: los eventos del agente con el id
    de sesión que se usó (`session_id`); `result()` lanza `AgentException` si el
    agente falló."""

    def __init__(self, stream: AsyncAgentDeploymentStream, session_id: str) -> None:
        self._stream = stream
        self._session_id = session_id

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def dropped_lines(self) -> int:
        return self._stream.dropped_lines

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    def __aiter__(self) -> AsyncIterator[AgentEvent]:
        return self._events()

    async def _events(self) -> AsyncIterator[AgentEvent]:
        async for event in self._stream:
            yield event_with_session_id(event, self._session_id)

    async def result(self) -> AgentResult:
        return with_session_id(await self._stream.result(), self._session_id)

    async def close(self) -> None:
        await self._stream.close()


class AsyncAgentCoreFleet(AgentCoreFleetView):
    """El espejo asíncrono de `AgentCoreFleet`: las mismas operaciones como
    corrutinas (`invoke`, `stream`, `status`, `stop_session`, `delete`,
    `connect`) y las mismas propiedades.

    Coste y activación
    -------------------
    Activa: crear la flota con `deploy_agent_async(target="agentcore")` o
        `AsyncAgentCoreFleet.connect()` no llama a AWS más que para leer el
        runtime; cada `invoke()` o `stream()` crea o reanuda una sesión.
    Recursos y llamadas AWS: los de `AgentCoreFleet`.
    Coste aproximado: el de `AgentCoreFleet` (V1: $0,0895 por vCPU-hora y
        $0,00945 por GB-hora de uso real, us-east-1, consultado 2026-10-19 en
        https://aws.amazon.com/bedrock/agentcore/pricing/).
    IAM: los de `AgentCoreFleet`.
    Cómo apagarla: `await fleet.stop_session()` termina una sesión; `await
        fleet.delete()` borra el agente.
    Ejemplo:
        fleet = await AsyncAgentCoreFleet.connect("support-bot")
        result = await fleet.invoke("Resume el README.")
    """

    @classmethod
    async def connect(cls, name: str, *, region: str | None = None, session: Any = None) -> Self:
        """Reconstruye la flota del agente `name` desde su runtime."""
        resolved = require_supported_region(deployment_region(region, session))
        gateways = gateways_for(name, resolved, session=session)
        _, status = await asyncio.to_thread(
            describe_agent, gateways.runtimes, name, resolved, runtime_name_for(name)
        )
        invoker = AgentCoreInvoker(AgentCoreClients(resolved, session))
        return cls(status, gateways=gateways, invoker=invoker)

    async def invoke(
        self,
        prompt: str | None = None,
        session_id: str | None = None,
        model: str | None = None,
        reasoning: bool = False,
        *,
        input: Mapping[str, Any] | None = None,
    ) -> AgentResult:
        """Una invocación sin streaming: el `AgentResult`, o `AgentException` si
        el agente falló. `result.session_id` es el id que usó AgentCore."""
        request = invocation_request(
            prompt, session_id, model, reasoning, streaming=False, input=input
        )
        response = await self._call(
            lambda: self._invoker.invoke(
                self._status.runtime_arn, request.session_id, request.body, request.accept
            )
        )
        try:
            body = await asyncio.to_thread(lambda: b"".join(response.chunks))
        finally:
            await asyncio.to_thread(response.close)
        return with_session_id(result_from_body(body), request.session_id)

    async def stream(
        self,
        prompt: str | None = None,
        session_id: str | None = None,
        model: str | None = None,
        reasoning: bool = False,
        *,
        input: Mapping[str, Any] | None = None,
    ) -> AsyncAgentCoreFleetStream:
        """Como `invoke()`, con los eventos a medida que llegan. La petición se
        hace aquí, así que un error de AWS salta antes del primer evento."""
        request = invocation_request(
            prompt, session_id, model, reasoning, streaming=True, input=input
        )
        response = await self._call(
            lambda: self._invoker.stream(
                self._status.runtime_arn, request.session_id, request.body, request.accept
            )
        )

        async def open_response() -> Any:
            return AsyncChunkedResponse(response)

        inner = AsyncAgentDeploymentStream(open_response)
        await inner.__aenter__()
        return AsyncAgentCoreFleetStream(inner, request.session_id)

    async def status(self) -> AgentCoreStatus:
        """El runtime tal como está ahora (`GetAgentRuntime`)."""
        described = await self._call(lambda: self._gateways.runtimes.get_runtime(self.runtime_id))
        return status_of(self.name, self.region, described)

    async def stop_session(self, session_id: str) -> bool:
        """Termina la microVM de una sesión (`StopRuntimeSession`); `False` si
        ya no existía."""
        used = agentcore_session_id(session_id)
        return bool(await self._call(lambda: self._invoker.stop_session(self.runtime_arn, used)))

    async def delete(self) -> AgentCoreDeleteResult:
        """Borra el agente: el runtime, la pila y el grupo de logs del runtime."""
        deleter = AgentCoreDeleter(self._gateways)
        plan = await asyncio.to_thread(
            deleter.plan, self.name, self.runtime_name, stack_name_for(self.name)
        )
        return await asyncio.to_thread(deleter.delete, plan)

    async def _call(self, operation: Callable[[], T]) -> T:
        try:
            return await asyncio.to_thread(operation)
        except RuntimeNotFound:
            raise runtime_not_found(self.name, self.region) from None
