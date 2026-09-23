/**
 * Precarga (`node --import`) del corpus E2B de `m9-e2b.e2e.test.ts`: acota el
 * tope de plataforma del shim a 900 s sin tocar los programas.
 *
 * Sin `maxLifetimeMs` (una opción sólo de Rayito que el corpus no lleva), el
 * shim pide `max(3 600 000, ...)` ms a Lambda MicroVMs; la regla del proyecto
 * es que ningún MicroVM de e2e pase de 1 800 s, y el e2e de Python usa 900 s
 * (`TEST_SANDBOX_TIMEOUT_SECONDS`). Los enlaces ESM no se pueden reasignar,
 * pero los estáticos de clase sí: se envuelve `Sandbox.createFor`, por donde
 * pasan `Sandbox.create(...)` y `new E2B(opts).Sandbox.create(...)` (la
 * subclase ligada llama `Sandbox.createFor` de la clase base). `connect` no
 * crea MicroVMs ni lleva tope, así que no se toca. Un `maxLifetimeMs` explícito
 * mayor que el tope también se recorta.
 */

import assert from "node:assert/strict";
import { E2B, Sandbox } from "rayito/e2b";

export const CORPUS_MAX_LIFETIME_MS = 900_000;
const MARK = Symbol.for("rayito.e2e.corpusCap");

/** Las opciones de la llamada con `maxLifetimeMs` acotado al tope. */
export function capCreateOpts(opts) {
  const requested = opts?.maxLifetimeMs ?? CORPUS_MAX_LIFETIME_MS;
  return { ...opts, maxLifetimeMs: Math.min(requested, CORPUS_MAX_LIFETIME_MS) };
}

const original = Sandbox.createFor;
assert.equal(typeof original, "function", "rayito/e2b ya no expone Sandbox.createFor");

if (original[MARK] !== true) {
  const capped = function createFor(cls, bound, templateOrOpts, opts) {
    return typeof templateOrOpts === "string"
      ? original.call(this, cls, bound, templateOrOpts, capCreateOpts(opts))
      : original.call(this, cls, bound, capCreateOpts(templateOrOpts), opts);
  };
  capped[MARK] = true;
  Sandbox.createFor = capped;
}

assert.equal(Sandbox.createFor[MARK], true, "el tope del corpus no quedó instalado");
assert.equal(new E2B().Sandbox.createFor, Sandbox.createFor, "E2B().Sandbox no hereda el tope");
