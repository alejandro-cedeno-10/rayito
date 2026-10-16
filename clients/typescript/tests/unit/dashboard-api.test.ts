/**
 * Lo del núcleo de `/api/v1` que los casos compartidos
 * (`dashboard-api-cases.test.ts`) no pueden expresar: la caché de lecturas
 * con reloj inyectado y la generación tras una acción, la cola llena, la
 * lectura de cuotas que falla sin bloquear las acciones, ventanas de coste
 * inválidas y qué peticiones exigen CSRF.
 */

import { describe, expect, it } from "vitest";
import { type JobQueue, JobsBusyError } from "../../src/dashboard/api/jobs.js";
import { actionTargets, translate } from "../../src/dashboard/api/router.js";
import type { QuotaSource } from "../../src/dashboard/ports.js";
import { AuthenticationError, InvalidArgumentError, QuotaExceededError } from "../../src/errors.js";
import { API_TPS, DASHBOARD_READ_CACHE_SECONDS } from "../../src/limits.js";
import { sandboxId } from "./fake/dashboard.js";
import { apiRequest, Rig } from "./fake/dashboard-world.js";

const KILL_ONE = {
  action: "kill",
  sandboxIds: [sandboxId(1)],
  confirm: true,
};

function listings(rig: Rig): number {
  return rig.plane.operations().filter((name) => name === "listMicrovms").length;
}

describe("read cache", () => {
  it("keeps following the injected clock after invalidate", async () => {
    const rig = new Rig({ sandboxes: [{ n: 1 }] });
    await rig.api.handle(apiRequest("GET", "/sandboxes"));
    rig.api.invalidate();
    await rig.api.handle(apiRequest("GET", "/sandboxes"));
    await rig.api.handle(apiRequest("GET", "/sandboxes"));
    expect(listings(rig)).toBe(2);
    rig.clock.advance(DASHBOARD_READ_CACHE_SECONDS);
    await rig.api.handle(apiRequest("GET", "/sandboxes"));
    expect(listings(rig)).toBe(3);
  });

  it("a read that started before an action finished does not refill the cache", async () => {
    const rig = new Rig({ sandboxes: [{ n: 1 }] });
    const original = rig.plane.listMicrovms.bind(rig.plane);
    let invalidated = false;
    rig.plane.listMicrovms = (options) => {
      const stream = original(options);
      if (!invalidated) {
        invalidated = true;
        rig.api.invalidate();
      }
      return stream;
    };
    await rig.api.handle(apiRequest("GET", "/sandboxes"));
    await rig.api.handle(apiRequest("GET", "/sandboxes"));
    expect(listings(rig)).toBe(2);
  });

  it("a finished action drops the cached reads", async () => {
    const rig = new Rig({ allowActions: true, sandboxes: [{ n: 1 }] });
    await rig.api.handle(apiRequest("GET", "/sandboxes"));
    const response = await rig.api.handle(apiRequest("POST", "/actions", { body: KILL_ONE }));
    await rig.waitForJob((response.body.job as { jobId: string }).jobId);
    const before = listings(rig);
    await rig.api.handle(apiRequest("GET", "/sandboxes"));
    expect(listings(rig)).toBe(before + 1);
    await rig.api.handle(apiRequest("GET", "/sandboxes"));
    expect(listings(rig)).toBe(before + 1);
  });
});

