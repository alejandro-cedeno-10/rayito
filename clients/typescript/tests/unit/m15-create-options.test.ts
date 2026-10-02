/**
 * `Sandbox.create()`: seis de las siete opciones 0.6 siguen lanzando
 * `UnimplementedError` antes de resolver ningún plano de control (y por
 * tanto antes de cualquier llamada a AWS: `planFeatures` corre antes de
 * `resolveControlPlane`). `mounts` (`m15-s3-mounts`) es la primera en dejar
 * de ser un stub: en una imagen nombrada de una variante no-caps rechaza
 * igual de pronto, por `requireCapsFor`; sobre una imagen opaca (un ARN) la
 * decisión se difiere al agente, así que no puede probarse aquí sin tocar
 * AWS. Espejo de `test_m15_create_kwargs.py`.
 */

import { describe, expect, test } from "vitest";
import { InvalidArgumentError, UnimplementedError } from "../../src/errors.js";
import type { SandboxPool } from "../../src/pool/pool.js";
import { S3Mount } from "../../src/s3-mounts/domain.js";
import { Sandbox } from "../../src/sandbox/sandbox.js";

const TEMPLATE = "arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base";

describe("Sandbox.create: 0.6 options", () => {
  test.each([
    ["volumes", { "/mnt/v": {} }],
    ["events", {}],
    ["telemetry", {}],
    ["gateways", { anthropic: {} }],
    ["domain", {}],
  ] as const)("rejects option %s before resolving a control plane", async (option, value) => {
    await expect(Sandbox.create({ template: TEMPLATE, [option]: value })).rejects.toThrow(
      UnimplementedError,
    );
  });

  test("mounts on a named non-caps image variant rejects before resolving a control plane", async () => {
    await expect(
      Sandbox.create({
        template: "rayito-base",
        mounts: { "/mnt/d": new S3Mount({ bucket: "team-data" }) },
      }),
    ).rejects.toThrow(UnimplementedError);
  });

  test("pool with a 0.6 option is invalid argument", async () => {
    const pool = Object.create(Object.getPrototypeOf({})) as SandboxPool;
    await expect(
      Sandbox.create({
        pool,
        mounts: { "/mnt/d": new S3Mount({ bucket: "team-data" }) },
      }),
    ).rejects.toThrow(InvalidArgumentError);
  });

  test("pool with size is invalid argument even though size is implemented", async () => {
    // m15-sizes-catalog: `size` ya resuelve de verdad, pero sigue sin poder
    // combinarse con `pool` (architecture §7.3).
    const pool = Object.create(Object.getPrototypeOf({})) as SandboxPool;
    await expect(Sandbox.create({ pool, size: "4gb" })).rejects.toThrow(InvalidArgumentError);
  });
});
