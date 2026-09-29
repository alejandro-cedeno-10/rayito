/**
 * Clasificación de fallos de stream (M5 D13/D14), sin depender de
 * `commands.ts` ni de `core.ts`: constante del sondeo de salud tras un
 * corte y el mapeo de un `ConnectError` de stream a un error de dominio.
 * `core.ts` la usa para su contrato de reconexión; `commands.ts` la
 * reexporta para no romper los imports existentes.
 */

import type { ConnectError } from "@connectrpc/connect";
import { SandboxError, SandboxNotFoundError, SandboxStateError } from "../errors.js";
import { SUSPENDED_STATES, TERMINAL_STATES } from "../limits.js";
import { isStreamReset, translateRpcError } from "../transport/errors.js";

export const STREAM_PROBE_TIMEOUT_MS = 5000;

/**
 * Clasifica un fallo de stream con lo que el sandbox ya averiguó: si no es
 * un reset, la tabla unaria; si `Health` respondió, el sandbox vive y el
 * cliente puede reengancharse; si no, el estado de `get-microvm` decide.
 */
export function streamFailureError(
  error: ConnectError,
  options: { readonly healthOk: boolean; readonly state: string | undefined },
): Error {
  if (!isStreamReset(error)) {
    return translateRpcError(error);
  }
  const base = { grpcCode: error.code, cause: error };
  const detail = error.rawMessage;
  if (options.healthOk) {
    return new SandboxError(
      `stream cortado (${detail}) pero el sandbox responde; reconecta con commands.connect(pid)`,
      base,
    );
  }
  const state = options.state;
  if (state !== undefined && TERMINAL_STATES.has(state)) {
    return new SandboxNotFoundError(`el sandbox está ${state}: stream cortado (${detail})`, base);
  }
  if (state !== undefined && SUSPENDED_STATES.has(state)) {
    return new SandboxStateError(`el sandbox está ${state}: stream cortado (${detail})`, base);
  }
  return new SandboxError(
    `stream cortado (${detail}) y el agente no responde (estado ${state ?? "desconocido"})`,
    base,
  );
}
