/**
 * Comprueba, sin crear nada, que la precarga `e2b-corpus-cap.mjs` hace llegar
 * `maxLifetimeMs` = 900 000 al `Sandbox.create` nativo por todas las formas de
 * crear del corpus. Se ejecuta con `node --import ./e2b-corpus-cap.mjs`; el
 * `Sandbox.create` nativo se sustituye por uno que captura las opciones y
 * rechaza, así que nunca se llama a AWS.
 */

import assert from "node:assert/strict";
import { Sandbox as NativeSandbox } from "rayito";
import { E2B, Sandbox } from "rayito/e2b";

const CAP_MS = 900_000;
const captured = [];
const sentinel = new Error("corpus-cap-check: sin llamadas a AWS");
NativeSandbox.create = async (options) => {
  captured.push(options.maxLifetimeMs);
  throw sentinel;
};

async function resolvedCap(create) {
  const before = captured.length;
  await assert.rejects(create, (error) => error === sentinel);
  assert.equal(captured.length, before + 1, "la creación no llegó al Sandbox nativo");
  return captured.at(-1);
}

const cases = {
  "Sandbox.create()": () => Sandbox.create(),
  "Sandbox.create(template, opts)": () => Sandbox.create("rayito-base", { timeoutMs: 300_000 }),
  "Sandbox.create({ timeoutMs: 1 h })": () => Sandbox.create({ timeoutMs: 3_600_000 }),
  "Sandbox.create({ maxLifetimeMs: 2 h })": () => Sandbox.create({ maxLifetimeMs: 7_200_000 }),
  "new E2B().Sandbox.create()": () => new E2B().Sandbox.create(),
};
for (const [label, create] of Object.entries(cases)) {
  assert.equal(await resolvedCap(create), CAP_MS, label);
}
console.log(`corpus-cap ok (${Object.keys(cases).length} rutas, maxLifetimeMs=${CAP_MS})`);
