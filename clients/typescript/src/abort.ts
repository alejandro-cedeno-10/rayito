/**
 * La política de cancelación del SDK, sin I/O: un `AbortSignal` abortado gana
 * a cualquier error y rechaza con su `reason` sin envolverlo. La comparten el
 * núcleo del sandbox y el adaptador del plano de control.
 */

/** Un `signal` abortado gana a cualquier error: se rechaza con su `reason`, sin envolverlo. */
export function abortReasonOr(signal: AbortSignal | undefined, error: unknown): unknown {
  return signal?.aborted ? signal.reason : error;
}

/**
 * `promise`, o el `reason` de `signal` en cuanto se aborte (lo que ya esté en
 * marcha sigue, pero nadie lo espera). Uno ya abortado rechaza sin esperar.
 */
export function raceAbort<T>(promise: Promise<T>, signal: AbortSignal | undefined): Promise<T> {
  if (signal === undefined) {
    return promise;
  }
  if (signal.aborted) {
    promise.catch(() => undefined);
    return Promise.reject(signal.reason);
  }
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => reject(signal.reason);
    signal.addEventListener("abort", onAbort, { once: true });
    promise.then(
      (value) => {
        signal.removeEventListener("abort", onAbort);
        resolve(value);
      },
      (error: unknown) => {
        signal.removeEventListener("abort", onAbort);
        reject(error);
      },
    );
  });
}
