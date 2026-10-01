/**
 * `rayito/src/otel.ts`: sin `tracerProvider`, {@link NOOP} ejecuta el
 * callback directamente (sin span, sin coste); con un `TracerProvider` del
 * SDK y un `InMemorySpanExporter`, cada `span()` abre un span
 * `SpanKind.CLIENT` cuyos atributos están contenidos en
 * `ALLOWED_SPAN_ATTRIBUTES`, un callback asíncrono mantiene el span abierto
 * hasta que la promesa se resuelve, una excepción (sync o async) pone el
 * estado en `ERROR` con el nombre de su clase (nunca su mensaje ni su pila)
 * y un atributo fuera de la lista se rechaza antes de abrir el span.
 */

import { SpanKind, SpanStatusCode } from "@opentelemetry/api";
import {
  BasicTracerProvider,
  InMemorySpanExporter,
  SimpleSpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import { beforeEach, describe, expect, test } from "vitest";
import {
  ALLOWED_SPAN_ATTRIBUTES,
  instrumentationFor,
  NOOP,
  type RayitoSpanAttributes,
} from "../../src/otel.js";

const SENTINEL = "s3cr3t-token-abc123-/home/user/secret.txt-SELECT * FROM users";

describe("NOOP", () => {
  test("ejecuta el callback directamente y devuelve su resultado", () => {
    const result = NOOP.span("rayito.sandbox.create", undefined, (span) => {
      expect(span).toBeUndefined();
      return 42;
    });
    expect(result).toBe(42);
  });

  test("propaga el resultado de un callback asíncrono", async () => {
    const result = await NOOP.span(
      "rayito.commands.run",
      { "rayito.commands.exit_code": 0 },
      async (span) => {
        expect(span).toBeUndefined();
        return "ok";
      },
    );
    expect(result).toBe("ok");
  });

  test("propaga una excepción síncrona sin envolverla", () => {
    expect(() =>
      NOOP.span("rayito.sandbox.kill", undefined, () => {
        throw new Error(SENTINEL);
      }),
    ).toThrow(SENTINEL);
  });

  test("propaga el rechazo de una promesa sin envolverlo", async () => {
    await expect(
      NOOP.span("rayito.sandbox.kill", undefined, async () => {
        throw new Error(SENTINEL);
      }),
    ).rejects.toThrow(SENTINEL);
  });
});

describe("instrumentationFor", () => {
  test("sin tracerProvider devuelve el NOOP compartido", () => {
    expect(instrumentationFor(undefined)).toBe(NOOP);
  });

  let exporter: InMemorySpanExporter;
  let provider: BasicTracerProvider;

  beforeEach(() => {
    exporter = new InMemorySpanExporter();
    provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  });

  test("abre un span SpanKind.CLIENT en el tracer 'rayito' con los atributos dados", () => {
    const instrumentation = instrumentationFor(provider);
    instrumentation.span(
      "rayito.sandbox.create",
      { "rayito.sandbox.id": "sbx-1" },
      () => undefined,
    );
    const [span] = exporter.getFinishedSpans();
    expect(span?.name).toBe("rayito.sandbox.create");
    expect(span?.kind).toBe(SpanKind.CLIENT);
    expect(span?.attributes["rayito.sandbox.id"]).toBe("sbx-1");
    expect(span?.instrumentationLibrary.name).toBe("rayito");
  });

  test("los atributos del span son un subconjunto de la lista permitida", () => {
    const instrumentation = instrumentationFor(provider);
    instrumentation.span(
      "rayito.commands.run",
      { "rayito.commands.exit_code": 0, "rayito.commands.background": false },
      () => undefined,
    );
    const [span] = exporter.getFinishedSpans();
    for (const key of Object.keys(span?.attributes ?? {})) {
      expect(ALLOWED_SPAN_ATTRIBUTES).toContain(key);
    }
  });

  test("un atributo no permitido se rechaza antes de abrir el span", () => {
    const instrumentation = instrumentationFor(provider);
    // Fuerza una clave fuera de la lista permitida: el tipo ya la impide en
    // tiempo de compilación, así que la prueba comprueba el guardia en
    // tiempo de ejecución (necesario para un llamante sin tipos estrictos).
    const unknownAttributes = { "rayito.commands.cmd": "ls" } as unknown as RayitoSpanAttributes;
    expect(() =>
      instrumentation.span("rayito.commands.run", unknownAttributes, () => undefined),
    ).toThrow("rayito.commands.cmd");
    expect(exporter.getFinishedSpans()).toEqual([]);
  });

  test("espera a que una promesa se resuelva antes de cerrar el span", async () => {
    const instrumentation = instrumentationFor(provider);
    let resolveInner: (() => void) | undefined;
    const pending = new Promise<void>((resolve) => {
      resolveInner = resolve;
    });
    const result = instrumentation.span("rayito.code.run", undefined, async () => {
      await pending;
      return "done";
    });
    expect(exporter.getFinishedSpans()).toEqual([]);
    resolveInner?.();
    expect(await result).toBe("done");
    expect(exporter.getFinishedSpans()).toHaveLength(1);
  });

  test("una excepción síncrona pone el estado en ERROR con sólo el nombre de la clase", () => {
    const instrumentation = instrumentationFor(provider);
    expect(() =>
      instrumentation.span("rayito.commands.run", undefined, () => {
        throw new TypeError(SENTINEL);
      }),
    ).toThrow(SENTINEL);
    const [span] = exporter.getFinishedSpans();
    expect(span?.status.code).toBe(SpanStatusCode.ERROR);
    expect(span?.status.message).toBe("TypeError");
  });

  test("un rechazo asíncrono pone el estado en ERROR con sólo el nombre de la clase", async () => {
    const instrumentation = instrumentationFor(provider);
    await expect(
      instrumentation.span("rayito.code.run", undefined, async () => {
        throw new RangeError(SENTINEL);
      }),
    ).rejects.toThrow(SENTINEL);
    const [span] = exporter.getFinishedSpans();
    expect(span?.status.code).toBe(SpanStatusCode.ERROR);
    expect(span?.status.message).toBe("RangeError");
  });

  test("el centinela nunca aparece en ningún atributo ni evento del span", async () => {
    const instrumentation = instrumentationFor(provider);
    await expect(
      instrumentation.span("rayito.code.run", { "rayito.sandbox.id": "sbx-1" }, async () => {
        throw new Error(SENTINEL);
      }),
    ).rejects.toThrow();
    const [span] = exporter.getFinishedSpans();
    const serialized = JSON.stringify({
      attributes: span?.attributes,
      status: span?.status,
      events: span?.events.map((event) => ({ name: event.name, attributes: event.attributes })),
    });
    expect(serialized).not.toContain(SENTINEL);
  });
});
