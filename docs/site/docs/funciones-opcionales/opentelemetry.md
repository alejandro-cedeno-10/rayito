# OpenTelemetry

Spans OpenTelemetry del lado del SDK sobre las operaciones que ya haces
(`create`, `commands.run`, `run_code`, `files.*`, `kill`…), exportados al
backend que tú configures. <small>Desde 0.5.0</small>

!!! info "Coste y activación"
    - **Por defecto**: apagado. Sin `tracer_provider=` (TypeScript:
      `tracerProvider`) el SDK no importa `opentelemetry` /
      `@opentelemetry/api` y no crea ningún span.
    - **Activa**: spans `rayito.*` (`SpanKind.CLIENT`) en
      `create/connect/kill/pause/resume` (instancia y clase),
      `commands.run`, `run_code` / `runCode` y `files.*`, sobre el
      `TracerProvider` que pases.
    - **Recursos y llamadas AWS**: ninguno. Rayito no crea ni llama ningún
      servicio de AWS por esto.
    - **Coste aproximado**: $0 desde Rayito. El coste, si lo hay, es el de tu
      exportador (CloudWatch, un collector propio…), que configuras tú.
    - **IAM**: ninguno propio; el que exija tu exportador.
    - **Cómo apagarla**: no pases `tracer_provider=` / `tracerProvider` (o
      pasa `None` / `undefined`).

## Cuándo usarlo

- Ya tienes OpenTelemetry en tu servicio y quieres ver cuánto tardan las
  llamadas a los sandboxes dentro de tus trazas.
- Quieres medir el arranque, las celdas y las transferencias por
  operación.
