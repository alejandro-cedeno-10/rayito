/**
 * Spans OpenTelemetry del lado del SDK, opt-in (M13b, ADR-014).
 *
 * Una función opcional más (apagada por defecto): `tracerProvider` en
 * `Sandbox.create()`, `connect()` (ambas formas) y en las variantes
 * estáticas `kill`/`pause`/`resume` guarda en el handle una
 * {@link Instrumentation} que envuelve cada operación de ciclo de vida,
 * comandos, código y ficheros en un span `rayito.*` de `SpanKind.CLIENT`.
 * Sin `tracerProvider` (el valor por defecto, `undefined`),
 * {@link instrumentationFor} devuelve {@link NOOP}: `span()` ejecuta el
 * callback directamente: ningún span ni import de `@opentelemetry/api`;
 * sólo queda el objeto de atributos trivial (y el cierre `async`) que cada
 * método instrumentado construye por llamada.
 *
 * Sólo se importa `@opentelemetry/api` con `import type`: el compilador lo
 * usa para comprobar tipos y `tsdown` lo borra del build, así que el bundle
 * publicado no tiene ningún `require`/`import` en tiempo de ejecución de
 * `@opentelemetry/api` (lo comprueba `pnpm pack:check`, con un grep de
 * `dist/`). Las constantes numéricas de `SpanKind.CLIENT` (2) y
 * `SpanStatusCode.ERROR` (2) están fijadas a mano más abajo para no
 * necesitar el módulo en tiempo de ejecución ni para eso.
 *
 * Lista cerrada de atributos ({@link RayitoSpanAttributes}): además de que
 * el tipo sólo admite esas claves, `validateAttributes` las comprueba en
 * tiempo de ejecución (el mismo contrato que la fachada de Python), para que
 * nunca se cuele el texto de un comando, código, una ruta, un valor de
 * `envs`, un nombre o valor de secreto, un valor de `metadata`, un access
 * token, un JWE o una URL prefirmada. Un error dentro de un span llama a
 * `recordException` y pone el estado en `ERROR`, pero el único dato que
 * entra es el nombre de la clase del error: nunca su `message` ni su
 * `stack`, porque el mensaje de un error del SDK puede llevar el propio dato
 * sensible que el span nunca debe ver.
 *
 * Desde 0.6 (m15-rayd-otlp, research Q92), con `tracerProvider` cada RPC
 * del transporte del handle lleva además `traceparent` (y `tracestate`, si
 * el propagador global de `@opentelemetry/api` lo usa; nunca `baggage`)
 * hacia `rayd`, que lo registra como `trace_id`/`span_id` en sus logs
 * (`telemetry-export/propagation.ts`). Sin la opción no se añade ninguna
 * cabecera. La exportación de métricas del sandbox es `telemetry` (otra
 * opción, con su propio coste); la instrumentación del shim de E2B queda
 * fuera de alcance. La exportación de los spans (y su coste) la configura
 * el `tracerProvider` que pasa el llamante.
 *
 * Coste y activación
 * -------------------
 * Activa: `tracerProvider` (un `opentelemetry.trace.TracerProvider`, o
 *     cualquier objeto con `getTracer(name, version)`) en `create()`,
 *     `connect()` o en `Sandbox.kill`/`pause`/`resume` estáticos; mismo
 *     nombre de opción en Python (`tracer_provider=`).
 * Recursos y llamadas AWS: ninguno. Rayito sólo crea spans en el proveedor
 *     que ya tiene el llamante; no hace ninguna llamada a AWS ni crea
 *     ningún cliente nuevo por esta opción.
 * Coste aproximado: $0 de AWS; el coste (si lo hay) es el del backend de
 *     exportación que configure el llamante (Collector, Jaeger, X-Ray...),
 *     fuera de rayito.
 * IAM: ninguno adicional.
 * Cómo apagarla: no pases `tracerProvider` (por defecto `undefined`).
 * Ejemplo:
 * ```ts
 * import { trace } from "@opentelemetry/api";
 * import { Sandbox } from "rayito";
 *
 * const sbx = await Sandbox.create({ tracerProvider: trace.getTracerProvider() });
 * await sbx.commands.run("echo hola"); // span "rayito.commands.run"
 * await sbx.kill();
 * ```
 */

import type { Span, Tracer, TracerProvider } from "@opentelemetry/api";
import { VERSION } from "./version.js";

/** `SpanKind.CLIENT` de `@opentelemetry/api`, fijada a mano para no importar el módulo. */
const SPAN_KIND_CLIENT = 2;
/** `SpanStatusCode.ERROR` de `@opentelemetry/api`, fijada a mano para no importar el módulo. */
const SPAN_STATUS_CODE_ERROR = 2;

/** Única fuente de verdad de las claves de atributo que un span de rayito
 * puede llevar; cualquier otra es un error en tiempo de ejecución. Nunca
 * texto libre (comando, código, ruta, env, secreto, metadata, token, URL). */
export const ALLOWED_SPAN_ATTRIBUTES = [
  "rayito.sandbox.id",
  "rayito.region",
  "rayito.template.name",
  "rayito.resume_generation",
  "rayito.operation",
  "rayito.commands.exit_code",
  "rayito.commands.background",
  "rayito.code.language",
  "rayito.files.operation",
  "rayito.files.count",
  "rayito.files.bytes",
  "rayito.error.type",
  // ai-agent-core (ADR-025, design.md §7): `rayito.agent.run`. Los
  // `gen_ai.*` siguen la convención semántica de OpenTelemetry para
  // agentes; ninguno lleva el prompt, el texto de la respuesta ni
  // argumentos de herramienta.
  "gen_ai.operation.name",
  "gen_ai.provider.name",
  "gen_ai.request.model",
  "gen_ai.agent.name",
  "gen_ai.conversation.id",
  "gen_ai.usage.input_tokens",
  "gen_ai.usage.output_tokens",
  "rayito.agent.runtime",
  "rayito.agent.steps",
  "rayito.agent.exit_code",
  "rayito.agent.failure_reason",
  "rayito.agent.cache_read_tokens",
  "rayito.agent.cache_write_tokens",
] as const;

