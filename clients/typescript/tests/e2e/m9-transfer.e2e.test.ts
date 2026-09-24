/**
 * M9 `m9-file-transfer` (ADR-010) contra AWS real con el SDK TypeScript, el
 * espejo de `clients/python/tests/e2e/test_m9_transfer.py`: `fetch` PUT y GET
 * de `uploadUrl`/`downloadUrl`, `files.uploadUrl` + `wait()`, la barrera de
 * lectura tras la subida, 200 MB de `writeFiles`/`read` por S3 frente a la
 * línea base gRPC, `gzip` y metadatos. Imagen M9 por defecto, sin execution
 * role. Variables: `RAYITO_E2E=1`, `RAYITO_TEMPLATE`,
 * `RAYITO_E2E_TRANSFER_BUCKET` (p. ej. `amzn-s3-demo-bucket`),
 * `RAYITO_E2E_TRANSFER_PREFIX` (por defecto `rayito-transfer`) y, si el
 * bucket no está en la región del sandbox, `RAYITO_E2E_TRANSFER_REGION`. La
 * limpieza usa `@aws-sdk/client-s3` con las credenciales del desarrollador:
 * borra los objetos de descarga (el SDK nunca los borra) y aborta toda subida
 * multiparte bajo `<prefijo>/<sandboxId>/`.
 */

import { createHash, randomBytes } from "node:crypto";
import {
  AbortMultipartUploadCommand,
  DeleteObjectCommand,
  GetObjectCommand,
  HeadObjectCommand,
  ListMultipartUploadsCommand,
  PutObjectCommand,
  S3Client,
} from "@aws-sdk/client-s3";
import { afterAll, describe, expect, test } from "vitest";
import { type DownloadLink, Sandbox, type UploadTicket } from "../../src/index.js";
import { e2eEnabled, useE2E } from "./helpers.js";

const BUCKET_VAR = "RAYITO_E2E_TRANSFER_BUCKET";
const PREFIX_VAR = "RAYITO_E2E_TRANSFER_PREFIX";
const REGION_VAR = "RAYITO_E2E_TRANSFER_REGION";
const DEFAULT_PREFIX = "rayito-transfer";
const SANDBOX_TIMEOUT_MS = 1_800_000;
const MB = 1_000_000;
const MIB = 1_048_576;
const HOME = "/home/user";
const UPLOAD_BYTES = 10 * MIB;
const DOWNLOAD_BYTES = 50 * MB;
const LARGE_BYTES = 200 * MB;
const BASELINE_BYTES = 20 * MB;
const GZIP_BYTES = 20 * MB;
const MIN_S3_SPEEDUP = 10;
const LINK_SHARE = 0.7;
const MIN_GZIP_SPEEDUP = 3;
const USER_UID = "1000";

const enabled = e2eEnabled() && Boolean(process.env[BUCKET_VAR]);

function sha256(data: Uint8Array): string {
  return createHash("sha256").update(data).digest("hex");
}

function seconds(sinceMs: number): number {
  return (performance.now() - sinceMs) / 1000;
}

function megabytesPerSecond(bytes: number, elapsedSeconds: number): number {
  return bytes / MB / elapsedSeconds;
}

function reportRate(label: string, bytes: number, elapsedSeconds: number): number {
  const rate = megabytesPerSecond(bytes, elapsedSeconds);
  console.log(`\n[m9-transfer] ${label}: ${rate.toFixed(2)} MB/s (${elapsedSeconds.toFixed(2)} s)`);
  return rate;
}

function reportSeconds(label: string, elapsedSeconds: number): void {
  console.log(`\n[m9-transfer] ${label}: ${elapsedSeconds.toFixed(3)} s`);
}

/** La clave de una URL prefirmada de host virtual es su ruta sin la barra inicial. */
function keyOf(url: string): string {
  return decodeURIComponent(new URL(url).pathname.slice(1));
}

function compressibleText(bytes: number): string {
  const line = "rayito m9 transfer: el mismo texto una y otra vez\n";
  return line.repeat(Math.ceil(bytes / line.length)).slice(0, bytes);
}

