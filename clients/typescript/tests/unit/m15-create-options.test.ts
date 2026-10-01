/**
 * `Sandbox.create()`: las seis opciones 0.6 que siguen siendo un stub
 * lanzan `UnimplementedError` antes de resolver ningún plano de control (y
 * por tanto antes de cualquier llamada a AWS: `planFeatures` corre antes
 * de `resolveControlPlane`). `gateways` (m15-secrets-gateway) ya no es un
 * stub: con un valor mal formado lanza `InvalidArgumentError` en su lugar,
 * en el mismo punto. Espejo de `test_m15_create_kwargs.py`.
 */

import { describe, expect, test } from "vitest";
import { InvalidArgumentError, UnimplementedError } from "../../src/errors.js";
import type { SandboxPool } from "../../src/pool/pool.js";
import { Sandbox } from "../../src/sandbox/sandbox.js";

const TEMPLATE = "arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base";

describe("Sandbox.create: 0.6 options", () => {
  test.each([
    ["mounts", { "/mnt/d": {} }],
    ["volumes", { "/mnt/v": {} }],
    ["size", "4gb"],
    ["events", {}],
    ["telemetry", {}],
    ["domain", {}],
  ] as const)("rejects option %s before resolving a control plane", async (option, value) => {
    await expect(Sandbox.create({ template: TEMPLATE, [option]: value })).rejects.toThrow(
      UnimplementedError,
    );
  });

  test("rejects a malformed gateways value before resolving a control plane", async () => {
    await expect(
      Sandbox.create({ template: TEMPLATE, gateways: { anthropic: {} } } as never),
    ).rejects.toThrow(InvalidArgumentError);
  });

  test("pool with a 0.6 option is invalid argument", async () => {
    const pool = Object.create(Object.getPrototypeOf({})) as SandboxPool;
    await expect(Sandbox.create({ pool, mounts: { "/mnt/d": {} } })).rejects.toThrow(
      InvalidArgumentError,
    );
  });
});
