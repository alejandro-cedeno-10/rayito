---
title: Templates de agente
description: AgentTemplate compone OpenCode, ripgrep y deepagents sobre rayito-base-caps, con pines por sha256 y el prefetch de arranque. Coste de build y de cada versión de imagen.
---

# Templates de agente

!!! warning "Próximamente"
    `AgentTemplate` y la orden `agent template build` de la CLI llegan con
    `ai-agent-fast-start`, todavía sin fusionar en `main`; los pines
    (`AGENT_OPENCODE_VERSION`, `AGENT_RIPGREP_VERSION` y sus sha256 en
    `limits.json`) y el manifiesto `/opt/agents/rayito-agent.json` que lee
    `sbx.agent` ya están. Se publicará con 0.8.0
    ([borrador de Novedades](../novedades/0.8.0.md)).

`AgentTemplate` es una receta fija sobre el [DSL de `Template`](templates.md)
que instala, en una sola imagen, todo lo que
[Agente en el sandbox](../guias/agente-en-el-sandbox.md) necesita: el
binario de [OpenCode](https://github.com/anomalyco/opencode), `ripgrep` (sus
herramientas de búsqueda lo necesitan y, si no lo encuentra, intentaría
descargarlo — bajo egress cerrado eso falla) y, opcionalmente, un venv con
[deepagents](https://github.com/langchain-ai/deepagents). La receta es la
que validó el spike en AWS real
([`docs/research/2026-10-agent-spike.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/docs/research/2026-10-agent-spike.md)).

=== "Python"

    <!-- noqa: example: API de ai-agent-fast-start, aún no fusionada -->
    ```python
    from rayito import AgentTemplate

    AgentTemplate(
        name="rayito-agent",
        base="rayito-base-caps",
        runtimes=("opencode", "deepagents"),
        prefetch=True,
        memory_mib=2048,
    ).build(bucket="tu-bucket-de-artefactos")
    ```

=== "TypeScript"

    <!-- noqa: example: API de ai-agent-fast-start, aún no fusionada -->
    ```ts
    import { AgentTemplate } from "rayito";

    await new AgentTemplate({
      name: "rayito-agent",
      base: "rayito-base-caps",
      runtimes: ["opencode", "deepagents"],
      prefetch: true,
      memoryMib: 2048,
    }).build({ bucket: "tu-bucket-de-artefactos" });
    ```

=== "CLI"

    <!-- noqa: example: subcomando de ai-agent-fast-start, aún no fusionado -->
    ```bash
    rayito agent template build --name rayito-agent --bucket tu-bucket-de-artefactos --no-deepagents
    ```

`memory_mib`/`memoryMib` por debajo de 2048 lanza
`InvalidArgumentException`/`InvalidArgumentError` antes de cualquier
llamada: el spike midió 575 MiB de RSS para OpenCode solo, y el guest de
2048 MB ve 8016 MiB ([Límites: tamaño](../limits.md#tamano-cpuram) ya
documenta que el guest ve más memoria que la configurada).

!!! info "Coste y activación"
    - **Por defecto**: no se construye nada hasta llamar a `.build()` /
      usar la CLI (subcomando `agent template build`, aún sin fusionar).
    - **Build**: ≈ 277 s medidos en el spike (necesita salida a Internet
      para GitHub y PyPI; el sandbox que arranca desde la imagen resultante,
      no).
    - **Almacenamiento de la versión**: tres snapshots (código, memoria,
      disco) suman ≈ 0,91 + 2,06 + 0,04 ≈ **3,0 GB** × $0,08/GB-mes, con el
      mínimo de una semana por versión de imagen ⇒ ≈ **$0,056/semana**
      (≈ $0,24/mes) por versión. Estimado: asume que el almacenamiento es
      la suma de los tres snapshots.
    - **Lanzamiento**: lectura del snapshot de memoria (0,91 GB)
      ≈ **$0,0014**, igual que cualquier otra imagen.
    - **IAM**: `RayitoTemplateBuilder`, la misma política que
      [Templates declarativos](templates.md) (pila `templates`).
    - **Cómo apagarla**: no construyas el template. Un sandbox normal
      (`rayito-base`/`rayito-base-caps`) no cambia.

## Qué instala

| Pieza | Licencia | Pin | Verificado por |
|---|---|---|---|
| OpenCode | MIT | versión + sha256 del asset de la release | `scripts/check_pins.py` contra `limits.json`/`agentRuntimes` |
| ripgrep | MIT/Unlicense | versión + sha256 | idem |
| deepagents + `langchain-aws` (y `langchain-openai` con el runtime OpenAI-compatible) | MIT | `requirements-deepagents.txt` con `--hash`, `pip install --require-hashes --no-deps --only-binary=:all:` | `pip check` en el propio `run_cmd`; `pip-audit` en CI |

Todo queda `root:root` y `0755`: el usuario del sandbox (uid 1000) no puede
reemplazar los binarios ni el venv ([T30](../security.md#agente-de-codigo-dentro-del-sandbox)).
Las variables `OPENCODE_DISABLE_AUTOUPDATE`, `OPENCODE_DISABLE_MODELS_FETCH`,
`OPENCODE_DISABLE_LSP_DOWNLOAD`, `OPENCODE_DISABLE_DEFAULT_PLUGINS`,
`OPENCODE_PURE` y `OPENCODE_DISABLE_CLAUDE_CODE` apagan todo lo que OpenCode
intentaría bajar de Internet al arrancar y evitan que lea un `.claude/` del
workdir. El manifiesto horneado en `/opt/agents/rayito-agent.json`
(`rayito.agent-template/1`) lista versiones, sha256 y las rutas que el
prefetch calienta; `sbx.agent` lo lee una vez por handle para comprobar
`runtime_version`.

## Prefetch

Con `prefetch=True` (por defecto), el template añade un `start_cmd` que,
desde el arranque, detecta si la VM viene de restaurar un snapshot (un
salto de reloj mayor que `PREFETCH_RESTORE_JUMP_SECONDS`) y, si es así,
precalienta en segundo plano y con prioridad baja (`nice -n19`) el binario
de OpenCode y, con el runtime deepagents instalado, el import de
`deepagents`/`langchain_aws`. No bloquea nada: el sandbox está listo igual
que sin prefetch, y el primer `sbx.agent.run()` encuentra el binario ya en
la caché de páginas la mayoría de las veces. `sbx.agent.prepare()` dispara
el mismo calentamiento cuando no hay demonio (por ejemplo, sin `prefetch`,
o para forzarlo tras un rato de inactividad).

## `--no-deepagents`, `--no-prefetch`

<!-- noqa: example: subcomando de ai-agent-fast-start, aún no fusionado -->
```bash
rayito agent template build --name rayito-agent --bucket tu-bucket-de-artefactos --no-deepagents   # sólo OpenCode: salta el venv (~409 MB menos)
rayito agent template build --name rayito-agent --bucket tu-bucket-de-artefactos --no-prefetch      # sin el start_cmd: el primer exec paga siempre el coste de disco
```

## Ver también

- [Agente en el sandbox](../guias/agente-en-el-sandbox.md).
- [Templates declarativos](templates.md): el DSL que `AgentTemplate` usa por
  debajo.
- [Pool: calentamiento y servidor residente](../pool.md#calentamiento-warmup-y-servidor-residente):
  evitar pagar el primer `exec` en cada toma.
- [Precios (MicroVMs, pool, agentes)](../cost.md#coste-de-un-agente-vm-frente-a-modelo).