async function putTo(ticket: UploadTicket | string, data: Uint8Array): Promise<Response> {
  return fetch(String(ticket), {
    method: "PUT",
    body: data,
    headers: { "Content-Type": "application/octet-stream" },
  });
}

/** `HeadObject` de una clave ausente llega como el error `NotFound` del SDK (404 sin cuerpo). */
async function objectExists(s3: S3Client, bucket: string, key: string): Promise<boolean> {
  try {
    await s3.send(new HeadObjectCommand({ Bucket: bucket, Key: key }));
    return true;
  } catch (error) {
    if ((error as Error).name === "NotFound") {
      return false;
    }
    throw error;
  }
}

/**
 * D24 asks for 10x the gRPC baseline; a developer link slower than that caps
 * what S3 routing can show, so the floor is the lower of the two.
 */
function routedFloor(grpcRate: number, linkRate: number): number {
  return Math.min(MIN_S3_SPEEDUP * grpcRate, LINK_SHARE * linkRate);
}

/** Direct `PutObject`/`GetObject` MB/s between this machine and S3: the ceiling of any routed transfer. */
async function linkRates(
  s3: S3Client,
  bucket: string,
  key: string,
  bytes: number,
): Promise<[number, number]> {
  const data = new Uint8Array(randomBytes(bytes));
  let started = performance.now();
  await s3.send(new PutObjectCommand({ Bucket: bucket, Key: key, Body: data }));
  const put = reportRate("developer -> S3 link PUT", bytes, seconds(started));
  started = performance.now();
  const got = await s3.send(new GetObjectCommand({ Bucket: bucket, Key: key }));
  await got.Body?.transformToByteArray();
  const get = reportRate("S3 -> developer link GET", bytes, seconds(started));
  await s3.send(new DeleteObjectCommand({ Bucket: bucket, Key: key }));
  return [put, get];
}

async function shellOutput(sandbox: Sandbox, command: string): Promise<string> {
  const result = await sandbox.commands.run(command, { timeoutMs: 300_000 });
  return result.stdout.trim();
}

async function sha256InVm(sandbox: Sandbox, path: string): Promise<string> {
  return (await shellOutput(sandbox, `sha256sum ${path}`)).split(/\s+/)[0] ?? "";
}

