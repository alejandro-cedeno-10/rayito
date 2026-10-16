/**
 * La cola de acciones masivas: al cerrar, el sandbox en curso termina y nada
 * más se toca (ni el resto del trabajo en curso ni los trabajos encolados);
 * tope de trabajos sin terminar, retención de los terminados, cola única y
 * ritmo del TPS aplicado. Espejo de `test_dashboard_jobs.py`.
 */

import { describe, expect, it } from "vitest";
import { ABORTED_CODE, BulkJobs, JobsBusyError } from "../../src/dashboard/api/jobs.js";
import { type BulkAction, type BulkProgress, planBulk } from "../../src/dashboard/domain.js";
import type { RunBulkOptions } from "../../src/dashboard/fleet.js";
import { API_TPS } from "../../src/limits.js";
import { sandboxId } from "./fake/dashboard.js";
import { Rig } from "./fake/dashboard-world.js";

const SUSPEND: BulkAction = "suspend";
const EPSILON = 1e-9;

/**
 * Aplica cada sandbox sólo cuando el test suelta `release`; así el trabajo
 * queda a medias mientras se cierra la cola.
 */
class GatedFleet {
  readonly applied: string[] = [];
  readonly active: string[] = [];
  maxConcurrent = 0;
  #release!: () => void;
  readonly released = new Promise<void>((resolve) => {
    this.#release = resolve;
  });
  #firstStarted!: () => void;
  readonly firstStarted = new Promise<void>((resolve) => {
    this.#firstStarted = resolve;
  });

  release(): void {
    this.#release();
  }

  async runBulk(
    _action: BulkAction,
    sandboxIds: Iterable<string>,
    options: RunBulkOptions = {},
  ): Promise<void> {
    const ids = [...sandboxIds];
    this.active.push("job");
    this.maxConcurrent = Math.max(this.maxConcurrent, this.active.length);
    try {
      for (const [index, id] of ids.entries()) {
        this.#firstStarted();
        await this.released;
        this.applied.push(id);
        const progress: BulkProgress = {
          done: index + 1,
          total: ids.length,
          last: { sandboxId: id, outcome: "done" },
        };
        options.onProgress?.(progress);
      }
    } finally {
      this.active.pop();
    }
  }
}

function plan(count: number) {
  return planBulk(SUSPEND, count, undefined);
}

function codes(snapshot: Record<string, unknown> | undefined): Array<string | null> {
  const items = (snapshot?.items ?? []) as Array<{ code: string | null }>;
  return items.map((item) => item.code);
}

