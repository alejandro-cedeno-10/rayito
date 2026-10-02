/**
 * W3C `traceparent` propagation from the SDK toward `rayd` (m15-rayd-otlp,
 * research Q92: only `traceparent`/`tracestate`, never `grpc-trace-bin` nor
 * `baggage` -- the proxy only lets `x-amzn-requestid` through and filters
 * `x-aws-proxy-*`, and the research review verified a plain W3C
 * interceptor crosses that proxy unchanged).
 *
 * `TraceparentProvider` mirrors `rayito._telemetry_export._propagation`'s
 * `TraceparentProvider` one to one: `create()` resolves the optional peer
 * `@opentelemetry/api` once (`loadOptionalPeer`, the same convention
 * `telemetry-export/section.ts`'s `resolveBearerToken` and `otel.ts` use
 * for an opt-in dependency), and `metadata()` is cheap and synchronous
 * after that, computed fresh on every call over whatever span is active at
 * that moment (never fixed for the life of a channel).
 *
 * **Estado de esta entrega**: implemented and unit-tested in isolation,
 * exactly mirroring Python, but not yet wired into the live gRPC channel
 * (no `CallMetadataProvider` seam exists on the TypeScript transport
 * either) -- see `openspec/changes/m15-rayd-otlp/design.md` D8 and
 * `proposal.md`'s "Non-blocking follow-up".
 */

import { loadOptionalPeer } from "../optional.js";

/** `baggage` never travels to `rayd`, even if the active context has one
 * (Q92): the proxy filters `grpc-*` keys inconsistently, so W3C
 * `traceparent`/`tracestate` is the only reliable path, and `baggage`
 * carries nothing `rayd` needs. */
const DROPPED_CARRIER_KEYS: readonly string[] = ["baggage"];

const TELEMETRY_PROPAGATION_FEATURE = "tracerProvider (traceparent propagation)";

/**
 * The minimal shape this module needs from `@opentelemetry/api`'s runtime
 * exports -- not the whole package's types, the same hand-rolled-interface
 * convention `telemetry-export/section.ts`'s `SecretsManagerModule` uses
 * for its own optional peer.
 */
interface OpenTelemetryPropagationModule {
  readonly context: { active(): unknown };
  readonly propagation: { inject(activeContext: unknown, carrier: Record<string, string>): void };
}

export class TraceparentProvider {
  private constructor(private readonly otel: OpenTelemetryPropagationModule) {}

  /** Resolves `@opentelemetry/api` once; throws `InvalidArgumentError`
   * (via `loadOptionalPeer`) naming the package to install if it is
   * missing. Never called unless `tracerProvider` was actually set. */
  static async create(): Promise<TraceparentProvider> {
    const otel = await loadOptionalPeer<OpenTelemetryPropagationModule>(
      "@opentelemetry/api",
      TELEMETRY_PROPAGATION_FEATURE,
    );
    return new TraceparentProvider(otel);
  }

  /** One `(key, value)` metadata pair per call, computed over the span
   * active right now. Never makes a network call. */
  metadata(): ReadonlyArray<readonly [string, string]> {
    const carrier: Record<string, string> = {};
    this.otel.propagation.inject(this.otel.context.active(), carrier);
    for (const key of DROPPED_CARRIER_KEYS) {
      delete carrier[key];
    }
    return Object.entries(carrier);
  }
}
