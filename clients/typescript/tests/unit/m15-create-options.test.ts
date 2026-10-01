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
import { OtlpAuth, TelemetryExport } from "../../src/telemetry-export/domain.js";

const TEMPLATE = "arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base";
// Not an ARN, so `resolveImageVariant` resolves it (unlike `TEMPLATE`
// above): needed for the `telemetry` + `executionRole` case below, which
// relies on the caps check actually running before any control plane call.
const NAMED_TEMPLATE = "rayito-base";

describe("Sandbox.create: 0.6 options", () => {
  // `telemetry` (m15-rayd-otlp) is the first real function: it validates
  // for real instead of throwing `UnimplementedError` unconditionally (see
  // the dedicated `telemetry` tests below), so it is not part of this
  // generic "still a stub" table.
  test.each([
    ["mounts", { "/mnt/d": {} }],
    ["volumes", { "/mnt/v": {} }],
    ["size", "4gb"],
    ["events", {}],
    ["gateways", { anthropic: {} }],
    ["domain", {}],
  ] as const)("rejects option %s before resolving a control plane", async (option, value) => {
    await expect(Sandbox.create({ template: TEMPLATE, [option]: value })).rejects.toThrow(
      UnimplementedError,
    );
  });

  test("rejects a telemetry value needing caps on a known non-caps image", async () => {
    await expect(
      Sandbox.create({
        template: NAMED_TEMPLATE,
        telemetry: new TelemetryExport({ auth: OtlpAuth.executionRole() }),
      }),
    ).rejects.toThrow(UnimplementedError);
  });

  test("rejects a garbage telemetry value as invalid argument", async () => {
    await expect(Sandbox.create({ template: TEMPLATE, telemetry: {} })).rejects.toThrow(
      InvalidArgumentError,
    );
  });

  test("pool with a 0.6 option is invalid argument", async () => {
    const pool = Object.create(Object.getPrototypeOf({})) as SandboxPool;
    await expect(Sandbox.create({ pool, mounts: { "/mnt/d": {} } })).rejects.toThrow(
      InvalidArgumentError,
    );
  });
});
