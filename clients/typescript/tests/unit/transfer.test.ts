import { createHash } from "node:crypto";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { inspect } from "node:util";
import { create } from "@bufbuild/protobuf";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import {
  AuthenticationError,
  DiskFullError,
  FileNotFoundError,
  FileUploadError,
  InvalidArgumentError,
  TimeoutError,
  TransferError,
  UnimplementedError,
} from "../../src/errors.js";
import { StreamErrorSchema } from "../../src/gen/rayito/v1/common_pb.js";
import {
  TransferDirection,
  TransferPhase,
  TransferStateSchema,
} from "../../src/gen/rayito/v1/filesystem_pb.js";
import type { S3Staging } from "../../src/models.js";
import { S3Prefix } from "../../src/sandbox/persistence.js";
import {
  contentDisposition,
  createS3Access,
  DownloadLink,
  effectiveExpiresIn,
  exportBudgetSeconds,
  failureFromState,
  partPlan,
  resolveS3Staging,
  type S3ClientOverrides,
  shouldRoute,
  stagingKey,
  transferStatusFromProto,
  type UploadTicket,
  uploadTicketHeaders,
  validateStagingAgainstPersist,
} from "../../src/sandbox/transfer.js";
import { SANDBOX_ID } from "./fake/control-plane.js";
import { FakeS3 } from "./fake/s3.js";
import {
  createTestSandbox,
  FakeControlPlane,
  IMAGE_ARN,
  Sandbox,
  type TestSandbox,
  waitUntil,
} from "./helpers.js";

const BUCKET = "amzn-s3-demo-bucket";
const MIB = 1024 * 1024;
const HOME = "/home/user";
const FAKE_CREDENTIALS = {
  accessKeyId: "AKIAIOSFODNN7EXAMPLE",
  secretAccessKey: "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
};
const UP_KEY = new RegExp(`^rayito-transfer/${SANDBOX_ID}/up/[0-9a-f]{32}$`);
const DOWN_KEY = new RegExp(`^rayito-transfer/${SANDBOX_ID}/down/[0-9a-f]{32}$`);

function sha256(data: Uint8Array): string {
  return createHash("sha256").update(data).digest("hex");
}

/** `toEqual` compara un `Uint8Array` elemento a elemento: con varios MiB son segundos y GB de heap. */
function expectSameBytes(actual: Uint8Array | undefined, expected: Uint8Array): void {
  expect(actual?.byteLength).toBe(expected.byteLength);
  expect(sha256(actual ?? new Uint8Array())).toBe(sha256(expected));
}

function patterned(size: number, seed = 7): Uint8Array {
  return new Uint8Array(size).map((_, index) => (index * seed + 13) & 0xff);
}

function staging(overrides: Partial<S3Staging> = {}) {
  const resolved = resolveS3Staging({ bucket: BUCKET, ...overrides }, {});
  if (resolved === undefined) {
    throw new Error("staging sin resolver");
  }
  return resolved;
}

function failedState(code: string, message: string, direction = TransferDirection.IMPORT) {
  return create(TransferStateSchema, {
    transferId: "t",
    direction,
    phase: TransferPhase.FAILED,
    error: create(StreamErrorSchema, { code, message }),
  });
}

const fakes: FakeS3[] = [];

beforeEach(() => {
  vi.stubEnv("RAYITO_TRANSFER_BUCKET", "");
});

afterEach(async () => {
  vi.unstubAllEnvs();
  for (const fake of fakes.splice(0)) {
    await fake.close();
  }
});

interface StagedSandbox extends TestSandbox {
  readonly s3: FakeS3;
}

async function stagedSandbox(overrides: Partial<S3Staging> = {}): Promise<StagedSandbox> {
  const s3 = await FakeS3.start();
  fakes.push(s3);
  const handle = await createTestSandbox({
    create: { transfer: { bucket: BUCKET, ...overrides } },
  });
  const clientOverrides: S3ClientOverrides = {
    endpoint: s3.endpoint,
    forcePathStyle: true,
    credentials: FAKE_CREDENTIALS,
  };
  Sandbox.coreOf(handle.sandbox).s3ClientOverrides = clientOverrides;
  return { ...handle, s3 };
}

async function putToTicket(ticket: UploadTicket, data: Uint8Array): Promise<Response> {
  return fetch(ticket.url, { method: "PUT", body: data, headers: ticket.headers });
}

async function postToTicket(ticket: UploadTicket, data: Uint8Array): Promise<Response> {
  const form = new FormData();
  for (const [name, value] of Object.entries(ticket.fields)) {
    form.append(name, value);
  }
  form.append("file", new Blob([data]));
  return fetch(ticket.url, { method: "POST", body: form });
}

