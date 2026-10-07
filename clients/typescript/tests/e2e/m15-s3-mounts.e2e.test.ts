/**
 * `m15-s3-mounts` contra AWS real (`RAYITO_E2E=1`): S3M-1..S3M-4 del plan de
 * aceptación de M15 (ADR-017; cap de gasto $0.30).
 *
 * Necesita, además de `RAYITO_TEMPLATE`: `RAYITO_TEMPLATE_CAPS` (imagen
 * `rayito-base-caps` publicada con el bucket de abajo en su allowlist:
 * `rayito image publish ... --env RAYITO_ALLOWED_MOUNT_BUCKETS=<bucket>`, o
 * `make image-publish-caps MOUNT_BUCKETS=<bucket>`; sin él, cada montaje es
 * `MountError` con `code: "not_allowed"`), `RAYITO_EXECUTION_ROLE_ARN` (con la política
 * `RayitoS3MountAccess` de `infra/s3-mounts.yaml` sobre el bucket de abajo)
 * y `RAYITO_S3_MOUNT_BUCKET` (un bucket ya existente; este fichero sólo
 * escribe y borra bajo `rayito-e2e-s3-mounts/<uuid>/`).
 */

import { randomUUID } from "node:crypto";
import {
  DeleteObjectsCommand,
  ListObjectsV2Command,
  PutObjectCommand,
  S3Client,
} from "@aws-sdk/client-s3";
import { afterEach, beforeEach, describe, expect, test } from "vitest";
import { CommandExitError, Sandbox } from "../../src/index.js";
import { S3Mount } from "../../src/s3-mounts/domain.js";
import { e2eEnabled, TEST_SANDBOX_TIMEOUT_MS, useE2E } from "./helpers.js";

const CAPS_TEMPLATE_VAR = "RAYITO_TEMPLATE_CAPS";
const BUCKET_VAR = "RAYITO_S3_MOUNT_BUCKET";
const TEST_PREFIX_ROOT = "rayito-e2e-s3-mounts";

const suiteEnabled =
  e2eEnabled() && Boolean(process.env[CAPS_TEMPLATE_VAR]) && Boolean(process.env[BUCKET_VAR]);

describe.runIf(suiteEnabled)(
  `montajes S3 en ${process.env[CAPS_TEMPLATE_VAR]} (requiere RAYITO_E2E=1, ${CAPS_TEMPLATE_VAR} y ${BUCKET_VAR})`,
  () => {
    const e2e = useE2E(CAPS_TEMPLATE_VAR);
    const bucket = process.env[BUCKET_VAR] as string;
    // `e2e.settings`/`e2e.controlPlane` are a lazy proxy populated by
    // `useE2E`'s own `beforeAll`: reading them (here, via `s3`) must wait
    // until a `beforeEach`/test body runs, never at `describe`-body scope.
    let s3: S3Client;
    let prefix = "";

    beforeEach(() => {
      s3 = new S3Client(e2e.settings.region === undefined ? {} : { region: e2e.settings.region });
      prefix = `${TEST_PREFIX_ROOT}/${randomUUID()}/`;
    });

    afterEach(async () => {
      const listed = await s3.send(new ListObjectsV2Command({ Bucket: bucket, Prefix: prefix }));
      const keys = (listed.Contents ?? []).map((object) => ({ Key: object.Key as string }));
      if (keys.length > 0) {
        await s3.send(new DeleteObjectsCommand({ Bucket: bucket, Delete: { Objects: keys } }));
      }
    });

    async function createMountSandbox(allowInternetAccess?: boolean): Promise<Sandbox> {
      return Sandbox.create({
        template: e2e.templateArn,
        timeoutMs: TEST_SANDBOX_TIMEOUT_MS,
        idle: null,
        executionRoleArn: e2e.settings.executionRoleArn,
        logging: e2e.settings.logging,
        controlPlane: e2e.controlPlane,
        ...(allowInternetAccess === undefined ? {} : { allowInternetAccess }),
        mounts: new Map([
          ["/mnt/ro", new S3Mount({ bucket, prefix, readOnly: true })],
          ["/mnt/rw", new S3Mount({ bucket, prefix, readOnly: false, allowOverwrite: true })],
        ]),
      });
    }

    test("S3M-1: un objeto escrito bajo /mnt/rw aparece en S3 y uno puesto en S3 se lee bajo /mnt/ro", async () => {
      await s3.send(
        new PutObjectCommand({ Bucket: bucket, Key: `${prefix}seed.txt`, Body: "hola desde S3\n" }),
      );
      const sandbox = await createMountSandbox();
      e2e.created.push(sandbox);
      const read = await sandbox.commands.run("cat /mnt/ro/seed.txt");
      expect(read.stdout.trim()).toBe("hola desde S3");

      const write = await sandbox.commands.run(
        "sh -c 'echo escrito-por-rayd > /mnt/rw/salida.txt'",
      );
      expect(write.exitCode).toBe(0);
      const object = await s3.send(
        new ListObjectsV2Command({ Bucket: bucket, Prefix: `${prefix}salida.txt` }),
      );
      expect(object.KeyCount).toBe(1);
    });

    test("S3M-2 (SEC-3): uid 1000 no puede leer una credencial del entorno/argv del daemon ni matarlo", async () => {
      const sandbox = await createMountSandbox();
      e2e.created.push(sandbox);
      const findPid = await sandbox.commands.run("pgrep -f mount-s3 | head -1");
      const pid = findPid.stdout.trim();
      expect(pid).not.toBe("");

      const cmdline = await sandbox.commands.run(`tr '\\0' ' ' < /proc/${pid}/cmdline`);
      const lower = cmdline.stdout.toLowerCase();
      for (const forbidden of ["akia", "aws_secret", "aws_session_token"]) {
        expect(lower).not.toContain(forbidden);
      }

      // A non-zero exit rejects `commands.run` with `CommandExitError`
      // (E2B contract) instead of resolving with an `exitCode`.
      const killAttempt = await sandbox.commands
        .run(`kill -0 ${pid}`)
        .catch((error: unknown) => error);
      expect(killAttempt).toBeInstanceOf(CommandExitError);
      expect((killAttempt as CommandExitError).exitCode).not.toBe(0);
    });

    test("S3M-3: pause() responde dentro de su presupuesto aunque S3 no sea alcanzable", async () => {
      const sandbox = await createMountSandbox();
      e2e.created.push(sandbox);
      const started = performance.now();
      await sandbox.pause();
      const elapsedS = (performance.now() - started) / 1000;
      expect(elapsedS).toBeLessThan(30);
      await sandbox.resume();
    });

    test("S3M-4: el montaje funciona con allowInternetAccess: false", async () => {
      const sandbox = await createMountSandbox(false);
      e2e.created.push(sandbox);
      const result = await sandbox.commands.run("ls /mnt/ro");
      expect(result.exitCode).toBe(0);
    });
  },
);
