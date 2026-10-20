"""Las versiones asíncronas del servicio de `--target agentcore` (ADR-031): la
misma superficie que `_service.py` sobre `asyncio.to_thread`, como
`AsyncOptionalStacks`. No reimplementan la lógica: la delegan en el servicio
síncrono, cuyos adaptadores son boto3 bloqueante.
"""

from __future__ import annotations

import asyncio
import time

from rayito._agentcore._models import (
    AgentCoreApplyRequest,
    AgentCoreDeletePlan,
    AgentCoreDeleteResult,
    AgentCoreDeployment,
    AgentCoreDeployPlan,
    AgentCoreListItem,
)
from rayito._agentcore._ports import AgentCoreGateways, RuntimeControl
from rayito._agentcore._service import (
    AgentCoreDeleter,
    AgentCoreDeployer,
    Clock,
    Sleeper,
    list_agents,
)


class AsyncAgentCoreDeployer:
    """`AgentCoreDeployer` con `apply` como corrutina.

    Coste y activación
    -------------------
    Activa: sólo `deploy_agent_async(target="agentcore")`; construirlo no llama
        a AWS.
    Recursos y llamadas AWS: los de `AgentCoreDeployer`.
    Coste aproximado: el de `AgentCoreDeployer`.
    IAM: los de `AgentCoreDeployer`.
    Cómo apagarla: no desplegar; `AsyncAgentCoreFleet.delete()` borra lo creado.
    Ejemplo:
        deployment = await AsyncAgentCoreDeployer(gateways).apply(plan, request)
    """

    def __init__(
        self,
        gateways: AgentCoreGateways,
        *,
        clock: Clock = time.monotonic,
        sleep: Sleeper = time.sleep,
    ) -> None:
        self._inner = AgentCoreDeployer(gateways, clock=clock, sleep=sleep)

    async def apply(
        self, plan: AgentCoreDeployPlan, request: AgentCoreApplyRequest
    ) -> AgentCoreDeployment:
        return await asyncio.to_thread(self._inner.apply, plan, request)


class AsyncAgentCoreDeleter:
    """`AgentCoreDeleter` con `plan` y `delete` como corrutinas.

    Coste y activación
    -------------------
    Activa: sólo `AsyncAgentCoreFleet.delete()`; construirlo no llama a AWS.
    Recursos y llamadas AWS: los de `AgentCoreDeleter`.
    Coste aproximado: ninguno; borrar es gratis.
    IAM: los de `AgentCoreDeleter`.
    Cómo apagarla: no llamar a `delete`.
    Ejemplo:
        plan = await deleter.plan("support-bot", "rayito_support_bot", stack_name)
        result = await deleter.delete(plan)
    """

    def __init__(self, gateways: AgentCoreGateways) -> None:
        self._inner = AgentCoreDeleter(gateways)

    async def plan(self, name: str, runtime_name: str, stack_name: str) -> AgentCoreDeletePlan:
        return await asyncio.to_thread(self._inner.plan, name, runtime_name, stack_name)

    async def delete(self, plan: AgentCoreDeletePlan) -> AgentCoreDeleteResult:
        return await asyncio.to_thread(self._inner.delete, plan)


async def list_agents_async(runtimes: RuntimeControl) -> tuple[AgentCoreListItem, ...]:
    return await asyncio.to_thread(list_agents, runtimes)
