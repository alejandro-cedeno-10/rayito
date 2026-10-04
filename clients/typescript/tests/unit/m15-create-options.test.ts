/**
 * `Sandbox.create()`: las opciones 0.6 que siguen siendo un stub lanzan
 * `UnimplementedError` antes de resolver ningún plano de control (y por
 * tanto antes de cualquier llamada a AWS: `planFeatures` corre antes de
 * `resolveControlPlane`). `mounts` (`m15-s3-mounts`) ya no es un stub: en
 * una imagen nombrada de una variante no-caps rechaza igual de pronto, por
 * `requireCapsFor`; sobre una imagen opaca (un ARN) la decisión se difiere
 * al agente, así que no puede probarse aquí sin tocar AWS. `gateways`
 * (m15-secrets-gateway) tampoco: con un valor mal formado lanza
 * `InvalidArgumentError` en su lugar, en el mismo punto. Espejo de
 * `test_m15_create_kwargs.py`.
 */

import { describe, expect, test } from "vitest";
import { InvalidArgumentError, UnimplementedError } from "../../src/errors.js";
import type { SandboxPool } from "../../src/pool/pool.js";
import { S3Mount } from "../../src/s3-mounts/domain.js";
import { Sandbox } from "../../src/sandbox/sandbox.js";
import { OtlpAuth, TelemetryExport } from "../../src/telemetry-export/domain.js";
import { EfsVolume } from "../../src/volumes/domain.js";

const TEMPLATE = "arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base";
// Not an ARN, so `resolveImageVariant` resolves it (unlike `TEMPLATE`
// above): needed for the `telemetry` + `executionRole` case below, which
// relies on the caps check actually running before any control plane call.
const NAMED_TEMPLATE = "rayito-base";
// m15-efs-volumes valida la forma de `volumes` (tipo, rutas, caps,
// conector, execution role) antes de `resolveControlPlane`.
const VALID_VOLUME = {
  "/mnt/v": new EfsVolume({ fileSystemId: "fs-0123abcd", accessPointId: "fsap-0123abcd" }),
};
// Marcador de documentación (cuenta ficticia): el conector propio que
// `volumes` exige como único `egress` (Q131).
const CONNECTOR = "arn:aws:lambda:us-east-1:123456789012:network-connector:rayito-efs";

describe("Sandbox.create: 0.6 options", () => {
  // `telemetry` (m15-rayd-otlp) is the first real function: it validates
  // for real instead of throwing `UnimplementedError` unconditionally (see
  // the dedicated `telemetry` tests below), so it is not part of this
  // generic "still a stub" table.
  test.each([
    // `volumes` left this stub list in m15-efs-volumes (it now mounts; its
    // validation is covered below and in `m15-efs-volumes.test.ts`).
    // `events` left this stub list in m15-events-webhooks (now
    // `InvalidArgumentError` without `logging: "cloudwatch"`, still before
    // any control plane) — see `m15-events-webhooks-feature-options.test.ts`.
    ["domain", {}, {}],
  ] as const)(
    "rejects option %s before resolving a control plane",
    async (option, value, extra) => {
      await expect(
        Sandbox.create({ template: TEMPLATE, ...extra, [option]: value }),
      ).rejects.toThrow(UnimplementedError);
    },
  );

  test.each([
    ["no egress", undefined],
    ["INTERNET_EGRESS alone", ["INTERNET_EGRESS"]],
    ["INTERNET_EGRESS plus the connector", [CONNECTOR, "INTERNET_EGRESS"]],
  ] as const)("rejects volumes with %s before resolving a control plane", async (_, egress) => {
    await expect(
      Sandbox.create({ template: TEMPLATE, volumes: VALID_VOLUME, egress }),
    ).rejects.toThrow(/NAT/);
  });

  test("rejects volumes without executionRoleArn before resolving a control plane", async () => {
    await expect(
      Sandbox.create({ template: TEMPLATE, volumes: VALID_VOLUME, egress: [CONNECTOR] }),
    ).rejects.toThrow(/executionRoleArn/);
  });

  test("rejects a telemetry value needing caps on a known non-caps image", async () => {
    await expect(
      Sandbox.create({
        template: NAMED_TEMPLATE,
        telemetry: new TelemetryExport({ auth: OtlpAuth.executionRole() }),
      }),
    ).rejects.toThrow(UnimplementedError);
  });

  test("rejects a malformed gateways value before resolving a control plane", async () => {
    await expect(
      Sandbox.create({ template: TEMPLATE, gateways: { anthropic: {} } } as never),
    ).rejects.toThrow(InvalidArgumentError);
  });

  test("mounts on a named non-caps image variant rejects before resolving a control plane", async () => {
    await expect(
      Sandbox.create({
        template: "rayito-base",
        mounts: { "/mnt/d": new S3Mount({ bucket: "team-data" }) },
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