describe("S3Staging", () => {
  test("defaults, env fallback and explicit disable", () => {
    expect(resolveS3Staging(undefined, {})).toBeUndefined();
    expect(resolveS3Staging(null, { RAYITO_TRANSFER_BUCKET: BUCKET })).toBeUndefined();
    expect(resolveS3Staging(undefined, { RAYITO_TRANSFER_BUCKET: "" })).toBeUndefined();
    expect(resolveS3Staging(undefined, { RAYITO_TRANSFER_BUCKET: BUCKET })).toEqual({
      bucket: BUCKET,
      prefix: "rayito-transfer",
      region: undefined,
      maxExpiresIn: 86_400,
      thresholdBytes: 8 * MIB,
      multipartThresholdBytes: 5 * 1024 * MIB,
    });
    expect(
      resolveS3Staging(undefined, {
        RAYITO_TRANSFER_BUCKET: BUCKET,
        RAYITO_TRANSFER_PREFIX: "tmp/xfer",
        RAYITO_TRANSFER_REGION: "eu-west-1",
      }),
    ).toMatchObject({ prefix: "tmp/xfer", region: "eu-west-1" });
    expect(Object.isFrozen(staging())).toBe(true);
  });

  test("bucket rules refuse dotted, punycode and access-point names without echoing them", () => {
    for (const bucket of ["my.bucket", "xn--bucket", "bucket-s3alias", "UPPER", "ab", "-lead"]) {
      let caught: unknown;
      try {
        staging({ bucket });
      } catch (error) {
        caught = error;
      }
      expect(caught, bucket).toBeInstanceOf(InvalidArgumentError);
      expect((caught as Error).message).not.toContain(bucket);
    }
  });

  test("prefix, region and numeric ranges", () => {
    for (const prefix of ["", "/a", "a/", "a//b", "a/../b", "./a", "rayito", "rayito/x", "a b"]) {
      expect(() => staging({ prefix }), prefix).toThrow(InvalidArgumentError);
    }
    expect(staging({ prefix: "rayito-transfer/sub" }).prefix).toBe("rayito-transfer/sub");
    expect(() => staging({ prefix: "a".repeat(257) })).toThrow(InvalidArgumentError);
    expect(() => staging({ region: "useast1" })).toThrow(InvalidArgumentError);
    expect(staging({ region: "us-gov-west-1" }).region).toBe("us-gov-west-1");
    expect(() => staging({ maxExpiresIn: 0 })).toThrow(InvalidArgumentError);
    expect(() => staging({ maxExpiresIn: 604_801 })).toThrow(InvalidArgumentError);
    expect(() => staging({ thresholdBytes: MIB - 1 })).toThrow(InvalidArgumentError);
    expect(() => staging({ thresholdBytes: 5 * 1024 * MIB + 1 })).toThrow(InvalidArgumentError);
    expect(() => staging({ multipartThresholdBytes: 16 * MIB - 1 })).toThrow(InvalidArgumentError);
    expect(staging({ multipartThresholdBytes: 64 * MIB }).multipartThresholdBytes).toBe(64 * MIB);
  });

  test("the persistence prefix must be component-disjoint on the same bucket", () => {
    const persist = new S3Prefix({ bucket: BUCKET, prefix: "data" });
    expect(() => validateStagingAgainstPersist(staging({ prefix: "data/tmp" }), persist)).toThrow(
      InvalidArgumentError,
    );
    expect(() => validateStagingAgainstPersist(staging({ prefix: "data" }), persist)).toThrow(
      InvalidArgumentError,
    );
    const nested = new S3Prefix({ bucket: BUCKET, prefix: "xfer/homes" });
    expect(() => validateStagingAgainstPersist(staging({ prefix: "xfer" }), nested)).toThrow(
      InvalidArgumentError,
    );
    validateStagingAgainstPersist(staging({ prefix: "data-tmp" }), persist);
    validateStagingAgainstPersist(
      staging({ prefix: "data/tmp" }),
      new S3Prefix({ bucket: "other-bucket", prefix: "data" }),
    );
    validateStagingAgainstPersist(staging(), undefined);
  });
});

