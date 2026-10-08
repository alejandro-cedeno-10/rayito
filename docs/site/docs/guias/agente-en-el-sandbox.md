---
title: Agente en el sandbox
description: Correr un agente de código (OpenCode o deepagents) dentro del sandbox, llamando a un modelo en Bedrock, Anthropic o un endpoint compatible con OpenAI sólo a través de la pasarela de secretos.
---

# Agente en el sandbox

<small>Desde 0.8.0 ([Novedades de 0.8.0](../novedades/0.8.0.md)).</small>

`sbx.agent` corre un agente de código ([OpenCode](https://github.com/anomalyco/opencode))
**dentro** del propio sandbox. El agente ve el mismo filesystem, los mismos
comandos y el mismo egress que cualquier otro código que ejecutes con
[`commands.run`](comandos.md), y llama a su modelo sólo a través de la
[pasarela de secretos](../funciones-opcionales/pasarela-de-secretos.md): el
agente **usa** la credencial del modelo, pero nunca puede **leerla**.

La forma normal de usarlo son dos llamadas: `Sandbox.create(...)` con la
pasarela del modelo y `sbx.agent.run(...)` ([sección 2](#2-arranque-normal-crear-el-sandbox-y-ejecutar)).
No hace falta ningún pool ni ningún calentamiento: el
[arranque rápido](#arranque-rapido) (prefetch, pausa entre turnos, pool con
`warmup`) es opcional, y [¿Qué uso?](#que-uso) dice cuándo compensa cada
opción. Si la conversación puede quedarse parada más de 8 h, la opción es
[`persist=`](#mas-de-8-h-parado-persist).

El sandbox necesita una imagen con el runtime instalado (OpenCode en
`/opt/agents`). Si `opencode` no está en el `PATH`, la ejecución falla con
`reason="runtime_missing"`. `AgentTemplate` construye esa imagen (OpenCode,
ripgrep y, si quieres, deepagents) sobre `rayito-base-caps`, la única base
que aplica el egress cerrado: ver
[Templates de agente](../funciones-opcionales/templates-de-agente.md). Los
ejemplos de esta página usan la imagen `rayito-agent` que construye.

## 1. El secreto y la pasarela

La credencial del modelo vive en Secrets Manager. Un preset por proveedor
construye la `SecretGateway` con el `allow=` ya restringido:

| Preset (Python / TypeScript) | Upstream | Cabecera | Rutas permitidas |
|---|---|---|---|
| `bedrock_gateway(secret, *, region, models, rate_per_minute=0)` / `bedrockGateway(secret, { region, models, ratePerMinute })` | `https://bedrock-runtime.<región>.amazonaws.com` | `authorization` | `POST /model/<id>/converse-stream` y `/converse`, **sólo** para los `models` dados |
| `anthropic_gateway(secret, *, rate_per_minute=0)` / `anthropicGateway(secret, { ratePerMinute })` | `https://api.anthropic.com` | `x-api-key` | `POST /v1/messages` |
| `openai_compatible_gateway(secret, *, upstream, base_path="", rate_per_minute=0)` / `openaiCompatibleGateway(secret, { upstream, basePath, ratePerMinute })` | el `upstream` que pases | `authorization` | `POST <base_path>/chat/completions` |

OpenAI, Gemini, Azure OpenAI, OpenRouter, Groq, Mistral, DeepSeek, xAI y un
proxy de LiteLLM tienen su propio preset: ver
[Proveedores del agente](agente-proveedores.md).

`secret` es el nombre de un secreto (o un `SecretRef`). `bedrock_gateway`
nunca abre `/model/*`: eso dejaría llamar desde dentro del sandbox a
cualquier modelo de la cuenta, fuera del presupuesto de tokens del SDK. No
admite ARNs (la pasarela rechaza un `/` codificado en la ruta): usa el id
del modelo o del perfil de inferencia.

Para Bedrock, el secreto guarda `Bearer <clave de API de Bedrock>`. Usa una
clave de corta duración (≤ 12 h) y rótala con `sbx.gateways.refresh()`. Para
una API compatible con OpenAI, también `Bearer <clave>`; para Anthropic, la
clave tal cual.

`rate_per_minute` / `ratePerMinute` (por defecto 0, sin tope) limita las
llamadas por minuto que la pasarela deja pasar, también las que el código
del sandbox haga por su cuenta fuera del presupuesto de tokens del SDK (ver
[Seguridad](../security.md#agente-de-codigo-dentro-del-sandbox)).

## 2. Arranque normal: crear el sandbox y ejecutar

Esto es todo lo que necesitas para la mayoría de los casos: un sandbox con
el egress cerrado, la pasarela del modelo y `run()`.

=== "Python"

    ```python
    from rayito import AgentModel, AgentSpec, Sandbox, SecretStore, bedrock_gateway

    MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

    SecretStore().create("bedrock-key", "Bearer <clave de Bedrock de corta duración>")

    spec = AgentSpec(
        model=AgentModel(
            provider="bedrock",
            id=MODEL_ID,
            gateway="bedrock",
            region="us-east-1",
        ),
        instructions="Responde en español y no modifiques ficheros fuera de /home/user.",
    )

    with Sandbox.create(
        template="rayito-agent",
        allow_internet_access=False,
        gateways={
            "bedrock": bedrock_gateway("bedrock-key", region="us-east-1", models=[MODEL_ID], rate_per_minute=60),
        },
    ) as sbx:
        result = sbx.agent.run("Lista los ficheros de /home/user y resume qué hay.", spec=spec)
        print(result.text, result.steps, result.usage.total)
    ```

=== "Python (async)"

    ```python
    from rayito import AgentModel, AgentSpec, AsyncSandbox, bedrock_gateway

    MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


    async def main() -> None:
        spec = AgentSpec(
            model=AgentModel(provider="bedrock", id=MODEL_ID, gateway="bedrock", region="us-east-1"),
        )
        async with await AsyncSandbox.create(
            template="rayito-agent",
            allow_internet_access=False,
            gateways={
                "bedrock": bedrock_gateway("bedrock-key", region="us-east-1", models=[MODEL_ID], rate_per_minute=60),
            },
        ) as sbx:
            result = await sbx.agent.run("Resume el README.", spec=spec)
            print(result.text)
    ```

=== "TypeScript"

    ```ts
    import { AgentModel, AgentSpec, Sandbox, bedrockGateway } from "rayito";

    const MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0";

    const spec = new AgentSpec({
      model: new AgentModel({
        provider: "bedrock",
        id: MODEL_ID,
        gateway: "bedrock",
        region: "us-east-1",
      }),
    });

    await using sbx = await Sandbox.create({
      template: "rayito-agent",
      allowInternetAccess: false,
      gateways: {
        bedrock: bedrockGateway("bedrock-key", { region: "us-east-1", models: [MODEL_ID], ratePerMinute: 60 }),
      },
    });
    const result = await sbx.agent.run("Resume el README.", { spec });
    console.log(result.text, result.usage.total);
    ```

El primer `run()` de una VM nueva tarda unos segundos más que los
siguientes (lee el binario del runtime del disco recién restaurado); los
turnos siguientes sobre la misma VM ya no lo pagan. Sólo si eso te importa,
mira [Arranque rápido](#arranque-rapido).

`AgentModel.gateway` debe nombrar una entrada de `gateways=`; si no existe,
`run()` lanza `InvalidArgumentException`/`InvalidArgumentError` antes de
cualquier llamada. La pasarela vive en el handle que la creó:
`Sandbox.connect(sandbox_id)` desde otro proceso no la recupera, así que
`agent.run()` sobre ese handle falla igual. Para varios turnos usa el mismo
handle (`sbx.pause()` / `sbx.connect()`, [opción B](#que-uso)) o, más allá
de 8 h, [`persist=`](#mas-de-8-h-parado-persist).

### Opciones de `run()` y `stream()`

| Python | TypeScript | Por defecto | Qué hace |
|---|---|---|---|
| `spec` | `spec` | obligatorio | el `AgentSpec` (abajo) |
| `runtime` | `runtime` | `"opencode"` | `"opencode"`, `"deepagents"` o un objeto runtime como [`DeepAgents(...)`](#deepagents) |
| `session_id` | `sessionId` | ninguna (sesión nueva) | continúa una sesión: el `session_id` de un `AgentResult` anterior |
| `model` | `model` | `spec.model.id` | otro id de modelo para esta ejecución, del mismo proveedor y pasarela (con `bedrock_gateway`, tiene que estar en sus `models`) |
| `limits` | `limits` | `AgentLimits()` | [límites](#3-stream-sesiones-abortar-y-limites) de pasos, tokens, tiempo y salida |
| `workdir` | `workdir` | `/home/user` | directorio de trabajo del agente |
| `reasoning` | `reasoning` | `False` | emite también eventos `Reasoning` |
| — | `signal` | ninguna | un `AbortSignal` que aborta la ejecución |

### `AgentSpec`

| Campo (Python / TypeScript) | Por defecto | Qué hace |
|---|---|---|
| `model` | obligatorio | `AgentModel(provider, id, gateway, region=None, base_path="", prompt_caching=True)` (TS: `new AgentModel({ provider, id, gateway, region, basePath, promptCaching })`). `provider` es `bedrock`, `anthropic` u `openai-compatible`; `region` es obligatoria en Bedrock; `base_path` sólo con `openai-compatible` |
| `small_model` / `smallModel` | el mismo `model.id` | modelo para las tareas auxiliares del runtime (títulos, resúmenes) |
| `instructions` | ninguna | instrucciones de sistema del agente |
| `permissions` | `AgentPermissions(default="allow")` | ver [Permisos](#permisos-no-son-una-frontera-de-seguridad) |
| `agents` | `{}` | subagentes por nombre: `SubAgent(description, instructions, model=None, permissions=None)`. `build` está reservado |
| `mcp` | `{}` | servidores MCP por nombre: `McpLocal(command, envs={}, timeout_seconds=None)` (TS: `timeoutMs`; un proceso dentro del sandbox) o `McpRemote(gateway, path="/")` (siempre por una pasarela). Sólo OpenCode |
| `raw_config` / `rawConfig` | ninguna | se fusiona en la configuración de OpenCode. Las claves que gestiona el SDK (`provider`, `autoupdate`, `share`, `enabled_providers`, `model`, `small_model`, `mcp`, `agent`, `permission`, `instructions`) lanzan `InvalidArgumentException`/`InvalidArgumentError`. Sólo OpenCode |
| `runtime_version` / `runtimeVersion` | ninguna | hoy sólo se valida como texto: no se compara con la versión de la imagen y nunca produce `runtime_version_mismatch` |

Los nombres de `agents` y `mcp` son de 1 a 64 caracteres `[a-z0-9_-]`.
`AgentSpec` no tiene ningún campo para claves: la credencial sólo llega por
la pasarela.

Otros proveedores: cambia el preset y `AgentModel.provider` (`bedrock`,
`anthropic`, `openai`, `google`, `azure` u `openai-compatible`). La tabla
completa, con el secreto y el `AgentModel` de cada uno, está en
[Proveedores del agente](agente-proveedores.md#configurar-cada-proveedor).

=== "Python"

    ```python
    from rayito import AgentModel, anthropic_gateway, openai_compatible_gateway

    anthropic = anthropic_gateway("anthropic-key")
    anthropic_model = AgentModel(provider="anthropic", id="claude-haiku-4-5", gateway="anthropic")

    openai = openai_compatible_gateway("openai-key", upstream="https://llm.example.com", base_path="/v1")
    openai_model = AgentModel(
        provider="openai-compatible", id="mi-modelo", gateway="openai", base_path="/v1"
    )
    ```

=== "TypeScript"

    ```ts
    import { AgentModel, anthropicGateway, openaiCompatibleGateway } from "rayito";

    const anthropic = anthropicGateway("anthropic-key");
    const anthropicModel = new AgentModel({ provider: "anthropic", id: "claude-haiku-4-5", gateway: "anthropic" });

    const openai = openaiCompatibleGateway("openai-key", { upstream: "https://llm.example.com", basePath: "/v1" });
    const openaiModel = new AgentModel({
      provider: "openai-compatible",
      id: "mi-modelo",
      gateway: "openai",
      basePath: "/v1",
    });
    console.log(anthropic, anthropicModel, openai, openaiModel);
    ```

### Subagentes, servidores MCP y permisos

Un `SubAgent` es un agente que el principal puede invocar; un servidor MCP
local corre como proceso hijo dentro del sandbox (lista de argumentos, sin
shell) y uno remoto se alcanza **sólo** por una pasarela, que también tiene
que estar en `gateways=`. `McpLocal.envs` se escribe en la configuración
del runtime, legible por el usuario del sandbox: nunca pongas ahí una
credencial.

=== "Python"

    ```python
    from rayito import AgentModel, AgentPermissions, AgentSpec, McpLocal, McpRemote, SubAgent

    spec = AgentSpec(
        model=AgentModel(
            provider="bedrock",
            id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
            gateway="bedrock",
            region="us-east-1",
        ),
        agents={
            "revisor": SubAgent(
                description="Revisa un diff y señala errores.",
                instructions="Sólo lees: no edites ficheros.",
                permissions=AgentPermissions(tools={"edit": "deny"}),
            ),
        },
        mcp={
            "fs": McpLocal(command=["node", "/opt/mcp/fs/index.js"], timeout_seconds=30),
            "docs": McpRemote(gateway="docs-mcp", path="/mcp"),
        },
        permissions=AgentPermissions(tools={"bash": {"git *": "allow", "*": "deny"}}),
    )
    ```

=== "TypeScript"

    ```ts
    import { AgentModel, AgentPermissions, AgentSpec, McpLocal, McpRemote, SubAgent } from "rayito";

    const spec = new AgentSpec({
      model: new AgentModel({
        provider: "bedrock",
        id: "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        gateway: "bedrock",
        region: "us-east-1",
      }),
      agents: {
        revisor: new SubAgent({
          description: "Revisa un diff y señala errores.",
          instructions: "Sólo lees: no edites ficheros.",
          permissions: new AgentPermissions({ tools: { edit: "deny" } }),
        }),
      },
      mcp: {
        fs: new McpLocal({ command: ["node", "/opt/mcp/fs/index.js"], timeoutMs: 30_000 }),
        docs: new McpRemote({ gateway: "docs-mcp", path: "/mcp" }),
      },
      permissions: new AgentPermissions({ tools: { bash: { "git *": "allow", "*": "deny" } } }),
    });
    console.log(spec.gatewayNames());
    ```

Con este `spec`, el sandbox necesita dos pasarelas: `bedrock` y
`docs-mcp` (`spec.gateway_names()` / `spec.gatewayNames()` las lista). Una
pasarela genérica se crea con `SecretGateway` (ver
[Pasarela de secretos](../funciones-opcionales/pasarela-de-secretos.md)).

!!! info "Coste y activación"
    - **Activa:** llamar a `sbx.agent.run()`, `stream()` o `prepare()`.
      `sbx.agent` es perezoso: tocarlo no hace ninguna llamada.
    - **Recursos y llamadas AWS:** ninguno nuevo de Rayito. El runtime llama
      al modelo (Bedrock `Converse`/`ConverseStream`, o la API del
      proveedor) a través de la pasarela. Cada ejecución hace 2–3 RPC al
      sandbox: escribir la configuración (se salta si su sha no cambió),
      `Start` y el *stdin* con el prompt.
    - **Coste aproximado:** lo domina el modelo, no la VM: en el ejemplo de
      referencia (10 pasos de Haiku 4.5), el modelo cuesta entre ≈ 7× y
      ≈ 21× la VM. Cifras y fórmula en
      [Precios: coste de un agente](../cost.md#coste-de-un-agente-vm-frente-a-modelo).
    - **IAM:** el de la pasarela (`secretsmanager:GetSecretValue` sobre el
      secreto).
    - **Cómo apagarla:** no llames a `sbx.agent`. El resto del sandbox no
      cambia.

## 3. `stream`, sesiones, abortar y límites

`run()` devuelve un `AgentResult` (`text`, el último texto del asistente;
`session_id`/`sessionId`, `steps`, `usage` (un `TokenUsage` con `input`,
`output`, `reasoning`, `cache_read`, `cache_write` y `total`),
`exit_code`/`exitCode`, `tool_calls`/`toolCalls` y
`dropped_lines`/`droppedLines`, las líneas del runtime descartadas) o lanza
`AgentException`/`AgentError`. `stream()` **nunca** lanza por un fallo del
agente: el último evento es `Done` o `AgentFailed`. Los errores del sandbox
o del transporte sí se propagan.

Eventos (`event.type`): `step_started` (`StepStarted`), `text` (`Text`, el
texto completo de una parte; OpenCode no emite deltas), `text_delta`
(`TextDelta`, trozos de texto en curso; sólo deepagents), `reasoning`
(`Reasoning`, sólo con `reasoning=True`), `tool_call` (`ToolCall`: `name`,
`status`, `input` y `output` recortado), `step_finished` (`StepFinished`,
con `usage`), `agent_failed` (`AgentFailed`, con `reason`) y `done`
(`Done`).

El `AgentStream` (`AsyncAgentStream` en async) tiene `result()`, que
consume lo que quede y devuelve el `AgentResult` (o lanza), `abort()`,
`session_id`/`sessionId` y `dropped_lines`/`droppedLines`; se cierra con
`with`/`async with`, `close()`/`aclose()` (Python) o `close()`
(TypeScript).

=== "Python"

    ```python
    from rayito import AgentLimits, AgentSpec, Sandbox


    def fix_tests(sbx: Sandbox, spec: AgentSpec) -> None:
        with sbx.agent.stream(
            "Arregla el test que falla.",
            spec=spec,
            limits=AgentLimits(max_steps=20, timeout_seconds=300),
        ) as s:
            for event in s:
                if event.type == "tool_call":
                    print(event.name, event.status)
            result = s.result()

        sbx.agent.run("Añade un test de regresión.", spec=spec, session_id=result.session_id)
    ```

=== "Python (async)"

    ```python
    from rayito import AgentSpec, AsyncSandbox


    async def fix_tests(sbx: AsyncSandbox, spec: AgentSpec) -> None:
        async with await sbx.agent.stream("Arregla el test que falla.", spec=spec) as s:
            async for event in s:
                if event.type == "tool_call":
                    print(event.name, event.status)
            result = await s.result()
        print(result.session_id)
    ```

=== "TypeScript"

    ```ts
    import { AgentLimits, type AgentSpec, type Sandbox } from "rayito";

    export async function fixTests(sbx: Sandbox, spec: AgentSpec): Promise<void> {
      const stream = await sbx.agent.stream("Arregla el test que falla.", {
        spec,
        limits: new AgentLimits({ maxSteps: 20, timeoutMs: 300_000 }),
      });
      for await (const event of stream) {
        if (event.type === "tool_call") console.log(event.name, event.status);
      }
      const result = await stream.result();

      const controller = new AbortController();
      setTimeout(() => controller.abort(), 60_000);
      await sbx.agent.run("Añade un test de regresión.", {
        spec,
        sessionId: result.sessionId,
        signal: controller.signal,
      });
    }
    ```

`abort()` (Python, también desde otro hilo), cancelar la tarea `asyncio`
o un `AbortSignal` matan el proceso del runtime y todo lo que lanzó. El
resultado es `reason="aborted"`.

Un sandbox corre una sola ejecución del agente a la vez: un segundo `run()`
mientras otro sigue en curso termina con `reason="busy"`.

| Límite (Python / TypeScript) | Por defecto | Qué hace |
|---|---|---|
| `timeout_seconds` / `timeoutMs` | 600 s | plazo del proceso del runtime; obligatorio porque, ante un 5xx del modelo, OpenCode reintenta sin fin. Al vencer: `timeout` |
| `max_steps` / `maxSteps` | 50 | el stream termina con `max_steps` al empezar el paso siguiente |
| `max_total_tokens` / `maxTotalTokens` | 1 000 000 (`None`/`null` lo apaga) | se comprueba tras cada `StepFinished` (puede pasarse hasta en un paso); el stream termina con `token_budget` |
| `max_output_bytes` / `maxOutputBytes` | 16 MiB | tope de la salida del runtime que el SDK guarda; superarlo no corta la ejecución ni produce `output_limit` (ese `reason` existe, pero hoy no se emite) |

Al superar `max_steps` o `max_total_tokens` el SDK para el runtime igual
que `abort()` y espera a que termine: el stream acaba con ese `reason` y el
siguiente `run()` no lo encuentra ocupado (`busy`).

### Qué garantizan el timeout y `abort()`

OpenCode y deepagents corren cada orden de su herramienta de shell en una
sesión propia (`setsid`), fuera del grupo del proceso del runtime, y una
orden puede además demonizarse (un servidor de desarrollo, un `nohup ... &`).
Por eso cada ejecución arranca como un comando con `kill_tree`
([Comandos](comandos.md#primer-plano)): `rayd` hace al runtime *child
subreaper* de todo lo que lance, así que ningún descendiente se le escapa
aunque su padre salga.

- **Timeout** (`timeout_seconds` / `timeoutMs`): `rayd` manda `SIGTERM` al
  runtime y a todos sus descendientes; si a los 5 s queda alguno, congela
  (`SIGSTOP`) el árbol entero y lo mata (`SIGKILL`). El stream acaba con
  `reason="timeout"` sólo cuando ya no queda ninguno, así que el siguiente
  `run()` no encuentra el agente `busy`.
- **`abort()`** y los límites del SDK (`max_steps`, `max_total_tokens`):
  `rayd` congela el árbol, mata a los descendientes y después al runtime, sin
  gracia.
- Sólo se señalan procesos del árbol de esa ejecución: nada del resto del
  sandbox (otros comandos, la PTY, el kernel de `run_code`).
- Una ejecución que termina por sí misma (`Done` o un fallo del runtime) no
  mata lo que dejó corriendo: si el agente arrancó un servidor a propósito,
  sigue vivo.
- Con una imagen cuyo `rayd` sea anterior a esta función, el timeout y
  `abort()` sólo alcanzan al grupo del runtime: lo que la herramienta de
  shell lanzó en su propia sesión sobrevive. Reconstruye la imagen con la
  versión de Rayito del SDK.

Los `reason` posibles están en
[Errores](../referencia/errores.md#agentexception-agenterror). Ningún mensaje
lleva el texto del proveedor, el prompt ni contenido del sandbox.

## Usar el agente desde otro proceso

Un proceso puede crear el sandbox con `gateways=` y otro (un worker, una
API, una tarea programada) conducir el agente con `Sandbox.connect()`, sin
recrearlo. Ese otro proceso necesita el access token del sandbox: el
`sbx.access_token` (`sbx.accessToken` en TypeScript) que guardó quien lo
creó, pasado como `access_token=` (`accessToken`) o en
`RAYITO_ACCESS_TOKEN`; sin él, `connect()` lanza `AuthenticationException`
(`AuthenticationError`). `connect()` pide a `rayd` un `ConfigureStatus` de sólo lectura y
reconstruye `sbx.gateways`: nombre, puerto y último error de cada ruta.
Ningún secreto sale de `rayd`: ni el upstream, ni las cabeceras, ni sus
valores.

=== "Python"

    ```python
    import os

    from rayito import AgentModel, AgentSpec, Sandbox

    MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    spec = AgentSpec(
        model=AgentModel(provider="bedrock", id=MODEL_ID, gateway="bedrock", region="us-east-1"),
    )

    sbx = Sandbox.connect(
        "microvm-00000000-0000-0000-0000-000000000001",
        access_token=os.environ["RAYITO_ACCESS_TOKEN"],
    )
    print(sbx.gateways["bedrock"].url)
    result = sbx.agent.run("Resume el README.", spec=spec)
    ```

=== "Python (async)"

    ```python
    import os

    from rayito import AgentModel, AgentSpec, AsyncSandbox

    MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


    async def main() -> None:
        spec = AgentSpec(
            model=AgentModel(provider="bedrock", id=MODEL_ID, gateway="bedrock", region="us-east-1"),
        )
        sbx = await AsyncSandbox.connect(
            "microvm-00000000-0000-0000-0000-000000000001",
            access_token=os.environ["RAYITO_ACCESS_TOKEN"],
        )
        result = await sbx.agent.run("Resume el README.", spec=spec)
        print(result.text)
    ```

=== "TypeScript"

    ```ts
    import { AgentModel, AgentSpec, Sandbox } from "rayito";

    const MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0";
    const spec = new AgentSpec({
      model: new AgentModel({ provider: "bedrock", id: MODEL_ID, gateway: "bedrock", region: "us-east-1" }),
    });

    const accessToken = process.env.RAYITO_ACCESS_TOKEN;
    if (accessToken === undefined) {
      throw new Error("falta RAYITO_ACCESS_TOKEN");
    }
    const sbx = await Sandbox.connect("microvm-00000000-0000-0000-0000-000000000001", {
      accessToken,
    });
    console.log(sbx.gateways.get("bedrock")?.url);
    const result = await sbx.agent.run("Resume el README.", { spec });
    ```

Sin la definición de la pasarela no hay nada que rotar: en un handle
recuperado, `sbx.gateways.refresh()` sólo relee el estado (puertos y
errores). Rotar la clave, con `refresh()` o con `reincarnate()`, sólo es
posible desde el handle que llamó a `create(gateways=)`: en un handle
recuperado, `reincarnate()` lanza porque no conoce la definición original.
El handle que creó el sandbox conserva el suyo aunque vuelva a llamar a
`connect()`; uno recuperado vuelve a leer el estado en cada `connect()`,
con el mismo `request_timeout` (`requestTimeoutMs` en TypeScript) que esa
llamada. Sobre un agente sin la función
`secret_gateway`, `sbx.gateways` queda vacío y `connect()` no hace ninguna
llamada extra. Si `rayd` rechaza el token en esa lectura, `connect()` no
falla: `sbx.gateways` queda vacío y el error sale en la primera llamada
autenticada, como en cualquier otro `connect()`.

## Permisos: no son una frontera de seguridad

`AgentPermissions(default="allow"|"deny", tools={...})` decide qué
herramientas puede usar el agente. Un valor de `tools` es una acción o un
mapa de patrones (por ejemplo, `{"bash": {"git *": "allow", "*": "deny"}}`).
`"ask"` no es válida: una ejecución sin terminal no puede contestar. Por
defecto se deniegan `question`, `webfetch` y `websearch`; una entrada tuya
en `tools` gana. Con deepagents sólo existen las herramientas `read`,
`edit`, `list`, `glob`, `grep`, `bash`, `task` y `todowrite`, y sólo `bash`
admite patrones.

Pero el runtime ejecuta lo que el modelo pida dentro de lo permitido, sin
preguntar. **La frontera real es la MicroVM más el egress cerrado**
(`allow_internet_access=False`), igual que para cualquier código del
sandbox. `Sandbox.create` deja el egress **abierto** por defecto y el SDK
no avisa si el agente corre así: pasa `allow_internet_access=False` tú
mismo. Riesgos y mitigaciones en
[Seguridad](../security.md#agente-de-codigo-dentro-del-sandbox).

## Arranque rápido

Todo lo de esta sección es **opcional**. El primer `exec` de OpenCode tras
`create()` es lento porque lee el binario desde el disco de la VM recién
restaurada (Q142): el primer token de una VM nueva llega en ≈ 13–28 s
según la tanda (tabla de abajo). **Para la mayoría de los casos no hace
falta ningún pool**: ese coste se paga una vez por conversación, y los
turnos siguientes van sobre la misma VM.

### ¿Qué uso?

1. **¿La conversación dura menos de 8 h desde `create()`?** (el tope de la
   plataforma cuenta el tiempo lanzada **más** el suspendido) → **una VM por
   conversación, pausada entre turnos (B)**. Crea una vez, pausa al acabar
   cada turno y reanuda con `connect()` (≈ 3 s hasta el primer token,
   Q149). Sólo cuesta un ciclo suspend/resume por pausa y el
   almacenamiento del snapshot mientras está pausada
   ([Precios](../cost.md#coste-de-la-vm-con-fast-start)). Sin escribir
   código: la [auto-suspensión](pausar-reanudar.md#auto-suspension-e-idlepolicy)
   por defecto (`IdlePolicy(max_idle_seconds=300, auto_resume=True)`) ya
   la suspende tras 300 s sin tráfico, y la siguiente llamada del SDK
   (`agent.run` incluida) la despierta sola. Sólo tienes que crearla con
   un `timeout` que cubra la conversación (por defecto 3600 s, tope 8 h).
2. **¿Puede quedarse parada más de 8 h?** → guarda su `HOME` en S3 con
   [persistencia](../persistence.md) (`persist=`) y, cuando vuelva el
   usuario, crea otra VM con el mismo `persist=`: restaura el `HOME`, y con
   él las sesiones de OpenCode, así que `session_id` sigue valiendo. Pagas
   otra vez el primer `exec` frío ([ejemplo](#mas-de-8-h-parado-persist)).
3. **¿Llegan muchas conversaciones *nuevas* y su primer mensaje tiene que
   ser rápido?** → un **pool con `warmup` (C)**: el primer token llega a
   ≈ 5 s de la toma, a cambio de un coste fijo por plaza ociosa al mes
   ([Precios](../cost.md#coste-de-la-vm-con-fast-start);
   [ejemplo](#pool-de-agentes-c)).
4. **¿Nada de lo anterior?** → **sin pool**: `Sandbox.create(...)` y
   `sbx.agent.run(...)`, el [arranque normal](#2-arranque-normal-crear-el-sandbox-y-ejecutar).

Una conversación de varios turnos sobre la misma VM (B):

=== "Python"

    ```python
    from rayito import AgentModel, AgentSpec, Sandbox, bedrock_gateway

    MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    spec = AgentSpec(model=AgentModel(provider="bedrock", id=MODEL_ID, gateway="bedrock", region="us-east-1"))

    sbx = Sandbox.create(
        template="rayito-agent",
        timeout=8 * 3600,  # (1)!
        allow_internet_access=False,
        gateways={"bedrock": bedrock_gateway("bedrock-key", region="us-east-1", models=[MODEL_ID])},
    )
    try:
        first = sbx.agent.run("Lee el README y resume el proyecto.", spec=spec)
        sbx.pause()  # (2)!
        sbx.connect()  # (3)!
        second = sbx.agent.run("Ahora propón tres mejoras.", spec=spec, session_id=first.session_id)
        print(second.text)
    finally:
        sbx.kill()
    ```

    1. El tope: 8 h lanzada + suspendida.
    2. Entre turnos; sin esta línea, la auto-suspensión lo hace a los 300 s sin tráfico.
    3. ≈ 3 s hasta el primer token del turno siguiente. Reanuda **este
       handle**, que conserva la pasarela; un `Sandbox.connect(sandbox_id)`
       desde otro proceso no la recupera y `agent.run()` fallaría.

=== "TypeScript"

    ```ts
    import { AgentModel, AgentSpec, Sandbox, bedrockGateway } from "rayito";

    const MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0";
    const spec = new AgentSpec({
      model: new AgentModel({ provider: "bedrock", id: MODEL_ID, gateway: "bedrock", region: "us-east-1" }),
    });

    const sbx = await Sandbox.create({
      template: "rayito-agent",
      timeoutMs: 8 * 3600 * 1000, // el tope: 8 h lanzada + suspendida
      allowInternetAccess: false,
      gateways: { bedrock: bedrockGateway("bedrock-key", { region: "us-east-1", models: [MODEL_ID] }) },
    });
    try {
      const first = await sbx.agent.run("Lee el README y resume el proyecto.", { spec });
      await sbx.pause(); // sin esta línea, la auto-suspensión lo hace a los 300 s sin tráfico
      await sbx.connect(); // ≈ 3 s hasta el primer token; reanuda este handle, que conserva la pasarela
      const second = await sbx.agent.run("Ahora propón tres mejoras.", { spec, sessionId: first.sessionId });
      console.log(second.text);
    } finally {
      await sbx.kill();
    }
    ```

### Más de 8 h parado: `persist=`

El `HOME` (con las sesiones del agente) va a S3 y vuelve en la VM
siguiente. Cada turno crea una VM nueva con el mismo `persist=` y la
pasarela, y guarda el `HOME` antes de soltarla:

=== "Python"

    ```python
    import os

    from rayito import AgentSpec, S3Prefix, Sandbox, bedrock_gateway

    MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    role = os.environ["RAYITO_EXECUTION_ROLE_ARN"]
    home = S3Prefix("amzn-s3-demo-bucket", prefix="rayito-home", name="conversacion-42")


    def turn(spec: AgentSpec, prompt: str, session_id: str | None) -> str:
        with Sandbox.create(  # (1)!
            "rayito-agent",
            execution_role_arn=role,
            persist=home,
            allow_internet_access=False,
            gateways={"bedrock": bedrock_gateway("bedrock-key", region="us-east-1", models=[MODEL_ID])},
        ) as sbx:
            result = sbx.agent.run(prompt, spec=spec, session_id=session_id)
            sbx.checkpoint_files()  # (2)!
            return result.session_id
    ```

    1. Restaura el `HOME` guardado, si lo hay. La imagen necesita las
       capacidades de persistencia (`AgentTemplate` parte de
       `rayito-base-caps`) y un execution role con acceso al prefijo.
    2. `s3://amzn-s3-demo-bucket/rayito-home/conversacion-42/home.tar.gz`.

=== "TypeScript"

    ```ts
    import { type AgentSpec, S3Prefix, Sandbox, bedrockGateway } from "rayito";

    const MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0";
    const executionRoleArn = process.env.RAYITO_EXECUTION_ROLE_ARN ?? "";
    const persist = new S3Prefix({ bucket: "amzn-s3-demo-bucket", prefix: "rayito-home", name: "conversacion-42" });

    export async function turn(spec: AgentSpec, prompt: string, sessionId?: string): Promise<string> {
      await using sbx = await Sandbox.create({
        template: "rayito-agent",
        executionRoleArn,
        persist,
        allowInternetAccess: false,
        gateways: { bedrock: bedrockGateway("bedrock-key", { region: "us-east-1", models: [MODEL_ID] }) },
      });
      const result = await sbx.agent.run(prompt, { spec, sessionId });
      await sbx.checkpointFiles(); // el HOME, con las sesiones del agente, a S3
      return result.sessionId;
    }
    ```

### Pool de agentes (C)

Sólo si llegan muchas conversaciones **nuevas** cuyo primer mensaje tiene
que ser rápido. La plaza se aparca sin pasarela (las plazas calientes nunca
llevan secretos): la pasarela se abre al tomarla, con
`pool.take(gateways=...)`. `Sandbox.create(pool=...)` no admite
`gateways=`, así que en un pool de agentes se usa `take()` directamente.

=== "Python"

    ```python
    from rayito import AgentModel, AgentSpec, PoolConfig, SandboxPool, agent_pool_warmup, bedrock_gateway

    MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    spec = AgentSpec(model=AgentModel(provider="bedrock", id=MODEL_ID, gateway="bedrock", region="us-east-1"))
    config = PoolConfig(
        size=2,
        template="rayito-agent",
        allow_internet_access=False,
        warmup=agent_pool_warmup("opencode"),
    )

    with SandboxPool(config) as pool:
        gateway = bedrock_gateway("bedrock-key", region="us-east-1", models=[MODEL_ID])
        with pool.take(gateways={"bedrock": gateway}) as sbx:
            print(sbx.agent.run("Resume el README.", spec=spec).text)
    ```

=== "TypeScript"

    ```ts
    import { AgentModel, AgentSpec, SandboxPool, agentPoolWarmup, bedrockGateway } from "rayito";

    const MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0";
    const spec = new AgentSpec({
      model: new AgentModel({ provider: "bedrock", id: MODEL_ID, gateway: "bedrock", region: "us-east-1" }),
    });

    await using pool = await new SandboxPool({
      size: 2,
      template: "rayito-agent",
      allowInternetAccess: false,
      warmup: agentPoolWarmup("opencode"),
    }).start();
    await using sbx = await pool.take({
      gateways: { bedrock: bedrockGateway("bedrock-key", { region: "us-east-1", models: [MODEL_ID] }) },
    });
    console.log((await sbx.agent.run("Resume el README.", { spec })).text);
    ```

Detalle del calentamiento y su coste por plaza en
[Pool: calentamiento](../pool.md#calentamiento-warmup).

### Las opciones

Se combinan (un pool con `warmup` sobre una imagen con prefetch, por
ejemplo). Los importes de cada una están en
[Precios: coste de la VM, con fast-start](../cost.md#coste-de-la-vm-con-fast-start).

| Opción | Qué hace | Cuándo usarla | Coste por encima de la VM |
|---|---|---|---|
| **A. Prefetch** | `AgentTemplate(prefetch=True)` (por defecto) hornea un demonio que, tras cada restauración del snapshot, trae el binario a la caché de páginas; en cualquier imagen, `sbx.agent.prepare()` hace lo mismo a mano | siempre que construyas la imagen con `AgentTemplate`; acorta la primera vuelta de cada VM nueva | ≈ $0 |
| **B. `pause()` entre turnos** | la VM se suspende con todo en memoria y `connect()` (o cualquier llamada, con auto-resume) la reanuda | **el caso general**: una conversación de menos de 8 h desde `create()` | un ciclo suspend/resume por pausa + almacenamiento mientras está pausada |
| **C. Pool con `warmup`** | `PoolConfig(warmup=agent_pool_warmup("opencode"))` corre el binario en cada plaza antes de aparcarla | sólo si llegan muchas conversaciones **nuevas** y el primer mensaje tiene que ser rápido | un poco más que una plaza de pool base, por plaza ociosa y mes |
| **Sin pool** | `Sandbox.create(...)` y `sbx.agent.run(...)`, sin calentamiento | conversaciones nuevas cuyo primer mensaje puede tardar lo de una VM nueva | nada además de la VM |

Cada ejecución, además, cuesta su VM (lanzamiento más los segundos de
cómputo hasta la respuesta) y su modelo; el desglose por escenario está en
[Precios](../cost.md#coste-de-la-vm-con-fast-start).

!!! success "Medido en AWS real (2026-10-07, us-east-1, Claude Haiku 4.5, n=5)"
    Tiempo hasta el primer token de una respuesta corta, con el egress
    cerrado y el modelo por `bedrock_gateway` (`AWS_API_NOTES.md` Q146–Q150,
    y Q153 para la fila tras el arreglo de A):

    | Escenario | `create()` / toma p50 | Primer token p50 | Primer token p95 |
    |---|---|---|---|
    | `create()` sin prefetch | 8,7 s | 28,3 s | 41,1 s |
    | A, **antes del arreglo** (el demonio leía en pleno arranque) | 16,6 s | 20,5 s | 37,0 s |
    | sin prefetch, en la misma tanda que la fila siguiente | 8,8 s | 13,3 s | 19,5 s |
    | A, con el demonio que espera a que el guest se calme | 9,2 s | 14,7 s | 15,5 s |
    | B: `connect()` tras `pause()` | 0,55 s | 3,1 s | 3,2 s |
    | C: `pool.take()` con `warmup` | 0,95 s | 5,1 s | 6,7 s |

    - **A**: el demonio baja el tramo posterior a `create()` de 19,6 s a
      4,7 s (−76 %), pero en su primera versión leía nada más restaurar y
      competía con el arranque que `create()` espera, así que `create()`
      tardaba ≈ 8 s más y de extremo a extremo sólo se ganaba un 28 %.
      Ahora espera a que el guest lleve 1 s sin E/S en curso antes de leer,
      y `create()` ya no crece (9,2 s frente a 8,8 s sin prefetch en la
      misma tanda). El tramo tras `create()` baja de 5,2 s a 3,8 s, pero de
      extremo a extremo la diferencia (14,7 s frente a 13,3 s) cae dentro
      del ruido de n=5: el control sin prefetch salió mucho más rápido que
      en la primera medida (13,3 s frente a 28,3 s), así que hoy la ganancia
      de A es pequeña. Se queda encendido porque ya no cuesta nada.
    - **deepagents** no se cachea con un prompt corto: su prompt de una
      palabra son ≈ 3 503 tokens de entrada, por debajo de los 4 096 que
      Haiku exige para un punto de caché, así que cada vuelta los paga
      enteros (≈ 4 veces lo que cuesta la vuelta de OpenCode, que lee 7 596
      de caché; [Precios](../cost.md#coste-de-la-vm-con-fast-start)). Tras
      `create()`, su primer token llega en 7,5 s de mediana frente a 5,0 s
      de OpenCode en la misma imagen.

!!! warning "Los primeros lanzamientos de una versión nueva son lentos"
    Justo después de publicar una versión de imagen, sus primeros
    lanzamientos tardan mucho más: en Q146 las 3 primeras de la versión 1
    tardaron 48–65 s en `create()`, y las 5 primeras de la versión 2,
    26–63 s, frente a 6,9–12,5 s de las siguientes (sin prefetch). Antes de medir o de mandarle tráfico,
    caliéntala con unos cuantos lanzamientos desechables o con un pool.

Sin demonio (por ejemplo, en una imagen propia sin `AgentTemplate`),
`prepare()` lanza el mismo calentamiento a mano:

=== "Python"

    ```python
    from rayito import Sandbox


    def warm(sbx: Sandbox) -> None:
        sbx.agent.prepare()
    ```

=== "TypeScript"

    ```ts
    import type { Sandbox } from "rayito";

    export async function warm(sbx: Sandbox): Promise<void> {
      await sbx.agent.prepare();
    }
    ```

`prepare(runtime="opencode")` (TS: `prepare({ runtime })`) lanza los pasos
de calentamiento en segundo plano y vuelve enseguida, sin esperar a
ninguno.

## deepagents

El runtime por defecto es OpenCode. Con `runtime="deepagents"` (o
`runtime=DeepAgents(...)`) el mismo `run`/`stream` corre un grafo de
[deepagents](https://github.com/langchain-ai/deepagents) (LangChain) en un
runner aislado del venv de la imagen (`AgentTemplate` con deepagents):
mismos eventos (más `TextDelta`), mismos límites del SDK y el modelo sólo
por la pasarela. Sin `entrypoint`, el runner monta `create_deep_agent` con
el modelo, las `instructions` y los subagentes del `AgentSpec`; con
`entrypoint="paquete.modulo:build"` importa tu función desde el directorio
de trabajo y le pasa un `RunnerContext` (modelo, instrucciones, subagentes,
backend, middleware de permisos, directorio de trabajo) para que devuelva
tu grafo compilado. `mcp` y `raw_config` no existen en este
runtime y fallan antes de cualquier llamada. Sus `session_id` tienen la
forma `rda_<32 hex>`.

=== "Python"

    ```python
    from rayito import AgentSpec, DeepAgents, Sandbox


    def run_deepagents(sbx: Sandbox, spec: AgentSpec) -> str:
        result = sbx.agent.run(
            "Resume el README.",
            spec=spec,
            runtime=DeepAgents(entrypoint="mi_agente.grafo:build"),
        )
        return result.text
    ```

=== "TypeScript"

    ```ts
    import { type AgentSpec, DeepAgents, type Sandbox } from "rayito";

    export async function runDeepAgents(sbx: Sandbox, spec: AgentSpec): Promise<string> {
      const result = await sbx.agent.run("Resume el README.", {
        spec,
        runtime: new DeepAgents({ entrypoint: "mi_agente.grafo:build" }),
      });
      return result.text;
    }
    ```

Si tu `build` no pasa `ctx.middleware` al grafo, se pierden los permisos de
`AgentSpec`. Con un prompt corto, deepagents no llega al mínimo de la caché
de prompts de Haiku (ver [Arranque rápido](#arranque-rapido)).

## Construir la imagen desde la CLI

La CLI no corre el agente ni gestiona pools (eso es del SDK); sí construye
la imagen:

```bash
rayito agent template build --bucket amzn-s3-demo-bucket
```

Opciones (`--name`, `--base`, `--memory-mb`, `--no-deepagents`,
`--no-prefetch`, `--force`, `--timeout`) en
[Templates de agente](../funciones-opcionales/templates-de-agente.md).

## El shim de E2B

`rayito.e2b.Sandbox` no tiene `.agent`, porque E2B no define esta
superficie, ni acepta `gateways=`. Para usar el agente, crea el sandbox con
el SDK nativo (`rayito.Sandbox.create(..., gateways=...)`, como en la
[sección 2](#2-arranque-normal-crear-el-sandbox-y-ejecutar)). El handle
nativo de un sandbox del shim (`sbx.native.agent`) no sirve: no tiene
pasarela, así que `agent.run()` falla con `InvalidArgumentException` antes
de cualquier llamada.

## Ver también

- [Templates de agente](../funciones-opcionales/templates-de-agente.md)
- [Pool: calentamiento](../pool.md#calentamiento-warmup)
- [Precios (MicroVMs, pool, agentes)](../cost.md#coste-de-un-agente-vm-frente-a-modelo)
- [Pasarela de secretos](../funciones-opcionales/pasarela-de-secretos.md)
- [Errores: `AgentException`/`AgentError`](../referencia/errores.md#agentexception-agenterror)
- [Seguridad: agente de código dentro del sandbox](../security.md#agente-de-codigo-dentro-del-sandbox)
