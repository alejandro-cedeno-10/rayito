/**
 * M7 track 4 (`m7-s3-persistence`): la ida y vuelta de un `HOME` por S3 a
 * través del SDK TypeScript contra AWS real — 20 MB generados dentro de la
 * VM, `checkpointFiles()`, `kill()`, `create({ persist })` → mismo sha256, y
 * `reincarnate()`. Mismas variables que el e2e Python
 * (`RAYITO_TEMPLATE_CAPS`, `RAYITO_EXECUTION_ROLE_ARN`, `RAYITO_PERSIST_BUCKET`,
 * `RAYITO_PERSIST_PREFIX`). La limpieza de S3 usa la CLI del desarrollador
 * (`aws s3 rm --recursive` + abort de multipart uploads huérfanos): el SDK no
 * depende de `@aws-sdk/client-s3` y el execution role no tiene `s3:DeleteObject`.
 */

import { execFile } from "node:child_process";
import { randomUUID } from "node:crypto";
import { promisify } from "node:util";
import { afterAll, describe, expect, test } from "vitest";
import { NotFoundError, S3Prefix, Sandbox } from "../../src/index.js";
import { e2eEnabled, report, seconds, useE2E } from "./helpers.js";

const CAPS_TEMPLATE_VAR = "RAYITO_TEMPLATE_CAPS";
const BUCKET_VAR = "RAYITO_PERSIST_BUCKET";
const PREFIX_VAR = "RAYITO_PERSIST_PREFIX";
const BLOB_BYTES = 20 * 1024 * 1024;
const SANDBOX_TIMEOUT_MS = 1_800_000;
const PERSIST_TIMEOUT_MS = 300_000;
const SEED_SCRIPT =
  "mkdir -p /home/user/data /home/user/skipme " +
  `&& head -c ${BLOB_BYTES} /dev/urandom > /home/user/data/blob.bin ` +
  "&& printf 'ts\\n' > /home/user/notes.txt && printf 'skip\\n' > /home/user/skipme/s";

const enabled =
  e2eEnabled() && Boolean(process.env[CAPS_TEMPLATE_VAR]) && Boolean(process.env[BUCKET_VAR]);

const run = promisify(execFile);

async function awsCli(args: readonly string[]): Promise<string> {
  const { stdout } = await run("aws", [...args], { shell: true });
  return stdout;
}

async function cleanPrefix(bucket: string, keyPrefix: string): Promise<void> {
  await awsCli(["s3", "rm", `s3://${bucket}/${keyPrefix}/`, "--recursive"]);
  const uploads = await awsCli([
    "s3api",
    "list-multipart-uploads",
    "--bucket",
    bucket,
    "--prefix",
    `${keyPrefix}/`,
    "--query",
    "Uploads[].[Key,UploadId]",
    "--output",
    "text",
  ]);
  for (const line of uploads.split("\n")) {
    const [key, uploadId] = line.trim().split(/\s+/);
    if (key && uploadId && key !== "None") {
      await awsCli([
        "s3api",
        "abort-multipart-upload",
        "--bucket",
        bucket,
        "--key",
        key,
        "--upload-id",
        uploadId,
      ]);
    }
  }
}

describe.skipIf(!enabled)("M7 persistence (TypeScript)", () => {
  const context = useE2E(CAPS_TEMPLATE_VAR);
  const bucket = process.env[BUCKET_VAR] ?? "";
  const prefix = process.env[PREFIX_VAR] || "rayito-e2e";
  const names: string[] = [];

  afterAll(async () => {
    for (const name of names) {
      await cleanPrefix(bucket, `${prefix}/${name}`);
    }
    console.log(`\n[m7-persist] cleaned ${names.length} prefixes under s3://${bucket}/${prefix}/`);
  });

  function fresh(): S3Prefix {
    const name = `e2e-ts-${randomUUID().replaceAll("-", "")}`;
    names.push(name);
    return new S3Prefix({ bucket, prefix, name });
  }

  async function createPersisted(persist: S3Prefix): Promise<Sandbox> {
    const started = performance.now();
    const sandbox = await Sandbox.create({
      template: context.templateArn,
      timeoutMs: SANDBOX_TIMEOUT_MS,
      idle: null,
      executionRoleArn: context.settings.executionRoleArn,
      ingress: ["ALL_INGRESS"],
      logging: context.settings.logging,
      controlPlane: context.controlPlane,
      persist,
      persistTimeoutMs: PERSIST_TIMEOUT_MS,
    });
    context.created.push(sandbox);
    report(`${sandbox.sandboxId}: create({ persist }) incl. restore`, seconds(started));
    return sandbox;
  }

  async function sha256(sandbox: Sandbox, path: string): Promise<string> {
    const result = await sandbox.commands.run(`sha256sum /home/user/${path}`, {
      timeoutMs: 60_000,
    });
    return result.stdout.split(/\s+/)[0] ?? "";
  }

  test("20 MB round trip, restore on create and reincarnate", async () => {
    const persist = fresh();
    const first = await createPersisted(persist);
    expect(first.persist?.equals(persist)).toBe(true);
    expect(first.lastRestore).toBeUndefined();
    await first.commands.run(SEED_SCRIPT, { timeoutMs: 120_000 });
    const blobBefore = await sha256(first, "data/blob.bin");
    let started = performance.now();
    const checkpoint = await first.checkpointFiles({ exclude: ["skipme"] });
    report(
      `checkpoint #1: ${checkpoint.files} files, ${checkpoint.bytesRead} bytes, ${checkpoint.archiveBytes} archive bytes`,
      seconds(started),
    );
    expect(checkpoint.bytesRead).toBeGreaterThanOrEqual(BLOB_BYTES);
    started = performance.now();
    const warm = await first.checkpointFiles({ exclude: ["skipme"] });
    report("checkpoint #2 (warm)", seconds(started));
    expect(warm.files).toBe(checkpoint.files);
    await first.kill();

    const second = await createPersisted(persist);
    expect(second.lastRestore?.sha256).toBe(warm.sha256);
    expect(await sha256(second, "data/blob.bin")).toBe(blobBefore);
    const absent = await second.commands.run("test ! -e /home/user/skipme/s && echo absent", {
      timeoutMs: 30_000,
    });
    expect(absent.stdout.trim()).toBe("absent");
    await expect(second.restoreFiles({ source: fresh() })).rejects.toBeInstanceOf(NotFoundError);

    await second.commands.run("printf 'marker\\n' > /home/user/marker.txt", { timeoutMs: 30_000 });
    started = performance.now();
    const reborn = await second.reincarnate({ exclude: ["skipme"] });
    context.created.push(reborn);
    report("reincarnate()", seconds(started));
    expect(reborn.sandboxId).not.toBe(second.sandboxId);
    expect(reborn.persist?.equals(persist)).toBe(true);
    expect(reborn.lastRestore).toBeDefined();
    const marker = await reborn.commands.run("cat /home/user/marker.txt", { timeoutMs: 30_000 });
    expect(marker.stdout.trim()).toBe("marker");
    expect(await sha256(reborn, "data/blob.bin")).toBe(blobBefore);
    const old = await context.controlPlane.getMicrovm(second.sandboxId);
    expect(["TERMINATING", "TERMINATED"]).toContain(old.state);
    await reborn.kill();
  });
});
