/**
 * Una salida cerrada por el otro extremo (`rayito sandbox list | head`): el
 * siguiente `write` a `stdout` o `stderr` falla con `EPIPE`, y sin un
 * oyente de `error` Node termina con una excepción sin capturar y su traza.
 * La CLI sale en silencio con `EXIT_FAILURE`, como la de Python, donde Click
 * convierte el `BrokenPipeError` en un `exit 1` sin traza. Cualquier otro
 * error del stream se relanza.
 */

import { EXIT_FAILURE } from "./exit-codes.js";

const BROKEN_PIPE_CODE = "EPIPE";

/** Lo que necesita `exitOnClosedPipe` de `process.stdout`/`process.stderr`. */
export interface ErrorSource {
  on(event: "error", listener: (error: unknown) => void): unknown;
}

/** Si `error` es el de escribir en una tubería que el lector ya cerró. */
export function isBrokenPipe(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    (error as { readonly code?: unknown }).code === BROKEN_PIPE_CODE
  );
}

/** Hace que un `EPIPE` en `stream` termine el proceso con `EXIT_FAILURE`,
 * sin escribir nada más; cualquier otro error del stream se relanza. */
export function exitOnClosedPipe(stream: ErrorSource, exit: (code: number) => void): void {
  stream.on("error", (error) => {
    if (!isBrokenPipe(error)) {
      throw error;
    }
    exit(EXIT_FAILURE);
  });
}