describe("actions", () => {
  it("refuses a new job with a 429 when the queue is full", async () => {
    const full: JobQueue = {
      submit() {
        throw new JobsBusyError();
      },
      get: () => undefined,
      snapshots: () => [],
      close: async () => undefined,
    };
    const rig = new Rig({ allowActions: true, sandboxes: [{ n: 1 }] }, { jobs: full });
    const response = await rig.api.handle(apiRequest("POST", "/actions", { body: KILL_ONE }));
    expect(response.status).toBe(429);
    expect(response.body).toMatchObject({ error: { code: "jobs_busy" } });
    expect(rig.writes()).toEqual([]);
  });

  it("an unreadable quota report does not block an action", async () => {
    const denied: QuotaSource = {
      async listAppliedQuotas() {
        throw new AuthenticationError("sin permiso", { awsCode: "AccessDeniedException" });
      },
    };
    const rig = new Rig({ allowActions: true, sandboxes: [{ n: 1 }] }, { quotaSource: denied });
    const response = await rig.api.handle(apiRequest("POST", "/actions", { body: KILL_ONE }));
    expect(response.status).toBe(202);
    expect(response.body).toMatchObject({ job: { plan: { tps: API_TPS.TerminateMicrovm } } });
    await rig.waitForJob((response.body.job as { jobId: string }).jobId);
    expect(rig.writes()).toEqual(["terminateMicrovm"]);
  });

  it("actionTargets answers undefined for any other route", () => {
    expect(actionTargets(apiRequest("GET", "/sandboxes"))).toBeUndefined();
    expect(actionTargets(apiRequest("POST", "/quotas"))).toBeUndefined();
    expect(
      actionTargets(
        apiRequest("POST", `/sandboxes/${sandboxId(1)}/resume`, { body: { confirm: true } }),
      ),
    ).toEqual({ action: "resume", sandboxIds: [sandboxId(1)] });
  });

  it("deduplicates repeated ids before queueing", () => {
    const targets = actionTargets(
      apiRequest("POST", "/actions", {
        body: { action: "kill", sandboxIds: [sandboxId(1), sandboxId(1)], confirm: true },
      }),
    );
    expect(targets?.sandboxIds).toEqual([sandboxId(1)]);
  });
});

describe("cost windows", () => {
  it("an invalid date is an argument error with a fixed message", async () => {
    const rig = new Rig({ costExplorer: true, sandboxes: [{ n: 1 }] });
    const response = await rig.api.handle(
      apiRequest("GET", "/cost", { query: { billed: ["true"], start: ["ayer"] } }),
    );
    expect(response.status).toBe(400);
    expect(rig.costSource.queries).toEqual([]);
  });

  it("passes the window and the region to Cost Explorer", async () => {
    const rig = new Rig({ costExplorer: true, sandboxes: [{ n: 1 }] });
    await rig.api.handle(
      apiRequest("GET", "/cost", {
        query: {
          billed: ["true"],
          start: ["2026-10-01"],
          end: ["2026-10-14"],
          region: ["us-east-1"],
        },
      }),
    );
    expect(rig.costSource.queries).toEqual([
      { start: "2026-10-01", end: "2026-10-14", granularity: "MONTHLY", region: "us-east-1" },
    ]);
  });
});

describe("requiresCsrf", () => {
  const rig = new Rig();

  it("writes and the billed cost need it, free reads do not", () => {
    expect(rig.api.requiresCsrf(apiRequest("POST", "/actions"))).toBe(true);
    expect(rig.api.requiresCsrf(apiRequest("GET", "/sandboxes"))).toBe(false);
    expect(rig.api.requiresCsrf(apiRequest("GET", "/cost"))).toBe(false);
    expect(rig.api.requiresCsrf(apiRequest("GET", "/cost", { query: { billed: ["true"] } }))).toBe(
      true,
    );
    expect(rig.api.requiresCsrf(apiRequest("GET", "/cost", { query: { billed: ["no"] } }))).toBe(
      false,
    );
    expect(rig.api.requiresCsrf(apiRequest("GET", "/cost", { query: { billed: ["YES"] } }))).toBe(
      true,
    );
  });
});

describe("translate", () => {
  it("maps a quota error to a throttle without a code", () => {
    expect(translate(new QuotaExceededError("cuota"))).toMatchObject({ code: "aws_throttled" });
  });

  it("keeps the message of an argument error without an AWS code", () => {
    expect(translate(new InvalidArgumentError("sandboxIds vacío"))).toMatchObject({
      code: "invalid_argument",
      message: "sandboxIds vacío",
    });
  });
});