export type RayitoSpanAttributeKey = (typeof ALLOWED_SPAN_ATTRIBUTES)[number];

/** Atributos de un span de rayito: sólo las claves de {@link ALLOWED_SPAN_ATTRIBUTES}. */
export type RayitoSpanAttributes = Partial<
  Record<RayitoSpanAttributeKey, string | number | boolean>
>;

const ALLOWED_SPAN_ATTRIBUTE_SET: ReadonlySet<string> = new Set(ALLOWED_SPAN_ATTRIBUTES);

/** Lo mínimo de `opentelemetry.trace.TracerProvider` que rayito necesita:
 * cualquier objeto con `getTracer(name, version)` sirve (el SDK real, su
 * `NoopTracerProvider` o un doble de test), sin tener que importar
 * `@opentelemetry/api` en tiempo de ejecución sólo para comprobar el tipo. */
export type TracerProviderLike = Pick<TracerProvider, "getTracer">;

/** Fachada de spans que guarda un `Sandbox`. `span()` ejecuta `fn` con el
 * span (o `undefined` en {@link NOOP}) activo, y lo cierra al terminar
 * (incluyendo cuando `fn` devuelve una promesa). */
export interface Instrumentation {
  span<T>(
    name: string,
    attributes: RayitoSpanAttributes | undefined,
    fn: (span: Span | undefined) => T,
  ): T;
}

function isPromiseLike<T>(value: T | Promise<T>): value is Promise<T> {
  return (
    typeof value === "object" && value !== null && typeof (value as Promise<T>).then === "function"
  );
}

/** `true` sólo cuando `attributes` sólo tiene claves de
 * {@link ALLOWED_SPAN_ATTRIBUTES}; lanza si no, con el nombre de la primera
 * clave rechazada. */
export function validateAttributes(
  attributes: RayitoSpanAttributes | undefined,
): Record<string, string | number | boolean> {
  if (attributes === undefined) {
    return {};
  }
  const unknown = Object.keys(attributes).filter((key) => !ALLOWED_SPAN_ATTRIBUTE_SET.has(key));
  if (unknown.length > 0) {
    throw new Error(`atributo de span de rayito no permitido: ${unknown.sort().join(", ")}`);
  }
  return { ...attributes } as Record<string, string | number | boolean>;
}

/** Sin `tracerProvider`: `span()` ejecuta `fn(undefined)` directamente, sin
 * validar ni copiar `attributes` (no hay a dónde escribirlos) y sin crear
 * ningún objeto nuevo por llamada. */
class NoopInstrumentation implements Instrumentation {
  span<T>(
    _name: string,
    _attributes: RayitoSpanAttributes | undefined,
    fn: (span: Span | undefined) => T,
  ): T {
    return fn(undefined);
  }
}

/** Instancia compartida; {@link instrumentationFor}(`undefined`) siempre
 * devuelve ésta. */
export const NOOP: Instrumentation = new NoopInstrumentation();

function errorTypeName(error: unknown): string {
  if (error instanceof Error) {
    return error.name || error.constructor.name;
  }
  return "UnknownError";
}

class OtelInstrumentation implements Instrumentation {
  readonly #tracer: Tracer;

  constructor(tracerProvider: TracerProviderLike) {
    this.#tracer = tracerProvider.getTracer("rayito", VERSION);
  }

  span<T>(
    name: string,
    attributes: RayitoSpanAttributes | undefined,
    fn: (span: Span | undefined) => T,
  ): T {
    const validated = validateAttributes(attributes);
    return this.#tracer.startActiveSpan(
      name,
      // `kind`/`code` son los valores numéricos de `SpanKind.CLIENT` y
      // `SpanStatusCode.ERROR`: el tipo real de `@opentelemetry/api` sólo
      // se usa en tiempo de compilación (`import type` más arriba).
      { kind: SPAN_KIND_CLIENT, attributes: validated } as Parameters<Tracer["startActiveSpan"]>[1],
      (span: Span) => {
        const finish = (error?: unknown): void => {
          if (error !== undefined) {
            const errorType = errorTypeName(error);
            // Nunca el error real: su `message`/`stack` pueden llevar el
            // mismo dato sensible que la lista de atributos prohíbe.
            span.recordException({ name: errorType, message: errorType });
            span.setStatus({ code: SPAN_STATUS_CODE_ERROR, message: errorType });
          }
          span.end();
        };
        try {
          const result = fn(span);
          if (isPromiseLike(result)) {
            return result.then(
              (value) => {
                finish();
                return value;
              },
              (error: unknown) => {
                finish(error);
                throw error;
              },
            ) as T;
          }
          finish();
          return result;
        } catch (error) {
          finish(error);
          throw error;
        }
      },
    );
  }
}

/** `undefined` (por defecto) → {@link NOOP}: ningún span y ningún import
 * de `@opentelemetry/api`. Con un proveedor, construye el tracer `rayito`
 * ya mismo (una vez, en `create()`/`connect()`) para que cada `span()`
 * posterior sólo abra y cierre un span de ese tracer. */
export function instrumentationFor(
  tracerProvider: TracerProviderLike | undefined,
): Instrumentation {
  if (tracerProvider === undefined) {
    return NOOP;
  }
  return new OtelInstrumentation(tracerProvider);
}
