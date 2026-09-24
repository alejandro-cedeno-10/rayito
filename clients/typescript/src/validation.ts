/**
 * Validación pura de los ajustes de cliente que comparten el plano de control
 * y `rayito/e2b` (`retries`, `integration`): `InvalidArgumentError` antes de
 * construir nada, sin I/O ni dependencias del SDK de AWS.
 */

import { InvalidArgumentError } from "./errors.js";

const INTEGRATION_PATTERN = /^[\x21-\x7e]+$/;

/** `retries` es un entero >= 0 (sin `boolean`): `InvalidArgumentError` antes de construir nada. */
export function validateRetries(retries: unknown): number | undefined {
  if (retries === undefined) {
    return undefined;
  }
  if (typeof retries !== "number" || !Number.isInteger(retries) || retries < 0) {
    throw new InvalidArgumentError("retries debe ser un entero >= 0");
  }
  return retries;
}

/** `integration` es ASCII imprimible sin espacios (`"acme/1.0"`), o nada. */
export function validateIntegration(integration: unknown): string | undefined {
  if (integration === undefined) {
    return undefined;
  }
  if (typeof integration !== "string" || !INTEGRATION_PATTERN.test(integration)) {
    throw new InvalidArgumentError("integration debe ser ASCII imprimible sin espacios");
  }
  return integration;
}
