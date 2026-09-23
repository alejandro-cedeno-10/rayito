/**
 * `getMetricsHistory` (instancia y estática) contra el `rayd` falso: el
 * request que llega, el mapeo ascendente con `memCacheBytes`, la validación
 * previa a todo RPC, un agente anterior a M9, el transporte dedicado de la
 * variante estática con el access token, y los datos del guest que
 * `getInfo()` toma del último `Health`.
 */

import { create } from "@bufbuild/protobuf";
import { Code } from "@connectrpc/connect";
import { afterEach, beforeEach, describe, expect, test } from "vitest";
import {
  AuthenticationError,
  InvalidArgumentError,
  SandboxError,
  SandboxNotFoundError,
  SandboxStateError,
  UnimplementedError,
} from "../../src/errors.js";
import {
  MetricsHistoryResponseSchema,
  type MetricsResponse,
  MetricsResponseSchema,
} from "../../src/gen/rayito/v1/health_pb.js";
import { generateAccessToken } from "../../src/payload.js";
import { ACCESS_TOKEN_ENV_VAR } from "../../src/sandbox/launch.js";
import {
  HISTORY_FEATURE,
  HISTORY_UNIMPLEMENTED_REASON,
  metricsHistoryFromProto,
  metricsHistoryRequest,
  STATIC_HISTORY_FEATURE,
} from "../../src/sandbox/metrics.js";
import { Sandbox } from "../../src/sandbox/sandbox.js";
import { deadlineFromHeaders } from "./fake/common.js";
import { FakeControlPlane, SANDBOX_ID } from "./fake/control-plane.js";
import type { FakeRayd } from "./fake/server.js";
import { ACCESS_TOKEN, createTestSandbox, sleep, startRayd } from "./helpers.js";

const WINDOW_START = new Date(Date.UTC(2026, 8, 22, 12, 0, 0));
const WINDOW_START_MS = WINDOW_START.getTime();
const MIB = 1024n * 1024n;

function sample(offsetSeconds: number, cache: bigint): MetricsResponse {
  return create(MetricsResponseSchema, {
    cpuUsedPct: offsetSeconds,
    memUsedBytes: 100n,
    memTotalBytes: 1000n,
    diskUsedBytes: 10n,
    diskTotalBytes: 20n,
    cpuCount: 2,
    timestampUnixMs: BigInt(WINDOW_START_MS + offsetSeconds * 1000),
    memCacheBytes: cache,
  });
}

function seededHistory(): MetricsResponse[] {
  return [sample(5, 11n), sample(10, 22n), sample(15, 33n)];
}

function openConnections(rayd: FakeRayd): Promise<number> {
  return new Promise((resolve, reject) => {
    rayd.server.getConnections((error, count) => (error ? reject(error) : resolve(count)));
  });
}

async function expectAllClosed(rayd: FakeRayd): Promise<void> {
  const deadline = performance.now() + 5000;
  while ((await openConnections(rayd)) !== 0) {
    if (performance.now() > deadline) {
      throw new Error("el transporte dedicado sigue abierto");
    }
    await sleep(20);
  }
}

