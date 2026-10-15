/**
 * M9 `m9-file-transfer` (ADR-010) contra AWS real con el SDK TypeScript, el
 * espejo de `clients/python/tests/e2e/test_m9_transfer.py`: `fetch` PUT y GET
 * de `uploadUrl`/`downloadUrl`, `files.uploadUrl` + `wait()`, la barrera de
 * lectura tras la subida, 200 MB de `writeFiles`/`read` por S3 frente a la
 * línea base gRPC, `gzip`, metadatos y los topes de tamaño (`maxBytes` y formulario POST). Imagen M9 por defecto, sin execution
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
  ListObjectsV2Command,
  PutObjectCommand,
  S3Client,
} from "@aws-sdk/client-s3";
import { afterAll, describe, expect, test } from "vitest";
import { Sandbox as ShimSandbox } from "../../src/e2b/index.js";
import {
  type DownloadLink,
  InvalidArgumentError,
  type Logger,
  Sandbox,
  TimeoutError,
  type UploadTicket,
} from "../../src/index.js";
import { e2eEnabled, sleep, useE2E } from "./helpers.js";
import { rayitoLogLines } from "./logs.js";

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
const MULTIPART_THRESHOLD_BYTES = 64 * MIB;
const MULTIPART_FILE_COUNT = 100;
const EXPIRING_TICKET_SECONDS = 5;
const EXPIRY_WAIT_TIMEOUT_MS = 60_000;
const UPLOAD_WAIT_TIMEOUT_MS = 300_000;
const COMMAND_TIMEOUT_MS = 300_000;
const EXPIRY_MIN_S = 3;
const EXPIRY_MAX_S = 12;
const EXPIRED_LINK_SECONDS = 1;
const EXPIRED_LINK_SETTLE_MS = 3_000;
const EXPIRED_STATUS = 403;
const EXPIRED_BODY = "Request has expired";
const SHIM_PAYLOAD_BYTES = 4096;
const INVALID_SIGNATURE_EXPIRATION = -1;
const CAPPED_UPLOAD_BYTES = 2 * MIB;
const FORM_CAP_BYTES = 1024;
const FORM_OVERSIZED_BYTES = 2048;
const FORM_SMALL_BYTES = 512;
const FORM_REJECTED_STATUS = 400;
const FORM_ACCEPTED_STATUSES = [200, 204];
const TOO_LARGE_CODE = "too_large";
const ENTITY_TOO_LARGE = "EntityTooLarge";
const TRANSFER_ENV_VARS = [
  "RAYITO_TRANSFER_BUCKET",
  "RAYITO_TRANSFER_PREFIX",
  "RAYITO_TRANSFER_REGION",
] as const;
const SECRET_DIRECTORY = "secret-dir";

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

/** Envía `data` como el campo `file` de un formulario POST prefirmado, tras sus campos. */
async function postForm(ticket: UploadTicket, data: Uint8Array): Promise<Response> {
  const form = new FormData();
  for (const [name, value] of Object.entries(ticket.fields)) {
    form.append(name, value);
  }
  form.append("file", new Blob([data]), "f");
  return fetch(ticket.url, { method: "POST", body: form });
}