describe("pure transfer helpers", () => {
  test("staging keys never carry the user's path", () => {
    const key = stagingKey("rayito-transfer", SANDBOX_ID, "up");
    expect(key).toMatch(UP_KEY);
    expect(key).not.toContain("secret-plan");
    expect(stagingKey("p", SANDBOX_ID, "down")).toMatch(
      new RegExp(`^p/${SANDBOX_ID}/down/[0-9a-f]{32}$`),
    );
    expect(stagingKey("p", SANDBOX_ID, "up")).not.toBe(stagingKey("p", SANDBOX_ID, "up"));
  });

  test("lifetimes: clamp to max_expires_in and seven days; refuse <= 0", () => {
    expect(effectiveExpiresIn(3600, staging())).toBe(3600);
    expect(effectiveExpiresIn(100_000, staging())).toBe(86_400);
    expect(effectiveExpiresIn(604_801, staging({ maxExpiresIn: 604_800 }))).toBe(604_800);
    for (const bad of [0, -1, 1.5, Number.NaN]) {
      expect(() => effectiveExpiresIn(bad, staging()), String(bad)).toThrow(InvalidArgumentError);
    }
    expect(exportBudgetSeconds(0)).toBe(900);
    expect(exportBudgetSeconds(1)).toBe(901);
    expect(exportBudgetSeconds(50_000_000)).toBe(950);
    expect(exportBudgetSeconds(10 ** 15)).toBe(604_800);
  });

  test("part plan: single PUT below the threshold, >= 8 MiB parts rounded to MiB, <= 1000 parts", () => {
    const plan = staging({ multipartThresholdBytes: 64 * MIB });
    expect(partPlan(64 * MIB - 1, plan)).toBeUndefined();
    expect(partPlan(64 * MIB, plan)).toEqual({ partSize: 8 * MIB, parts: 8 });
    expect(partPlan(100_000_000, plan)).toEqual({ partSize: 8 * MIB, parts: 12 });
    const huge = partPlan(30 * 1024 * MIB, plan);
    expect(huge?.partSize).toBe(31 * MIB);
    expect(huge?.parts).toBeLessThanOrEqual(1000);
    expect(partPlan(5 * 1024 * MIB - 1, staging())).toBeUndefined();
  });

  test("Content-Disposition: ascii fallback plus RFC 5987 percent-encoding", () => {
    expect(contentDisposition("report.csv")).toBe(
      "attachment; filename=\"report.csv\"; filename*=UTF-8''report.csv",
    );
    expect(contentDisposition('a"b\\c.txt')).toBe(
      "attachment; filename=\"a_b_c.txt\"; filename*=UTF-8''a%22b%5Cc.txt",
    );
    expect(contentDisposition("año (1)*.txt")).toBe(
      "attachment; filename=\"a__o (1)*.txt\"; filename*=UTF-8''a%C3%B1o%20%281%29%2A.txt",
    );
  });

  test("failure mapping follows the D13 table and keeps '<reason>: ' first", () => {
    const rows: Array<[string, new (...args: never[]) => Error]> = [
      ["deadline_exceeded", TimeoutError],
      ["invalid_argument", InvalidArgumentError],
      ["resource_exhausted", DiskFullError],
      ["permission_denied", AuthenticationError],
      ["not_found", FileNotFoundError],
    ];
    for (const [code, kind] of rows) {
      const error = failureFromState(failedState(code, `why: frase fija`), "import");
      expect(error, code).toBeInstanceOf(kind);
      expect(error.message.startsWith("why: ")).toBe(true);
    }
    expect(
      (
        failureFromState(
          failedState("permission_denied", "access_denied: x"),
          "export",
        ) as AuthenticationError
      ).proxyRejected,
    ).toBe(false);
    const tooLarge = failureFromState(
      failedState("invalid_argument", "too_large: supera"),
      "import",
    );
    expect(tooLarge.message.startsWith("too_large")).toBe(true);
    for (const code of [
      "failed_precondition",
      "unavailable",
      "cancelled",
      "internal",
      "brand_new",
    ]) {
      const upload = failureFromState(failedState(code, "r: m"), "import");
      expect(upload, code).toBeInstanceOf(FileUploadError);
      expect([(upload as FileUploadError).code, (upload as FileUploadError).reason]).toEqual([
        code,
        "r",
      ]);
      const download = failureFromState(failedState(code, "r: m"), "export");
      expect(download).toBeInstanceOf(TransferError);
      expect(download).not.toBeInstanceOf(FileUploadError);
    }
    const cancelled = create(TransferStateSchema, { phase: TransferPhase.CANCELLED });
    expect((failureFromState(cancelled, "import") as FileUploadError).reason).toBe("cancelled");
  });

  test("status, ticket headers and the routing predicate", () => {
    const state = create(TransferStateSchema, {
      transferId: "abc",
      direction: TransferDirection.EXPORT,
      phase: TransferPhase.RUNNING,
      bytesDone: 5n,
      bytesTotal: 10n,
      probes: 2,
    });
    expect(transferStatusFromProto(state)).toEqual({
      transferId: "abc",
      direction: "export",
      phase: "running",
      bytesDone: 5,
      bytesTotal: 10,
      probes: 2,
      errorCode: undefined,
      errorReason: undefined,
    });
    expect(uploadTicketHeaders(false)).toEqual({ "Content-Type": "application/octet-stream" });
    expect(uploadTicketHeaders(true)).toEqual({});
    const plan = staging({ thresholdBytes: MIB });
    expect(shouldRoute(MIB - 1, plan)).toBe(false);
    expect(shouldRoute(MIB, plan)).toBe(true);
    expect(shouldRoute(undefined, plan)).toBe(true);
    expect(shouldRoute(10 * MIB, undefined)).toBe(false);
  });

  test("presigning is SigV4 on the regional virtual host in us-east-1, unsigned body headers", async () => {
    const access = createS3Access("us-east-1", { credentials: FAKE_CREDENTIALS });
    const url = new URL(
      await access.presignPut(
        BUCKET,
        `rayito-transfer/${SANDBOX_ID}/up/${"a".repeat(32)}`,
        604_800,
      ),
    );
    expect(url.protocol).toBe("https:");
    expect(url.host).toBe(`${BUCKET}.s3.us-east-1.amazonaws.com`);
    expect(url.searchParams.get("X-Amz-Algorithm")).toBe("AWS4-HMAC-SHA256");
    expect(url.searchParams.get("X-Amz-Expires")).toBe("604800");
    expect(url.searchParams.get("X-Amz-SignedHeaders")).toBe("host");
    expect(url.searchParams.has("AWSAccessKeyId")).toBe(false);
    expect([...url.searchParams.keys()].some((name) => name.includes("checksum"))).toBe(false);
    const get = new URL(await access.presignGet(BUCKET, "k", 60, contentDisposition("a.txt")));
    expect(get.searchParams.get("response-content-disposition")).toBe(contentDisposition("a.txt"));
  });

  test("the S3 packages load lazily: no source file imports them statically", () => {
    const sourceRoot = fileURLToPath(new URL("../../src", import.meta.url));
    const staticImport =
      /^import\s+(?!type\s)[^;]*from\s+"@aws-sdk\/(client-s3|s3-request-presigner|s3-presigned-post|lib-storage)"/m;
    const offenders = readdirSync(sourceRoot, { recursive: true, encoding: "utf8" })
      .filter((name) => name.endsWith(".ts"))
      .filter((name) => staticImport.test(readFileSync(join(sourceRoot, name), "utf8")));
    expect(offenders).toEqual([]);
    expect(readFileSync(join(sourceRoot, "sandbox", "transfer.ts"), "utf8")).toContain(
      'import("@aws-sdk/client-s3")',
    );
  });
});

