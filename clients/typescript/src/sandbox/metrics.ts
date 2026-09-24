/**
 * El historial de métricas (`HealthService.MetricsHistory`, design D6/D10):
 * la validación y el request, el mapeo de la respuesta, el error de una
 * imagen anterior a M9 (`UnimplementedError`) y la variante estática, que lee un sandbox `RUNNING`
 * por su id con el access token sin despertarlo nunca. Mismas reglas y mismo
 * texto que `clients/python/src/rayito/_metrics_base.py`.
 */

import { create } from "@bufbuild/protobuf";
import { Code } from "@connectrpc/connect";
import type { ControlPlane } from "../aws/control-plane.js";
import {
  InvalidArgumentError,
  SandboxError,
  SandboxStateError,
  UnimplementedError,
} from "../errors.js";
import {
  type MetricsHistoryRequest,
  MetricsHistoryRequestSchema,
  type MetricsHistoryResponse,
} from "../gen/rayito/v1/health_pb.js";
import { TERMINAL_STATES } from "../limits.js";
import type { SandboxInfo, SandboxMetrics } from "../models.js";
import { translateRpcError } from "../transport/errors.js";
import type { TransportSettings } from "../transport/transport.js";
import { metricsFromProto, type RequestOptions } from "./commands.js";
import { withDedicatedHealthClient } from "./probe.js";
import { terminalStateError } from "./readiness.js";

export const HISTORY_UNIMPLEMENTED_REASON =
  "la imagen es anterior a M9 (rayd sin MetricsHistory): publica una imagen M9";
export const HISTORY_FEATURE = "getMetricsHistory";
export const STATIC_HISTORY_FEATURE = "Sandbox.getMetricsHistory(sandboxId)";

const UINT32_MAX = 0xffff_ffff;

/**
 * `start`/`end` son límites inclusivos del reloj de pared del guest (ausente =
 * sin límite); `maxPoints` reduce la serie a ese número de puntos (la última
 * muestra de cada tramo con la CPU promediada).
 */
export interface MetricsHistoryOptions extends RequestOptions {
  readonly start?: Date | undefined;
  readonly end?: Date | undefined;
  readonly maxPoints?: number | undefined;
}

function unixMsOrZero(moment: Date | undefined, field: string): bigint {
  if (moment === undefined) {
    return 0n;
  }
  const ms = moment instanceof Date ? moment.getTime() : Number.NaN;
  if (!Number.isFinite(ms) || ms < 0) {
    throw new InvalidArgumentError(
      `${field} debe ser un Date válido no anterior a 1970-01-01T00:00:00Z`,
    );
  }
  return BigInt(ms);
}

/** Por encima del `uint32` del proto se recorta: pedir más puntos que muestras es un no-op en `rayd`. */
function validateMaxPoints(maxPoints: number | undefined): number {
  if (maxPoints === undefined) {
    return 0;
  }
  if (!Number.isInteger(maxPoints) || maxPoints < 1) {
    throw new InvalidArgumentError(
      `maxPoints debe ser un entero >= 1, recibido ${String(maxPoints)}`,
    );
  }
  return Math.min(maxPoints, UINT32_MAX);
}

/** Toda la validación antes de cualquier RPC: fechas, `start > end` y `maxPoints`. */
export function metricsHistoryRequest(options: MetricsHistoryOptions): MetricsHistoryRequest {
  const startUnixMs = unixMsOrZero(options.start, "start");
  const endUnixMs = unixMsOrZero(options.end, "end");
  if (options.start !== undefined && options.end !== undefined && startUnixMs > endUnixMs) {
    throw new InvalidArgumentError("start es posterior a end");
  }
  return create(MetricsHistoryRequestSchema, {
    startUnixMs,
    endUnixMs,
    maxPoints: validateMaxPoints(options.maxPoints),
  });
}

/** Las muestras en el orden ascendente en que las manda el agente. */
export function metricsHistoryFromProto(response: MetricsHistoryResponse): SandboxMetrics[] {
  return response.samples.map(metricsFromProto);
}

/**
 * La tabla unaria, salvo `Unimplemented` (un `rayd` anterior a M9 no conoce
 * el método), que pasa a `UnimplementedError` de `feature` con el motivo de
 * M9 y el error gRPC en `cause`, como las transferencias y el plazo.
 */
export function historyErrorTranslator(feature: string): (error: unknown) => Error {
  return (error: unknown): Error => {
    const translated = translateRpcError(error);
    if (translated instanceof SandboxError && translated.grpcCode === Code.Unimplemented) {
      return new UnimplementedError(feature, HISTORY_UNIMPLEMENTED_REASON, undefined, {
        cause: translated,
      });
    }
    return translated;
  };
}

/** El `UnimplementedError` de `historyErrorTranslator`: el shim de E2B cae en la instantánea. */
export function isHistoryUnavailable(error: unknown): error is UnimplementedError {
  return error instanceof UnimplementedError && error.reason === HISTORY_UNIMPLEMENTED_REASON;
}

/**
 * Leer el historial de un sandbox que no está `RUNNING` lo despertaría: se
 * rechaza sin acuñar ningún JWE.
 */
export function assertReadableWithoutWaking(info: SandboxInfo): void {
  if (TERMINAL_STATES.has(info.state)) {
    throw terminalStateError(info);
  }
  if (info.state !== "RUNNING") {
    throw new SandboxStateError(
      `el sandbox ${info.sandboxId} está ${info.state}: leer su historial lo despertaría; ` +
        "reanúdalo con connect()",
    );
  }
}

export interface DedicatedHistoryCall {
  readonly plane: ControlPlane;
  readonly info: SandboxInfo;
  readonly settings: TransportSettings;
  readonly accessToken: string;
  readonly request: MetricsHistoryRequest;
  readonly timeoutMs: number;
}

/** Un `MetricsHistory` por un transporte dedicado con `x-access-token`. */
export async function fetchMetricsHistory(call: DedicatedHistoryCall): Promise<SandboxMetrics[]> {
  let response: MetricsHistoryResponse;
  try {
    response = await withDedicatedHealthClient(
      call.plane,
      call.info,
      call.settings,
      call.accessToken,
      (client) => client.metricsHistory(call.request, { timeoutMs: call.timeoutMs }),
    );
  } catch (error) {
    throw historyErrorTranslator(STATIC_HISTORY_FEATURE)(error);
  }
  return metricsHistoryFromProto(response);
}
