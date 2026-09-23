/**
 * Las clases de error de E2B sin equivalente que lanzar: existen para que
 * `instanceof TemplateError`/`BuildError` compile y nunca acierte. Los alias
 * (`NotEnoughSpaceError`, `ServiceBusyError`) se re-exportan desde `index.ts`
 * como bindings de las clases nativas, así `instanceof` vale entre entradas.
 */

import { SandboxError } from "../errors.js";

/** Nunca se lanza: Rayito no tiene API de templates (SPEC.md §4). */
export class TemplateError extends SandboxError {}

/** Nunca se lanza: no hay construcción de templates que pueda fallar. */
export class BuildError extends Error {
  constructor(message: string) {
    super(message);
    Object.setPrototypeOf(this, new.target.prototype);
    this.name = new.target.name;
  }
}
