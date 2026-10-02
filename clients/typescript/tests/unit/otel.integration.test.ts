/**
 * Spans OpenTelemetry opt-in (M13b) sobre `Sandbox` contra el `rayd`/plano
 * falsos: sin `tracerProvider` el resto de la suite no cambia (ya lo prueba
 * el resto de `tests/unit`); con un `BasicTracerProvider` y un
 * `InMemorySpanExporter`, cada operación instrumentada emite exactamente un
 * span con el nombre esperado, anidado bajo un span padre del llamante, con
 * atributos ⊆ `ALLOWED_SPAN_ATTRIBUTES` y sin ningún centinela de datos
 * sensibles.
 */

import { AsyncLocalStorage } from "node:async_hooks";
import {
  type Context,
  type ContextManager,
  context as contextApi,
  propagation,
  ROOT_CONTEXT,
  SpanKind,
  SpanStatusCode,
  type TextMapPropagator,
  trace,
} from "@opentelemetry/api";
import {
  BasicTracerProvider,
  InMemorySpanExporter,
  SimpleSpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import { afterEach, beforeEach, describe, expect, test } from "vitest";
import { CommandExitError } from "../../src/errors.js";
import { ALLOWED_SPAN_ATTRIBUTES } from "../../src/otel.js";
import { Sandbox } from "../../src/sandbox/sandbox.js";
import { SANDBOX_ID } from "./fake/control-plane.js";
import { createTestSandbox } from "./helpers.js";

const SENTINEL = "s3cr3t-token-/home/user/.aws/credentials-SELECT*FROM-users";

/**
 * El mínimo `ContextManager` que necesita este fichero para comprobar el
 * anidado de spans a través de un `await`: en una app real lo registra el
 * SDK de Node del usuario (`@opentelemetry/sdk-node` o
 * `NodeTracerProvider.register()`), nunca rayito. Sin él, `@opentelemetry/api`
 * usa un `NoopContextManager` que no propaga nada entre tareas asíncronas.
 */
class AsyncLocalStorageContextManager implements ContextManager {
  readonly #storage = new AsyncLocalStorage<Context>();

  active(): Context {
    return this.#storage.getStore() ?? ROOT_CONTEXT;
  }

  with<A extends unknown[], F extends (...args: A) => ReturnType<F>>(
    context: Context,
    fn: F,
    thisArg?: ThisParameterType<F>,
    ...args: A
  ): ReturnType<F> {
    return this.#storage.run(context, () => fn.apply(thisArg, args));
  }

  bind<T>(_context: Context, target: T): T {
    return target;
  }

  enable(): this {
    return this;
  }

  disable(): this {
    return this;
  }
}

let exporter: InMemorySpanExporter;
let provider: BasicTracerProvider;

beforeEach(() => {
  exporter = new InMemorySpanExporter();
  provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  contextApi.setGlobalContextManager(new AsyncLocalStorageContextManager().enable());
});

afterEach(() => {
  contextApi.disable();
});

function spanNames(): string[] {
  return exporter.getFinishedSpans().map((span) => span.name);
}

describe("rayito.sandbox.create", () => {
  test("emite un único span con la región y el id del sandbox", async () => {
    await createTestSandbox({ create: { tracerProvider: provider } });
    const [span] = exporter.getFinishedSpans();
    expect(span?.name).toBe("rayito.sandbox.create");
    expect(span?.kind).toBe(SpanKind.CLIENT);
    expect(span?.attributes["rayito.sandbox.id"]).toBe(SANDBOX_ID);
    expect(span?.attributes["rayito.operation"]).toBe("create");
    expect(Object.keys(span?.attributes ?? {})).toEqual(expect.arrayContaining([]));
    for (const key of Object.keys(span?.attributes ?? {})) {
      expect(ALLOWED_SPAN_ATTRIBUTES).toContain(key);
    }
  });
});

describe("rayito.sandbox.kill", () => {
  test("abre su propio span tras el de create", async () => {
    const { sandbox } = await createTestSandbox({ create: { tracerProvider: provider } });
    await sandbox.kill();
    expect(spanNames()).toEqual(["rayito.sandbox.create", "rayito.sandbox.kill"]);
  });

  test("Sandbox.kill estático también instrumenta con tracerProvider", async () => {
    const { plane } = await createTestSandbox({ create: { tracerProvider: provider } });
    exporter.reset();
    await Sandbox.kill(SANDBOX_ID, { controlPlane: plane, tracerProvider: provider });
    const [span] = exporter.getFinishedSpans();
    expect(span?.name).toBe("rayito.sandbox.kill");
    expect(span?.attributes["rayito.sandbox.id"]).toBe(SANDBOX_ID);
  });
});

describe("rayito.commands.run", () => {
  test("el span en segundo plano se cierra en cuanto run devuelve el handle", async () => {
    const { sandbox } = await createTestSandbox({ create: { tracerProvider: provider } });
    const handle = await sandbox.commands.run("sleep 5", { background: true });
    const span = exporter.getFinishedSpans().find((s) => s.name === "rayito.commands.run");
    expect(span).toBeDefined();
    expect(span?.attributes["rayito.commands.background"]).toBe(true);
    expect(span?.attributes["rayito.commands.exit_code"]).toBeUndefined();
    await handle.kill();
  });

  test("el span en foreground lleva el exit code al terminar", async () => {
    const { sandbox } = await createTestSandbox({ create: { tracerProvider: provider } });
    await sandbox.commands.run("echo hola");
    const span = exporter.getFinishedSpans().find((s) => s.name === "rayito.commands.run");
    expect(span?.attributes["rayito.commands.background"]).toBe(false);
    expect(span?.attributes["rayito.commands.exit_code"]).toBe(0);
  });

  test("un comando foreground que falla también lleva su exit code en el span", async () => {
    const { sandbox } = await createTestSandbox({ create: { tracerProvider: provider } });
    await expect(sandbox.commands.run("exit 7")).rejects.toBeInstanceOf(CommandExitError);
    const span = exporter.getFinishedSpans().find((s) => s.name === "rayito.commands.run");
    expect(span?.status.code).toBe(SpanStatusCode.ERROR);
    expect(span?.attributes["rayito.commands.exit_code"]).toBe(7);
  });

  test("un comando que falla pone el estado en ERROR con el nombre de la clase, nunca el texto del comando", async () => {
    const { sandbox } = await createTestSandbox({ create: { tracerProvider: provider } });
    await expect(sandbox.commands.run(`exit 3`, { tag: SENTINEL })).rejects.toBeTruthy();
    const span = exporter.getFinishedSpans().find((s) => s.name === "rayito.commands.run");
    expect(span?.status.code).toBe(SpanStatusCode.ERROR);
    expect(span?.status.message).not.toContain(SENTINEL);
    for (const value of Object.values(span?.attributes ?? {})) {
      expect(String(value)).not.toContain(SENTINEL);
    }
    for (const event of span?.events ?? []) {
      for (const value of Object.values(event.attributes ?? {})) {
        expect(String(value)).not.toContain(SENTINEL);
      }
    }
  });
});

describe("rayito.code.run", () => {
  test("lleva el atributo de lenguaje", async () => {
    const { sandbox } = await createTestSandbox({ create: { tracerProvider: provider } });
    await sandbox.runCode("1 + 1");
    const span = exporter.getFinishedSpans().find((s) => s.name === "rayito.code.run");
    expect(span).toBeDefined();
    for (const key of Object.keys(span?.attributes ?? {})) {
      expect(ALLOWED_SPAN_ATTRIBUTES).toContain(key);
    }
  });
});

describe("rayito.files.*", () => {
  test("write/read abren sus propios spans sin filtrar la ruta", async () => {
    const { sandbox } = await createTestSandbox({ create: { tracerProvider: provider } });
    const secretPath = `/home/user/${SENTINEL}.txt`;
    await sandbox.files.write(secretPath, "hola mundo");
    await sandbox.files.read(secretPath);
    const names = spanNames();
    expect(names).toContain("rayito.files.write");
    expect(names).toContain("rayito.files.write_files");
    expect(names).toContain("rayito.files.read");
    for (const span of exporter.getFinishedSpans()) {
      if (!span.name.startsWith("rayito.files.")) {
        continue;
      }
      for (const [key, value] of Object.entries(span.attributes)) {
        expect(ALLOWED_SPAN_ATTRIBUTES).toContain(key);
        expect(String(value)).not.toContain(SENTINEL);
      }
    }
    const writeSpan = exporter.getFinishedSpans().find((s) => s.name === "rayito.files.write");
    expect(writeSpan?.attributes["rayito.files.bytes"]).toBe(
      new TextEncoder().encode("hola mundo").length,
    );
  });
});

describe("un span padre del llamante", () => {
  test("es el padre de cada span de rayito, incluso a través de await", async () => {
    const tracer = provider.getTracer("test");
    await tracer.startActiveSpan("user.workflow", async (parent) => {
      const { sandbox } = await createTestSandbox({ create: { tracerProvider: provider } });
      await sandbox.commands.run("echo hola");
      await sandbox.kill();
      parent.end();
      const parentSpanId = parent.spanContext().spanId;
      const rayitoSpans = exporter.getFinishedSpans().filter((s) => s.name.startsWith("rayito."));
      expect(rayitoSpans).toHaveLength(3);
      for (const span of rayitoSpans) {
        expect(span.parentSpanId).toBe(parentSpanId);
      }
    });
  });
});

/**
 * The minimal W3C trace-context propagator this file needs (the real one
 * lives in `@opentelemetry/core`, which the user's SDK registers; rayito
 * never does): `traceparent` from the active span, nothing else.
 */
const W3C_TRACE_CONTEXT: TextMapPropagator = {
  inject(activeContext, carrier, setter) {
    const spanContext = trace.getSpanContext(activeContext);
    if (spanContext === undefined) {
      return;
    }
    const flags = spanContext.traceFlags.toString(16).padStart(2, "0");
    setter.set(carrier, "traceparent", `00-${spanContext.traceId}-${spanContext.spanId}-${flags}`);
  },
  extract: (activeContext) => activeContext,
  fields: () => ["traceparent"],
};

describe("traceparent toward rayd (m15-rayd-otlp)", () => {
  beforeEach(() => {
    propagation.setGlobalPropagator(W3C_TRACE_CONTEXT);
  });

  afterEach(() => {
    propagation.disable();
  });

  test("with tracerProvider every handle RPC carries the active span's traceparent", async () => {
    const { sandbox, rayd, close } = await createTestSandbox({
      create: { tracerProvider: provider },
    });
    try {
      await sandbox.commands.run("echo hola");
      const [headers] = rayd.process.startHeaders;
      expect(headers?.traceparent).toMatch(/^00-[0-9a-f]{32}-[0-9a-f]{16}-01$/);
      expect(headers?.baggage).toBeUndefined();
    } finally {
      await close();
    }
  });

  test("each Start carries the span of the very commands.run that sent it", async () => {
    // Acceptance on AWS (2026-10-02) found the Python SDK sending one fixed
    // traceparent on every RPC; this pins the same contract here.
    const { sandbox, rayd, close } = await createTestSandbox({
      create: { tracerProvider: provider },
    });
    try {
      await sandbox.commands.run("echo uno");
      await sandbox.commands.run("echo dos");
      const runSpans = exporter
        .getFinishedSpans()
        .filter((span) => span.name === "rayito.commands.run")
        .map((span) => `${span.spanContext().traceId}-${span.spanContext().spanId}`);
      const sent = rayd.process.startHeaders.map((headers) =>
        (headers.traceparent ?? "").split("-").slice(1, 3).join("-"),
      );
      expect(runSpans).toHaveLength(2);
      expect(sent).toEqual(runSpans);
    } finally {
      await close();
    }
  });

  test("without tracerProvider no RPC carries one, even with a global propagator", async () => {
    const { sandbox, rayd, close } = await createTestSandbox();
    try {
      await sandbox.commands.run("echo hola");
      for (const headers of rayd.process.startHeaders) {
        expect(headers.traceparent).toBeUndefined();
        expect(headers.tracestate).toBeUndefined();
      }
      expect(rayd.process.startHeaders.length).toBeGreaterThan(0);
    } finally {
      await close();
    }
  });
});