describe("upload tickets", () => {
  test("without staging upload_url raises UnimplementedError naming S3Staging and sends nothing", async () => {
    const { sandbox, rayd } = await createTestSandbox();
    await expect(sandbox.files.uploadUrl("a.txt")).rejects.toThrow(/S3Staging/);
    await expect(sandbox.files.downloadUrl("a.txt")).rejects.toBeInstanceOf(UnimplementedError);
    expect(rayd.filesystem.headers.GetTransfer).toBeUndefined();
    expect(rayd.filesystem.headers.StartImport).toBeUndefined();
  });

  test("a pre-M9 agent is detected once and refuses transfers, metadata and gzip writes", async () => {
    const { sandbox, rayd } = await stagedSandbox();
    rayd.filesystem.transfers.mode = "unimplemented";
    for (const attempt of [
      () => sandbox.files.uploadUrl("a.txt"),
      () => sandbox.files.write("m.txt", "x", { metadata: { a: "1" } }),
      () => sandbox.files.write("g.txt", "x", { gzip: true }),
    ]) {
      const error = await attempt().catch((caught: unknown) => caught);
      expect(error).toBeInstanceOf(UnimplementedError);
      expect((error as Error).message).toContain("actualiza la imagen");
    }
    expect(rayd.filesystem.headers.GetTransfer).toHaveLength(1);
    expect(rayd.filesystem.writeStreams).toHaveLength(0);
    await sandbox.files.write("plain.txt", "x");
    expect(rayd.filesystem.writeStreams).toHaveLength(1);
  });

  test("UNIMPLEMENTED from a transfer RPC after a passing probe maps to UnimplementedError", async () => {
    const { sandbox, rayd } = await stagedSandbox();
    await sandbox.files.write("seed.txt", "x", { metadata: { a: "1" } });
    rayd.filesystem.transfers.mode = "unimplemented";
    await expect(sandbox.files.uploadUrl("a.txt")).rejects.toThrow(/actualiza la imagen/);
  });

  test("PUT then wait: armed before returning, file written, staging object deleted", async () => {
    const { sandbox, rayd, s3 } = await stagedSandbox();
    const ticket = await sandbox.files.uploadUrl("up/data.bin", { user: "user" });
    const imports = rayd.filesystem.transfers.imports;
    expect(imports).toHaveLength(1);
    expect(imports[0]?.waitForObject).toBe(true);
    expect(imports[0]?.maxBytes).toBe(0n);
    expect(imports[0]?.object?.key).toMatch(UP_KEY);
    expect(imports[0]?.object?.key).not.toContain("data.bin");
    expect(imports[0]?.object?.region).toBe("us-east-1");
    expect(ticket.method).toBe("PUT");
    expect(ticket.path).toBe("up/data.bin");
    expect(ticket.transferId).toMatch(/^[0-9a-f]{32}$/);
    expect(ticket.headers).toEqual({ "Content-Type": "application/octet-stream" });
    const data = patterned(MIB);
    expect((await putToTicket(ticket, data)).status).toBe(200);
    const entry = await ticket.wait();
    expect(entry.path).toBe(`${HOME}/up/data.bin`);
    expect(entry.size).toBe(MIB);
    expectSameBytes(rayd.filesystem.fileBytes("up/data.bin"), data);
    await waitUntil(() => s3.keys(BUCKET).length === 0, 5000);
    expect((await ticket.status()).phase).toBe("done");
  });

  test("the ticket is its URL as a string and never serialises or prints it", async () => {
    const { sandbox, logger } = await stagedSandbox();
    const ticket = await sandbox.files.uploadUrl("a.txt");
    expect(String(ticket)).toBe(ticket.url);
    expect(`${ticket}`).toBe(ticket.url);
    expect(() => JSON.stringify({ ticket })).toThrow(TypeError);
    const printed = inspect(ticket);
    expect(printed).toContain("UploadTicket");
    expect(printed).not.toContain("X-Amz-Signature");
    expect(printed).not.toContain(ticket.url);
    logger.debug("ticket", { ticket: inspect(ticket) });
    expect(logger.dump()).not.toContain("X-Amz-Signature");
    expect(logger.dump()).not.toContain("http");
  });

  test("the seven-day clamp reaches the URL and expiresAt", async () => {
    const { sandbox } = await stagedSandbox({ maxExpiresIn: 604_800 });
    const before = Date.now();
    const ticket = await sandbox.files.uploadUrl("a.txt", { expiresIn: 604_801 });
    const after = Date.now();
    expect(new URL(ticket.url).searchParams.get("X-Amz-Expires")).toBe("604800");
    expect(ticket.expiresAt.getTime()).toBeGreaterThanOrEqual(before + 604_800_000 - 1000);
    expect(ticket.expiresAt.getTime()).toBeLessThanOrEqual(after + 604_800_000 + 1000);
    await expect(sandbox.files.uploadUrl("a.txt", { expiresIn: 0 })).rejects.toBeInstanceOf(
      InvalidArgumentError,
    );
    await expect(sandbox.files.uploadUrl("a.txt", { maxBytes: 0 })).rejects.toBeInstanceOf(
      InvalidArgumentError,
    );
  });

  test("an unused ticket expires as TimeoutError('expired: ...')", async () => {
    const { sandbox } = await stagedSandbox();
    const ticket = await sandbox.files.uploadUrl("late.txt", { expiresIn: 1 });
    const error = await ticket.wait().catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(TimeoutError);
    expect((error as Error).message.startsWith("expired")).toBe(true);
  });

  test("max_bytes refuses a larger PUT with too_large, writes nothing and deletes the object", async () => {
    const { sandbox, rayd, s3 } = await stagedSandbox();
    const ticket = await sandbox.files.uploadUrl("big.txt", { maxBytes: 10 });
    expect(rayd.filesystem.transfers.imports[0]?.maxBytes).toBe(10n);
    await putToTicket(ticket, patterned(100));
    const error = await ticket.wait().catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(InvalidArgumentError);
    expect((error as Error).message.startsWith("too_large")).toBe(true);
    expect(rayd.filesystem.nodeAt("big.txt")).toBeUndefined();
    await waitUntil(() => s3.keys(BUCKET).length === 0, 5000);
  });

  test("form=true gives POST fields with content-length-range", async () => {
    const { sandbox, rayd, s3 } = await stagedSandbox();
    const ticket = await sandbox.files.uploadUrl("form.bin", { form: true, maxBytes: 1024 });
    expect(ticket.method).toBe("POST");
    expect(ticket.headers).toEqual({});
    expect(ticket.fields.key).toMatch(UP_KEY);
    expect(ticket.fields.Policy).toBeTypeOf("string");
    const refused = await postToTicket(ticket, patterned(2048));
    expect(refused.status).toBe(400);
    expect(await refused.text()).toContain("EntityTooLarge");
    const small = patterned(512);
    expect((await postToTicket(ticket, small)).status).toBe(204);
    await ticket.wait();
    expect(rayd.filesystem.fileBytes("form.bin")).toEqual(small);
    await waitUntil(() => s3.keys(BUCKET).length === 0, 5000);
  });

  test("wait re-issues WatchTransfer after UNAVAILABLE suspending and completes", async () => {
    const { sandbox, rayd } = await stagedSandbox();
    const ticket = await sandbox.files.uploadUrl("resumed.txt");
    const waiting = ticket.wait();
    await waitUntil(() => rayd.filesystem.transfers.liveWatchers === 1, 5000);
    rayd.suspendResume({ unavailableCalls: 1 });
    await waitUntil(() => (rayd.filesystem.headers.WatchTransfer?.length ?? 0) >= 2, 10_000);
    await putToTicket(ticket, patterned(64));
    expect((await waiting).size).toBe(64);
  });

  test("wait(timeoutMs) raises TimeoutError and leaves the ticket armed; cancel ends it", async () => {
    const { sandbox, rayd } = await stagedSandbox();
    const ticket = await sandbox.files.uploadUrl("slow.txt");
    await expect(ticket.wait({ timeoutMs: 300 })).rejects.toBeInstanceOf(TimeoutError);
    expect((await ticket.status()).phase).toBe("waiting");
    await ticket.cancel();
    expect(rayd.filesystem.transfers.cancels).toEqual([ticket.transferId]);
    const status = await ticket.status();
    expect([status.phase, status.errorCode, status.errorReason]).toEqual([
      "cancelled",
      "cancelled",
      "cancelled",
    ]);
    const error = await ticket.wait().catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(FileUploadError);
  });

  test("sandbox.uploadUrl/downloadUrl return plain URL strings; useSignatureExpiration <= 0 raises", async () => {
    const { sandbox, rayd } = await stagedSandbox();
    rayd.filesystem.addFile("out.txt", "hola");
    const upload = await sandbox.uploadUrl("in.txt", { useSignatureExpiration: 120 });
    expect(typeof upload).toBe("string");
    expect(new URL(upload).searchParams.get("X-Amz-Expires")).toBe("120");
    const download = await sandbox.downloadUrl("out.txt");
    expect(typeof download).toBe("string");
    expect(await (await fetch(download)).text()).toBe("hola");
    for (const bad of [0, -1]) {
      await expect(
        sandbox.uploadUrl("in.txt", { useSignatureExpiration: bad }),
      ).rejects.toBeInstanceOf(InvalidArgumentError);
      await expect(
        sandbox.downloadUrl("out.txt", { useSignatureExpiration: bad }),
      ).rejects.toBeInstanceOf(InvalidArgumentError);
    }
  });
});

