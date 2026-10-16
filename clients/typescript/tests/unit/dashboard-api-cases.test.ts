/**
 * Los casos de `testdata/dashboard/api-v1-cases.json` contra el núcleo de
 * `/api/v1`, endpoint por endpoint. El mismo fichero lo ejecuta Python
 * (`test_dashboard_api_cases.py`): cada respuesta valida contra
 * `api-v1.schema.json` y coincide con el subconjunto del cuerpo que fija el
 * vector.
 */

import { describe, expect, it } from "vitest";
import {
  apiRequest,
  expand,
  loadDashboardVectors,
  Rig,
  validateAgainstSchema,
  type World,
} from "./fake/dashboard-world.js";

interface Expect {
  readonly status: number;
  readonly schema?: string;
  readonly body?: unknown;
  readonly absentText?: string[];
  readonly writes?: string[];
  readonly noPlaneCalls?: boolean;
  readonly costQueries?: number;
  readonly listings?: number;
  readonly eventsQuery?: Record<string, unknown>;
  readonly logRead?: Record<string, unknown>;
}

interface Step {
  readonly request?: {
    readonly method: string;
    readonly path: string;
    readonly query?: Record<string, string[]>;
    readonly body?: unknown;
    readonly rawBody?: string;
  };
  readonly waitForJob?: boolean;
  readonly expect: Expect;
}

interface Case {
  readonly id: string;
  readonly world: World;
  readonly steps: Step[];
}

const CASES = loadDashboardVectors<{ cases: Case[] }>("api-v1-cases.json").cases;

function assertSubset(actual: unknown, expected: unknown, where: string): void {
  if (Array.isArray(expected)) {
    expect(Array.isArray(actual), where).toBe(true);
    expect((actual as unknown[]).length, where).toBe(expected.length);
    expected.forEach((item, index) => {
      assertSubset((actual as unknown[])[index], item, `${where}[${index}]`);
    });
  } else if (typeof expected === "object" && expected !== null) {
    expect(typeof actual === "object" && actual !== null, where).toBe(true);
    for (const [key, value] of Object.entries(expected)) {
      expect(actual, `${where}.${key}`).toHaveProperty(key);
      assertSubset((actual as Record<string, unknown>)[key], value, `${where}.${key}`);
    }
  } else {
    expect(actual, where).toBe(expected);
  }
}

function assertWorld(rig: Rig, expected: Expect): void {
  if (expected.writes !== undefined) {
    expect(rig.writes()).toEqual(expected.writes);
  }
  if (expected.noPlaneCalls === true) {
    expect(rig.plane.calls).toEqual([]);
  }
  if (expected.costQueries !== undefined) {
    expect(rig.costSource.queries).toHaveLength(expected.costQueries);
  }
  if (expected.listings !== undefined) {
    expect(rig.plane.operations().filter((name) => name === "listMicrovms")).toHaveLength(
      expected.listings,
    );
  }
  if (expected.eventsQuery !== undefined) {
    const last = rig.events?.calls.at(-1);
    expect({
      sandboxId: last?.sandboxId ?? null,
      types: last?.types === undefined ? null : [...last.types],
      limit: last?.limit,
      order: last?.order,
    }).toEqual(expected.eventsQuery);
  }
  if (expected.logRead !== undefined) {
    expect(rig.logs?.reads.at(-1)).toEqual(expected.logRead);
  }
}

describe("api-v1 shared cases (core router)", () => {
  for (const testCase of CASES) {
    it(testCase.id, async () => {
      const rig = new Rig(testCase.world);
      let lastBody: Record<string, unknown> = {};
      try {
        for (const step of testCase.steps) {
          let response: { status: number; body: Record<string, unknown> };
          if (step.waitForJob === true) {
            const jobId = (lastBody.job as { jobId: string }).jobId;
            await rig.waitForJob(jobId);
            response = await rig.api.handle(apiRequest("GET", `/actions/${jobId}`));
          } else {
            const request = step.request as NonNullable<Step["request"]>;
            response = await rig.api.handle(
              apiRequest(request.method, request.path, {
                query: request.query ?? {},
                body: expand(request.body),
                ...(request.rawBody === undefined ? {} : { rawBody: request.rawBody }),
              }),
            );
          }
          lastBody = response.body;
          expect(response.status, JSON.stringify(response.body)).toBe(step.expect.status);
          if (step.expect.schema !== undefined) {
            validateAgainstSchema(response.body, step.expect.schema);
          }
          if (step.expect.body !== undefined) {
            assertSubset(response.body, expand(step.expect.body), "body");
          }
          const text = JSON.stringify(response.body);
          for (const forbidden of step.expect.absentText ?? []) {
            expect(text).not.toContain(forbidden);
          }
          assertWorld(rig, step.expect);
        }
      } finally {
        await rig.api.close();
      }
    });
  }
});
