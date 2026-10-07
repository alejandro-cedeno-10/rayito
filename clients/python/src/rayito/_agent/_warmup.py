"""Calentamiento de plazas de pool para el agente de IA
(`ai-agent-fast-start`, design.md §6, opción C).

`agent_pool_warmup(runtime)` devuelve la lista de `WarmupStep` para
`PoolConfig(warmup=...)`: los pasos del runtime (`warmup_steps`), que cargan
el runtime en la caché de páginas antes del `pause()` de la plaza.
"""

from __future__ import annotations

from rayito._agent._domain import DEFAULT_AGENT_RUNTIME
from rayito._agent._runtime import AgentRuntime, WarmupStep
from rayito._agent._runtimes import resolve_runtime


def agent_pool_warmup(
    runtime: str | AgentRuntime = DEFAULT_AGENT_RUNTIME,
) -> tuple[WarmupStep, ...]:
    """Los pasos de `PoolConfig(warmup=...)` para un pool de agentes: cargan
    el runtime en la caché de páginas antes de aparcar cada plaza.

    Un pool sólo compensa si llegan muchas conversaciones nuevas cuyo
    primer mensaje tiene que ser rápido: los turnos de una misma
    conversación de menos de 8 h van mejor en una sola VM pausada entre
    turnos (`pause()` y `connect()`, o la auto-suspensión de `IdlePolicy`),
    y más allá de 8 h, con `persist=`. Guía "Agente en el sandbox",
    "¿Qué uso?".

    Coste y activación
    -------------------
    Activa: `PoolConfig(warmup=agent_pool_warmup(...))`.
    Recursos y llamadas AWS: ninguna llamada nueva; cada plaza corre los
        pasos antes de su `pause()` y su snapshot crece con la caché.
    Coste aproximado: ≈ $0,0054 por ciclo de reciclado (≈ 103 al mes) ≈
        $0,64/plaza/mes, frente a ≈ $0,60 de una plaza base; cada `take()`
        lee ≈ $0,0014. Tiempos medidos en AWS (AWS_API_NOTES Q147) por
        precios de lista, us-east-1, consultados 2026-10-06
        (https://aws.amazon.com/lambda/pricing/).
    IAM: ninguna además de la del pool.
    Cómo apagarla: `PoolConfig(warmup=())` (por defecto).
    Ejemplo:
        PoolConfig(size=2, template="rayito-agent", allow_internet_access=False,
                   warmup=agent_pool_warmup("opencode"))
    """
    return tuple(resolve_runtime(runtime).warmup_steps())


__all__ = ["agent_pool_warmup"]
