/**
 * `SandboxPool` sobre el plano de control falso multi-VM (máquina de estados
 * + token buckets en un reloj falso) y un `rayd` falso por plaza: los casos
 * de `clients/python/tests/unit/test_pool_sync.py` menos los de hilos, más
 * los temporizadores sin referencia y `await using`.
 */

import { chmod, readdir, readFile, stat, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { afterEach, describe, expect, test } from "vitest";
import {
  InMemoryPoolBackend,
  InvalidArgumentError,
  JsonFilePoolBackend,
  PoolClosedError,
  QuotaExceededError,
  Sandbox,
  SandboxPool,
  validatePoolConfig,
} from "../../src/index.js";
import { accessTokenSha256 } from "../../src/payload.js";
import { TEMP_SUFFIX } from "../../src/pool/backend.js";
import {
  FillBackoff,
  POOL_SCHEMA,
  pickReadySlot,
  reconcileAction,
  recordFromJson,
  recordToJson,
  type SlotRecord,
  statsFromRecords,
  TakePoll,
} from "../../src/pool/core.js";
import { ReadinessPoll } from "../../src/sandbox/readiness.js";
import { ACCESS_TOKEN_HEADER } from "../../src/transport/headers.js";
import { presentedTokenSha256 } from "./fake/common.js";
import { IMAGE_ARN, JWE } from "./fake/control-plane.js";
import { FakeClock, FakePoolControlPlane, maxCallsInWindow } from "./fake/pool-plane.js";
import { RecordingLogger, sleep, waitUntil } from "./helpers.js";

const POOL_TIMEOUT_MS = 7_200_000;
const POOL_MIN_REMAINING_MS = 3_600_000;
const TRANSPORT = { scheme: "http" as const, pingIdleConnection: false };
const NOW = new Date(Date.UTC(2026, 8, 16, 12, 0, 0));

class TickingNow {
  current = new Date(NOW);
  stepMs = 0;

  now = (): Date => {
    const value = new Date(this.current);
    this.current = new Date(this.current.getTime() + this.stepMs);
    return value;
  };

  advance(ms: number): void {
    this.current = new Date(this.current.getTime() + ms);
  }
}

class SpyBackend extends InMemoryPoolBackend {
  readonly saved = new Map<string, string[]>();

  override async save(record: SlotRecord): Promise<void> {
    const states = this.saved.get(record.sandboxId) ?? [];
    states.push(record.state);
    this.saved.set(record.sandboxId, states);
    await super.save(record);
  }
}

class BrokenWarmingSaveBackend extends SpyBackend {
  rejected = 0;

  override async save(record: SlotRecord): Promise<void> {
    if (record.state === "warming" && this.rejected === 0) {
      this.rejected += 1;
      throw new InvalidArgumentError("backend roto");
    }
    await super.save(record);
  }
}

interface Rig {
  readonly plane: FakePoolControlPlane;
  readonly clock: FakeClock;
  readonly now: TickingNow;
  readonly logger: RecordingLogger;
  readonly sleeps: number[];
  pool(size?: number, overrides?: Record<string, unknown>): SandboxPool;
}

const rigs: Rig[] = [];
const pools: SandboxPool[] = [];

afterEach(async () => {
  const finished = rigs.splice(0);
  for (const rig of finished) {
    rig.plane.releaseAll();
  }
  for (const pool of pools.splice(0)) {
    await pool.close();
  }
  for (const rig of finished) {
    await rig.plane.close();
  }
});

function rig(): Rig {
  const clock = new FakeClock();
  const now = new TickingNow();
  const plane = new FakePoolControlPlane({ clock, now: now.now });
  const logger = new RecordingLogger();
  const sleeps: number[] = [];
  const built: Rig = {
    plane,
    clock,
    now,
    logger,
    sleeps,
    pool(size = 2, overrides = {}) {
      const { backend, ...config } = overrides;
      const pool = new SandboxPool(
        {
          size,
          template: IMAGE_ARN,
          timeoutMs: POOL_TIMEOUT_MS,
          minRemainingMs: POOL_MIN_REMAINING_MS,
          readyTimeoutMs: 10_000,
          ...config,
        },
        {
          backend: backend as InMemoryPoolBackend | undefined,
          controlPlane: plane,
          transport: TRANSPORT,
          logger,
          monotonic: () => clock.seconds() * 1000,
          now: now.now,
          sleep: async (ms) => {
            sleeps.push(ms);
          },
          random: () => 0.5,
        },
      );
      pools.push(pool);
      return pool;
    },
  };
  rigs.push(built);
  return built;
}

async function waitIdle(pool: SandboxPool, ready: number): Promise<void> {
  await waitUntil(
    () => pool.stats().ready === ready && pool.stats().warming === 0,
    15_000,
    `el pool no llegó a ${ready} plazas listas`,
  );
}

function readyIds(pool: SandboxPool): string[] {
  return pool
    .stats()
    .slots.filter((slot) => slot.state === "ready")
    .map((slot) => slot.sandboxId);
}

function tempFile(name: string): string {
  return join(tmpdir(), `rayito-pool-${process.pid}-${Date.now()}-${name}`);
}

// ---------------------------------------------------------------- config

describe("PoolConfig", () => {
  test("defaults park for the whole wall", () => {
    const config = validatePoolConfig({ size: 1 });
    expect(config.timeoutMs).toBe(28_800_000);
    expect(config.minRemainingMs).toBe(3_600_000);
    expect(config.idle).toEqual({ maxIdleSeconds: 300, autoResume: true });
    expect(config.fillConcurrency).toBe(4);
    expect(config.sweepIntervalMs).toBe(30_000);
    expect(config.readyTimeoutMs).toBe(90_000);
  });

  test("rejects each rule naming the field", () => {
    expect(() => validatePoolConfig({ size: 0 })).toThrow(/size.*1\.\.=64/);
    expect(() => validatePoolConfig({ size: 65 })).toThrow(/size/);
    expect(() => validatePoolConfig({ size: 1, idle: { autoResume: false } })).toThrow(
      /autoResume/,
    );
    expect(() =>
      validatePoolConfig({ size: 1, timeoutMs: 600_000, idle: { maxIdleSeconds: 600 } }),
    ).toThrow(/idle\.maxIdleSeconds/);
    expect(() => validatePoolConfig({ size: 1, timeoutMs: 28_800_001 })).toThrow(/timeoutMs/);
    expect(() => validatePoolConfig({ size: 1, minRemainingMs: 59_000 })).toThrow(/minRemainingMs/);
    expect(() =>
      validatePoolConfig({ size: 1, timeoutMs: 720_000, minRemainingMs: 661_000 }),
    ).toThrow(/minRemainingMs.*660000/);
    expect(() => validatePoolConfig({ size: 1, fillConcurrency: 9 })).toThrow(/fillConcurrency/);
    expect(() => validatePoolConfig({ size: 1, sweepIntervalMs: 4999 })).toThrow(/sweepIntervalMs/);
    expect(() => validatePoolConfig({ size: 1, readyTimeoutMs: 0 })).toThrow(/readyTimeoutMs/);
    expect(() => validatePoolConfig({ size: 1, envs: { "": "x" } })).toThrow(/envs/);
    expect(() => validatePoolConfig({ size: 1, metadata: { "": "x" } })).toThrow(/metadata/);
    expect(() => validatePoolConfig({ size: 1, cpuTimeLimit: 0 })).toThrow(/cpuTimeLimit/);
  });
});

// ------------------------------------------------------------------ core

function record(
  sandboxId: string,
  startedAt: Date,
  state: "warming" | "ready" = "ready",
): SlotRecord {
  return {
    sandboxId,
    accessToken: "a".repeat(43),
    endpoint: "host:1",
    template: IMAGE_ARN,
    templateVersion: "1.0",
    startedAt,
    maximumDurationSeconds: 7200,
    region: "us-east-1",
    state,
    idle: { maxIdleSeconds: 300, suspendedDurationSeconds: 6900, autoResume: true },
    executionRoleArn: undefined,
    ingress: [],
    egress: [],
    parkedAt: state === "ready" ? new Date(startedAt.getTime() + 8000) : undefined,
  };
}

describe("pure core", () => {
  test("pickReadySlot takes the earliest expiry with enough life", () => {
    const older = record("microvm-old", new Date(NOW.getTime() - 60_000));
    const newer = record("microvm-new", NOW);
    const stale = record("microvm-stale", new Date(NOW.getTime() - 3_660_000));
    const warming = record("microvm-warm", new Date(NOW.getTime() - 3_600_000), "warming");
    expect(pickReadySlot([newer, warming, stale, older], NOW, POOL_MIN_REMAINING_MS)).toBe(older);
    expect(pickReadySlot([stale, warming], NOW, POOL_MIN_REMAINING_MS)).toBeUndefined();
  });

  test("reconcileAction branches", () => {
    const slot = record("microvm-x", NOW);
    expect(reconcileAction(slot, "SUSPENDED")).toBe("keep");
    expect(reconcileAction(slot, "PENDING")).toBe("keep");
    expect(reconcileAction(slot, "RUNNING")).toBe("repark");
    expect(reconcileAction(slot, undefined)).toBe("check");
    expect(reconcileAction(slot, "TERMINATED")).toBe("drop");
    expect(reconcileAction(record("microvm-w", NOW, "warming"), undefined)).toBe("keep");
  });

  test("FillBackoff doubles to sixty seconds and resets", () => {
    const backoff = new FillBackoff(() => 0.5);
    const delays = Array.from({ length: 8 }, () => backoff.nextDelayMs());
    expect(delays).toEqual([1000, 2000, 4000, 8000, 16000, 32000, 60000, 60000]);
    backoff.reset();
    expect(backoff.nextDelayMs()).toBe(1000);
  });

  test("TakePoll schedule is 100 -> 500 ms without jitter", () => {
    let now = 0;
    const poll = new TakePoll({ timeoutMs: 60_000, now: () => now });
    const delays = Array.from({ length: 5 }, () => poll.nextDelayMs());
    expect(delays).toEqual([100, 200, 400, 500, 500]);
    expect(TakePoll.jitter).toBe(0);
    now = 0;
    const plain = new ReadinessPoll({ timeoutMs: 60_000, now: () => now });
    expect(Array.from({ length: 5 }, () => plain.nextDelayMs())).toEqual([
      250, 500, 1000, 2000, 2000,
    ]);
  });

  test("record round-trips through the Python JSON schema", () => {
    const original = record("microvm-x", NOW);
    const json = recordToJson(original);
    expect(json.started_at).toBe("2026-09-16T12:00:00.000Z");
    expect(json.idle).toEqual({
      max_idle_seconds: 300,
      suspended_duration_seconds: 6900,
      auto_resume: true,
    });
    expect(Object.keys(json).sort()).toEqual([
      "access_token",
      "egress",
      "endpoint",
      "execution_role_arn",
      "idle",
      "ingress",
      "maximum_duration_seconds",
      "parked_at",
      "region",
      "sandbox_id",
      "started_at",
      "state",
      "template",
      "template_version",
    ]);
    expect(recordFromJson(json)).toEqual(original);
    expect(() => recordFromJson({ sandbox_id: "x" })).toThrow(InvalidArgumentError);
    expect(() => recordFromJson({ ...json, state: "taken" })).toThrow(/estado de plaza/);
  });

  test("stats arithmetic without tokens", () => {
    const stats = statsFromRecords(
      [record("microvm-b", NOW), record("microvm-a", NOW, "warming")],
      {
        size: 2,
        warming: 1,
        counters: { takes: 3, hits: 2, misses: 1, launched: 5, recycled: 1, lost: 1, failed: 0 },
      },
    );
    expect(stats.ready).toBe(1);
    expect(stats.takes).toBe(stats.hits + stats.misses);
    expect(stats.slots.map((slot) => slot.sandboxId)).toEqual(["microvm-a", "microvm-b"]);
    expect(JSON.stringify(stats)).not.toContain("a".repeat(43));
  });
});

// -------------------------------------------------------------- backends

describe("JsonFilePoolBackend", () => {
  test("round trip, atomic write, schema and permissions", async () => {
    const path = tempFile("pool.json");
    const backend = new JsonFilePoolBackend(path);
    const first = record("microvm-a", NOW);
    const second = record("microvm-b", NOW, "warming");
    expect(backend.persistent).toBe(true);
    await backend.save(first);
    await backend.save(second);
    const reloaded = await new JsonFilePoolBackend(path).load();
    expect(new Set(reloaded)).toEqual(new Set([first, second]));
    await expect(stat(`${path}.tmp`)).rejects.toThrow();
    const text = await readFile(path, "utf8");
    expect(JSON.parse(text).schema).toBe(POOL_SCHEMA);
    expect(text).toContain("a".repeat(43));
    if (process.platform !== "win32") {
      expect((await stat(path)).mode & 0o777).toBe(0o600);
    }
    await backend.delete("microvm-a");
    await backend.delete("microvm-zzz");
    expect((await backend.load()).map((r) => r.sandboxId)).toEqual(["microvm-b"]);
  });

  test("missing file loads empty and a foreign schema is rejected", async () => {
    const missing = new JsonFilePoolBackend(tempFile("missing.json"));
    expect(await missing.load()).toEqual([]);
    const path = tempFile("foreign.json");
    await writeFile(path, JSON.stringify({ schema: "other/1", slots: [] }));
    await expect(new JsonFilePoolBackend(path).load()).rejects.toThrow(/other\/1/);
  });

  test("a pre-created temporary is never reused", async () => {
    const path = tempFile("hijack.json");
    const planted = `${path}${TEMP_SUFFIX}`;
    await writeFile(planted, "del atacante", "utf8");
    if (process.platform !== "win32") {
      await chmod(planted, 0o666);
    }

    await new JsonFilePoolBackend(path).save(record("microvm-a", NOW));

    const text = await readFile(path, "utf8");
    expect(JSON.parse(text).schema).toBe(POOL_SCHEMA);
    expect(await readFile(planted, "utf8")).toBe("del atacante");
    if (process.platform !== "win32") {
      expect((await stat(path)).mode & 0o777).toBe(0o600);
    }
  });

  test.skipIf(process.platform === "win32")("a symlinked temporary truncates nothing", async () => {
    const path = tempFile("symlink-temp.json");
    const victim = tempFile("victim.txt");
    await writeFile(victim, "intacto", "utf8");
    await symlink(victim, `${path}${TEMP_SUFFIX}`);

    await new JsonFilePoolBackend(path).save(record("microvm-a", NOW));

    expect(await readFile(victim, "utf8")).toBe("intacto");
    expect(await readFile(path, "utf8")).toContain("a".repeat(43));
  });

  test.skipIf(process.platform === "win32")(
    "a symlinked state file is refused on read",
    async () => {
      const real = tempFile("real.json");
      await new JsonFilePoolBackend(real).save(record("microvm-a", NOW));
      const path = tempFile("symlinked.json");
      await symlink(real, path);

      await expect(new JsonFilePoolBackend(path).load()).rejects.toThrow(/fichero regular/);
    },
  );

  test("no temporary is left behind", async () => {
    const path = tempFile("leftovers.json");
    const backend = new JsonFilePoolBackend(path);

    await backend.save(record("microvm-a", NOW));
    await backend.save(record("microvm-b", NOW));
    await backend.delete("microvm-b");

    const stem = path.slice(dirname(path).length + 1);
    const siblings = (await readdir(dirname(path))).filter((name) => name.startsWith(stem));
    expect(siblings).toEqual([stem]);
  });
});

// -------------------------------------------------------------- warm-up

describe("SandboxPool", () => {
  test("warm-up sequence and record contents", async () => {
    const { plane, pool: make } = rig();
    const pool = await make(2).start();
    await waitIdle(pool, 2);

    const records = await pool.backend.load();
    expect(records).toHaveLength(2);
    expect(new Set(records.map((r) => r.accessToken)).size).toBe(2);
    for (const slot of records) {
      expect(slot.state).toBe("ready");
      expect(slot.parkedAt).toBeInstanceOf(Date);
      const vm = plane.microvms.get(slot.sandboxId);
      expect(vm?.tokenSha256).toBe(accessTokenSha256(slot.accessToken));
      expect(vm?.state).toBe("SUSPENDED");
      expect(plane.opsFor(slot.sandboxId)).toEqual([
        "CreateMicrovmAuthToken",
        "GetMicrovm",
        "SuspendMicrovm",
        "GetMicrovm",
      ]);
      expect(vm?.rayd.health.healthCalls[0]?.[ACCESS_TOKEN_HEADER]).toBe(slot.accessToken);
      expect(slot.idle).toEqual({
        maxIdleSeconds: 300,
        suspendedDurationSeconds: 7200 - 300,
        autoResume: true,
      });
    }
    expect(plane.callsTo("RunMicrovm")).toHaveLength(2);
    expect(pool.stats().launched).toBe(2);
  });

  test("record saved before the park", async () => {
    const { plane, pool: make } = rig();
    const backend = new SpyBackend();
    plane.fail("SuspendMicrovm", 1, new Error("suspend exploded"));
    const pool = await make(1, { backend }).start();
    await waitUntil(() => pool.stats().failed === 1, 15_000, "el calentamiento no falló");
    await waitIdle(pool, 1);

    const failed = [...plane.microvms.values()].find((vm) => vm.state === "TERMINATED");
    expect(failed).toBeDefined();
    expect(backend.saved.get(failed?.sandboxId ?? "")).toEqual(["warming"]);
    expect((await backend.load()).map((r) => r.sandboxId)).not.toContain(failed?.sandboxId);
    expect(pool.stats().launched).toBe(2);
  });

  test("backend failure at launch terminates the VM instead of leaking it", async () => {
    const { plane, sleeps, pool: make } = rig();
    const backend = new BrokenWarmingSaveBackend();
    const pool = await make(1, { backend }).start();
    await waitIdle(pool, 1);

    const launched = [...plane.microvms.keys()];
    expect(launched).toHaveLength(2);
    const [first, second] = launched as [string, string];
    expect(plane.microvms.get(first)?.state).toBe("TERMINATED");
    expect(plane.opsFor(first)).toEqual(["TerminateMicrovm"]);
    expect(backend.rejected).toBe(1);
    expect(pool.stats().failed).toBe(1);
    expect(pool.stats().launched).toBe(1);
    expect(sleeps).toEqual([1000]);
    expect(plane.liveIds).toEqual([second]);
    expect(readyIds(pool)).toEqual([second]);
    expect((await backend.load()).map((r) => r.sandboxId)).toEqual([second]);
  });

  test("fill respects fillConcurrency and the bucket pacing", async () => {
    const { plane, pool: make } = rig();
    const pool = await make(6, { fillConcurrency: 2 }).start();
    await waitIdle(pool, 6);

    const runs = plane.timestamps("RunMicrovm");
    const suspends = plane.timestamps("SuspendMicrovm");
    expect(runs).toHaveLength(6);
    expect(maxCallsInWindow(runs, 1)).toBeLessThanOrEqual(10);
    expect(maxCallsInWindow(suspends, 1)).toBeLessThanOrEqual(4);
    expect(Math.max(...suspends) - Math.min(...suspends)).toBeGreaterThanOrEqual((6 - 2) / 2);
    expect(plane.maxRunningAtLaunch).toBeLessThanOrEqual(2);
  });

  // ------------------------------------------------------------------ take

  test("take pops the oldest and resumes explicitly without getMicrovm", async () => {
    const { plane, now, pool: make } = rig();
    now.stepMs = 60_000;
    const pool = await make(2).start();
    await waitIdle(pool, 2);
    const records = await pool.backend.load();
    const oldest = [...records].sort(
      (a, b) => a.startedAt.getTime() - b.startedAt.getTime(),
    )[0] as SlotRecord;
    const before = plane.calls.length;

    const sandbox = await pool.take();
    try {
      expect(sandbox.sandboxId).toBe(oldest.sandboxId);
      expect(sandbox.accessToken).toBe(oldest.accessToken);
      expect(plane.opsFor(oldest.sandboxId, before)).toEqual([
        "ResumeMicrovm",
        "CreateMicrovmAuthToken",
      ]);
      expect((await pool.backend.load()).map((r) => r.sandboxId)).not.toContain(oldest.sandboxId);
      expect((await sandbox.runCode("1+1")).text).toBe("2");
      expect((await sandbox.getHealth()).sandboxId).toBe(oldest.sandboxId);
      await sandbox.getMetrics();
      const presented = plane.microvms.get(oldest.sandboxId)?.rayd.health.metricsCalls.at(-1);
      expect(presentedTokenSha256(presented ?? {})).toBe(accessTokenSha256(oldest.accessToken));
      expect(pool.stats()).toMatchObject({ hits: 1, takes: 1, misses: 0 });
    } finally {
      await sandbox.kill();
    }
    await waitIdle(pool, 2);
    expect(pool.stats().launched).toBe(3);
  });

  test("empty pool falls back to create()", async () => {
    const { plane, pool: make } = rig();
    const pool = await make(2).start();
    await waitIdle(pool, 2);
    const parked = new Set(readyIds(pool));
    plane.hold("RunMicrovm", 2);

    const first = await pool.take();
    const second = await pool.take();
    await waitUntil(() => plane.callsTo("RunMicrovm").length === 4, 15_000, "rellenos");
    const third = await pool.take();
    try {
      expect(parked).toEqual(new Set([first.sandboxId, second.sandboxId]));
      expect(parked.has(third.sandboxId)).toBe(false);
      expect((await third.runCode("1+1")).text).toBe("2");
      expect(pool.stats()).toMatchObject({ hits: 2, misses: 1, takes: 3 });
      expect(plane.callsTo("RunMicrovm")).toHaveLength(5);
    } finally {
      plane.release("RunMicrovm");
      for (const sandbox of [first, second, third]) {
        await sandbox.kill();
      }
    }
    await waitIdle(pool, 2);
    expect(pool.stats().launched).toBe(5);
  });

  test("take({ waitMs }) is served by a refill", async () => {
    const { plane, pool: make } = rig();
    plane.hold("RunMicrovm", 1);
    const pool = await make(1).start();
    await waitUntil(() => plane.callsTo("RunMicrovm").length === 1, 15_000, "calentamiento");
    setTimeout(() => plane.release("RunMicrovm"), 300);

    const sandbox = await pool.take({ waitMs: 5000 });
    try {
      expect(pool.stats()).toMatchObject({ hits: 1, misses: 0 });
    } finally {
      await sandbox.kill();
    }
  });

  test("slot lost at take falls back", async () => {
    const { plane, pool: make } = rig();
    const pool = await make(1).start();
    await waitIdle(pool, 1);
    const lostId = readyIds(pool)[0] as string;
    plane.fail("ResumeMicrovm", 1, false);

    const sandbox = await pool.take();
    try {
      expect(sandbox.sandboxId).not.toBe(lostId);
      expect((await sandbox.runCode("1+1")).text).toBe("2");
      expect(plane.opsFor(lostId)).toContain("TerminateMicrovm");
      expect(pool.stats()).toMatchObject({ lost: 1, misses: 1, hits: 0, takes: 1 });
    } finally {
      await sandbox.kill();
    }
    await waitIdle(pool, 1);
  });

  test("open failure at take falls back", async () => {
    const { plane, pool: make } = rig();
    const pool = await make(1).start();
    await waitIdle(pool, 1);
    const failingId = readyIds(pool)[0] as string;
    plane.fail("CreateMicrovmAuthToken", 2, new Error("denied"));

    const sandbox = await pool.take();
    try {
      expect(sandbox.sandboxId).not.toBe(failingId);
      expect(plane.opsFor(failingId)).toContain("TerminateMicrovm");
      expect(pool.stats()).toMatchObject({ failed: 1, misses: 1, hits: 0, lost: 0 });
    } finally {
      await sandbox.kill();
    }
  });

  // ----------------------------------------------------------------- sweep

  test("recycle with a fake clock", async () => {
    const { plane, now, pool: make } = rig();
    const pool = await make(1).start();
    await waitIdle(pool, 1);
    const oldId = readyIds(pool)[0] as string;

    now.advance(3_700_000);
    pool.requestSweep();

    await waitUntil(() => pool.stats().recycled === 1, 15_000, "reciclado");
    await waitIdle(pool, 1);
    expect(readyIds(pool)).not.toEqual([oldId]);
    expect(plane.microvms.get(oldId)?.state).toBe("TERMINATED");
    expect(pool.stats().launched).toBe(2);
  });

  test("terminated out of band is dropped after one getMicrovm", async () => {
    const { plane, logger, pool: make } = rig();
    const pool = await make(1).start();
    await waitIdle(pool, 1);
    const deadId = readyIds(pool)[0] as string;
    plane.setState(deadId, "TERMINATED", "Success.");
    const before = plane.opsFor(deadId).length;

    pool.requestSweep();

    await waitUntil(() => pool.stats().lost === 1, 15_000, "pérdida");
    await waitIdle(pool, 1);
    expect(plane.opsFor(deadId).slice(before)).toEqual(["GetMicrovm"]);
    expect(readyIds(pool)).not.toEqual([deadId]);
    const warning = logger.at("warn").find((line) => line.fields?.sandboxId === deadId);
    expect(String(warning?.fields?.reason)).toContain("Success.");
  });

  test("resumed out of band is re-parked", async () => {
    const { plane, logger, pool: make } = rig();
    const pool = await make(1).start();
    await waitIdle(pool, 1);
    const slotId = readyIds(pool)[0] as string;
    plane.setState(slotId, "RUNNING");
    const suspends = plane.callsTo("SuspendMicrovm").length;

    pool.requestSweep();

    await waitUntil(
      () => plane.callsTo("SuspendMicrovm").length === suspends + 1,
      15_000,
      "repark",
    );
    expect(plane.microvms.get(slotId)?.state).toBe("SUSPENDED");
    expect(readyIds(pool)).toEqual([slotId]);
    expect(
      logger.at("warn").filter((line) => line.message.includes("vuelta a aparcar")),
    ).toHaveLength(1);
    expect(pool.stats().lost).toBe(0);
  });

  test("a listing failure aborts the sweep", async () => {
    const { plane, logger, pool: make } = rig();
    const pool = await make(1).start();
    await waitIdle(pool, 1);
    const slotId = readyIds(pool)[0] as string;
    plane.setState(slotId, "TERMINATED", "Success.");
    const listings = plane.callsTo("ListMicrovms").length;
    plane.fail("ListMicrovms", listings + 1, new Error("throttled"));

    pool.requestSweep();

    await waitUntil(() => plane.callsTo("ListMicrovms").length === listings + 1, 15_000, "barrido");
    await sleep(100);
    expect(readyIds(pool)).toEqual([slotId]);
    expect(logger.at("warn").some((line) => line.message.includes("list-microvms"))).toBe(true);
  });

  // --------------------------------------------------------------- backoff

  test("backoff 1 s then 2 s after two failures, reset on success", async () => {
    const { plane, logger, sleeps, pool: make } = rig();
    plane.fail("RunMicrovm", 1, new QuotaExceededError("memory quota"));
    plane.fail("RunMicrovm", 2, new QuotaExceededError("memory quota"));
    const pool = await make(1).start();
    await waitIdle(pool, 1);

    expect(sleeps).toEqual([1000, 2000]);
    expect(plane.callsTo("RunMicrovm")).toHaveLength(3);
    expect(pool.stats().failed).toBe(2);
    const warnings = logger.at("warn");
    expect(warnings).toHaveLength(2);
    expect(warnings.every((line) => line.fields?.reason === "QuotaExceededError")).toBe(true);
    const token = (await pool.backend.load())[0]?.accessToken ?? "";
    expect(logger.dump()).not.toContain(token);

    plane.fail("RunMicrovm", 4, new QuotaExceededError("q"));
    await (await pool.take()).kill();
    await waitIdle(pool, 1);
    expect(sleeps).toEqual([1000, 2000, 1000]);
  });

  // ----------------------------------------------------------------- close

  test("close drains everything and later takes are rejected", async () => {
    const { plane, pool: make } = rig();
    const pool = await make(3).start();
    await waitIdle(pool, 3);
    const ids = readyIds(pool);

    await pool.close();

    for (const id of ids) {
      expect(plane.microvms.get(id)?.state).toBe("TERMINATED");
    }
    expect(await pool.backend.load()).toEqual([]);
    expect(plane.liveIds).toEqual([]);
    await expect(pool.take()).rejects.toThrow(PoolClosedError);
    await pool.close();
  });

  test("await using drains", async () => {
    const { plane, pool: make } = rig();
    let ids: string[] = [];
    {
      await using pool = await make(2).start();
      await waitIdle(pool, 2);
      ids = readyIds(pool);
    }
    expect(ids).toHaveLength(2);
    expect(plane.liveIds).toEqual([]);
  });

  test("close with a warm-up in flight terminates instead of parking", async () => {
    const { plane, pool: make } = rig();
    plane.hold("RunMicrovm", 1);
    const pool = await make(1).start();
    await waitUntil(() => plane.callsTo("RunMicrovm").length === 1, 15_000, "calentamiento");

    const closing = pool.close();
    await sleep(100);
    plane.release("RunMicrovm");
    await closing;

    expect(plane.liveIds).toEqual([]);
    expect(await pool.backend.load()).toEqual([]);
  });

  test("drain: false needs a persistent backend", async () => {
    const { plane, pool: make } = rig();
    const memory = await make(1).start();
    await waitIdle(memory, 1);
    await expect(memory.close({ drain: false })).rejects.toThrow(/drain.*persistent/);
    await memory.close();

    const backend = new JsonFilePoolBackend(tempFile("nodrain.json"));
    const persistent = await make(1, { backend }).start();
    await waitIdle(persistent, 1);
    const slotId = readyIds(persistent)[0] as string;
    const terminates = plane.callsTo("TerminateMicrovm").length;

    await persistent.close({ drain: false });

    expect((await backend.load()).map((r) => r.sandboxId)).toEqual([slotId]);
    expect(plane.callsTo("TerminateMicrovm")).toHaveLength(terminates);
    expect(plane.microvms.get(slotId)?.state).toBe("SUSPENDED");
  });

  test("recovery after a simulated crash", async () => {
    const { plane, pool: make } = rig();
    const path = tempFile("recovery.json");
    const first = await make(2, { backend: new JsonFilePoolBackend(path) }).start();
    await waitIdle(first, 2);
    await first.close({ drain: false });
    const [orphanId, readyId] = [...readyIds(first)].sort() as [string, string];
    const document = JSON.parse(await readFile(path, "utf8")) as {
      slots: Array<Record<string, unknown>>;
    };
    for (const slot of document.slots) {
      if (slot.sandbox_id === orphanId) {
        slot.state = "warming";
        slot.parked_at = null;
      }
    }
    await writeFile(path, JSON.stringify(document));
    const terminates = plane.callsTo("TerminateMicrovm").length;

    const second = await make(1, { backend: new JsonFilePoolBackend(path) }).start();

    expect(
      plane
        .callsTo("TerminateMicrovm")
        .slice(terminates)
        .map((c) => c.sandboxId),
    ).toEqual([orphanId]);
    expect(second.stats()).toMatchObject({ ready: 1, lost: 1, launched: 0 });
    const sandbox = await second.take();
    try {
      expect(sandbox.sandboxId).toBe(readyId);
      expect((await sandbox.runCode("1+1")).text).toBe("2");
    } finally {
      await sandbox.kill();
    }
  });

  // ---------------------------------------------------------- create({pool})

  test("Sandbox.create({ pool }) is sugar for take() and rejects launch options", async () => {
    const { pool: make } = rig();
    const pool = await make(1).start();
    await waitIdle(pool, 1);

    const sandbox = await Sandbox.create({ pool });
    try {
      expect(pool.stats().hits).toBe(1);
      expect((await sandbox.runCode("1+1")).text).toBe("2");
    } finally {
      await sandbox.kill();
    }
    await expect(Sandbox.create({ pool, envs: { A: "1" } })).rejects.toThrow(/`envs`/);
    await expect(Sandbox.create({ pool, template: IMAGE_ARN })).rejects.toThrow(/`template`/);
    await expect(Sandbox.create({ pool, idle: null })).rejects.toThrow(/`idle`/);
    await expect(Sandbox.create({ pool, metadata: { a: "b" } })).rejects.toThrow(/`metadata`/);
    await expect(Sandbox.create({ pool, cpuTimeLimit: 10 })).rejects.toThrow(/`cpuTimeLimit`/);
    await expect(Sandbox.create({ pool, keepOnFailure: true })).rejects.toThrow(/`keepOnFailure`/);
    await expect(Sandbox.create({ pool, transport: TRANSPORT })).rejects.toThrow(/`transport`/);
  });

  test("Sandbox.create({ pool }) passes the knobs through", async () => {
    const { plane, pool: make } = rig();
    const pool = await make(1).start();
    await waitIdle(pool, 1);

    const sandbox = await Sandbox.create({ pool, requestTimeoutMs: 7500 });
    try {
      await sandbox.getMetrics();
      const headers = plane.microvms.get(sandbox.sandboxId)?.rayd.health.metricsCalls.at(-1);
      expect(headers?.["grpc-timeout"]).toMatch(/^7[0-5]\d\dm$/);
      expect(Sandbox.coreOf(sandbox).requestTimeoutMs).toBe(7500);
    } finally {
      await sandbox.kill();
    }
  });

  test("an unstarted pool raises PoolClosedError", async () => {
    const { pool: make } = rig();
    const pool = make(1);
    await expect(Sandbox.create({ pool })).rejects.toThrow(/not started/);
    await expect(pool.take()).rejects.toThrow(PoolClosedError);
  });

  // --------------------------------------------------------------- secrets

  test("the logger never sees a secret", async () => {
    const { plane, logger, pool: make } = rig();
    const pool = await make(2).start();
    await waitIdle(pool, 2);
    const tokens = (await pool.backend.load()).map((r) => r.accessToken);
    const sandbox = await pool.take();
    tokens.push(sandbox.accessToken);
    await sandbox.kill();
    await waitIdle(pool, 2);
    const lostId = readyIds(pool)[0] as string;
    plane.setState(lostId, "TERMINATED", "Success.");
    pool.requestSweep();
    await waitUntil(() => pool.stats().lost === 1, 15_000, "pérdida");
    await waitIdle(pool, 2);
    tokens.push(...(await pool.backend.load()).map((r) => r.accessToken));
    await pool.close();

    const text = logger.dump();
    expect(logger.lines.length).toBeGreaterThan(0);
    expect(text).not.toContain(JWE);
    expect(text).not.toContain("token_sha256");
    for (const token of tokens) {
      expect(text).not.toContain(token);
    }
  });

  // ---------------------------------------------------------------- timers

  test("an idle started pool holds no ref'd timer", async () => {
    const { pool: make } = rig();
    const pool = await make(1).start();
    await waitIdle(pool, 1);
    await sleep(50);

    const timers = SandboxPool.timersOf(pool);
    expect(timers.size).toBeGreaterThan(0);
    for (const timer of timers) {
      expect(timer.hasRef()).toBe(false);
    }
  });

  // ----------------------------------------------------------------- stats

  test("stats after a scripted sequence", async () => {
    const { plane, now, pool: make } = rig();
    now.stepMs = 60_000;
    const pool = await make(2).start();
    await waitIdle(pool, 2);
    now.stepMs = 0;
    const [oldest, newest] = [...(await pool.backend.load())].sort(
      (a, b) => a.startedAt.getTime() - b.startedAt.getTime(),
    ) as [SlotRecord, SlotRecord];
    expect(oldest.startedAt.getTime()).toBeLessThan(newest.startedAt.getTime());
    now.current = new Date(newest.startedAt.getTime() + 7_200_000 - POOL_MIN_REMAINING_MS);
    pool.requestSweep();
    await waitUntil(() => pool.stats().recycled === 1, 15_000, "reciclado");
    await waitIdle(pool, 2);
    plane.hold("RunMicrovm", 2);

    const hit = await pool.take();
    await waitUntil(() => plane.callsTo("RunMicrovm").length === 4, 15_000, "relleno");
    const remainingId = readyIds(pool)[0] as string;
    plane.setState(remainingId, "TERMINATED", "Success.");
    pool.requestSweep();
    await waitUntil(() => plane.callsTo("RunMicrovm").length === 5, 15_000, "segundo relleno");
    const miss = await pool.take();
    plane.release("RunMicrovm");
    await waitIdle(pool, 2);
    await hit.kill();
    await miss.kill();

    const stats = pool.stats();
    expect(stats).toMatchObject({
      size: 2,
      ready: 2,
      warming: 0,
      takes: 2,
      hits: 1,
      misses: 1,
      launched: 6,
      recycled: 1,
      lost: 1,
      failed: 0,
    });
    expect(stats.slots.every((slot) => slot.state === "ready")).toBe(true);
    expect("accessToken" in (stats.slots[0] as object)).toBe(false);
  });
});
