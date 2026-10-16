# OpenTelemetry

SDK-side OpenTelemetry spans over the operations you already perform
(`create`, `commands.run`, `run_code`, `files.*`, `kill`…), exported to the
backend you configure. <small>Since 0.5.0</small>

!!! info "Cost and activation"
    - **Default**: off. Without `tracer_provider=` (TypeScript:
      `tracerProvider`) the SDK does not import `opentelemetry` /
      `@opentelemetry/api` and creates no span.
    - **Turns on**: `rayito.*` spans (`SpanKind.CLIENT`) on
      `create/connect/kill/pause/resume` (instance and class),
      `commands.run`, `run_code` / `runCode` and `files.*`, on the
      `TracerProvider` you pass.
    - **Resources and AWS calls**: none. Rayito creates or calls no AWS
      service for this.
    - **Approximate cost**: $0 from Rayito. The cost, if any, is that of your
      exporter (CloudWatch, your own collector…), which you configure.
    - **IAM**: none of its own; whatever your exporter requires.
    - **How to turn it off**: do not pass `tracer_provider=` /
      `tracerProvider` (or pass `None` / `undefined`).

## When to use it { #cuando-usarlo }

- You already have OpenTelemetry in your service and want to see how long
  the sandbox calls take inside your traces.
- You want to measure startup, cells and transfers per operation.
- **When not to**: for metrics from inside the sandbox (CPU, memory), use
  [`get_metrics_history()`](../observability.md#instantanea-e-historial).

## Installation { #instalacion }

=== "Python"

    ```bash
    pip install "rayito[otel]" opentelemetry-sdk
    ```

    The `otel` extra only brings `opentelemetry-api`. The examples use the
    OpenTelemetry SDK (`TracerProvider`, `ConsoleSpanExporter`), which comes
    separately; if you already have a provider configured in your service,
    the extra is enough.

=== "TypeScript"

    ```bash
    npm i @opentelemetry/api @opentelemetry/sdk-trace-base
    ```

## Quick example { #ejemplo-rapido }

=== "Python"

    ```python
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

    from rayito import Sandbox

    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))

    with Sandbox.create(tracer_provider=provider) as sbx:  # span "rayito.sandbox.create"
        sbx.commands.run("echo hello")  # span "rayito.commands.run"
        sbx.run_code("1 + 1")  # span "rayito.code.run"
    # on exit: span "rayito.sandbox.kill"
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
            await sbx.commands.run("echo hello")
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
    await sbx.commands.run("echo hello"); // span "rayito.commands.run"
    await sbx.runCode("1 + 1"); // span "rayito.code.run"
    ```

To send the spans to your backend, replace `ConsoleSpanExporter` with the
OTLP exporter of your OpenTelemetry SDK. If you already have a global
provider, pass it: `tracer_provider=trace.get_tracer_provider()`.

## Span names { #nombres-de-span }

| Span | Where |
|---|---|
| `rayito.sandbox.create` | `create()` (wraps `run-microvm`, the wait for the agent, the index and the network) |
| `rayito.sandbox.connect` | `connect()`, both forms |
| `rayito.sandbox.kill`, `pause`, `resume` | instance and class / static |
| `rayito.code.run` | `run_code()` / `runCode()` |
| `rayito.commands.run` | `commands.run()` (in the background, the span closes when the process starts, not when it ends) |
| `rayito.files.read`, `write`, `write_files`, `list`, `exists`, `get_info`, `remove`, `rename`, `make_dir` | `files.*` |
| `rayito.agent.run` | `sbx.agent.run()` / `.stream()` ([Agent in the sandbox](../guias/agente-en-el-sandbox.md)) |

## Attributes { #atributos }

Closed list of keys (any other one is an error before the span is opened):

| Span | Attributes |
|---|---|
| `rayito.sandbox.create` | `rayito.region`, `rayito.operation` and `rayito.sandbox.id` as soon as `run-microvm` answers |
| `rayito.sandbox.connect`, `kill`, `pause`, `resume` | `rayito.sandbox.id`, `rayito.operation` |
| `rayito.commands.run` | `rayito.commands.background`; in the foreground, `rayito.commands.exit_code` |
| `rayito.code.run` | `rayito.code.language` when known |
| `rayito.files.*` | `rayito.files.operation`; `read`/`write` add `rayito.files.bytes`; `write_files` and `list` add `rayito.files.count` |
| `rayito.agent.run` | at the start, `gen_ai.operation.name` (`invoke_agent`), `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.agent.name` and `rayito.agent.runtime`; at the end, `gen_ai.conversation.id`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `rayito.agent.steps`, `rayito.agent.exit_code`, `rayito.agent.cache_read_tokens`, `rayito.agent.cache_write_tokens` and, on failure, `rayito.agent.failure_reason` |

`rayito.template.name`, `rayito.resume_generation` and `rayito.error.type`
are reserved: no span emits them today.

!!! note "What is never recorded"
    The text of a command, source code, file paths, `envs` values, secret
    names or values, `metadata` values, the access token, the proxy token or
    presigned URLs. In `rayito.agent.run`, neither the prompt, the response
    text nor a tool's arguments: only model, provider and counters. An error
    inside a span records only the exception's **class name**, never its
    message or its traceback.

## What it does not include { #que-no-incluye }

- With `tracer_provider=`, each handle call also carries `traceparent` (and
  `tracestate`, never `baggage`) to `rayd`, which writes it as
  `trace_id`/`span_id` in its logs. `rayd` creates no spans: a Rayito span
  is never the parent of anything inside the MicroVM. Class calls
  (`Sandbox.kill(id)` and the like) do not carry it. Without the option, no
  header is added.
- Metrics from inside the sandbox (CPU, memory, disk) do not travel this
  way: they are the [OTLP export](exportacion-otlp.md) (`telemetry=`),
  another option with its own cost.
- The E2B shim (`rayito.e2b` / `rayito/e2b`) is not instrumented.

## Errors and troubleshooting { #errores-y-solucion-de-problemas }

| Symptom | Cause | What to do |
|---|---|---|
| `ModuleNotFoundError: opentelemetry` when passing `tracer_provider=` | the extra is missing | `pip install "rayito[otel]"` |
| `ModuleNotFoundError: No module named 'opentelemetry.sdk'` | the `otel` extra only installs the API, not the SDK | `pip install opentelemetry-sdk` |
| `Cannot find module '@opentelemetry/sdk-trace-base'` (TypeScript) | the tracing SDK is missing | `npm i @opentelemetry/sdk-trace-base` |
| no span shows up | the provider has no processor or is not flushed on exit | add a `SpanProcessor` and call `provider.shutdown()` when done |
| E2B shim spans | the shim is not instrumented | use the native SDK (`sbx.native` in the shim) |

## Differences from E2B { #diferencias-con-e2b }

These spans are **client**-side, over your calls; E2B has no equivalent in
its SDK. Its sandbox telemetry export (Enterprise) is closer to Rayito's
[OTLP export](exportacion-otlp.md).

## See also { #ver-tambien }

- [Optional features](../optional-features.md#otel-sdk)
- [Metrics and listing](../observability.md)
- [Python reference: `Sandbox.create`](../referencia/python/sandbox.md)

??? info "Sources and measurements"
    - Implementation: [`clients/python/src/rayito/_otel.py`](https://github.com/alejandro-cedeno-10/rayito/blob/main/clients/python/src/rayito/_otel.py)
      and [`clients/typescript/src/otel.ts`](https://github.com/alejandro-cedeno-10/rayito/blob/main/clients/typescript/src/otel.ts).
    - Tested with `InMemorySpanExporter` in both SDKs: since it does not call
      AWS, it needs no acceptance run against real AWS.
