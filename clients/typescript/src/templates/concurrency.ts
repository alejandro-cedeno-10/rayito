/**
 * Límite de concurrencia de builds en proceso (investigación §3.4/TPL-1,
 * Q83: 10 builds concurrentes por cuenta, `ServiceQuotaExceededException`
 * en el undécimo). Espejo de `rayito._templates._concurrency`: guarda
 * local, no un contador distribuido.
 */

import { BuildError } from "../errors.js";

/** Cuota medida de builds concurrentes por cuenta (Q83). */
export const MAX_CONCURRENT_BUILDS = 10;

let inFlight = 0;

/** Reserva un hueco de build para este proceso y lo libera al terminar
 * `fn` (éxito o error); si ya hay `MAX_CONCURRENT_BUILDS` en vuelo, lanza
 * `BuildError({reason: "build_quota"})` en el acto. */
export async function withBuildSlot<T>(fn: () => Promise<T>): Promise<T> {
  if (inFlight >= MAX_CONCURRENT_BUILDS) {
    throw new BuildError(
      `ya hay ${MAX_CONCURRENT_BUILDS} builds de templates en marcha en este proceso ` +
        "(límite local, Q83); espera a que termine alguno",
      { reason: "build_quota" },
    );
  }
  inFlight += 1;
  try {
    return await fn();
  } finally {
    inFlight -= 1;
  }
}
