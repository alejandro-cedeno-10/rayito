---
title: Templates de agente
description: AgentTemplate compone OpenCode, ripgrep y deepagents sobre rayito-base-caps, con pines por sha256 y el prefetch de arranque. Coste de build y de cada versión de imagen.
---

# Templates de agente

<small>Desde 0.8.0 ([Novedades de 0.8.0](../novedades/0.8.0.md)).</small>

`AgentTemplate` es una receta fija sobre el [DSL de `Template`](templates.md)
que instala, en una sola imagen, todo lo que
[Agente en el sandbox](../guias/agente-en-el-sandbox.md) necesita: el
binario de [OpenCode](https://github.com/anomalyco/opencode), `ripgrep` (sus
herramientas de búsqueda lo necesitan y, si no lo encuentra, intentaría
descargarlo — bajo egress cerrado eso falla) y, opcionalmente, un venv con
[deepagents](https://github.com/langchain-ai/deepagents). La receta es la
que validó el spike en AWS real
([`docs/research/2026-10-agent-spike.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/docs/research/2026-10-agent-spike.md)).

Todos los campos tienen valor por defecto: `AgentTemplate().build(bucket=...)`
construye `rayito-agent` sobre `rayito-base-caps` con OpenCode, deepagents
y el prefetch. El ejemplo los escribe para que se vean:

=== "Python"

    ```python
    from rayito import AgentTemplate

    info = AgentTemplate(
        name="rayito-agent",
        base="rayito-base-caps",
        runtimes=("opencode", "deepagents"),
        prefetch=True,
        memory_mib=2048,
    ).build(bucket="amzn-s3-demo-bucket", on_build_logs=print)
    print(info)
    ```

=== "Python (async)"

    ```python
    from rayito import AsyncAgentTemplate


    async def build() -> None:
        info = await AsyncAgentTemplate(runtimes=("opencode",)).build(bucket="amzn-s3-demo-bucket")
        print(info)
    ```

=== "TypeScript"

    ```ts
    import { AgentTemplate } from "rayito";

    const info = await new AgentTemplate({
      name: "rayito-agent",
      base: "rayito-base-caps",
      runtimes: ["opencode", "deepagents"],
      prefetch: true,
      memoryMib: 2048,
    }).build({ bucket: "amzn-s3-demo-bucket" });
    console.log(info);
    ```

=== "CLI"

    ```bash
    rayito agent template build --name rayito-agent --bucket amzn-s3-demo-bucket
    ```

| Campo (Python / TypeScript) | CLI | Por defecto | Qué es |
|---|---|---|---|
| `name` | `--name` | `rayito-agent` | nombre de la imagen que se crea o actualiza |
| `base` | `--base` | `rayito-base-caps` | imagen base; la variante con capabilities es la única donde `allow_internet_access=False` se aplica |
| `base_version` / `baseVersion` | — | la última | versión de la imagen base |
| `runtimes` | `--deepagents` / `--no-deepagents` | `("opencode", "deepagents")` | qué se instala; la CLI siempre instala OpenCode |
| `prefetch` | `--prefetch` / `--no-prefetch` | `True` | hornea el demonio de [prefetch](#prefetch) |
| `memory_mib` / `memoryMib` | `--memory-mb` | 2048 (mínimo) | memoria de la imagen |

`build()` acepta `bucket` (obligatorio), `force`, `timeout` (segundos;
TS: `timeoutMs`), `on_build_logs`/`onBuildLogs`, `region` y `session`
(TS: `credentials`), como [`Template.build`](templates.md); la CLI, `--bucket`, `--force` y
`--timeout` (1800 s por defecto). Devuelve el `BuildInfo` del build. Sin
construir nada, `to_dockerfile()`/`toDockerfile()` enseña la receta y
`manifest()` el manifiesto que hornea.

`memory_mib`/`memoryMib` por debajo de 2048 lanza
`InvalidArgumentException`/`InvalidArgumentError` antes de cualquier
llamada: el spike midió 575 MiB de RSS para OpenCode solo, y el guest de
2048 MB ve 8016 MiB ([Límites: tamaño](../limits.md#tamano-cpuram) ya
documenta que el guest ve más memoria que la configurada).

!!! info "Coste y activación"
    - **Por defecto**: no se construye nada hasta llamar a `.build()` /
      usar la CLI (`rayito agent template build`).
    - **Build**: 271–320 s medidos en AWS real (2026-10-07, tres builds;
      necesita salida a Internet para GitHub y PyPI; el sandbox que arranca
      desde la imagen resultante, no).
    - **Almacenamiento de la versión**: tres snapshots medidos, código
      2,10 GB + memoria 0,92 GB + disco 0,04 GB ≈ **3,1 GB** × $0,08/GB-mes,
      con el mínimo de una semana por versión de imagen ⇒ ≈
      **$0,057/semana** (≈ $0,25/mes) por versión. Asume que el
      almacenamiento es la suma de los tres snapshots. Precios de lista,
      us-east-1, consultados 2026-10-06: ver
      [Precios](../cost.md#coste-de-la-vm-con-fast-start).
    - **Lanzamiento**: lectura del snapshot de memoria (0,92 GB)
      ≈ **$0,0014**, igual que cualquier otra imagen.
    - **IAM**: `RayitoTemplateBuilder`, la misma política que
      [Templates declarativos](templates.md) (pila `templates`).
    - **Cómo apagarla**: no construyas el template; borra sus versiones
      con `rayito image prune`. Un sandbox normal
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
prefetch calienta. `sbx.agent` no lo lee: `AgentSpec.runtime_version` no se
compara con él.

## Prefetch

Con `prefetch=True` (por defecto), el template añade un `start_cmd` que,
desde el arranque, detecta si la VM viene de restaurar un snapshot (un
salto de reloj mayor que `AGENT_PREFETCH_RESTORE_JUMP_SECONDS`). Entonces
espera a que el guest lleve 1 s seguido sin E/S en curso (como mucho 60 s)
y precalienta con prioridad baja (`nice -n 19`) el binario de OpenCode y,
con el runtime deepagents instalado, el import de
`deepagents`/`langchain_aws`. Espera porque la restauración también lee del
disco (la rotación del kernel que `create()` espera): leer a la vez alargaba
`create()`. Después no cede el paso: el primer `sbx.agent.run()` lee el
mismo binario, así que leerlo por delante le adelanta páginas.
`sbx.agent.prepare()` dispara el mismo calentamiento cuando no hay demonio
(por ejemplo, sin `prefetch`, o para forzarlo tras un rato de inactividad).
Cuándo conviene cada opción de arranque rápido:
[Agente en el sandbox](../guias/agente-en-el-sandbox.md#arranque-rapido).

!!! success "Medido en AWS real (2026-10-07, n=5)"
    El demonio no cambia el tamaño del snapshot de memoria (916–925 MB con
    y sin él). **Antes del arreglo** (el demonio leía nada más restaurar):
    tras `create()`, el primer token de OpenCode llegaba en 4,7 s de
    mediana con prefetch frente a 19,6 s sin él (−76 %), pero `create()`
    tardaba 16,6 s frente a 8,7 s, así que de extremo a extremo (de
    `create()` al primer token) la mejora era 28,3 s → 20,5 s (−28 %).
    Con el demonio que espera a que el guest se calme (Q153, n=5, con un
    control sin prefetch en la misma tanda): `create()` 9,2 s frente a
    8,8 s, así que ya no lo retrasa; el tramo tras `create()` 3,8 s frente a
    5,2 s; de extremo a extremo 14,7 s frente a 13,3 s, dentro del ruido.
    El control salió mucho más rápido que en la primera medida, así que hoy
    la ganancia de A es pequeña; sigue encendido por defecto porque ya no
    cuesta tiempo de `create()`.
    Los primeros lanzamientos de una versión recién publicada son más
    lentos (`create()` de 26 a 65 s): no midas justo después del build.
    Detalle en `AWS_API_NOTES.md` Q146 y Q150.

## `--no-deepagents`, `--no-prefetch`

```bash
rayito agent template build --bucket amzn-s3-demo-bucket --no-deepagents   # sólo OpenCode: salta el venv (~409 MB menos)
rayito agent template build --bucket amzn-s3-demo-bucket --no-prefetch      # sin el start_cmd: el primer exec paga siempre el coste de disco
```

## Ver también

- [Agente en el sandbox](../guias/agente-en-el-sandbox.md).
- [Templates declarativos](templates.md): el DSL que `AgentTemplate` usa por
  debajo.
- [Pool: calentamiento y servidor residente](../pool.md#calentamiento-warmup-y-servidor-residente):
  evitar pagar el primer `exec` en cada toma.
- [Precios (MicroVMs, pool, agentes)](../cost.md#coste-de-un-agente-vm-frente-a-modelo).