describe("download links", () => {
  test("a missing file raises before any presign, export or S3 request", async () => {
    const { sandbox, rayd, s3 } = await stagedSandbox();
    await expect(sandbox.files.downloadUrl(`${HOME}/missing`)).rejects.toBeInstanceOf(
      FileNotFoundError,
    );
    await expect(sandbox.files.downloadUrl(HOME)).rejects.toBeInstanceOf(InvalidArgumentError);
    expect(rayd.filesystem.headers.StartExport).toBeUndefined();
    expect(s3.requests).toHaveLength(0);
  });

  test("single PUT export: snapshot served with Content-Disposition, sha256 and size", async () => {
    const { sandbox, rayd } = await stagedSandbox();
    const data = patterned(300_000);
    rayd.filesystem.addFile("report.csv", data);
    const link = await sandbox.files.downloadUrl("report.csv", { filename: "informe.csv" });
    expect(link).toBeInstanceOf(DownloadLink);
    expect([link.size, link.sha256, link.path]).toEqual([
      data.byteLength,
      sha256(data),
      "report.csv",
    ]);
    const exported = rayd.filesystem.transfers.exports[0];
    expect(exported?.target.case).toBe("put");
    expect(exported?.object?.key).toMatch(DOWN_KEY);
    rayd.filesystem.addFile("report.csv", "changed");
    const response = await fetch(link.url);
    expectSameBytes(new Uint8Array(await response.arrayBuffer()), data);
    expect(new URL(link.url).searchParams.get("response-content-disposition")).toBe(
      contentDisposition("informe.csv"),
    );
    expect(inspect(link)).not.toContain("X-Amz-Signature");
    expect(() => JSON.stringify(link)).toThrow(TypeError);
  });

  test("multipart export completes with the ETags in order and leaves no upload behind", async () => {
    const { sandbox, rayd, s3 } = await stagedSandbox({ multipartThresholdBytes: 16 * MIB });
    const data = patterned(20 * MIB, 11);
    rayd.filesystem.addFile("big.bin", data);
    const link = await sandbox.files.downloadUrl("big.bin");
    const exported = rayd.filesystem.transfers.exports[0];
    expect(exported?.target.case).toBe("multipart");
    if (exported?.target.case === "multipart") {
      expect(exported.target.value.partSize).toBe(BigInt(8 * MIB));
      expect(exported.target.value.parts).toHaveLength(3);
    }
    expect(link.sha256).toBe(sha256(data));
    const key = exported?.object?.key ?? "";
    expectSameBytes(s3.object(BUCKET, key)?.data, data);
    expect(s3.uploads.size).toBe(0);
  });

  test("a failed multipart export aborts the upload and raises the mapped error", async () => {
    const { sandbox, rayd, s3 } = await stagedSandbox({ multipartThresholdBytes: 16 * MIB });
    rayd.filesystem.addFile("big.bin", patterned(17 * MIB));
    rayd.filesystem.transfers.failNextExport = {
      code: "unavailable",
      message: "s3_unavailable: S3 no responde",
    };
    const error = await sandbox.files.downloadUrl("big.bin").catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(TransferError);
    expect([(error as TransferError).code, (error as TransferError).reason]).toEqual([
      "unavailable",
      "s3_unavailable",
    ]);
    expect(s3.uploads.size).toBe(0);
    expect(s3.requestsFor("DELETE").some((request) => "uploadId" in request.query)).toBe(true);
  });
});