describe("BulkJobs", () => {
  it("close finishes the running item and aborts the rest without touching it", async () => {
    const fleet = new GatedFleet();
    const jobs = new BulkJobs(fleet);
    const first = jobs.submit(SUSPEND, ["a", "b", "c"], { plan: plan(3), appliedRate: undefined });
    const second = jobs.submit(SUSPEND, ["d", "e"], { plan: plan(2), appliedRate: undefined });
    await fleet.firstStarted;

    const closing = jobs.close();
    fleet.release();
    await closing;

    expect(fleet.applied).toEqual(["a"]);
    const finishedFirst = jobs.get(String(first.jobId));
    const finishedSecond = jobs.get(String(second.jobId));
    expect(finishedFirst?.status).toBe("finished");
    expect(finishedSecond?.status).toBe("finished");
    expect(codes(finishedFirst)).toEqual([null, ABORTED_CODE, ABORTED_CODE]);
    expect(codes(finishedSecond)).toEqual([ABORTED_CODE, ABORTED_CODE]);
  });

  it("close without jobs returns at once", async () => {
    await new BulkJobs(new GatedFleet()).close();
  });

  it("refuses more unfinished jobs than the cap", async () => {
    const fleet = new GatedFleet();
    const jobs = new BulkJobs(fleet, { activeMax: 1 });
    jobs.submit(SUSPEND, ["a"], { plan: plan(1), appliedRate: undefined });

    expect(() => jobs.submit(SUSPEND, ["b"], { plan: plan(1), appliedRate: undefined })).toThrow(
      JobsBusyError,
    );

    fleet.release();
    await jobs.close();
  });

  it("runs the jobs one at a time, in order", async () => {
    const fleet = new GatedFleet();
    const jobs = new BulkJobs(fleet);
    fleet.release();
    const ids = ["j1", "j2", "j3"].map(
      (id) => jobs.submit(SUSPEND, [id], { plan: plan(1), appliedRate: undefined }).jobId,
    );
    await new Promise((resolve) => setTimeout(resolve, 20));
    await jobs.close();
    expect(fleet.applied).toEqual(["j1", "j2", "j3"]);
    expect(fleet.maxConcurrent).toBe(1);
    expect(ids).toEqual(["job-1", "job-2", "job-3"]);
  });

  it("lists the newest first and keeps only the retained finished jobs", async () => {
    const fleet = new GatedFleet();
    fleet.release();
    const jobs = new BulkJobs(fleet, { retained: 2 });
    for (const id of ["a", "b", "c"]) {
      jobs.submit(SUSPEND, [id], { plan: plan(1), appliedRate: undefined });
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
    await jobs.close();
    expect(jobs.snapshots().map((job) => job.jobId)).toEqual(["job-3", "job-2"]);
    expect(jobs.get("job-1")).toBeUndefined();
  });

  it("calls onFinished after every job and snapshots hold ids, outcomes and codes only", async () => {
    const fleet = new GatedFleet();
    fleet.release();
    let finished = 0;
    const jobs = new BulkJobs(fleet, {
      onFinished: () => {
        finished += 1;
      },
      clock: () => new Date("2026-10-13T12:00:00Z"),
    });
    const snapshot = jobs.submit(SUSPEND, ["a", "a"], { plan: plan(1), appliedRate: undefined });
    await new Promise((resolve) => setTimeout(resolve, 10));
    await jobs.close();
    expect(finished).toBe(1);
    expect(snapshot.createdAt).toBe("2026-10-13T12:00:00.000Z");
    expect(Object.keys(snapshot).sort()).toEqual(
      [
        "action",
        "completed",
        "counts",
        "createdAt",
        "items",
        "jobId",
        "plan",
        "status",
        "total",
      ].sort(),
    );
  });
});

describe("the bulk queue pacing", () => {
  it("is paced at the applied TPS", async () => {
    const ids = Array.from({ length: 10 }, (_, index) => sandboxId(index + 1));
    const rig = new Rig({
      allowActions: true,
      sandboxes: ids.map((_, index) => ({ n: index + 1 })),
    });
    const starts: number[] = [];
    const original = rig.plane.suspendMicrovm.bind(rig.plane);
    rig.plane.suspendMicrovm = (id: string) => {
      starts.push(rig.clock.now);
      return original(id);
    };
    const response = await rig.api.handle({
      method: "POST",
      path: "/api/v1/actions",
      query: {},
      body: new TextEncoder().encode(
        JSON.stringify({ action: "suspend", sandboxIds: ids, confirm: true }),
      ),
    });
    await rig.waitForJob((response.body.job as { jobId: string }).jobId);
    const tps = API_TPS.SuspendMicrovm as number;
    expect(starts).toHaveLength(10);
    const gaps = starts
      .slice(tps - 1)
      .flatMap((later, index, rest) => (index === 0 ? [] : [later - (rest[index - 1] as number)]));
    expect(gaps.every((gap) => gap >= 1 / tps - EPSILON)).toBe(true);
    expect((starts.at(-1) as number) - (starts[0] as number)).toBeGreaterThanOrEqual(
      (10 - tps) / tps - EPSILON,
    );
  });

  it("the plan uses the applied rate", async () => {
    const rig = new Rig({ allowActions: true });
    const response = await rig.api.handle({
      method: "GET",
      path: "/api/v1/actions/plan",
      query: { action: ["suspend"], count: ["10"] },
      body: new Uint8Array(),
    });
    const tps = API_TPS.SuspendMicrovm as number;
    expect(response.body.plan).toMatchObject({ tps, estimatedSeconds: 10 / tps });
  });
});