describe.skipIf(!enabled)("M9 file transfer (TypeScript, real AWS)", () => {
  const context = useE2E();
  const bucket = process.env[BUCKET_VAR] ?? "";
  const prefix = process.env[PREFIX_VAR] || DEFAULT_PREFIX;
  const bucketRegion = process.env[REGION_VAR] || undefined;
  const downloadKeys: string[] = [];
  const sandboxIds: string[] = [];
  let s3: S3Client | undefined;

  function s3Client(): S3Client {
    s3 ??= new S3Client({ region: bucketRegion ?? context.controlPlane.region });
    return s3;
  }

  async function createStagedSandbox(): Promise<Sandbox> {
    const started = performance.now();
    const sandbox = await Sandbox.create({
      template: context.templateArn,
      timeoutMs: SANDBOX_TIMEOUT_MS,
      idle: null,
      ingress: ["ALL_INGRESS"],
      logging: "disabled",
      controlPlane: context.controlPlane,
      transfer: { bucket, prefix, region: bucketRegion },
    });
    context.created.push(sandbox);
    context.bootTimings.set(sandbox.sandboxId, seconds(started));
    sandboxIds.push(sandbox.sandboxId);
    expect(sandbox.info.executionRoleArn).toBeUndefined();
    return sandbox;
  }

  function remember(link: DownloadLink): DownloadLink {
    downloadKeys.push(keyOf(link.url));
    return link;
  }

  afterAll(async () => {
    const client = s3Client();
    for (const key of downloadKeys) {
      await client.send(new DeleteObjectCommand({ Bucket: bucket, Key: key }));
    }
    for (const sandboxId of sandboxIds) {
      const listed = await client.send(
        new ListMultipartUploadsCommand({ Bucket: bucket, Prefix: `${prefix}/${sandboxId}/` }),
      );
      for (const upload of listed.Uploads ?? []) {
        await client.send(
          new AbortMultipartUploadCommand({
            Bucket: bucket,
            Key: upload.Key,
            UploadId: upload.UploadId,
          }),
        );
      }
    }
    client.destroy();
  });

  test("files.uploadUrl + fetch PUT + wait(): same sha256, owned by the user, staging object gone", async () => {
    const sandbox = await createStagedSandbox();
    const path = `${HOME}/upload.bin`;
    const data = new Uint8Array(randomBytes(UPLOAD_BYTES));
    const ticket = await sandbox.files.uploadUrl(path);
    expect(ticket.method).toBe("PUT");
    expect(new URL(ticket.url).searchParams.get("X-Amz-Algorithm")).toBe("AWS4-HMAC-SHA256");
    const started = performance.now();
    const response = await putTo(ticket, data);
    expect(response.status).toBe(200);
    const entry = await ticket.wait({ timeoutMs: 300_000 });
    reportRate("uploadUrl 10 MiB: PUT + import", UPLOAD_BYTES, seconds(started));
    expect(entry.size).toBe(UPLOAD_BYTES);
    expect(await sha256InVm(sandbox, path)).toBe(sha256(data));
    expect(await shellOutput(sandbox, `stat -c %u ${path}`)).toBe(USER_UID);
    expect(await objectExists(s3Client(), bucket, keyOf(ticket.url))).toBe(false);
    expect((await ticket.status()).phase).toBe("done");
  });

  test("barrier: sandbox.uploadUrl + PUT, then files.read and commands.run without wait()", async () => {
    const sandbox = await createStagedSandbox();
    const first = new Uint8Array(randomBytes(MIB));
    const firstUrl = await sandbox.uploadUrl(`${HOME}/barrier-read.bin`);
    expect(typeof firstUrl).toBe("string");
    const putStarted = performance.now();
    expect((await putTo(firstUrl, first)).status).toBe(200);
    const read = await sandbox.files.read(`${HOME}/barrier-read.bin`, { format: "bytes" });
    reportSeconds("PUT -> visible through files.read (barrier)", seconds(putStarted));
    expect(sha256(read)).toBe(sha256(first));
    const second = new Uint8Array(randomBytes(MIB));
    const secondUrl = await sandbox.uploadUrl(`${HOME}/barrier-run.bin`);
    expect((await putTo(secondUrl, second)).status).toBe(200);
    expect(await sha256InVm(sandbox, `${HOME}/barrier-run.bin`)).toBe(sha256(second));
  });

  test("downloadUrl of 50 MB: plain fetch, Range 206, snapshot survives an overwrite", async () => {
    const sandbox = await createStagedSandbox();
    const path = `${HOME}/download.bin`;
    await shellOutput(sandbox, `head -c ${DOWNLOAD_BYTES} /dev/urandom > ${path}`);
    const expected = await sha256InVm(sandbox, path);
    const link = remember(await sandbox.files.downloadUrl(path, { filename: "descarga.bin" }));
    expect(link.size).toBe(DOWNLOAD_BYTES);
    expect(link.sha256).toBe(expected);
    const started = performance.now();
    const body = new Uint8Array(await (await fetch(link.url)).arrayBuffer());
    reportRate("downloadUrl 50 MB: export + GET", DOWNLOAD_BYTES, seconds(started));
    expect(sha256(body)).toBe(expected);
    const ranged = await fetch(link.url, { headers: { Range: "bytes=0-99" } });
    expect(ranged.status).toBe(206);
    expect((await ranged.arrayBuffer()).byteLength).toBe(100);
    await shellOutput(sandbox, `head -c 1000 /dev/urandom > ${path}`);
    const again = new Uint8Array(await (await fetch(link.url)).arrayBuffer());
    expect(sha256(again)).toBe(expected);
    const shim = await sandbox.downloadUrl(path);
    downloadKeys.push(keyOf(shim));
    expect((await (await fetch(shim)).arrayBuffer()).byteLength).toBe(1000);
  });

  test("200 MB writeFiles/read go through S3 at 10x the gRPC baseline or near the link rate", async () => {
    const sandbox = await createStagedSandbox();
    const plain = await Sandbox.connect(sandbox.sandboxId, {
      accessToken: sandbox.accessToken,
      controlPlane: context.controlPlane,
      transfer: null,
    });
    try {
      expect(plain.transfer).toBeUndefined();
      const baseline = new Uint8Array(randomBytes(BASELINE_BYTES));
      let started = performance.now();
      await plain.files.write(`${HOME}/baseline.bin`, baseline);
      const grpcWrite = reportRate("gRPC write 20 MB (baseline)", BASELINE_BYTES, seconds(started));
      started = performance.now();
      const baselineRead = await plain.files.read(`${HOME}/baseline.bin`, { format: "bytes" });
      const grpcRead = reportRate("gRPC read 20 MB (baseline)", BASELINE_BYTES, seconds(started));
      expect(sha256(baselineRead)).toBe(sha256(baseline));

      const large = new Uint8Array(randomBytes(LARGE_BYTES));
      started = performance.now();
      const [entry] = await sandbox.files.writeFiles([{ path: `${HOME}/large.bin`, data: large }]);
      const s3Write = reportRate("S3 write 200 MB", LARGE_BYTES, seconds(started));
      expect(entry?.size).toBe(LARGE_BYTES);
      started = performance.now();
      const back = await sandbox.files.read(`${HOME}/large.bin`, { format: "bytes" });
      const s3Read = reportRate("S3 read 200 MB", LARGE_BYTES, seconds(started));
      expect(sha256(back)).toBe(sha256(large));
      const [linkPut, linkGet] = await linkRates(
        s3Client(),
        bucket,
        `${prefix}/${sandbox.sandboxId}/link-probe`,
        BASELINE_BYTES,
      );
      expect(s3Write).toBeGreaterThanOrEqual(routedFloor(grpcWrite, linkPut));
      expect(s3Read).toBeGreaterThanOrEqual(routedFloor(grpcRead, linkGet));
    } finally {
      plain.close();
    }
  });

  test("gzip round trip of 20 MB of text: identical and at least 3x the uncompressed rate", async () => {
    const sandbox = await createStagedSandbox();
    const plain = await Sandbox.connect(sandbox.sandboxId, {
      accessToken: sandbox.accessToken,
      controlPlane: context.controlPlane,
      transfer: null,
    });
    try {
      const text = compressibleText(GZIP_BYTES);
      let started = performance.now();
      await plain.files.write(`${HOME}/plain.txt`, text);
      await plain.files.read(`${HOME}/plain.txt`);
      const identity = reportRate("write+read 20 MB identity", 2 * GZIP_BYTES, seconds(started));
      started = performance.now();
      await plain.files.write(`${HOME}/gzip.txt`, text, { gzip: true });
      const back = await plain.files.read(`${HOME}/gzip.txt`, { gzip: true });
      const gzip = reportRate("write+read 20 MB gzip", 2 * GZIP_BYTES, seconds(started));
      expect(back).toBe(text);
      expect(gzip).toBeGreaterThanOrEqual(MIN_GZIP_SPEEDUP * identity);
    } finally {
      plain.close();
    }
  });

  test("metadata: stored lowercased, listed, cleared by an overwrite", async () => {
    const sandbox = await createStagedSandbox();
    const path = `${HOME}/meta.txt`;
    await sandbox.files.write(path, "x", { metadata: { Owner: "alice" } });
    expect((await sandbox.files.getInfo(path)).metadata).toEqual({ owner: "alice" });
    const listed = await sandbox.files.list(HOME);
    expect(listed.find((entry) => entry.path === path)?.metadata).toEqual({ owner: "alice" });
    await sandbox.files.write(path, "y");
    expect((await sandbox.files.getInfo(path)).metadata).toEqual({});
  });
});
