"""Calentamiento de plazas de pool para el agente de IA
(`ai-agent-fast-start`, design.md §6, opciones C y D).

`agent_pool_warmup(runtime, serve=False)` devuelve la lista de `WarmupStep`
para `PoolConfig(warmup=...)`: los pasos del runtime (`warmup_steps`) más,
con `serve=True`, los que dejan el `opencode serve` residente listo antes
del `pause()` de la plaza:

1. antes de arrancarlo, una configuración mínima en `OPENCODE_CONFIG` (la
   real, con el puerto de la pasarela, sólo existe tras el `take()`);
2. el servidor en segundo plano con una contraseña aleatoria generada
   dentro de cada VM (paso del runtime), así que cada plaza tiene la suya
   (§15) y un reciclado la regenera;
3. un sondeo de `/global/health` con esa contraseña;
4. una instancia caliente con `GET /config?directory=<estado>/warm`.

OpenCode carga la configuración por directorio y de forma perezosa (F1):
tras el `take()`, `agent.run` escribe la configuración con el puerto real y
se engancha con `--dir <workdir>`, que crea una instancia nueva que la lee.
"""

from __future__ import annotations

from typing import Final
from urllib.parse import quote

from rayito._agent._opencode import (
    OPENCODE_CONFIG_PATH,
    OPENCODE_SERVE_SECRET_PATH,
    OPENCODE_SERVE_URL,
    OPENCODE_SERVE_USER,
    shell_quote,
)
from rayito._agent._runtime import AgentRuntime, WarmupStep
from rayito._agent._runtimes import resolve_runtime
from rayito._limits import AGENT_STATE_DIR, DEFAULT_WARMUP_STEP_TIMEOUT_SECONDS
from rayito.exceptions import InvalidArgumentException

#: Único runtime con servidor residente (`opencode serve`).
OPENCODE_RUNTIME_NAME: Final = "opencode"

#: Directorio de la instancia que se calienta antes de aparcar la plaza.
SERVE_WARM_DIR: Final = f"{AGENT_STATE_DIR}/warm"
#: Configuración mínima con la que arranca el servidor residente en una
#: plaza: sin proveedor ni modelo, que llegan con la configuración real.
SERVE_PLACEHOLDER_CONFIG: Final = '{"$schema":"https://opencode.ai/config.json"}'
#: Segundos entre sondeos de `/global/health` mientras arranca el servidor.
SERVE_HEALTH_POLL_SECONDS: Final = 0.5
#: Segundos de `curl` de cada sondeo y de la petición que calienta.
_SERVE_CURL_TIMEOUT_SECONDS: Final = 5
#: Etiquetas de los pasos de primer plano (visibles en `commands.list()`).
SERVE_CONFIG_TAG: Final = "rayito-agent-serve-config"
SERVE_READY_TAG: Final = "rayito-agent-serve-ready"


def _placeholder_config_step() -> WarmupStep:
    config = shell_quote(OPENCODE_CONFIG_PATH)
    return WarmupStep(
        cmd=(
            f'mkdir -p "$(dirname {config})" && '
            f"{{ [ -e {config} ] || printf '%s\\n' {shell_quote(SERVE_PLACEHOLDER_CONFIG)}"
            f" > {config}; }}"
        ),
        tag=SERVE_CONFIG_TAG,
    )


def _serve_ready_step() -> WarmupStep:
    """Espera al servidor (secreto escrito y `/global/health` en 200) y
    calienta una instancia; si no responde dentro de `timeout_seconds`, el
    paso agota su plazo y el calentamiento de la plaza falla."""
    secret = shell_quote(OPENCODE_SERVE_SECRET_PATH)
    health = shell_quote(f"{OPENCODE_SERVE_URL}/global/health")
    warm = shell_quote(f"{OPENCODE_SERVE_URL}/config?directory={quote(SERVE_WARM_DIR, safe='')}")
    auth = f'"{OPENCODE_SERVE_USER}:$(cat {secret})"'
    curl = f"curl -fsS -m {_SERVE_CURL_TIMEOUT_SECONDS} -u {auth}"
    return WarmupStep(
        cmd=(
            "set -u\n"
            f"mkdir -p {shell_quote(SERVE_WARM_DIR)}\n"
            f"until [ -s {secret} ] && {curl} {health} >/dev/null 2>&1; do\n"
            f"  sleep {SERVE_HEALTH_POLL_SECONDS}\n"
            "done\n"
            f"{curl} {warm} >/dev/null\n"
        ),
        timeout_seconds=DEFAULT_WARMUP_STEP_TIMEOUT_SECONDS,
        tag=SERVE_READY_TAG,
    )


def agent_pool_warmup(
    runtime: str | AgentRuntime = "opencode", *, serve: bool = False
) -> tuple[WarmupStep, ...]:
    """Los pasos de `PoolConfig(warmup=...)` para un pool de agentes.

    Sin `serve` (opción C) sólo carga el runtime en la caché de páginas
    antes de aparcar. Con `serve=True` (opción D, sólo OpenCode) deja
    además el servidor residente arrancado y caliente; `agent.run` se
    engancha a él tras el `take()`. D no se recomienda: sólo gana unas
    décimas a C (Q154) y cuesta más memoria y más por plaza.

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
        pasos antes de su `pause()` y su snapshot crece con la caché y, con
        `serve=True`, con el proceso del servidor.
    Coste aproximado: ≈ $0,0054 por ciclo de reciclado (≈ 103 al mes) ≈
        $0,64/plaza/mes sin `serve` y ≈ $0,0069 ≈ $0,82 con `serve`, frente
        a ≈ $0,60 de una plaza base; cada `take()` lee ≈ $0,0014 (≈ $0,0020
        con `serve`). Tiempos medidos en AWS (AWS_API_NOTES Q147 y Q148) por
        precios de lista, us-east-1, consultados 2026-10-06
        (https://aws.amazon.com/lambda/pricing/).
    IAM: ninguna además de la del pool.
    Cómo apagarla: `PoolConfig(warmup=())` (por defecto).
    Ejemplo:
        PoolConfig(size=2, template="rayito-agent", allow_internet_access=False,
                   warmup=agent_pool_warmup("opencode"))
    """
    rt = resolve_runtime(runtime)
    if serve and rt.name != OPENCODE_RUNTIME_NAME:
        raise InvalidArgumentException("serve=True sólo existe para el runtime opencode")
    steps = list(rt.warmup_steps(serve=serve))
    if serve:
        steps = [_placeholder_config_step(), *steps, _serve_ready_step()]
    return tuple(steps)


__all__ = ["SERVE_WARM_DIR", "agent_pool_warmup"]
