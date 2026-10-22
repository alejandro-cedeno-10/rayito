"""`AgentCoreFleet`: un agente desplegado en Amazon Bedrock AgentCore Runtime
(`deploy_agent(target="agentcore")`, ADR-031). Tiene la API de invocación de
`AgentFleet` (`invoke`, `stream`, el id de sesión que se devuelve y se pasa de
nuevo) sobre `InvokeAgentRuntime` firmado con SigV4, y además `status`,
`stop_session` y `delete`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from typing import Any, Self, TypeVar

from rayito._agent._deployment import AgentDeploymentStream
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
    ChunkedResponse,
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


class AgentCoreFleetStream:
    """El stream de una invocación: los eventos del agente con el id de
    sesión que se usó (`session_id`); el último es `Done` o `AgentFailed`, y
    `result()` lanza `AgentException` si el agente falló."""

    def __init__(self, stream: AgentDeploymentStream, session_id: str) -> None:
        self._stream = stream
        self._session_id = session_id

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def dropped_lines(self) -> int:
        return self._stream.dropped_lines

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __iter__(self) -> Iterator[AgentEvent]:
        for event in self._stream:
            yield event_with_session_id(event, self._session_id)

    def result(self) -> AgentResult:
        return with_session_id(self._stream.result(), self._session_id)

    def close(self) -> None:
        self._stream.close()


class AgentCoreFleet(AgentCoreFleetView):
    """Un agente desplegado en AgentCore Runtime. `invoke()` y `stream()`
    tienen la firma y los resultados de `AgentFleet`: el `session_id` del
    resultado es el que se usó (si pasas uno tuyo, el mismo id llega siempre a
    la misma sesión), y pasarlo de nuevo continúa la conversación: cada sesión
    corre en su propia microVM. `status()` lee el runtime, `stop_session()`
    termina la microVM de una sesión y `delete()` borra el agente.

    Coste y activación
    -------------------
    Activa: crear la flota con `deploy_agent(target="agentcore")` o
        `AgentCoreFleet.connect()` no llama a AWS más que para leer el
        runtime; cada `invoke()` o `stream()` crea o reanuda una sesión de
        AgentCore, que se factura por segundo de uso.
    Recursos y llamadas AWS: `InvokeAgentRuntime`, `StopRuntimeSession`;
        `connect()` y `status()` leen con `ListAgentRuntimes` y
        `GetAgentRuntime`; `delete()` los de `AgentCoreDeleter`.
    Coste aproximado: V1 de la plataforma, $0,0895 por vCPU-hora y $0,00945
        por GB-hora de uso real, por segundo, sin cargo de CPU en espera de
        E/S y con la memoria cobrada hasta que la sesión termina (15 min sin
        uso por defecto); us-east-1, consultado 2026-10-19 en
        https://aws.amazon.com/bedrock/agentcore/pricing/. El modelo lo cobra
        Bedrock.
    IAM: `bedrock-agentcore:InvokeAgentRuntime`, `StopRuntimeSession`,
        `ListAgentRuntimes` y `GetAgentRuntime`.
    Cómo apagarla: `stop_session()` termina una sesión; `delete()` borra el
        agente; sin sesiones activas no hay cómputo.
    Ejemplo:
        fleet = AgentCoreFleet.connect("support-bot")
        result = fleet.invoke("Resume el README.")
        fleet.invoke("¿Y el de docs?", session_id=result.session_id)
    """

    @classmethod
    def connect(
        cls,
        name: str,
        *,
        region: str | None = None,
        session: Any = None,
    ) -> Self:
        """Reconstruye la flota del agente `name` desde su runtime, en
        cualquier proceso. Si no hay ningún agente de Rayito con ese nombre en
        la región, `AgentCoreException` con código `runtime_not_found`."""
        resolved = require_supported_region(deployment_region(region, session))
        gateways = gateways_for(name, resolved, session=session)
        _, status = describe_agent(gateways.runtimes, name, resolved, runtime_name_for(name))
        invoker = AgentCoreInvoker(AgentCoreClients(resolved, session))
        return cls(status, gateways=gateways, invoker=invoker)

    def invoke(
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
        response = self._call(
            lambda: self._invoker.invoke(
                self._status.runtime_arn, request.session_id, request.body, request.accept
            )
        )
        try:
            body = b"".join(response.chunks)
        finally:
            response.close()
        return with_session_id(result_from_body(body), request.session_id)

    def stream(
        self,
        prompt: str | None = None,
        session_id: str | None = None,
        model: str | None = None,
        reasoning: bool = False,
        *,
        input: Mapping[str, Any] | None = None,
    ) -> AgentCoreFleetStream:
        """Como `invoke()`, con los eventos a medida que llegan:
        `with fleet.stream("hola") as events: for event in events`."""
        request = invocation_request(
            prompt, session_id, model, reasoning, streaming=True, input=input
        )
        response = self._call(
            lambda: self._invoker.stream(
                self._status.runtime_arn, request.session_id, request.body, request.accept
            )
        )
        return AgentCoreFleetStream(
            AgentDeploymentStream(ChunkedResponse(response)), request.session_id
        )

    def status(self) -> AgentCoreStatus:
        """El runtime tal como está ahora (`GetAgentRuntime`)."""
        described = self._call(lambda: self._gateways.runtimes.get_runtime(self.runtime_id))
        return status_of(self.name, self.region, described)

    def stop_session(self, session_id: str) -> bool:
        """Termina la microVM de una sesión (`StopRuntimeSession`); `False` si
        ya no existía. `session_id` se mapea igual que en `invoke()`."""
        used = agentcore_session_id(session_id)
        return bool(self._call(lambda: self._invoker.stop_session(self.runtime_arn, used)))

    def delete(self) -> AgentCoreDeleteResult:
        """Borra el agente: el runtime, la pila `rayito-agentcore-<nombre>`
        (con el repositorio de ECR, el proyecto de CodeBuild y los roles) y el
        grupo de logs del runtime. Sólo borra lo que Rayito creó con este
        nombre."""
        deleter = AgentCoreDeleter(self._gateways)
        plan = deleter.plan(self.name, self.runtime_name, stack_name_for(self.name))
        return deleter.delete(plan)

    def _call(self, operation: Callable[[], T]) -> T:
        try:
            return operation()
        except RuntimeNotFound:
            raise runtime_not_found(self.name, self.region) from None