describe("pure helpers", () => {
  test("the request comes from Dates, zeros when absent", () => {
    const request = metricsHistoryRequest({
      start: WINDOW_START,
      end: new Date(WINDOW_START_MS + 300_000),
      maxPoints: 2,
    });
    expect([request.startUnixMs, request.endUnixMs, request.maxPoints]).toEqual([
      BigInt(WINDOW_START_MS),
      BigInt(WINDOW_START_MS + 300_000),
      2,
    ]);
    const empty = metricsHistoryRequest({});
    expect([empty.startUnixMs, empty.endUnixMs, empty.maxPoints]).toEqual([0n, 0n, 0]);
  });

  test("validation: inverted range, maxPoints and dates before the epoch", () => {
    expect(() =>
      metricsHistoryRequest({ start: WINDOW_START, end: new Date(WINDOW_START_MS - 1) }),
    ).toThrow(/posterior/);
    for (const maxPoints of [0, -1, 1.5, Number.NaN, Number.POSITIVE_INFINITY]) {
      expect(() => metricsHistoryRequest({ maxPoints })).toThrow(/maxPoints/);
    }
    expect(metricsHistoryRequest({ maxPoints: 2 ** 32 }).maxPoints).toBe(2 ** 32 - 1);
    expect(() => metricsHistoryRequest({ start: new Date(Date.UTC(1960, 0, 1)) })).toThrow(/start/);
    expect(() => metricsHistoryRequest({ end: new Date(Number.NaN) })).toThrow(/end/);
    expect(() => metricsHistoryRequest({ start: "2026" as unknown as Date })).toThrow(
      InvalidArgumentError,
    );
  });

  test("the response maps ascending with memCacheBytes", () => {
    const samples = metricsHistoryFromProto(
      create(MetricsHistoryResponseSchema, { samples: seededHistory() }),
    );
    expect(samples.map((entry) => entry.memCacheBytes)).toEqual([11, 22, 33]);
    expect(samples.map((entry) => entry.timestamp.getTime())).toEqual([
      WINDOW_START_MS + 5000,
      WINDOW_START_MS + 10_000,
      WINDOW_START_MS + 15_000,
    ]);
    expect(samples[0]).toMatchObject({ cpuUsedPct: 5, cpuCount: 2, memTotalBytes: 1000 });
  });
});

describe("sandbox.getMetricsHistory", () => {
  test("sends the range with the access token and maps the samples", async () => {
    const { sandbox, rayd } = await createTestSandbox();
    rayd.health.history = seededHistory();
    const samples = await sandbox.getMetricsHistory({
      start: WINDOW_START,
      end: new Date(WINDOW_START_MS + 300_000),
      maxPoints: 2,
    });
    const request = rayd.health.historyRequests.at(-1);
    expect(request?.startUnixMs).toBe(BigInt(WINDOW_START_MS));
    expect(request?.endUnixMs).toBe(BigInt(WINDOW_START_MS + 300_000));
    expect(request?.maxPoints).toBe(2);
    expect(samples.map((entry) => entry.memCacheBytes)).toEqual([11, 22, 33]);
    expect(rayd.health.historyCalls.at(-1)?.["x-access-token"]).toBe(ACCESS_TOKEN);
  });

  test("without options it sends zeros and honours requestTimeoutMs", async () => {
    const { sandbox, rayd } = await createTestSandbox();
    expect(await sandbox.getMetricsHistory({ requestTimeoutMs: 1234 })).toEqual([]);
    const request = rayd.health.historyRequests.at(-1);
    expect([request?.startUnixMs, request?.endUnixMs, request?.maxPoints]).toEqual([0n, 0n, 0]);
    expect(deadlineFromHeaders(rayd.health.historyCalls.at(-1) ?? {})).toBe(1234);
  });

  test("validates before any RPC", async () => {
    const { sandbox, rayd } = await createTestSandbox();
    await expect(
      sandbox.getMetricsHistory({ start: WINDOW_START, end: new Date(WINDOW_START_MS - 1000) }),
    ).rejects.toThrow(/posterior/);
    await expect(sandbox.getMetricsHistory({ maxPoints: 0 })).rejects.toThrow(/maxPoints/);
    expect(rayd.health.historyCalls).toEqual([]);
  });

  test("a pre-M9 agent is UnimplementedError with Python's reason and the gRPC cause", async () => {
    const { sandbox, rayd } = await createTestSandbox();
    rayd.health.historyUnimplemented = true;
    const error = await sandbox.getMetricsHistory().catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(UnimplementedError);
    expect(error).not.toBeInstanceOf(SandboxError);
    expect(error).toMatchObject({ feature: HISTORY_FEATURE, reason: HISTORY_UNIMPLEMENTED_REASON });
    expect(HISTORY_UNIMPLEMENTED_REASON).toBe(
      "la imagen es anterior a M9 (rayd sin MetricsHistory): publica una imagen M9",
    );
    expect(((error as Error).cause as SandboxError).grpcCode).toBe(Code.Unimplemented);
  });

  test("the getMetrics snapshot is unchanged and maps memCacheBytes", async () => {
    const { sandbox, rayd } = await createTestSandbox();
    rayd.health.memCacheBytes = 4096n;
    expect((await sandbox.getMetrics()).memCacheBytes).toBe(4096);
    expect(rayd.health.historyCalls).toEqual([]);
  });
});

