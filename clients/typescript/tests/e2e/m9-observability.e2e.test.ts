/**
 * M9 `m9-sandbox-observability` a través del SDK TypeScript contra AWS real
 * (design D14, espejo de `clients/python/tests/e2e/test_m9_observability.py`):
 * el historial de métricas de un sandbox con 16 MiB en page cache
 * (`getMetricsHistory`, `maxPoints`, una ventana `start`/`end`, la variante
 * estática con el access token), los datos del guest en `getInfo()` frente a
 * `/proc`, y el paginador con filtro de metadatos, reanudación por
 * `nextToken` y `order`. Requiere una versión de `rayito-base` publicada desde
 * este árbol en `RAYITO_TEMPLATE`. Tres MicroVMs de `timeoutMs` 900 000.
 */

import { randomBytes, randomUUID } from "node:crypto";
import { describe, expect, test } from "vitest";
import {
  Sandbox,
  type SandboxListItem,
  type SandboxListPaginator,
  type SandboxMetrics,
} from "../../src/index.js";
import {
  type E2EContext,
  e2eEnabled,
  MAX_TEST_SANDBOX_TIMEOUT_MS,
  seconds,
  sleep,
  TEST_SANDBOX_TIMEOUT_MS,
  useE2E,
} from "./helpers.js";

const PAGE_CACHE_BYTES = 16 * 1024 * 1024;
const SAMPLING_WAIT_MS = 60_000;
const MIN_SAMPLES_AFTER_WAIT = 9;
const REDUCED_POINTS = 3;
const CREATE_GAP_MS = 2000;
const HOME = "/home/user";
const SAMPLE_INTERVAL_S = 5;
const SAMPLE_TOLERANCE_S = 1.5;
const REGULAR_DELTA_SHARE = 0.9;
const PAUSE_AT_MS = 90_000;
const PAUSED_FOR_MS = 30_000;
const WINDOW_MS = 300_000;
const MIN_WINDOW_SAMPLES = 45;
const GAP_MIN_S = 30;
const CLOCK_TOLERANCE_MS = 2_000;
const WINDOWED_FROM_MS = 120_000;
const WINDOWED_TO_MS = 180_000;
const DOWNSAMPLED_POINTS = 10;

function note(label: string, value: string | number): void {
  console.log(`\n[m9-observability] ${label}: ${value}`);
}

/** `create()` con los guardrails de todo sandbox de test y unos metadatos propios de la corrida. */
async function createObservedSandbox(
  context: E2EContext,
  metadata: Readonly<Record<string, string>>,
): Promise<Sandbox> {
  if (TEST_SANDBOX_TIMEOUT_MS > MAX_TEST_SANDBOX_TIMEOUT_MS) {
    throw new Error("guardrail: timeoutMs <= 1 800 000");
  }
  const started = performance.now();
  const sandbox = await Sandbox.create({
    template: context.templateArn,
    timeoutMs: TEST_SANDBOX_TIMEOUT_MS,
    idle: null,
    metadata,
    executionRoleArn: context.settings.executionRoleArn,
    ingress: ["ALL_INGRESS"],
    logging: context.settings.logging,
    controlPlane: context.controlPlane,
  });
  context.created.push(sandbox);
  context.bootTimings.set(sandbox.sandboxId, seconds(started));
  return sandbox;
}

function timestamps(samples: readonly SandboxMetrics[]): number[] {
  return samples.map((sample) => sample.timestamp.getTime());
}

function expectStrictlyAscending(values: readonly number[]): void {
  for (let index = 1; index < values.length; index += 1) {
    expect(values[index]).toBeGreaterThan(values[index - 1] as number);
  }
}

/** Los segundos entre muestras consecutivas. */
function deltasSeconds(samples: readonly SandboxMetrics[]): number[] {
  return samples
    .slice(1)
    .map(
      (later, index) =>
        (later.timestamp.getTime() - (samples[index] as SandboxMetrics).timestamp.getTime()) / 1000,
    );
}

interface Gap {
  readonly startMs: number;
  readonly endMs: number;
}

