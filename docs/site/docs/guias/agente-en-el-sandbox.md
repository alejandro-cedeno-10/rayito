---
title: Agente en el sandbox
description: Correr OpenCode o deepagents dentro del sandbox, llamando a un modelo en Bedrock, Anthropic o un endpoint compatible con OpenAI sólo por la pasarela de secretos.
---

# Agente en el sandbox

!!! warning "Borrador, aún no publicado"
    Esta página documenta el diseño aceptado de `sbx.agent` (`ai-agent-core`,
    `ai-agent-fast-start` y `ai-agent-deepagents` en
    [`openspec/changes/`](https://github.com/alejandro-cedeno-10/rayito/tree/main/openspec/changes)).
    Ninguno de los tres cambios está fusionado todavía: la API, los números y
    los nombres de esta página pueden cambiar hasta que lo estén. Llegará
    como parte de una futura 0.8.0 ([Novedades](../novedades/0.8.0.md)). Los
    bloques de código están marcados como no comprobables por ese motivo.

`sbx.agent` corre un agente de código (hoy, [OpenCode](https://github.com/anomalyco/opencode)
o [deepagents](https://github.com/langchain-ai/deepagents)) **dentro** del
propio sandbox: el agente ve el mismo filesystem, los mismos comandos y el
mismo egress que cualquier otro código que ejecutes con
[`commands.run`](comandos.md), y llama a su modelo sólo a través de la
[pasarela de secretos](../funciones-opcionales/pasarela-de-secretos.md) — el
agente **usa** la credencial del modelo, nunca puede **leerla**. La base es
el spike de investigación
([`docs/research/2026-10-agent-spike.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/docs/research/2026-10-agent-spike.md)),
que dio GO para esta fase tras probar los dos runtimes en local (Docker +
Floci) y en AWS real con egress cerrado.

## Definir el `AgentSpec`

`spec=`/`{ spec }` en `run()`/`stream()`/`prepare()` es un `AgentSpec`:
modelo, permisos, instrucciones y límites. Nada de esto corre hasta que lo
pasas a una de esas tres llamadas — ver la caja "Coste y activación" más
abajo.

=== "Python"

    <!-- noqa: example: API de ai-agent-core, aún no fusionada -->
    ```python
    from rayito import SecretStore
    from rayito.agent import AgentSpec, AgentModel, AgentPermissions, AgentLimits
    from rayito.agent.gateways import bedrock_gateway

    # 1. La credencial vive en Secrets Manager, nunca en el código del sandbox.
    secret = SecretStore().create(
        name="bedrock-key",
        value="Bearer <clave de Bedrock de corta duración>",
    )

    # 2. La pasarela: un preset por proveedor, no un allow= a mano.
    gateways = {
        "bedrock": bedrock_gateway(
            secret,
            region="us-east-1",  # obligatorio: Bedrock lo exige en la URL firmada
            models=["us.anthropic.claude-haiku-4-5-20251001-v1:0"],
        ),
    }

    # 3. El AgentSpec: qué modelo, qué puede tocar y hasta dónde.
    agent_spec = AgentSpec(
        model=AgentModel(
            provider="bedrock",
            id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
            gateway="bedrock",       # debe nombrar una entrada de sbx.gateways
            region="us-east-1",
            # small_model=...        # por defecto, el mismo que model.id
        ),
        permissions=AgentPermissions(default="deny", tools={"read": "allow", "write": "allow"}),
        instructions="Eres un asistente que sólo lee y resume ficheros del repo.",
        limits=AgentLimits(timeout_seconds=600, max_steps=50),
    )

    with Sandbox.create(
        template="rayito-agent",
        allow_internet_access=False,
        gateways=gateways,
    ) as sbx:
        result = sbx.agent.run(
            "Lee el README y resume en tres frases qué hace este proyecto.",
            spec=agent_spec,
        )
        print(result.text)
    ```

=== "Python (async)"

    <!-- noqa: example: API de ai-agent-core, aún no fusionada; fragmento async fuera de función -->
    ```python
    from rayito import AsyncSecretStore
    from rayito.agent import AgentSpec, AgentModel
    from rayito.agent.gateways import bedrock_gateway

    secret = await AsyncSecretStore().create(
        name="bedrock-key", value="Bearer <clave de Bedrock de corta duración>",
    )
    gateways = {
        "bedrock": bedrock_gateway(
            secret, region="us-east-1",
            models=["us.anthropic.claude-haiku-4-5-20251001-v1:0"],
        ),
    }
    agent_spec = AgentSpec(
        model=AgentModel(
            provider="bedrock",
            id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
            gateway="bedrock",
            region="us-east-1",
        ),
    )
    async with Sandbox.create(
        template="rayito-agent", allow_internet_access=False, gateways=gateways,
    ) as sbx:
        result = await sbx.agent.run("Resume el README.", spec=agent_spec)
    ```

=== "TypeScript"

    <!-- noqa: example: API de ai-agent-core, aún no fusionada -->
    ```ts
    import { Sandbox, SecretStore } from "rayito";
    import { AgentSpec, AgentModel, AgentPermissions, AgentLimits, bedrockGateway } from "rayito/agent";

    const secret = await new SecretStore().create({
      name: "bedrock-key",
      value: "Bearer <clave de Bedrock de corta duración>",
    });

    const gateways = {
      bedrock: bedrockGateway(secret, {
        region: "us-east-1",
        models: ["us.anthropic.claude-haiku-4-5-20251001-v1:0"],
      }),
    };

    const agentSpec = new AgentSpec({
      model: new AgentModel({
        provider: "bedrock",
        id: "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        gateway: "bedrock",   // debe nombrar una entrada de sbx.gateways
        region: "us-east-1",
      }),
      permissions: new AgentPermissions({ default: "deny", tools: { read: "allow", write: "allow" } }),
      instructions: "Eres un asistente que sólo lee y resume ficheros del repo.",
      limits: new AgentLimits({ timeoutSeconds: 600, maxSteps: 50 }),
    });

    const sbx = await Sandbox.create({
      template: "rayito-agent",
      allowInternetAccess: false,
      gateways,
    });
    try {
      const result = await sbx.agent.run(
        "Lee el README y resume en tres frases qué hace este proyecto.",
        { spec: agentSpec },
      );
      console.log(result.text);
    } finally {
      await sbx.kill();
    }
    ```

| Campo de `AgentSpec` | Tipo | Por defecto | Qué hace |
|---|---|---|---|
| `model` | `AgentModel` | obligatorio | proveedor, id del modelo, qué entrada de `gateways` usar y, en Bedrock, la región |
| `model.gateway` | `str` | obligatorio | debe nombrar una entrada de `sbx.gateways`: una pasarela que no exista es `InvalidArgumentException`/`InvalidArgumentError` antes de llamar a nada |
| `model.region` | `str` | obligatorio en Bedrock | Bedrock firma la URL por región; Anthropic directa y OpenAI-compatible no lo necesitan |
| `model.small_model` | `str` | `model.id` | el modelo que usan las sub-tareas baratas de OpenCode (resumen de título, etc.); por defecto, el mismo que el principal |
| `permissions` | `AgentPermissions` | `default="allow"`, sin `tools` | ver [Permisos](#permisos-no-son-una-frontera-de-seguridad) |
| `instructions` | `str \| Path` | ninguna | se escribe en `AGENT_STATE_DIR/<sha256>/AGENTS.md` dentro del sandbox (el sha es del contenido, para no reescribir si no cambió); OpenCode además lee el `AGENTS.md` del propio *workdir* si existe, y lo combina con éste |
| `limits` | `AgentLimits` | ver [Límites](#limites) | los mismos `timeout_seconds`/`max_steps`/`max_total_tokens`/`max_output_bytes` que puedes pasar sueltos a `run()` |
| `sub_agents`/`subAgents` | `list[SubAgent]` | `[]` | sub-agentes con su propio modelo/instrucciones/herramientas, invocables como herramienta por el principal |
| `mcp` | `list[McpLocal \| McpRemote]` | `[]` | servidores MCP adicionales: `McpLocal` (comando dentro del sandbox) o `McpRemote` (URL, también por una pasarela si pide credencial) |
| `raw_config` | `dict` | `{}` | se vuelca tal cual en el `opencode.json` generado; las claves de `RESERVED_CONFIG_KEYS` (`model`, `provider`, `mcp`, `permission`, `instructions`) las gestiona el `AgentSpec` y no se pueden pisar desde aquí — lanza `InvalidArgumentException`/`InvalidArgumentError` |

Variante con la API de Anthropic directa o un endpoint compatible con
OpenAI: cambia sólo el preset de la pasarela y `AgentModel.provider`/`id`,
sin tocar nada más del `AgentSpec` (ambos en Python; TypeScript es la
misma llamada en `camelCase`).

<!-- noqa: example: API de ai-agent-core, aún no fusionada -->
```python
# Anthropic directa: cabecera x-api-key, sin region=
from rayito.agent.gateways import anthropic_gateway

gateways = {"anthropic": anthropic_gateway(secret)}
agent_spec = AgentSpec(model=AgentModel(provider="anthropic", id="claude-haiku-4-5", gateway="anthropic"))

# Endpoint compatible con OpenAI
from rayito.agent.gateways import openai_compatible_gateway

gateways = {"openai": openai_compatible_gateway(secret, base_path="/v1")}
agent_spec = AgentSpec(model=AgentModel(provider="openai-compatible", id="mi-modelo", gateway="openai"))
```

## Por qué por la pasarela, no por `secrets=`

Igual que cualquier otra integración con una API externa
([Pasarela de secretos](../funciones-opcionales/pasarela-de-secretos.md)),
el patrón es: un secreto en Secrets Manager y una pasarela — para un
modelo, uno de los presets `bedrock_gateway`/`anthropic_gateway`/
`openai_compatible_gateway` de `rayito.agent.gateways` (arriba), en vez de
una `SecretGateway` con `allow=` a mano — con el runtime del agente
configurado con la URL de loopback de la pasarela como `baseURL`/`endpoint`
en vez de la del proveedor real. El código que el modelo pide ejecutar
dentro del sandbox nunca ve la cabecera real: ni en su entorno, ni en el de
`rayd`, ni en un fichero (verificado en el spike con `grep -r` sobre todo
`/` antes y después de cada corrida).

`bedrock_gateway(secret, region=..., models=[...])` construye por ti el
`allow=` restringido a esos modelos exactos: `POST
/model/<id-url-encoded>/converse-stream` y `/converse` por cada uno de
`models`, nunca `/model/*`. `/model/*` dejaría llamar desde dentro del
sandbox a cualquier modelo de la cuenta (incluido uno caro, como Sonnet u
Opus) fuera del presupuesto de tokens del SDK — es el riesgo residual que
describe [Seguridad: T29](../security.md#agente-de-codigo-dentro-del-sandbox).
Documéntalo sólo como salida de emergencia, pendiente de verificar el
escapado de `:` en la ruta (Q142 del spike), si necesitas de verdad abrir
todos los modelos.

La credencial del spike es una clave de API de Bedrock de corta duración
(≤ 12 h, firmada en local con el mismo algoritmo que
`aws-bedrock-token-generator`, nunca una credencial de larga duración de
servicio); vive en Secrets Manager y se rota con `sbx.gateways.refresh()`.
La API de Anthropic directa (cabecera `x-api-key`, `POST /v1/messages`) y un
endpoint compatible con OpenAI (`authorization`, `POST {base_path}/chat/completions`)
usan el mismo patrón, con un `upstream` distinto.

!!! info "Coste y activación"
    - **Por defecto: apagado y $0** — la propiedad `sbx.agent` existe
      siempre (es una propiedad perezosa), pero tocarla no hace ninguna
      RPC ni llamada a AWS; nada corre hasta `run()`/`stream()`/`prepare()`,
      que exigen el template `rayito-agent`
      ([Templates de agente](../funciones-opcionales/templates-de-agente.md))
      y, para hablar con un modelo real, una pasarela.
    - **Activa**: `sbx.agent.run(...)` / `sbx.agent.stream(...)` /
      `sbx.agent.prepare(...)`. Cada ejecución hace 2–3 RPC al sandbox (una
      escritura de ficheros con la configuración del runtime, salvo que el
      sha256 ya coincida con el de la última vez; `Start`; el *stdin* del
      prompt) y, la primera vez sobre un handle, una lectura del manifiesto
      del template.
    - **Lo que cuesta de verdad es el modelo, no el SDK**: ver
      [Coste de un agente](../cost.md#coste-de-un-agente-vm-frente-a-modelo).
    - **Egress**: se recomienda `allow_internet_access=False` con la
      pasarela como única salida; sin ella, un aviso en el logger
      `rayito.agent` (una vez por handle).
    - **Cómo apagarla**: no construyas un `AgentSpec` ni llames a
      `sbx.agent`. El resto del sandbox no cambia.

## Elegir el runtime

| Runtime | Qué es | Cuándo |
|---|---|---|
| `"opencode"` (por defecto) | binario único (MIT), su propio bucle de herramientas, streaming JSONL por `run --format json` | el caso general: no necesitas tocar el grafo del agente |
| `DeepAgents(entrypoint=...)` | ejecuta un grafo de LangChain/LangGraph (`create_deep_agent` por defecto) dentro de un runner de Python aislado | cuando ya tienes subagentes, herramientas o middleware de LangChain/LangGraph que quieres reutilizar tal cual |

<!-- noqa: example: API de ai-agent-core / ai-agent-deepagents, aún no fusionada -->
```python
# por defecto, runtime="opencode"
sbx.agent.run(prompt, spec=spec)

# deepagents con el grafo por defecto
from rayito.agent import DeepAgents
sbx.agent.run(prompt, spec=spec, runtime=DeepAgents())

# deepagents con tu propio punto de entrada: módulo:función que devuelve un grafo compilado
sbx.agent.run(prompt, spec=spec, runtime=DeepAgents(entrypoint="mi_paquete.agente:construir"))
```

## `run`, `stream`, sesiones y abortar

`run()` bloquea hasta que el agente termina o falla; una excepción
(`AgentException` / `AgentError`) lleva `reason`, `session_id`, `usage` y
`exit_code`. `stream()` **nunca** lanza por un fallo del agente: el último
evento del iterador es `Done` o `AgentFailed` (errores de transporte o del
propio sandbox sí propagan, como en cualquier otra llamada).

=== "Python"

    <!-- noqa: example: API de ai-agent-core, aún no fusionada -->
    ```python
    with sbx.agent.stream("Arregla el test que falla.", spec=spec) as s:
        for event in s:
            if event.type == "tool_call":
                print(event.name, event.status)
        result = s.result()
    print(result.session_id, result.usage.total)

    # continuar la misma sesión
    sbx.agent.run("Ahora añade un test de regresión.", spec=spec, session_id=result.session_id)
    ```

=== "Python (async)"

    <!-- noqa: example: API de ai-agent-core, aún no fusionada; fragmento async fuera de función -->
    ```python
    async with sbx.agent.stream(prompt, spec=spec) as s:
        async for event in s:
            ...
        result = await s.result()
    ```

=== "TypeScript"

    <!-- noqa: example: API de ai-agent-core, aún no fusionada -->
    ```ts
    const stream = sbx.agent.stream(prompt, { spec });
    for await (const event of stream) {
      if (event.type === "tool_call") console.log(event.name, event.status);
    }
    const result = await stream.result;

    // AbortSignal: cancela igual que stream.abort()
    const controller = new AbortController();
    sbx.agent.run(prompt, { spec, signal: controller.signal });
    controller.abort();
    ```

`stream.abort()` (o cancelar la `Task`/promesa) manda, cuando el runtime
está servido, un `POST /session/<id>/abort` antes de matar el comando; sin
servir, mata directamente el proceso.

## Límites

| Límite | Por qué |
|---|---|
| `timeout_seconds` (600 s por defecto) | el *fallback* obligatorio: en el spike, un 5xx del modelo hace que OpenCode reintente para siempre |
| `max_steps` (50 por defecto) | el límite de OpenCode es blando (fuerza una respuesta de sólo texto); el SDK aborta de verdad con `reason="max_steps"` cuando empieza el paso 51 |
| `max_total_tokens` (1 000 000 por defecto, `None` lo apaga) | se comprueba tras cada `StepFinished`: puede sobrepasarse hasta en un paso completo antes de cortar |
| `max_output_bytes` (16 MiB) | por debajo del límite general de salida de un comando (64 MiB) |

Superar cualquiera de ellos lanza/emite `AgentFailed`/`AgentException` con
el `reason` correspondiente (`timeout`, `max_steps`, `token_budget`,
`output_limit`); nunca con el texto crudo del proveedor, el prompt ni el
contenido generado — ver [Errores](../referencia/errores.md#agentexception-agenterror).

## Permisos: no son una frontera de seguridad

`AgentPermissions(default="allow"|"deny", tools={...})` controla qué
herramientas puede usar el agente, pero **`--auto` (OpenCode) y
`LocalShellBackend` (deepagents) ejecutan lo que el modelo pida sin
preguntar**: la frontera real es el MicroVM más el
[deny-all de egress](../network.md) de `rayito-base-caps`, igual que para
cualquier otro código que corra dentro del sandbox. `"ask"` no es una
acción válida (`InvalidArgumentException`/`InvalidArgumentError`): una
ejecución sin terminal no puede responder a una pregunta de permiso.
`question`, `webfetch` y `websearch` están denegadas por defecto
(`DEFAULT_DENIED_TOOLS`): la primera colgaría una ejecución sin terminal, y
las otras dos no sirven de nada bajo egress cerrado. Riesgo residual y
mitigaciones: [Seguridad](../security.md#agente-de-codigo-dentro-del-sandbox).

## Fast start: cuatro formas de no pagar el primer `exec`

El spike midió el primer `opencode --version` tras `create()` en una
mediana de 11,3 s (el resto de la VM está lista en segundos): es la primera
lectura del binario de 185 MB desde el disco de código restaurado. Cuatro
opciones, de la más barata a la más rápida:

| Opción | Qué hace | Coste aproximado |
|---|---|---|
| **A. Prefetch en `create()`** (encendida por defecto en `AgentTemplate`) | un demonio del template detecta la restauración (salto de reloj) y precalienta el binario y el venv en segundo plano, sin bloquear | ≈ $0: la VM se factura por segundo de todas formas |
| **B. `pause()`/`resume()` entre turnos** | la caché de páginas de una VM que ya corrió el agente sigue caliente tras reanudar (0,67 s de media) | un ciclo suspend/resume ≈ $0,008 |
| **C. Pool caliente con `warmup`** | `PoolConfig.warmup` precalienta el runtime antes de aparcar cada plaza | ≈ $0,78/plaza/mes (frente a $0,60 de una plaza base) |
| **D. Servidor residente (`opencode serve`) en el pool** | igual que C, además con `opencode serve` ya arrancado: el primer `run --attach` ya tiene el proceso caliente | ≈ $0,92/plaza/mes |

Detalle, números medidos y "Medido en AWS real" en
[Pool: calentamiento y servidor residente](../pool.md#calentamiento-warmup-y-servidor-residente)
y en [Coste y pricing](../cost.md#coste-de-un-agente-vm-frente-a-modelo).
`prepare()` dispara el mismo calentamiento sin pool, en segundo plano:

<!-- noqa: example: API de ai-agent-core, aún no fusionada -->
```python
sbx.agent.prepare()              # opcional: runtime=, serve=True
```

## Punto de entrada de deepagents

Por defecto, `DeepAgents()` llama a `create_deep_agent(model, system_prompt=instructions,
subagents=..., backend=LocalShellBackend(root_dir=workdir), middleware=ctx.middleware)`.
Un `entrypoint="paquete.modulo:funcion"` propio recibe un
`RunnerContext(model, instructions, subagents, backend, middleware)` y debe
devolver un grafo ya compilado; **omitir `ctx.middleware` pierde los
permisos y el caché de prompt** de la pasarela, así que si construyes el
agente a mano, pásalo igual.

## El shim de E2B

`rayito.e2b.Sandbox` no tiene `.agent`: E2B no define esta superficie.
Conecta con el SDK nativo sobre el mismo sandbox:

<!-- noqa: example: API de ai-agent-core, aún no fusionada -->
```python
import rayito

native = rayito.Sandbox.connect(sbx.sandbox_id)
native.agent.run(prompt, spec=spec)
```

## Ver también

- [Templates de agente](../funciones-opcionales/templates-de-agente.md): la
  imagen `rayito-agent`, sus pines y su coste de build.
- [Pool: calentamiento y servidor residente](../pool.md#calentamiento-warmup-y-servidor-residente).
- [Pasarela de secretos](../funciones-opcionales/pasarela-de-secretos.md).
- [Errores: `AgentException`/`AgentError`](../referencia/errores.md#agentexception-agenterror).
- [Seguridad: agente de código dentro del sandbox](../security.md#agente-de-codigo-dentro-del-sandbox).