- **Cuándo no**: para métricas del interior del sandbox (CPU, memoria), usa
  [`get_metrics_history()`](../observability.md#instantanea-e-historial).

## Instalación

=== "Python"

    ```bash
    pip install "rayito[otel]" opentelemetry-sdk
    ```

    El extra `otel` sólo trae `opentelemetry-api`. Los ejemplos usan el SDK
    de OpenTelemetry (`TracerProvider`, `ConsoleSpanExporter`), que va
    aparte; si ya tienes un proveedor configurado en tu servicio, el extra
    basta.

=== "TypeScript"

    ```bash
    npm i @opentelemetry/api @opentelemetry/sdk-trace-base
    ```

## Ejemplo rápido

=== "Python"

    ```python
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

    from rayito import Sandbox

    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))

    with Sandbox.create(tracer_provider=provider) as sbx:  # span "rayito.sandbox.create"
        sbx.commands.run("echo hola")  # span "rayito.commands.run"
        sbx.run_code("1 + 1")  # span "rayito.code.run"
    # al salir: span "rayito.sandbox.kill"
    ```

=== "Python (async)"

    ```python
    import asyncio

    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

    from rayito import AsyncSandbox


    async def main() -> None:
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        async with await AsyncSandbox.create(tracer_provider=provider) as sbx:
            await sbx.commands.run("echo hola")
            await sbx.run_code("1 + 1")


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { BasicTracerProvider, ConsoleSpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
    import { Sandbox } from "rayito";

    const provider = new BasicTracerProvider({
      spanProcessors: [new SimpleSpanProcessor(new ConsoleSpanExporter())],
    });

    await using sbx = await Sandbox.create({ tracerProvider: provider });
    await sbx.commands.run("echo hola"); // span "rayito.commands.run"
    await sbx.runCode("1 + 1"); // span "rayito.code.run"
    ```

Para enviar los spans a tu backend, cambia `ConsoleSpanExporter` por el
exportador OTLP de tu SDK de OpenTelemetry. Si ya tienes un proveedor
global, pásalo: `tracer_provider=trace.get_tracer_provider()`.

## Nombres de span

| Span | Dónde |
|---|---|
| `rayito.sandbox.create` | `create()` (envuelve `run-microvm`, la espera del agente, el índice y la red) |
| `rayito.sandbox.connect` | `connect()`, las dos formas |
| `rayito.sandbox.kill`, `pause`, `resume` | instancia y clase / estático |
| `rayito.code.run` | `run_code()` / `runCode()` |
| `rayito.commands.run` | `commands.run()` (en segundo plano, el span se cierra cuando arranca el proceso, no cuando termina) |
| `rayito.files.read`, `write`, `write_files`, `list`, `exists`, `get_info`, `remove`, `rename`, `make_dir` | `files.*` |
| `rayito.agent.run` | `sbx.agent.run()` / `.stream()` ([Agente en el sandbox](../guias/agente-en-el-sandbox.md)) |

## Atributos

Lista cerrada de claves (cualquier otra es un error antes de abrir el span):

| Span | Atributos |
|---|---|
| `rayito.sandbox.create` | `rayito.region`, `rayito.operation` y `rayito.sandbox.id` en cuanto `run-microvm` responde |
| `rayito.sandbox.connect`, `kill`, `pause`, `resume` | `rayito.sandbox.id`, `rayito.operation` |
| `rayito.commands.run` | `rayito.commands.background`; en primer plano, `rayito.commands.exit_code` |
| `rayito.code.run` | `rayito.code.language` cuando se conoce |
| `rayito.files.*` | `rayito.files.operation`; `read`/`write` añaden `rayito.files.bytes`; `write_files` y `list` añaden `rayito.files.count` |
| `rayito.agent.run` | al empezar, `gen_ai.operation.name` (`invoke_agent`), `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.agent.name` y `rayito.agent.runtime`; al terminar, `gen_ai.conversation.id`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `rayito.agent.steps`, `rayito.agent.exit_code`, `rayito.agent.attached`, `rayito.agent.cache_read_tokens`, `rayito.agent.cache_write_tokens` y, si falla, `rayito.agent.failure_reason` |

`rayito.template.name`, `rayito.resume_generation` y `rayito.error.type`
están reservadas: hoy ningún span las emite.

!!! note "Lo que nunca se registra"
    El texto de un comando, código fuente, rutas de ficheros, valores de
    `envs`, nombres o valores de secretos, valores de `metadata`, el access
    token, el token del proxy ni URLs prefirmadas. En `rayito.agent.run`,
    tampoco el prompt, el texto de la respuesta ni los argumentos de una
    herramienta: sólo modelo, proveedor y contadores. Un error dentro de un span
    registra sólo el **nombre de la clase** de la excepción, nunca su mensaje
    ni su traza.

## Qué no incluye

- Con `tracer_provider=`, cada llamada del handle lleva además
  `traceparent` (y `tracestate`, nunca `baggage`) hacia `rayd`, que lo
  escribe como `trace_id`/`span_id` en sus logs. `rayd` no crea spans: un
  span de Rayito nunca es padre de nada dentro del MicroVM. Las llamadas de
  clase (`Sandbox.kill(id)` y compañía) no lo llevan. Sin la opción, no se
  añade ninguna cabecera.
- Las métricas del interior del sandbox (CPU, memoria, disco) no viajan
  por aquí: son la [exportación OTLP](exportacion-otlp.md) (`telemetry=`),
  otra opción con su propio coste.
- El shim de E2B (`rayito.e2b` / `rayito/e2b`) no está instrumentado.

## Errores y solución de problemas

| Síntoma | Causa | Qué hacer |
|---|---|---|
| `ModuleNotFoundError: opentelemetry` al pasar `tracer_provider=` | falta el extra | `pip install "rayito[otel]"` |
| `ModuleNotFoundError: No module named 'opentelemetry.sdk'` | el extra `otel` sólo instala la API, no el SDK | `pip install opentelemetry-sdk` |
| `Cannot find module '@opentelemetry/sdk-trace-base'` (TypeScript) | falta el SDK de trazas | `npm i @opentelemetry/sdk-trace-base` |
| no aparece ningún span | el provider no tiene un procesador o no se vacía al salir | añade un `SpanProcessor` y llama a `provider.shutdown()` al terminar |
| spans del shim de E2B | el shim no está instrumentado | usa el SDK nativo (`sbx.native` en el shim) |

## Diferencias con E2B

Estos spans son del lado del **cliente**, sobre tus llamadas; E2B no tiene
un equivalente en su SDK. Su exportación de telemetría del sandbox
(Enterprise) se parece más a la [exportación OTLP](exportacion-otlp.md) de
Rayito.

## Ver también

- [Funciones opcionales](../optional-features.md#otel-sdk)
- [Métricas y listado](../observability.md)
- [Referencia Python: `Sandbox.create`](../referencia/python/sandbox.md)

??? info "Fuentes y mediciones"
    - Implementación: [`clients/python/src/rayito/_otel.py`](https://github.com/alejandro-cedeno-10/rayito/blob/main/clients/python/src/rayito/_otel.py)
      y [`clients/typescript/src/otel.ts`](https://github.com/alejandro-cedeno-10/rayito/blob/main/clients/typescript/src/otel.ts).
    - Probado con `InMemorySpanExporter` en los dos SDK: al no llamar a AWS,
      no necesita una aceptación contra AWS real.
