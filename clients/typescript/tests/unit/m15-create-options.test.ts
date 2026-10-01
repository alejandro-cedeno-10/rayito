/**
 * `Sandbox.create()`: las siete opciones 0.6 lanzan `UnimplementedError`
 * antes de resolver ningún plano de control (y por tanto antes de
 * cualquier llamada a AWS: `planFeatures` corre antes de
 * `resolveControlPlane`). Espejo de `test_m15_create_kwargs.py`.
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
    ["gateways", { anthropic: {} }],
    ["domain", {}],
  ] as const)("rejects option %s before resolving a control plane", async (option, value) => {
    await expect(Sandbox.create({ template: TEMPLATE, [option]: value })).rejects.toThrow(
      UnimplementedError,
    );
  });

  test("pool with a 0.6 option is invalid argument", async () => {
    const pool = Object.create(Object.getPrototypeOf({})) as SandboxPool;
    await expect(Sandbox.create({ pool, mounts: { "/mnt/d": {} } })).rejects.toThrow(
      InvalidArgumentError,
    );
  });
});