describe("large-file routing", () => {
  test("threshold boundary: one byte below uses gRPC Write, the threshold goes through S3", async () => {
    const { sandbox, rayd, s3 } = await stagedSandbox({ thresholdBytes: MIB });
    const below = patterned(MIB - 1);
    await sandbox.files.write("below.bin", below);
    expect(rayd.filesystem.writeStreams).toHaveLength(1);
    expect(rayd.filesystem.transfers.imports).toHaveLength(0);
    const at = patterned(MIB, 3);
    const entry = await sandbox.files.write("at.bin", at, { mode: 0o600 });
    expect(rayd.filesystem.writeStreams).toHaveLength(1);
    const imported = rayd.filesystem.transfers.imports[0];
    expect(imported?.waitForObject).toBe(false);
    expect(imported?.expectedSha256).toBe(sha256(at));
    expect(imported?.maxBytes).toBe(BigInt(MIB));
    expect(imported?.mode).toBe(0o600);
    expect([entry.path, entry.size, entry.mode]).toEqual([`${HOME}/at.bin`, MIB, 0o600]);
    expectSameBytes(rayd.filesystem.fileBytes("at.bin"), at);
    await waitUntil(() => s3.keys(BUCKET).length === 0, 5000);
  });

  test("writeFiles keeps request order with routed and gRPC entries mixed", async () => {
    const { sandbox, rayd } = await stagedSandbox({ thresholdBytes: MIB });
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(patterned(1000));
        controller.enqueue(patterned(2000));
        controller.close();
      },
    });
    const entries = await sandbox.files.writeFiles([
      { path: "a.txt", data: "hola" },
      { path: "b.bin", data: patterned(2 * MIB) },
      { path: "c.bin", data: stream },
      { path: "d.txt", data: "adiós" },
    ]);
    expect(entries.map((entry) => [entry.name, entry.size])).toEqual([
      ["a.txt", 4],
      ["b.bin", 2 * MIB],
      ["c.bin", 3000],
      ["d.txt", 6],
    ]);
    expect(rayd.filesystem.writeStreams).toHaveLength(1);
    expect(rayd.filesystem.writeStreams[0]?.map((message) => message.path)).toEqual([
      "a.txt",
      "d.txt",
    ]);
    const imports = rayd.filesystem.transfers.imports;
    expect(imports.map((request) => request.path)).toEqual(["b.bin", "c.bin"]);
    expect(imports[1]?.maxBytes).toBe(3000n);
  });

  test("metadata travels with a routed write", async () => {
    const { sandbox, rayd } = await stagedSandbox({ thresholdBytes: MIB });
    const entry = await sandbox.files.write("m.bin", patterned(MIB), {
      metadata: { Owner: "alice" },
    });
    expect(rayd.filesystem.transfers.imports[0]?.metadata).toEqual({ owner: "alice" });
    expect(entry.metadata).toEqual({ owner: "alice" });
  });

  test("large reads export, download with the caller's credentials, verify and clean up", async () => {
    const { sandbox, rayd, s3 } = await stagedSandbox({ thresholdBytes: MIB });
    const data = patterned(2 * MIB + 5, 5);
    rayd.filesystem.addFile("big.bin", data);
    expectSameBytes(await sandbox.files.read("big.bin", { format: "bytes" }), data);
    expect(rayd.filesystem.readCalls).toBe(0);
    expect(rayd.filesystem.transfers.exports).toHaveLength(1);
    await waitUntil(() => s3.keys(BUCKET).length === 0, 5000);
    const stream = await sandbox.files.read("big.bin", { format: "stream" });
    const chunks: Uint8Array[] = [];
    for await (const chunk of stream) {
      chunks.push(chunk);
    }
    expectSameBytes(Buffer.concat(chunks), data);
    const blob = await sandbox.files.read("big.bin", { format: "blob" });
    expectSameBytes(new Uint8Array(await blob.arrayBuffer()), data);
    rayd.filesystem.addFile("text.txt", "ñ".repeat(MIB));
    expect(await sandbox.files.read("text.txt")).toBe("ñ".repeat(MIB));
    expect(rayd.filesystem.readCalls).toBe(0);
    await waitUntil(() => s3.keys(BUCKET).length === 0, 5000);
  });

  test("streamIdleTimeoutMs also guards the export's WatchTransfer on a routed read", async () => {
    const { sandbox, rayd } = await stagedSandbox({ thresholdBytes: MIB });
    rayd.filesystem.addFile("stuck.bin", patterned(MIB));
    rayd.filesystem.transfers.holdExports = true;
    const error = await sandbox.files
      .read("stuck.bin", { format: "bytes", streamIdleTimeoutMs: 300 })
      .catch((caught: unknown) => caught);
    rayd.filesystem.transfers.holdExports = false;
    expect(error).toBeInstanceOf(TimeoutError);
    expect((error as Error).message.startsWith("stream_idle_timeout")).toBe(true);
    await waitUntil(() => rayd.filesystem.transfers.liveWatchers === 0, 5000);
  });

  test("requestTimeoutMs bounds a stalled S3 upload and no import is started", async () => {
    const { sandbox, rayd, s3 } = await stagedSandbox({ thresholdBytes: MIB });
    s3.stalled.add("PUT");
    const started = Date.now();
    const error = await sandbox.files
      .write("big.bin", patterned(MIB), { requestTimeoutMs: 500 })
      .catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(TimeoutError);
    expect((error as Error).message).toContain("plazo");
    expect(Date.now() - started).toBeLessThan(5000);
    expect(rayd.filesystem.transfers.imports).toHaveLength(0);
    await waitUntil(() => s3.requestsFor("DELETE").length > 0, 5000);
  });

  test("a failed cleanup after a stalled upload is logged without a transfer id", async () => {
    const { sandbox, s3, logger } = await stagedSandbox({ thresholdBytes: MIB });
    s3.stalled.add("PUT");
    s3.denied.add("DELETE");
    const error = await sandbox.files
      .write("big.bin", patterned(MIB), { requestTimeoutMs: 500 })
      .catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(TimeoutError);
    const cleanup = logger
      .at("debug")
      .find((line) => line.message === "no se pudo borrar el objeto de staging");
    expect(cleanup?.fields).toEqual({ outcome: expect.any(String) });
  });

  test("requestTimeoutMs bounds a stalled S3 download and the staging object is deleted", async () => {
    const { sandbox, rayd, s3 } = await stagedSandbox({ thresholdBytes: MIB });
    rayd.filesystem.addFile("big.bin", patterned(MIB));
    s3.stalled.add("GET");
    const started = Date.now();
    const error = await sandbox.files
      .read("big.bin", { format: "bytes", requestTimeoutMs: 1000 })
      .catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(TimeoutError);
    expect((error as Error).message).toContain("plazo");
    expect(Date.now() - started).toBeLessThan(5000);
    await waitUntil(() => s3.keys(BUCKET).length === 0, 5000);
  });

  test("a sha256 mismatch on a routed read raises TransferError(checksum_mismatch)", async () => {
    const { sandbox, rayd } = await stagedSandbox({ thresholdBytes: MIB });
    rayd.filesystem.addFile("big.bin", patterned(MIB));
    rayd.filesystem.transfers.corruptNextExportSha = true;
    const error = await sandbox.files
      .read("big.bin", { format: "bytes" })
      .catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(TransferError);
    expect([(error as TransferError).code, (error as TransferError).reason]).toEqual([
      "failed_precondition",
      "checksum_mismatch",
    ]);
  });

  test("without staging a transport spy sees Write/Stat/Read and no transfer RPC", async () => {
    const { sandbox, rayd } = await createTestSandbox();
    const data = patterned(3 * MIB);
    await sandbox.files.write("plain.bin", data);
    expectSameBytes(await sandbox.files.read("plain.bin", { format: "bytes" }), data);
    const headers = rayd.filesystem.headers;
    expect(Object.keys(headers).sort()).toEqual(["Read", "Stat", "Write"]);
  });

  test("the logger never receives a URL, bucket, key or path", async () => {
    const { sandbox, rayd, logger } = await stagedSandbox({ thresholdBytes: MIB });
    rayd.filesystem.addFile("secret-plan.bin", patterned(MIB));
    await sandbox.files.read("secret-plan.bin", { format: "bytes" });
    await sandbox.files.write("secret-copy.bin", patterned(MIB));
    const ticket = await sandbox.files.uploadUrl("secret-upload.bin");
    await putToTicket(ticket, patterned(10));
    await ticket.wait();
    await sandbox.files.downloadUrl("secret-plan.bin");
    const dump = logger.dump();
    for (const needle of [
      "X-Amz-Signature",
      "X-Amz-Credential",
      "http",
      BUCKET,
      "secret-",
      "rayito-transfer/",
    ]) {
      expect(dump, needle).not.toContain(needle);
    }
    expect(logger.at("debug").some((line) => line.fields?.transferId !== undefined)).toBe(true);
  });
});

