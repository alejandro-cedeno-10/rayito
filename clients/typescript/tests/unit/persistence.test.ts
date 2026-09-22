/**
 * `S3Prefix`, `Sandbox.create({ persist })`, `checkpointFiles`, `restoreFiles`
 * y `reincarnate` contra el `rayd` falso y el plano falso: los mismos
 * escenarios que `tests/unit/test_persistence_{base,sync}.py` del SDK Python.
 */

import { create } from "@bufbuild/protobuf";
import { Code, ConnectError } from "@connectrpc/connect";
import { describe, expect, it } from "vitest";
import { StreamErrorSchema } from "../../src/gen/rayito/v1/common_pb.js";
import {
  AuthenticationError,
  InvalidArgumentError,
  NotFoundError,
  PersistenceError,
  S3Prefix,
  Sandbox,
  SandboxError,
  TimeoutError,
} from "../../src/index.js";
import {
  bindPersist,
  checkpointRequest,
  interruptedMessage,
  midStreamError,
  requireNamedPersist,
  requireRoleForPersist,
  resolveTarget,
  shouldAutoRestore,
  statusError,
  streamErrorFrom,
  UNIMPLEMENTED_MESSAGE,
  validateExclude,
  validatePersistTimeoutMs,
} from "../../src/sandbox/persistence.js";
import { PROXY_FORBIDDEN_MESSAGE } from "../../src/transport/errors.js";
import { FakeControlPlane, IMAGE_ARN, SANDBOX_ID } from "./fake/control-plane.js";
import {
  ACCESS_TOKEN,
  createTestSandbox,
  RecordingLogger,
  startRayd,
  type TestSandbox,
} from "./helpers.js";

const BUCKET = "my-bucket";
const ROLE = "arn:aws:iam::123456789012:role/rayito-execution";
const SUCCESSOR_ID = "microvm-00000000-0000-0000-0000-000000000002";

function prefix(name = "n"): S3Prefix {
  return new S3Prefix({ bucket: BUCKET, name });
}

async function persistedSandbox(
  persist: S3Prefix = new S3Prefix({ bucket: BUCKET }),
): Promise<TestSandbox> {
  return createTestSandbox({ create: { executionRoleArn: ROLE, persist } });
}

// ------------------------------------------------------------------ models

describe("S3Prefix", () => {
  it("defaults, keys and uri", () => {
    const bare = new S3Prefix({ bucket: BUCKET });
    expect(bare.prefix).toBe("rayito");
    expect(bare.name).toBeUndefined();
    expect(() => bare.keyPrefix).toThrow(InvalidArgumentError);
    const named = bare.withName("agent-7");
    expect(named.keyPrefix).toBe("rayito/agent-7");
    expect(named.archiveKey).toBe("rayito/agent-7/home.tar.gz");
    expect(named.manifestKey).toBe("rayito/agent-7/manifest.json");
    expect(named.uri).toBe("s3://my-bucket/rayito/agent-7");
    expect(named.equals(prefix("agent-7"))).toBe(true);
    expect(named.equals(prefix("other"))).toBe(false);
    expect(
      new S3Prefix({ bucket: BUCKET, prefix: "a/b", name: "c/d", region: "eu-west-1" }).keyPrefix,
    ).toBe("a/b/c/d");
  });

  it.each(["ab", "a".repeat(64), "Bucket", "my_bucket", "-abc", "abc.", "a..b", "192.168.0.1"])(
    "rejects the bucket %s",
    (bucket) => {
      expect(() => new S3Prefix({ bucket })).toThrow(InvalidArgumentError);
    },
  );

  it.each([
    "",
    "/rayito",
    "rayito/",
    "a//b",
    "a/./b",
    "a/../b",
    "ray ito",
    "rayitó",
    "a&b",
    "a".repeat(901),
  ])("rejects the prefix %j", (value) => {
    expect(() => new S3Prefix({ bucket: BUCKET, prefix: value })).toThrow(InvalidArgumentError);
  });

  it.each(["", "/x", "x/", "a//b", "..", "x y"])("rejects the name %j", (name) => {
    expect(() => new S3Prefix({ bucket: BUCKET, name })).toThrow(/name/);
  });

  it("accepts the safe set and rejects an empty region", () => {
    expect(new S3Prefix({ bucket: BUCKET, prefix: "a!b_c.d*e'f(g)h-i", name: "n" }).keyPrefix).toBe(
      "a!b_c.d*e'f(g)h-i/n",
    );
    expect(() => new S3Prefix({ bucket: BUCKET, region: "" })).toThrow(/region/);
  });
});

