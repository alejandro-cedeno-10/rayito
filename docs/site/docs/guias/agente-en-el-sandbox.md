---
title: Agente en el sandbox
description: Correr un agente de código (OpenCode) dentro del sandbox, llamando a un modelo en Bedrock, Anthropic o un endpoint compatible con OpenAI sólo a través de la pasarela de secretos.
---

# Agente en el sandbox

!!! warning "Sin publicar todavía"
    `sbx.agent` está en `main` (`ai-agent-core`, ADR-025) pero aún no en
    una release: llegará con la 0.8.0 ([borrador de Novedades](../novedades/0.8.0.md)).
    Lo marcado como **próximamente** en esta página (el runtime deepagents,
    `AgentTemplate` y el calentamiento del pool) pertenece a los cambios
    `ai-agent-deepagents` y `ai-agent-fast-start`, que todavía no están
    fusionados.

`sbx.agent` corre un agente de código ([OpenCode](https://github.com/anomalyco/opencode))
**dentro** del propio sandbox. El agente ve el mismo filesystem, los mismos
comandos y el mismo egress que cualquier otro código que ejecutes con
[`commands.run`](comandos.md), y llama a su modelo sólo a través de la
[pasarela de secretos](../funciones-opcionales/pasarela-de-secretos.md): el
agente **usa** la credencial del modelo, pero nunca puede **leerla**.

El sandbox necesita una imagen con el runtime instalado (OpenCode en
`/opt/agents`, con el manifiesto `/opt/agents/rayito-agent.json`). Sin él,
la ejecución falla con `reason="runtime_missing"`. El constructor de esa
imagen, `AgentTemplate`, es **próximamente**: ver
[Templates de agente](../funciones-opcionales/templates-de-agente.md).

## 1. El secreto y la pasarela

La credencial del modelo vive en Secrets Manager. Un preset por proveedor
construye la `SecretGateway` con el `allow=` ya restringido:

| Preset (Python / TypeScript) | Upstream | Cabecera | Rutas permitidas |
|---|---|---|---|
| `bedrock_gateway` / `bedrockGateway` | `https://bedrock-runtime.<región>.amazonaws.com` | `authorization` | `POST /model/<id>/converse-stream` y `/converse`, **sólo** para los `models` dados |
| `anthropic_gateway` / `anthropicGateway` | `https://api.anthropic.com` | `x-api-key` | `POST /v1/messages` |
| `openai_compatible_gateway` / `openaiCompatibleGateway` | el `upstream` que pases | `authorization` | `POST <base_path>/chat/completions` |

`bedrock_gateway` nunca abre `/model/*`: eso dejaría llamar desde dentro del
sandbox a cualquier modelo de la cuenta, fuera del presupuesto de tokens
del SDK. No admite ARNs (la pasarela rechaza un `/` codificado en la ruta):
usa el id del modelo o del perfil de inferencia.

Para Bedrock, el secreto guarda `Bearer <clave de API de Bedrock>`. Usa una
clave de corta duración (≤ 12 h) y rótala con `sbx.gateways.refresh()`.

## 2. Crear el sandbox y ejecutar

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
            "bedrock": bedrock_gateway("bedrock-key", region="us-east-1", models=[MODEL_ID]),
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
                "bedrock": bedrock_gateway("bedrock-key", region="us-east-1", models=[MODEL_ID]),
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
        bedrock: bedrockGateway("bedrock-key", { region: "us-east-1", models: [MODEL_ID] }),
      },
    });
    const result = await sbx.agent.run("Resume el README.", { spec });
    console.log(result.text, result.usage.total);
    ```

`AgentModel.gateway` debe nombrar una entrada de `gateways=`; si no existe,
`run()` lanza `InvalidArgumentException`/`InvalidArgumentError` antes de
cualquier llamada.

| Campo de `AgentSpec` | Por defecto | Qué hace |
|---|---|---|
| `model` | obligatorio | `AgentModel(provider, id, gateway, region=None, base_path="", prompt_caching=True)`. `provider` es `bedrock`, `anthropic` u `openai-compatible`; `region` es obligatoria en Bedrock |
| `small_model` / `smallModel` | el mismo `model.id` | modelo para las tareas auxiliares del runtime |
| `instructions` | ninguna | instrucciones de sistema del agente |
| `permissions` | `AgentPermissions(default="allow")` | ver [Permisos](#permisos-no-son-una-frontera-de-seguridad) |
| `agents` | `{}` | subagentes por nombre: `SubAgent(description, instructions, model=None, permissions=None)`. `build` está reservado |
| `mcp` | `{}` | servidores MCP por nombre: `McpLocal(command, envs, timeout_seconds)` (un proceso dentro del sandbox) o `McpRemote(gateway, path)` (siempre por una pasarela) |
| `raw_config` / `rawConfig` | ninguna | se mezcla en la configuración de OpenCode. Las claves de `RESERVED_CONFIG_KEYS` (entre ellas `provider`) las gestiona el SDK y lanzan `InvalidArgumentException`/`InvalidArgumentError` |
| `runtime_version` / `runtimeVersion` | ninguna | exige esa versión del runtime en el manifiesto de la imagen (`runtime_version_mismatch` si no coincide) |

`AgentSpec` no tiene ningún campo para claves: la credencial sólo llega por
la pasarela.

Otros proveedores: cambia el preset y `AgentModel.provider`.

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

!!! info "Coste y activación"
    - **Activa:** llamar a `sbx.agent.run()`, `stream()` o `prepare()`.
      `sbx.agent` es perezoso: tocarlo no hace ninguna llamada.
    - **Recursos y llamadas AWS:** ninguno nuevo de Rayito. El runtime llama
      al modelo (Bedrock `Converse`/`ConverseStream`, o la API del
      proveedor) a través de la pasarela. Cada ejecución hace 2–3 RPC al
      sandbox: escribir la configuración (se salta si su sha no cambió),
      `Start` y el *stdin* con el prompt.
    - **Coste aproximado:** lo domina el modelo, no la VM. 10 pasos de
      Haiku 4.5 regional (15k tokens de entrada + 400 de salida por paso)
      ≈ $0,187 sin caché / $0,083 con caché; 5 minutos de VM de 2 GB ≈
      $0,0119 (us-east-1, precios de lista consultados el 2026-10-06).
      Detalle en [Precios](../cost.md#coste-de-un-agente-vm-frente-a-modelo).
    - **IAM:** el de la pasarela (`secretsmanager:GetSecretValue` sobre el
      secreto).
    - **Cómo apagarla:** no llames a `sbx.agent`. El resto del sandbox no
      cambia.

## 3. `stream`, sesiones, abortar y límites

`run()` devuelve un `AgentResult` (`text`, `session_id`, `steps`, `usage`,
`exit_code`, `tool_calls`) o lanza `AgentException`/`AgentError`.
`stream()` **nunca** lanza por un fallo del agente: el último evento es
`Done` o `AgentFailed`. Los errores del sandbox o del transporte sí se
propagan.

Eventos: `StepStarted`, `Text` (el texto completo de una parte; OpenCode no
emite deltas), `Reasoning` (sólo con `reasoning=True`), `ToolCall`,
`StepFinished` (con `usage`), `AgentFailed` y `Done`.

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
o un `AbortSignal` piden primero al runtime que pare con elegancia y
después matan el proceso. El resultado es `reason="aborted"`.

| Límite (Python / TypeScript) | Por defecto | Por qué |
|---|---|---|
| `timeout_seconds` / `timeoutMs` | 600 s | obligatorio: ante un 5xx del modelo, OpenCode reintenta sin fin |
| `max_steps` / `maxSteps` | 50 | el SDK aborta con `max_steps` al empezar el paso siguiente |
| `max_total_tokens` / `maxTotalTokens` | 1 000 000 (`None`/`null` lo apaga) | se comprueba tras cada `StepFinished`: puede pasarse hasta en un paso |
| `max_output_bytes` / `maxOutputBytes` | 16 MiB | tope de la salida del runtime |

Los `reason` posibles están en
[Errores](../referencia/errores.md#agentexception-agenterror). Ningún mensaje
lleva el texto del proveedor, el prompt ni contenido del sandbox.

## Permisos: no son una frontera de seguridad

`AgentPermissions(default="allow"|"deny", tools={...})` decide qué
herramientas puede usar el agente. Un valor de `tools` es una acción o un
mapa de patrones (por ejemplo, `{"bash": {"git *": "allow", "*": "deny"}}`).
`"ask"` no es válida: una ejecución sin terminal no puede contestar. Por
defecto se deniegan `question`, `webfetch` y `websearch`
(`DEFAULT_DENIED_TOOLS`).

Pero el runtime ejecuta lo que el modelo pida dentro de lo permitido, sin
preguntar. **La frontera real es la MicroVM más el egress cerrado**
(`allow_internet_access=False`), igual que para cualquier código del
sandbox. Si el egress no está cerrado, el SDK avisa una vez por handle en
el logger `rayito.agent`. Riesgos y mitigaciones en
[Seguridad](../security.md#agente-de-codigo-dentro-del-sandbox).

## Arranque rápido

El primer `exec` de OpenCode tras `create()` tardó una mediana de 11,3 s en
el spike (es la primera lectura del binario desde el disco restaurado).
Cómo elegir:

| Si… | Usa | Estado | Coste por encima de la VM |
|---|---|---|---|
| no quieres pagar nada extra | **A. Prefetch** al crear (`AgentTemplate(prefetch=True)`) o `sbx.agent.prepare()` en cualquier imagen | `prepare()` en `main`; el demonio de prefetch, próximamente | ≈ $0 (Precio de lista) |
| la misma conversación sigue más tarde (< 8 h) | **B. `pause()` entre turnos** y `connect()` | disponible | ≈ $0,008 por ciclo suspend/resume (Precio de lista) |
| necesitas el agente caliente al instante, con tráfico regular | **C. Pool con `warmup`** | próximamente | ≈ $0,78/plaza/mes (estimación) |
| además quieres el servidor de OpenCode ya arrancado | **D. Pool con servidor residente** | próximamente | ≈ $0,92/plaza/mes (estimación) |

La etapa de aceptación en AWS medirá A, C y D (AWS_API_NOTES Q146–Q152); las
cifras "Medido en AWS real" se añadirán aquí entonces. Desglose en
[Precios](../cost.md#plaza-de-pool-de-agente-c-y-d) y
[Pool](../pool.md#calentamiento-warmup-y-servidor-residente).

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

`prepare()` lanza los pasos de calentamiento en segundo plano y vuelve
enseguida, sin esperar a ninguno.

## deepagents (próximamente)

`runtime=DeepAgents(entrypoint=...)` correrá un grafo de
[deepagents](https://github.com/langchain-ai/deepagents) (LangChain) dentro
de un runner aislado, con la misma API de `run`/`stream`. Llega con
`ai-agent-deepagents`; hasta entonces el único runtime es `"opencode"`
(`DEFAULT_AGENT_RUNTIME`).

## El shim de E2B

`rayito.e2b.Sandbox` no tiene `.agent`, porque E2B no define esta
superficie. Conecta con el SDK nativo al mismo sandbox:

```python
import rayito
from rayito import AgentSpec


def run_native(sandbox_id: str, spec: AgentSpec) -> None:
    native = rayito.Sandbox.connect(sandbox_id)
    native.agent.run("Resume el README.", spec=spec)
```

## Ver también

- [Templates de agente](../funciones-opcionales/templates-de-agente.md)
- [Precios (MicroVMs, pool, agentes)](../cost.md)
- [Pasarela de secretos](../funciones-opcionales/pasarela-de-secretos.md)
- [Errores: `AgentException`/`AgentError`](../referencia/errores.md#agentexception-agenterror)
- [Seguridad: agente de código dentro del sandbox](../security.md#agente-de-codigo-dentro-del-sandbox)