describe("Sandbox transfer option", () => {
  test("create resolves transfer, falls back to the environment, and null disables it", async () => {
    const explicit = await createTestSandbox({
      create: { transfer: { bucket: BUCKET, prefix: "xfer" } },
    });
    expect(explicit.sandbox.transfer?.prefix).toBe("xfer");
    vi.stubEnv("RAYITO_TRANSFER_BUCKET", BUCKET);
    const fromEnv = await createTestSandbox();
    expect(fromEnv.sandbox.transfer).toEqual(staging());
    const disabled = await createTestSandbox({ create: { transfer: null } });
    expect(disabled.sandbox.transfer).toBeUndefined();
  });

  test("an invalid staging or a persist overlap fails before run-microvm", async () => {
    const plane = new FakeControlPlane({ endpoint: "127.0.0.1", states: ["RUNNING"] });
    await expect(
      Sandbox.create({
        template: IMAGE_ARN,
        controlPlane: plane,
        executionRoleArn: "arn:aws:iam::123456789012:role/rayito-exec",
        persist: new S3Prefix({ bucket: BUCKET, prefix: "data" }),
        transfer: { bucket: BUCKET, prefix: "data/tmp" },
      }),
    ).rejects.toBeInstanceOf(InvalidArgumentError);
    await expect(
      Sandbox.create({ template: IMAGE_ARN, controlPlane: plane, transfer: { bucket: "a.b.c" } }),
    ).rejects.toBeInstanceOf(InvalidArgumentError);
    expect(plane.callsTo("runMicrovm")).toHaveLength(0);
  });

  test("connect takes transfer too", async () => {
    const { sandbox, rayd, plane } = await createTestSandbox();
    const connected = await Sandbox.connect(sandbox.sandboxId, {
      accessToken: sandbox.accessToken,
      controlPlane: plane,
      transport: rayd.transport,
      transfer: { bucket: BUCKET, thresholdBytes: 2 * MIB },
    });
    try {
      expect(connected.transfer?.thresholdBytes).toBe(2 * MIB);
    } finally {
      connected.close();
    }
  });
});