// ------------------------------------------------------------ pure rules

describe("persistence rules", () => {
  it("validates exclude like a request path and caps it at 64", () => {
    for (const bad of ["/abs", "", "../x", "a/../b", ".", "a\0b", "a".repeat(4097)]) {
      expect(() => validateExclude([bad])).toThrow(InvalidArgumentError);
    }
    expect(validateExclude(Array.from({ length: 64 }, (_, index) => `d${index}`))).toHaveLength(64);
    expect(() => validateExclude(Array.from({ length: 65 }, (_, index) => `d${index}`))).toThrow(
      /64/,
    );
  });

  it("builds the request with location, excludes and user", () => {
    const request = checkpointRequest(
      new S3Prefix({ bucket: BUCKET, prefix: "base", name: "x", region: "eu-west-1" }),
      ["skipme", "a/b"],
      "user",
    );
    expect(request.target?.bucket).toBe(BUCKET);
    expect(request.target?.keyPrefix).toBe("base/x");
    expect(request.target?.region).toBe("eu-west-1");
    expect(request.exclude).toEqual(["skipme", "a/b"]);
    expect(request.user?.username).toBe("user");
    expect(checkpointRequest(prefix(), [], undefined).user).toBeUndefined();
  });

  it("create rules: role required, name defaulted, connect needs a name", () => {
    expect(() => requireRoleForPersist(prefix(), undefined)).toThrow(/executionRoleArn/);
    requireRoleForPersist(undefined, undefined);
    expect(bindPersist(new S3Prefix({ bucket: BUCKET }), "mvm-1").name).toBe("mvm-1");
    expect(bindPersist(prefix("given"), "mvm-1").name).toBe("given");
    expect(shouldAutoRestore(prefix("given"))).toBe(true);
    expect(shouldAutoRestore(new S3Prefix({ bucket: BUCKET }))).toBe(false);
    expect(() => requireNamedPersist(new S3Prefix({ bucket: BUCKET }))).toThrow(/connect/);
    expect(() => resolveTarget(undefined, undefined)).toThrow(/destino/);
    expect(() => resolveTarget(new S3Prefix({ bucket: BUCKET }), undefined)).toThrow(/name/);
    expect(resolveTarget(undefined, prefix("b")).name).toBe("b");
    expect(validatePersistTimeoutMs(undefined)).toBe(600_000);
    expect(() => validatePersistTimeoutMs(0)).toThrow(InvalidArgumentError);
  });

  it("maps StreamError codes and statuses per D8", () => {
    expect(
      streamErrorFrom(create(StreamErrorSchema, { code: "not_found", message: "m" })),
    ).toBeInstanceOf(NotFoundError);
    expect(
      streamErrorFrom(create(StreamErrorSchema, { code: "invalid_argument", message: "m" })),
    ).toBeInstanceOf(InvalidArgumentError);
    expect(
      streamErrorFrom(create(StreamErrorSchema, { code: "deadline_exceeded", message: "m" })),
    ).toBeInstanceOf(TimeoutError);
    const denied = streamErrorFrom(
      create(StreamErrorSchema, { code: "permission_denied", message: "m" }),
    );
    expect(denied).toBeInstanceOf(PersistenceError);
    expect((denied as PersistenceError).code).toBe("permission_denied");
    const cut = streamErrorFrom(
      create(StreamErrorSchema, { code: "suspending", message: "suspending" }),
    );
    expect((cut as PersistenceError).code).toBe("interrupted");
    const old = streamErrorFrom(create(StreamErrorSchema, { code: "unimplemented", message: "x" }));
    expect(old.message).toBe(UNIMPLEMENTED_MESSAGE);
    const busy = statusError(new ConnectError("persistence busy", Code.FailedPrecondition));
    expect((busy as PersistenceError).code).toBe("failed_precondition");
    const noRole = statusError(
      new ConnectError("no execution role credentials", Code.PermissionDenied),
    );
    expect((noRole as PersistenceError).code).toBe("permission_denied");
    expect(
      statusError(new ConnectError(PROXY_FORBIDDEN_MESSAGE, Code.PermissionDenied)),
    ).toBeInstanceOf(AuthenticationError);
    expect(statusError(new ConnectError("x", Code.NotFound))).toBeInstanceOf(NotFoundError);
    expect((statusError(new ConnectError("x", Code.Unimplemented)) as PersistenceError).code).toBe(
      "unimplemented",
    );
    expect(statusError(new ConnectError("x", Code.DeadlineExceeded))).toBeInstanceOf(TimeoutError);
    const reset = midStreamError(new ConnectError("suspending", Code.Unavailable));
    expect((reset as PersistenceError).code).toBe("interrupted");
    expect(reset.message).toBe(interruptedMessage("suspending"));
    expect(midStreamError(new ConnectError("d", Code.DeadlineExceeded))).toBeInstanceOf(
      TimeoutError,
    );
  });
});