async function shellOutput(sandbox: Sandbox, command: string): Promise<string> {
  const result = await sandbox.commands.run(command, { timeoutMs: COMMAND_TIMEOUT_MS });
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

  async function createStagedSandbox(
    options: {
      readonly multipartThresholdBytes?: number;
      readonly executionRoleArn?: string;
      readonly logger?: Logger;
    } = {},
  ): Promise<Sandbox> {
    const started = performance.now();
    const sandbox = await Sandbox.create({
      template: context.templateArn,
      timeoutMs: SANDBOX_TIMEOUT_MS,
      idle: null,
      ingress: ["ALL_INGRESS"],
      logging: options.executionRoleArn === undefined ? "disabled" : "cloudwatch",
      controlPlane: context.controlPlane,
      transfer: {
        bucket,
        prefix,
        region: bucketRegion,
        ...(options.multipartThresholdBytes === undefined
          ? {}
          : { multipartThresholdBytes: options.multipartThresholdBytes }),
      },
      ...(options.executionRoleArn === undefined
        ? {}
        : { executionRoleArn: options.executionRoleArn }),
      ...(options.logger === undefined ? {} : { logger: options.logger }),
    });
    context.created.push(sandbox);
    context.bootTimings.set(sandbox.sandboxId, seconds(started));
    sandboxIds.push(sandbox.sandboxId);
    if (options.executionRoleArn === undefined) {
      expect(sandbox.info.executionRoleArn).toBeUndefined();
    }
    return sandbox;
  }

  async function openUploads(sandboxId: string): Promise<number> {
    const listed = await s3Client().send(
      new ListMultipartUploadsCommand({ Bucket: bucket, Prefix: `${prefix}/${sandboxId}/` }),
    );
    return (listed.Uploads ?? []).length;
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
    const entry = await ticket.wait({ timeoutMs: UPLOAD_WAIT_TIMEOUT_MS });
    reportRate("uploadUrl 10 MiB: PUT + import", UPLOAD_BYTES, seconds(started));
    expect(entry.size).toBe(UPLOAD_BYTES);
    expect(await sha256InVm(sandbox, path)).toBe(sha256(data));
    expect(await shellOutput(sandbox, `stat -c %u ${path}`)).toBe(USER_UID);
    expect(await objectExists(s3Client(), bucket, keyOf(ticket.url))).toBe(false);
    expect((await ticket.status()).phase).toBe("done");
  });

  test("size caps: maxBytes gives too_large and form gives EntityTooLarge", async () => {
    const sandbox = await createStagedSandbox();
    const capped = await sandbox.files.uploadUrl(`${HOME}/m9/capped.bin`, { maxBytes: MIB });
    expect((await putTo(capped, new Uint8Array(randomBytes(CAPPED_UPLOAD_BYTES)))).status).toBe(
      200,
    );
    const failure = await capped
      .wait({ timeoutMs: UPLOAD_WAIT_TIMEOUT_MS })
      .catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(InvalidArgumentError);
    expect((failure as Error).message.startsWith(TOO_LARGE_CODE)).toBe(true);
    expect(await sandbox.files.exists(`${HOME}/m9/capped.bin`)).toBe(false);
    const staged = await s3Client().send(
      new ListObjectsV2Command({ Bucket: bucket, Prefix: `${prefix}/${sandbox.sandboxId}/` }),
    );
    expect(staged.Contents ?? []).toEqual([]);

    const form = await sandbox.files.uploadUrl(`${HOME}/m9/form.bin`, {
      form: true,
      maxBytes: FORM_CAP_BYTES,
    });
    const oversized = await postForm(form, new Uint8Array(randomBytes(FORM_OVERSIZED_BYTES)));
    expect(oversized.status).toBe(FORM_REJECTED_STATUS);
    expect(await oversized.text()).toContain(ENTITY_TOO_LARGE);
    const small = new Uint8Array(randomBytes(FORM_SMALL_BYTES));
    expect(FORM_ACCEPTED_STATUSES).toContain((await postForm(form, small)).status);
    expect((await form.wait({ timeoutMs: UPLOAD_WAIT_TIMEOUT_MS })).size).toBe(FORM_SMALL_BYTES);
    const stored = await sandbox.files.read(`${HOME}/m9/form.bin`, { format: "bytes" });
    expect(sha256(stored)).toBe(sha256(small));
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
  test("multipart export of 100 MB leaves no open upload behind", async () => {
    const sandbox = await createStagedSandbox({
      multipartThresholdBytes: MULTIPART_THRESHOLD_BYTES,
    });
    const path = `${HOME}/m9/parts.bin`;
    await shellOutput(
      sandbox,
      `mkdir -p ${HOME}/m9 && dd if=/dev/urandom of=${path} bs=${MB} count=${MULTIPART_FILE_COUNT} status=none`,
    );
    const started = performance.now();
    const link = remember(await sandbox.files.downloadUrl(path));
    reportSeconds("multipart export 100 MB", seconds(started));
    const body = new Uint8Array(await (await fetch(link.url)).arrayBuffer());
    expect(sha256(body)).toBe(await sha256InVm(sandbox, path));
    expect(await openUploads(sandbox.sandboxId)).toBe(0);
  });

  test("expiry: an unused upload ticket times out and an expired download link is refused", async () => {
    const sandbox = await createStagedSandbox();
    const ticket = await sandbox.files.uploadUrl(`${HOME}/m9/never.bin`, {
      expiresIn: EXPIRING_TICKET_SECONDS,
    });
    const started = performance.now();
    await expect(ticket.wait({ timeoutMs: EXPIRY_WAIT_TIMEOUT_MS })).rejects.toBeInstanceOf(
      TimeoutError,
    );
    const elapsed = seconds(started);
    reportSeconds("uploadUrl(expiresIn: 5) -> TimeoutError", elapsed);
    expect(elapsed).toBeGreaterThanOrEqual(EXPIRY_MIN_S);
    expect(elapsed).toBeLessThanOrEqual(EXPIRY_MAX_S);
    await sandbox.files.write(`${HOME}/m9/short.txt`, "caduca");
    const link = remember(
      await sandbox.files.downloadUrl(`${HOME}/m9/short.txt`, { expiresIn: EXPIRED_LINK_SECONDS }),
    );
    await sleep(EXPIRED_LINK_SETTLE_MS);
    const response = await fetch(link.url);
    expect(response.status).toBe(EXPIRED_STATUS);
    expect(await response.text()).toContain(EXPIRED_BODY);
  });

  test("the E2B shim hands out upload and download URLs", async () => {
    const sandbox = await createStagedSandbox();
    const saved = new Map(TRANSFER_ENV_VARS.map((name) => [name, process.env[name]]));
    process.env.RAYITO_TRANSFER_BUCKET = bucket;
    process.env.RAYITO_TRANSFER_PREFIX = prefix;
    if (bucketRegion !== undefined) {
      process.env.RAYITO_TRANSFER_REGION = bucketRegion;
    }
    try {
      const shim = await ShimSandbox.connect(sandbox.sandboxId, {
        accessToken: sandbox.accessToken,
        controlPlane: context.controlPlane,
      });
      const path = `${HOME}/m9/e2b.bin`;
      const data = new Uint8Array(randomBytes(SHIM_PAYLOAD_BYTES));
      const url = await shim.uploadUrl(path);
      expect((await putTo(url, data)).status).toBe(200);
      expect(sha256(await shim.files.read(path, { format: "bytes" }))).toBe(sha256(data));
      const download = await shim.downloadUrl(path);
      downloadKeys.push(keyOf(download));
      expect(sha256(new Uint8Array(await (await fetch(download)).arrayBuffer()))).toBe(
        sha256(data),
      );
      await expect(
        shim.uploadUrl(path, { useSignatureExpiration: INVALID_SIGNATURE_EXPIRATION }),
      ).rejects.toBeInstanceOf(InvalidArgumentError);
    } finally {
      for (const [name, value] of saved) {
        if (value === undefined) {
          delete process.env[name];
        } else {
          process.env[name] = value;
        }
      }
    }
  });

  test("log hygiene: neither rayd nor the SDK log URLs, bucket, prefix or paths", async (ctx) => {
    const role = context.settings.executionRoleArn;
    if (role === undefined) {
      ctx.skip("exporta RAYITO_EXECUTION_ROLE_ARN (el rol de sólo logs)");
      return;
    }
    const sdkLines: string[] = [];
    const record = (message: string, fields?: Readonly<Record<string, unknown>>): void => {
      sdkLines.push(fields === undefined ? message : `${message} ${JSON.stringify(fields)}`);
    };
    const sandbox = await createStagedSandbox({
      executionRoleArn: role,
      logger: { debug: record, info: record, warn: record, error: record },
    });
    const ticket = await sandbox.files.uploadUrl(`${HOME}/m9/${SECRET_DIRECTORY}/in.bin`);
    expect((await putTo(ticket, new TextEncoder().encode("hola"))).status).toBe(200);
    await ticket.wait({ timeoutMs: UPLOAD_WAIT_TIMEOUT_MS });
    const link = remember(await sandbox.files.downloadUrl(`${HOME}/m9/${SECRET_DIRECTORY}/in.bin`));
    expect(await (await fetch(link.url)).text()).toBe("hola");
    const raydLines = await rayitoLogLines(sandbox, context.settings.region);
    console.log(`\n[m9-transfer] rayd log lines read: ${raydLines.length}`);
    expect(raydLines.length, "sin eventos de rayd en CloudWatch").toBeGreaterThan(0);
    const rayd = raydLines.join("\n");
    for (const text of [rayd, sdkLines.join("\n")]) {
      for (const forbidden of ["X-Amz-Signature", bucket, `${prefix}/`, SECRET_DIRECTORY]) {
        expect(text).not.toContain(forbidden);
      }
    }
  });
});
