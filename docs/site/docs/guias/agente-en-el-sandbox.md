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

## Por qué por la pasarela, no por `secrets=`

Igual que cualquier otra integración con una API externa
([Pasarela de secretos](../funciones-opcionales/pasarela-de-secretos.md)),
el patrón es: un secreto en Secrets Manager, una `SecretGateway` con una
`allowlist` de `(método, ruta)` restringida a los modelos que vas a usar, y
el runtime del agente configurado con la URL de loopback de la pasarela
como `baseURL`/`endpoint` en vez de la del proveedor real. El código que el
modelo pide ejecutar dentro del sandbox nunca ve la cabecera real: ni en su
entorno, ni en el de `rayd`, ni en un fichero (verificado en el spike con
`grep -r` sobre todo `/` antes y después de cada corrida).

=== "Python"

    <!-- noqa: example: API de ai-agent-core, aún no fusionada -->
    ```python
    from rayito import Sandbox, SecretGateway

    with Sandbox.create(
        template="rayito-agent",
        allow_internet_access=False,
        gateways={
            "bedrock": SecretGateway(
                upstream="https://bedrock-runtime.us-east-1.amazonaws.com",
                headers={"authorization": secret_ref},  # "Bearer <clave de corta duración>"
                allow=[("POST", "/model/*")],
                rate_per_minute=60,
            ),
        },
    ) as sbx:
        result = sbx.agent.run(
            "Lee el README y resume en tres frases qué hace este proyecto.",
            spec=agent_spec,
        )
        print(result.text)
    ```

=== "TypeScript"

    <!-- noqa: example: API de ai-agent-core, aún no fusionada -->
    ```ts
    import { Sandbox, SecretGateway } from "rayito";

    const sbx = await Sandbox.create({
      template: "rayito-agent",
      allowInternetAccess: false,
      gateways: {
        bedrock: new SecretGateway({
          upstream: "https://bedrock-runtime.us-east-1.amazonaws.com",
          headers: { authorization: secretRef },
          allow: [["POST", "/model/*"]],
          ratePerMinute: 60,
        }),
      },
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

La credencial del spike es una clave de API de Bedrock de corta duración
(≤ 12 h, firmada en local con el mismo algoritmo que
`aws-bedrock-token-generator`, nunca una credencial de larga duración de
servicio); vive en Secrets Manager y se rota con `sbx.gateways.refresh()`.
La API de Anthropic directa (cabecera `x-api-key`, `POST /v1/messages`) y un
endpoint compatible con OpenAI (`authorization`, `POST {base_path}/chat/completions`)
usan el mismo patrón, con un `upstream` distinto.

!!! info "Coste y activación"
    - **Por defecto**: no existe `sbx.agent` sin un `AgentSpec`: tocar la
      propiedad `sbx.agent` no hace ninguna llamada; hace falta el template
      `rayito-agent` ([Templates de agente](../funciones-opcionales/templates-de-agente.md))
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