// ------------------------------------------------------------- create rules

describe("Sandbox.create({ persist })", () => {
  it("rejects persist without an execution role before any plane call", async () => {
    const rayd = await startRayd();
    try {
      const plane = new FakeControlPlane({ endpoint: rayd.host });
      await expect(
        Sandbox.create({
          template: IMAGE_ARN,
          persist: new S3Prefix({ bucket: BUCKET }),
          controlPlane: plane,
          transport: rayd.transport,
        }),
      ).rejects.toThrow(/executionRoleArn/);
      expect(plane.calls).toHaveLength(0);
    } finally {
      await rayd.close();
    }
  });

  it("binds without a name to the sandbox id and does not restore", async () => {
    const { sandbox, rayd } = await persistedSandbox();
    expect(sandbox.persist?.equals(prefix(SANDBOX_ID))).toBe(true);
    expect(sandbox.persist?.uri).toBe(`s3://${BUCKET}/rayito/${SANDBOX_ID}`);
    expect(sandbox.lastRestore).toBeUndefined();
    expect(rayd.filesystem.persistence.restoreRequests).toHaveLength(0);
  });

  it("binds with a name, restores what exists and swallows not found", async () => {
    const first = await persistedSandbox(prefix("agent-7"));
    expect(first.sandbox.lastRestore).toBeUndefined();
    expect(first.rayd.filesystem.persistence.restoreRequests).toHaveLength(1);
    await first.close();
    const second = await createTestSandbox({
      create: { executionRoleArn: ROLE, persist: prefix("agent-7") },
      beforeCreate: (rayd) =>
        rayd.filesystem.persistence.seed(BUCKET, "rayito/agent-7", { files: 5 }),
    });
    expect(second.sandbox.lastRestore?.files).toBe(5);
    expect(second.sandbox.lastRestore?.uri).toBe(`s3://${BUCKET}/rayito/agent-7`);
  });

  it("terminates the sandbox when the auto restore fails", async () => {
    const rayd = await startRayd();
    try {
      rayd.filesystem.persistence.seed(BUCKET, "rayito/broken");
      rayd.filesystem.persistence.failAfterStarted = {
        code: "internal",
        message: "archive checksum mismatch",
      };
      const plane = new FakeControlPlane({ endpoint: rayd.host });
      await expect(
        Sandbox.create({
          template: IMAGE_ARN,
          idle: null,
          accessToken: ACCESS_TOKEN,
          executionRoleArn: ROLE,
          persist: prefix("broken"),
          controlPlane: plane,
          transport: rayd.transport,
        }),
      ).rejects.toMatchObject({ code: "internal" });
      expect(plane.callsTo("terminateMicrovm")).toHaveLength(1);
    } finally {
      await rayd.close();
    }
  });

  it("refuses persist together with pool", async () => {
    await expect(
      Sandbox.create({
        executionRoleArn: ROLE,
        persist: new S3Prefix({ bucket: BUCKET }),
        pool: {} as never,
      }),
    ).rejects.toThrow(/pool/);
  });

  it("connect binds without restoring and requires a name", async () => {
    const rayd = await startRayd();
    try {
      const plane = new FakeControlPlane({ endpoint: rayd.host, states: ["RUNNING"] });
      await expect(
        Sandbox.connect(SANDBOX_ID, {
          accessToken: ACCESS_TOKEN,
          persist: new S3Prefix({ bucket: BUCKET }),
          controlPlane: plane,
          transport: rayd.transport,
        }),
      ).rejects.toThrow(/name/);
      expect(plane.calls).toHaveLength(0);
      const handle = await Sandbox.connect(SANDBOX_ID, {
        accessToken: ACCESS_TOKEN,
        persist: prefix("named"),
        controlPlane: plane,
        transport: rayd.transport,
      });
      try {
        expect(handle.persist?.name).toBe("named");
        expect(handle.lastRestore).toBeUndefined();
        expect(rayd.filesystem.persistence.restoreRequests).toHaveLength(0);
        await expect(handle.reincarnate()).rejects.toThrow(/create/);
      } finally {
        handle.close();
      }
    } finally {
      await rayd.close();
    }
  });
});