describe("Sandbox.getMetricsHistory (static)", () => {
  let rayd: FakeRayd;
  let plane: FakeControlPlane;
  let savedToken: string | undefined;

  beforeEach(async () => {
    savedToken = process.env[ACCESS_TOKEN_ENV_VAR];
    delete process.env[ACCESS_TOKEN_ENV_VAR];
    rayd = await startRayd(ACCESS_TOKEN);
    plane = new FakeControlPlane({ endpoint: rayd.host, states: ["RUNNING"] });
  });

  afterEach(async () => {
    if (savedToken === undefined) {
      delete process.env[ACCESS_TOKEN_ENV_VAR];
    } else {
      process.env[ACCESS_TOKEN_ENV_VAR] = savedToken;
    }
    await rayd.close();
  });

  function call(options: Parameters<typeof Sandbox.getMetricsHistory>[1] = {}) {
    return Sandbox.getMetricsHistory(SANDBOX_ID, {
      controlPlane: plane,
      transport: rayd.transport,
      ...options,
    });
  }

  test("without a token it never calls AWS", async () => {
    await expect(call()).rejects.toBeInstanceOf(AuthenticationError);
    await expect(call()).rejects.toThrow(/accessToken/);
    expect(plane.calls).toEqual([]);
  });

  test("validates the id and the range before any AWS call", async () => {
    await expect(call({ accessToken: ACCESS_TOKEN, maxPoints: -1 })).rejects.toThrow(/maxPoints/);
    await expect(
      Sandbox.getMetricsHistory("", { accessToken: ACCESS_TOKEN, controlPlane: plane }),
    ).rejects.toBeInstanceOf(InvalidArgumentError);
    expect(plane.calls).toEqual([]);
  });

  test.each(["SUSPENDED", "SUSPENDING", "PENDING"])(
    "a %s sandbox is never woken: no JWE, no transport",
    async (state) => {
      plane.setStates([state]);
      const error = await call({ accessToken: ACCESS_TOKEN }).catch((caught: unknown) => caught);
      expect(error).toBeInstanceOf(SandboxStateError);
      expect((error as Error).message).toMatch(/connect\(\)/);
      expect(plane.callsTo("createAuthToken")).toEqual([]);
      expect(rayd.sessions).toBe(0);
    },
  );

  test("a terminated sandbox is not found", async () => {
    plane.setStates(["TERMINATED"]);
    await expect(call({ accessToken: ACCESS_TOKEN })).rejects.toBeInstanceOf(SandboxNotFoundError);
    expect(plane.callsTo("createAuthToken")).toEqual([]);
  });

  test("a running sandbox gets one JWE, one MetricsHistory with the token, and a closed transport", async () => {
    rayd.health.history = seededHistory();
    const samples = await call({ accessToken: ACCESS_TOKEN, start: WINDOW_START, maxPoints: 3 });
    expect(samples.map((entry) => entry.memCacheBytes)).toEqual([11, 22, 33]);
    const mints = plane.callsTo("createAuthToken");
    expect(mints).toHaveLength(1);
    expect(mints[0]?.ports?.map((spec) => [spec.start, spec.end])).toEqual([[8080, 8080]]);
    expect(rayd.health.historyRequests).toHaveLength(1);
    const request = rayd.health.historyRequests[0];
    expect([request?.startUnixMs, request?.endUnixMs, request?.maxPoints]).toEqual([
      BigInt(WINDOW_START_MS),
      0n,
      3,
    ]);
    expect(rayd.health.historyCalls[0]?.["x-access-token"]).toBe(ACCESS_TOKEN);
    expect(rayd.health.healthCalls).toEqual([]);
    expect(rayd.sessions).toBe(1);
    await expectAllClosed(rayd);
  });

  test("the token can come from RAYITO_ACCESS_TOKEN", async () => {
    process.env[ACCESS_TOKEN_ENV_VAR] = ACCESS_TOKEN;
    expect(await call()).toEqual([]);
    expect(rayd.health.historyCalls[0]?.["x-access-token"]).toBe(ACCESS_TOKEN);
    await expectAllClosed(rayd);
  });

  test("a wrong token is AuthenticationError and the transport is closed", async () => {
    await expect(call({ accessToken: generateAccessToken() })).rejects.toBeInstanceOf(
      AuthenticationError,
    );
    await expectAllClosed(rayd);
  });

  test("a proxy 403 re-mints once and retries", async () => {
    rayd.health.history = seededHistory();
    rayd.forbidNext(1);
    expect(await call({ accessToken: ACCESS_TOKEN })).toHaveLength(3);
    expect(plane.callsTo("createAuthToken")).toHaveLength(2);
    await expectAllClosed(rayd);
  });

  test("a pre-M9 agent is UnimplementedError", async () => {
    rayd.health.historyUnimplemented = true;
    const error = await call({ accessToken: ACCESS_TOKEN }).catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(UnimplementedError);
    expect(error).toMatchObject({
      feature: STATIC_HISTORY_FEATURE,
      reason: HISTORY_UNIMPLEMENTED_REASON,
    });
    expect(((error as Error).cause as SandboxError).grpcCode).toBe(Code.Unimplemented);
    await expectAllClosed(rayd);
  });

  test("an unreachable agent surfaces the translated error and still closes", async () => {
    rayd.health.history = seededHistory();
    rayd.forbidNext(2);
    const error = await call({ accessToken: ACCESS_TOKEN }).catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(AuthenticationError);
    expect((error as AuthenticationError).proxyRejected).toBe(true);
    await expectAllClosed(rayd);
  });
});