/** Los huecos de al menos `GAP_MIN_S` entre muestras: lo que deja una pausa. */
function gapsOf(samples: readonly SandboxMetrics[]): Gap[] {
  return samples.slice(1).flatMap((later, index) => {
    const earlier = samples[index] as SandboxMetrics;
    const gap = {
      startMs: earlier.timestamp.getTime(),
      endMs: later.timestamp.getTime(),
    };
    return (gap.endMs - gap.startMs) / 1000 >= GAP_MIN_S ? [gap] : [];
  });
}

async function walk(paginator: SandboxListPaginator): Promise<SandboxListItem[]> {
  const items: SandboxListItem[] = [];
  while (paginator.hasNext) {
    items.push(...(await paginator.nextItems()));
  }
  return items;
}

function ids(items: readonly SandboxListItem[]): string[] {
  return items.map((item) => item.sandboxId);
}

function byStartedAt(items: readonly SandboxListItem[]): string[] {
  return [...items]
    .sort(
      (left, right) =>
        left.startedAt.getTime() - right.startedAt.getTime() ||
        (left.sandboxId < right.sandboxId ? -1 : 1),
    )
    .map((item) => item.sandboxId);
}

describe.skipIf(!e2eEnabled())("M9 observabilidad (TypeScript)", () => {
  const e2e = useE2E();
  const runId = randomUUID();

  test("historial de métricas, maxPoints, ventana, variante estática y getInfo", async () => {
    const sandbox = await createObservedSandbox(e2e, { run: `other-${runId}` });
    await sandbox.commands.run(
      `head -c ${PAGE_CACHE_BYTES} /dev/urandom > ${HOME}/page-cache.bin && sync`,
    );
    await sleep(SAMPLING_WAIT_MS);

    const rpcStarted = performance.now();
    const history = await sandbox.getMetricsHistory();
    note("history_rpc_s", seconds(rpcStarted).toFixed(3));
    note("samples", history.length);
    expect(history.length).toBeGreaterThanOrEqual(MIN_SAMPLES_AFTER_WAIT);
    expectStrictlyAscending(timestamps(history));
    expect(Math.max(...history.map((sample) => sample.memCacheBytes))).toBeGreaterThan(0);

    const reduced = await sandbox.getMetricsHistory({ maxPoints: REDUCED_POINTS });
    expect(reduced).toHaveLength(REDUCED_POINTS);
    expectStrictlyAscending(timestamps(reduced));

    const start = history[2]?.timestamp as Date;
    const end = history[5]?.timestamp as Date;
    const window = await sandbox.getMetricsHistory({ start, end });
    expect(timestamps(window)).toEqual(timestamps(history.slice(2, 6)));

    const fromId = await Sandbox.getMetricsHistory(sandbox.sandboxId, {
      accessToken: sandbox.accessToken,
      controlPlane: e2e.controlPlane,
    });
    expect(fromId.length).toBeGreaterThanOrEqual(history.length);
    expectStrictlyAscending(timestamps(fromId));

    const info = await sandbox.getInfo();
    const nproc = Number((await sandbox.commands.run("nproc")).stdout.trim());
    const memTotalKb = Number(
      (await sandbox.commands.run("awk '/^MemTotal:/ {print $2}' /proc/meminfo")).stdout.trim(),
    );
    note("guest_cpu_count", String(info.cpuCount));
    note("guest_memory_mb", String(info.memoryMb));
    note("nproc", nproc);
    expect(info.agentVersion).toBeTruthy();
    expect(info.cpuCount).toBeGreaterThanOrEqual(1);
    expect(info.cpuCount).toBeLessThanOrEqual(nproc);
    expect(info.memoryMb).toBe(Math.floor(memTotalKb / 1024));
  });

  test("paginador con filtro de metadatos, reanudación por nextToken y orden", async () => {
    const first = await createObservedSandbox(e2e, { run: runId });
    await sleep(CREATE_GAP_MS);
    const second = await createObservedSandbox(e2e, { run: runId });
    const expected = [first.sandboxId, second.sandboxId].sort();
    const filters = {
      template: e2e.templateArn,
      metadata: { run: runId },
      controlPlane: e2e.controlPlane,
    };

    const walkStarted = performance.now();
    const paginator = Sandbox.paginate({ ...filters, limit: 1 });
    expect(paginator.hasNext).toBe(true);
    const walked = await walk(paginator);
    note("walk_s", seconds(walkStarted).toFixed(3));
    expect(ids(walked).sort()).toEqual(expected);
    expect(walked.every((item) => item.metadata?.run === runId)).toBe(true);
    expect(walked.every((item) => item.template === e2e.templateArn)).toBe(true);

    const resumable = Sandbox.paginate({ ...filters, limit: 1 });
    const head = await resumable.nextItems();
    expect(head).toHaveLength(1);
    expect(resumable.hasNext).toBe(true);
    const rest = await walk(
      Sandbox.paginate({ ...filters, limit: 1, nextToken: resumable.nextToken }),
    );
    const union = [...ids(head), ...ids(rest)];
    expect(union.sort()).toEqual(expected);
    expect(new Set(union).size).toBe(union.length);

    const ascending = await walk(Sandbox.paginate({ ...filters, order: "asc" }));
    const descending = await walk(Sandbox.paginate({ ...filters, order: "desc" }));
    expect(ids(ascending)).toEqual(byStartedAt(ascending));
    expect(ids(ascending)).toEqual([first.sandboxId, second.sandboxId]);
    expect(ids(descending)).toEqual([second.sandboxId, first.sandboxId]);
  });
  test("metrics history across a pause shows one gap that covers it", async () => {
    const sandbox = await createObservedSandbox(e2e, { run: `pause-${runId}` });
    const t0 = Date.now();
    await sandbox.files.write(
      `${HOME}/m9-page-cache.bin`,
      new Uint8Array(randomBytes(PAGE_CACHE_BYTES)),
    );
    await sleep(Math.max(0, t0 + PAUSE_AT_MS - Date.now()));
    expect(await sandbox.pause()).toBe(true);
    const pausedAt = Date.now();
    await sleep(PAUSED_FOR_MS);
    const resumeCalledAt = Date.now();
    await sandbox.resume();
    const end = t0 + WINDOW_MS;
    await sleep(Math.max(0, end + CLOCK_TOLERANCE_MS - Date.now()));

    const rpcStarted = performance.now();
    const history = await sandbox.getMetricsHistory({ start: new Date(t0), end: new Date(end) });
    note("history_rpc_s", seconds(rpcStarted).toFixed(3));
    note("samples", history.length);
    expect(history.length).toBeGreaterThanOrEqual(MIN_WINDOW_SAMPLES);
    const stamps = timestamps(history);
    expect(stamps.every((stamp) => stamp >= t0 && stamp <= end)).toBe(true);
    expectStrictlyAscending(stamps);
    const found = gapsOf(history);
    const regular = deltasSeconds(history).filter(
      (delta) => Math.abs(delta - SAMPLE_INTERVAL_S) <= SAMPLE_TOLERANCE_S,
    );
    expect(regular.length).toBeGreaterThanOrEqual(
      REGULAR_DELTA_SHARE * (history.length - 1 - found.length),
    );
    expect(found).toHaveLength(1);
    const [gap] = found as [Gap];
    note("gap_s", ((gap.endMs - gap.startMs) / 1000).toFixed(1));
    expect(gap.startMs).toBeLessThanOrEqual(pausedAt + CLOCK_TOLERANCE_MS);
    expect(gap.endMs).toBeGreaterThanOrEqual(resumeCalledAt - CLOCK_TOLERANCE_MS);
    expect(Math.max(...history.map((sample) => sample.memCacheBytes))).toBeGreaterThan(0);

    const windowStart = t0 + WINDOWED_FROM_MS;
    const windowEnd = t0 + WINDOWED_TO_MS;
    const windowed = await sandbox.getMetricsHistory({
      start: new Date(windowStart),
      end: new Date(windowEnd),
    });
    expect(windowed.length).toBeGreaterThan(0);
    expect(timestamps(windowed).every((stamp) => stamp >= windowStart && stamp <= windowEnd)).toBe(
      true,
    );
    const reduced = await sandbox.getMetricsHistory({ maxPoints: DOWNSAMPLED_POINTS });
    expect(reduced).toHaveLength(DOWNSAMPLED_POINTS);
    expectStrictlyAscending(timestamps(reduced));

    const fromId = await Sandbox.getMetricsHistory(sandbox.sandboxId, {
      accessToken: sandbox.accessToken,
      controlPlane: e2e.controlPlane,
      start: new Date(t0),
      end: new Date(end),
    });
    expect(fromId).toHaveLength(history.length);
  });
});