// -------------------------------------------------------------- checkpoint

describe("checkpointFiles", () => {
  it("returns the done fields, reports progress and sends the excludes", async () => {
    const { sandbox, rayd } = await persistedSandbox();
    const seen: number[] = [];
    const result = await sandbox.checkpointFiles({
      exclude: ["skipme", "data/raw"],
      onProgress: (progress) => seen.push(progress.filesDone),
    });
    expect(result.bucket).toBe(BUCKET);
    expect(result.keyPrefix).toBe(`rayito/${SANDBOX_ID}`);
    expect(result.files).toBe(4);
    expect(result.bytesRead).toBe(52_428_800);
    expect(result.archiveBytes).toBe(Math.floor(52_428_800 / 3));
    expect(result.sha256).toHaveLength(64);
    expect(result.skipped).toBe(1);
    expect(result.durationMs).toBe(1500);
    expect(seen).toEqual([1, 2]);
    const request = rayd.filesystem.persistence.checkpointRequests.at(-1);
    expect(request?.exclude).toEqual(["skipme", "data/raw"]);
    expect(rayd.filesystem.persistence.stored(BUCKET, `rayito/${SANDBOX_ID}`)?.excluded).toEqual([
      "skipme",
      "data/raw",
    ]);
  });

  it("goes through the stream transport with the deadline", async () => {
    const { sandbox, rayd } = await persistedSandbox();
    await sandbox.checkpointFiles({ timeoutMs: 120_000 });
    const deadline = rayd.filesystem.deadlines.Checkpoint?.at(-1);
    expect(deadline).toBeDefined();
    expect(deadline as number).toBeGreaterThan(100_000);
    expect(deadline as number).toBeLessThanOrEqual(122_000);
  });

  it("validates locally and honours an explicit target", async () => {
    const { sandbox, rayd } = await persistedSandbox();
    const result = await sandbox.checkpointFiles({
      target: new S3Prefix({ bucket: "other-bucket", prefix: "p", name: "q" }),
    });
    expect(result.uri).toBe("s3://other-bucket/p/q");
    await expect(sandbox.checkpointFiles({ exclude: ["../etc"] })).rejects.toThrow(
      InvalidArgumentError,
    );
    await expect(
      sandbox.checkpointFiles({ target: new S3Prefix({ bucket: BUCKET }) }),
    ).rejects.toThrow(/name/);
    expect(rayd.filesystem.persistence.checkpointRequests).toHaveLength(1);
  });

  it("maps the statuses before started", async () => {
    const { sandbox, rayd } = await persistedSandbox();
    const persistence = rayd.filesystem.persistence;
    persistence.hasCredentials = false;
    await expect(sandbox.checkpointFiles()).rejects.toMatchObject({
      code: "permission_denied",
      grpcCode: Code.PermissionDenied,
    });
    persistence.hasCredentials = true;
    await expect(sandbox.checkpointFiles({ user: "root" })).rejects.toMatchObject({
      code: "permission_denied",
    });
    persistence.busy = true;
    await expect(sandbox.checkpointFiles()).rejects.toMatchObject({ code: "failed_precondition" });
    persistence.busy = false;
    persistence.unimplemented = true;
    await expect(sandbox.checkpointFiles()).rejects.toMatchObject({
      code: "unimplemented",
      message: UNIMPLEMENTED_MESSAGE,
    });
    await expect(sandbox.restoreFiles()).rejects.toMatchObject({ code: "unimplemented" });
  });

  it("turns a StreamError after started into a PersistenceError and never reconnects", async () => {
    const { sandbox, rayd } = await persistedSandbox();
    const persistence = rayd.filesystem.persistence;
    persistence.failAfterStarted = {
      code: "permission_denied",
      message: "access denied by the bucket",
    };
    const error = await sandbox.checkpointFiles().catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(PersistenceError);
    expect((error as PersistenceError).code).toBe("permission_denied");
    expect(persistence.stored(BUCKET, `rayito/${SANDBOX_ID}`)).toBeUndefined();
    persistence.failAfterStarted = { code: "suspending", message: "suspending" };
    await expect(sandbox.checkpointFiles()).rejects.toMatchObject({ code: "interrupted" });
    expect(persistence.checkpointRequests).toHaveLength(2);
  });

  it("re-mints once on a proxy 403 before the first message", async () => {
    const { sandbox, rayd, plane } = await persistedSandbox();
    const mints = plane.mints;
    rayd.forbidNext(1);
    const result = await sandbox.checkpointFiles();
    expect(result.files).toBe(4);
    expect(plane.mints).toBe(mints + 1);
    expect(rayd.filesystem.persistence.checkpointRequests).toHaveLength(1);
  });
});