describe("guest facts on getInfo", () => {
  test("the instance getInfo merges the facts of the readiness Health", async () => {
    const { sandbox } = await createTestSandbox({
      beforeCreate: (rayd) => {
        rayd.health.cpuCount = 2;
        rayd.health.memoryTotalBytes = 8016n * MIB;
      },
    });
    const info = await sandbox.getInfo();
    expect([info.agentVersion, info.cpuCount, info.memoryMb]).toEqual(["test", 2, 8016]);
    expect([sandbox.info.agentVersion, sandbox.info.cpuCount]).toEqual([undefined, undefined]);
    expect(sandbox.launchInfo.agentVersion).toBeUndefined();
  });

  test("the instance getInfo carries the Health metadata; the static one does not read it", async () => {
    const { sandbox, plane } = await createTestSandbox({
      beforeCreate: (rayd) => {
        rayd.health.metadata = { env: "ci" };
      },
    });
    expect((await sandbox.getInfo()).metadata).toEqual({ env: "ci" });
    expect(sandbox.info.metadata).toBeUndefined();
    expect((await Sandbox.getInfo(SANDBOX_ID, { controlPlane: plane })).metadata).toBeUndefined();
  });

  test("a pre-M9 agent yields its version and leaves the rest undefined", async () => {
    const { sandbox } = await createTestSandbox();
    const info = await sandbox.getInfo();
    expect([info.agentVersion, info.cpuCount, info.memoryMb]).toEqual([
      "test",
      undefined,
      undefined,
    ]);
  });

  test("every Health refreshes the facts, even without a new resume generation", async () => {
    const { sandbox, rayd } = await createTestSandbox();
    rayd.health.cpuCount = 4;
    rayd.health.memoryTotalBytes = 2048n * MIB;
    await sandbox.getHealth();
    const info = await sandbox.getInfo();
    expect([info.cpuCount, info.memoryMb]).toEqual([4, 2048]);
  });

  test("the static getInfo never probes Health", async () => {
    const { plane, rayd } = await createTestSandbox();
    rayd.health.cpuCount = 2;
    const probes = rayd.health.healthCalls.length;
    const info = await Sandbox.getInfo(SANDBOX_ID, { controlPlane: plane });
    expect([info.agentVersion, info.cpuCount, info.memoryMb]).toEqual([
      undefined,
      undefined,
      undefined,
    ]);
    expect(rayd.health.healthCalls).toHaveLength(probes);
  });

  test("health() exposes cpuCount and memoryTotalBytes", async () => {
    const { sandbox, rayd } = await createTestSandbox();
    rayd.health.cpuCount = 2;
    rayd.health.memoryTotalBytes = 3n * MIB;
    expect(await sandbox.getHealth()).toMatchObject({
      cpuCount: 2,
      memoryTotalBytes: Number(3n * MIB),
    });
  });
});