// ----------------------------------------------------------------- restore

describe("restoreFiles", () => {
  it("returns the done fields and reports progress", async () => {
    const { sandbox, rayd } = await persistedSandbox();
    rayd.filesystem.persistence.seed(BUCKET, `rayito/${SANDBOX_ID}`, {
      files: 7,
      sha256: "cd".repeat(32),
    });
    const seen: number[] = [];
    const result = await sandbox.restoreFiles({
      onProgress: (progress) => seen.push(progress.bytesDownloaded),
    });
    expect(result.files).toBe(7);
    expect(result.sha256).toBe("cd".repeat(32));
    expect(result.bytesWritten).toBe(1234);
    expect(result.durationMs).toBe(800);
    expect(seen).toEqual([500, 1000]);
  });

  it("an explicit restore of a missing checkpoint is NotFoundError", async () => {
    const { sandbox } = await persistedSandbox();
    await expect(sandbox.restoreFiles({ source: prefix("never") })).rejects.toBeInstanceOf(
      NotFoundError,
    );
  });

  it("a restore error after started is a PersistenceError", async () => {
    const { sandbox, rayd } = await persistedSandbox();
    rayd.filesystem.persistence.seed(BUCKET, `rayito/${SANDBOX_ID}`);
    rayd.filesystem.persistence.failAfterStarted = {
      code: "internal",
      message: "archive checksum mismatch",
    };
    await expect(sandbox.restoreFiles()).rejects.toMatchObject({ code: "internal" });
  });
});

// ------------------------------------------------------------- reincarnate

describe("reincarnate", () => {
  it("checkpoints, creates with the same options, restores and kills the old one", async () => {
    const logger = new RecordingLogger();
    const { sandbox, rayd, plane } = await createTestSandbox({
      create: {
        executionRoleArn: ROLE,
        persist: new S3Prefix({ bucket: BUCKET }),
        metadata: { agent: "7" },
        logger,
      },
    });
    plane.sandboxIds.push(SUCCESSOR_ID);
    const successor = await sandbox.reincarnate({ exclude: ["skipme"] });
    try {
      expect(successor).not.toBe(sandbox);
      expect(successor.sandboxId).toBe(SUCCESSOR_ID);
      expect(successor.persist?.equals(sandbox.persist)).toBe(true);
      expect(successor.lastRestore?.files).toBe(4);
      const persistence = rayd.filesystem.persistence;
      expect(persistence.checkpointRequests.at(-1)?.exclude).toEqual(["skipme"]);
      expect(persistence.restoreRequests.at(-1)?.source?.keyPrefix).toBe(`rayito/${SANDBOX_ID}`);
      const operations = plane.calls.map((call) => `${call.operation}:${call.sandboxId}`);
      const launch = operations.indexOf(`runMicrovm:${SUCCESSOR_ID}`);
      const kill = operations.indexOf(`terminateMicrovm:${SANDBOX_ID}`);
      expect(launch).toBeGreaterThan(-1);
      expect(kill).toBeGreaterThan(launch);
      expect(plane.launches.at(-1)?.runHookPayload).toContain('"agent":"7"');
    } finally {
      successor.close();
    }
  });

  it("leaves the old sandbox alive when create fails", async () => {
    const { sandbox, rayd, plane } = await persistedSandbox();
    plane.runMicrovmError = new SandboxError("ServiceQuotaExceededException");
    const error = await sandbox.reincarnate().catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(SandboxError);
    expect((error as Error).message).toContain("reincarnate()");
    expect(plane.callsTo("terminateMicrovm")).toHaveLength(0);
    expect(rayd.filesystem.persistence.stored(BUCKET, `rayito/${SANDBOX_ID}`)).toBeDefined();
    expect((await sandbox.checkpointFiles()).files).toBe(4);
  });

  it("rethrows the typed create error with its fields intact", async () => {
    const { sandbox, rayd, plane } = await persistedSandbox();
    plane.sandboxIds.push(SUCCESSOR_ID);
    rayd.filesystem.persistence.failRestoreAfterStarted = {
      code: "permission_denied",
      message: "AccessDenied",
    };
    const error = await sandbox.reincarnate().catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(PersistenceError);
    expect((error as PersistenceError).code).toBe("permission_denied");
    expect((error as PersistenceError).message).toContain("AccessDenied");
    expect((error as PersistenceError).message).toContain(
      `reincarnate(): el checkpoint en ${sandbox.persist?.uri}`,
    );
    expect(plane.callsTo("terminateMicrovm").map((call) => call.sandboxId)).toEqual([SUCCESSOR_ID]);
    expect(rayd.filesystem.persistence.stored(BUCKET, `rayito/${SANDBOX_ID}`)).toBeDefined();
  });

  it("wraps a non-Error rejection in SandboxError with cause", async () => {
    const { sandbox, plane } = await persistedSandbox();
    plane.runMicrovmError = "quota" as unknown as Error;
    const error = await sandbox.reincarnate().catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(SandboxError);
    expect((error as SandboxError).cause).toBe("quota");
    expect((error as SandboxError).message).toContain("quota; reincarnate()");
  });

  it("requires persist", async () => {
    const { sandbox } = await createTestSandbox({ create: { executionRoleArn: ROLE } });
    await expect(sandbox.reincarnate()).rejects.toThrow(/persist/);
    await expect(sandbox.checkpointFiles()).rejects.toThrow(/destino/);
  });
});
