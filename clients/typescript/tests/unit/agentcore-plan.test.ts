/**
 * El plan de `--target agentcore` sin AWS (ADR-031, D5): cada caso de
 * `testdata/agentcore-deploy/plan-vectors.json`, que Python comparte, pasa por
 * `planDeploy()` y no construye ningún cliente de AWS.
 */

import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { planDeploy } from "../../src/agent/deploy.js";
import { AgentDeployOptions } from "../../src/agent/deploy-plan.js";
import { AgentProject } from "../../src/agent/project.js";
import { ScalingOptions } from "../../src/agent/scaling-domain.js";
import type { AgentCoreOptions } from "../../src/agentcore/models.js";
import { AgentCoreError } from "../../src/errors.js";
import { MS_PER_SECOND } from "../../src/units.js";
import { FOLDER, type PlanCase, planVectors } from "./agentcore-vectors.js";

const BASE_DIR = join(import.meta.dirname, "..", "..", "..", "..", "testdata", FOLDER);
const IGNORED_KEYS = new Set(["imageTag", "cost", "error"]);
const PLAN_ENVIRON_KEYS = ["AWS_REGION", "AWS_DEFAULT_REGION", "RAYITO_BUCKET"];

function scalingOf(values: Record<string, number>): ScalingOptions {
  const defaults = new ScalingOptions();
  return Object.assign(Object.create(ScalingOptions.prototype), {
    minVms: values.minVms ?? defaults.minVms,
    maxVms: values.maxVms ?? defaults.maxVms,
    warmVms: values.warmVms ?? defaults.warmVms,
    sessions: defaults.sessions,
    scaleInAfterMs: defaults.scaleInAfterMs,
  }) as ScalingOptions;
}

function agentcoreOf(call: PlanCase["call"]["agentcore"]): AgentCoreOptions | undefined {
  if (call === undefined) {
    return undefined;
  }
  const { idleSessionTimeoutSeconds: idle, maxSessionLifetimeSeconds: max, ...rest } = call;
  return {
    ...rest,
    ...(idle === undefined ? {} : { idleSessionTimeoutMs: idle * MS_PER_SECOND }),
    ...(max === undefined ? {} : { maxSessionLifetimeMs: max * MS_PER_SECOND }),
  };
}

function optionsOf(call: PlanCase["call"]): AgentDeployOptions {
  const { scaling, secretNames, accessToken, keepOnFailure, bucket } = call;
  const input = {
    ...(scaling === undefined ? {} : { scaling: scalingOf(scaling) }),
    ...(secretNames === undefined ? {} : { secretNames }),
    ...(accessToken === undefined ? {} : { accessToken }),
    ...(keepOnFailure === undefined ? {} : { keepOnFailure }),
    ...(bucket === undefined ? {} : { bucket }),
  };
  return new AgentDeployOptions(input);
}

async function planOf(testCase: PlanCase): Promise<unknown> {
  const saved = Object.fromEntries(PLAN_ENVIRON_KEYS.map((key) => [key, process.env[key]]));
  for (const key of PLAN_ENVIRON_KEYS) {
    delete process.env[key];
  }
  Object.assign(process.env, testCase.environ);
  try {
    const project = AgentProject.fromObject(testCase.project, { baseDir: BASE_DIR });
    return await planDeploy(".", {
      project,
      region: testCase.region,
      options: optionsOf(testCase.call),
      target: testCase.target as "agentcore",
      agentcore: agentcoreOf(testCase.call.agentcore),
    });
  } finally {
    for (const [key, value] of Object.entries(saved)) {
      if (value === undefined) {
        delete process.env[key];
      } else {
        process.env[key] = value;
      }
    }
    for (const key of Object.keys(testCase.environ)) {
      if (!(key in saved)) {
        delete process.env[key];
      }
    }
  }
}

describe("plan de AgentCore", () => {
  for (const testCase of planVectors) {
    it(testCase.name, async () => {
      const { error } = testCase.expected;
      if (error !== undefined) {
        const failure = await planOf(testCase).then(
          () => undefined,
          (thrown: unknown) => thrown,
        );
        expect(failure).toBeInstanceOf(AgentCoreError);
        const raised = failure as AgentCoreError;
        expect(raised.code).toBe(error.code);
        for (const [field, value] of Object.entries(error)) {
          if (field !== "code") {
            expect(raised.message).toContain(String(value));
          }
        }
        return;
      }
      const plan = (await planOf(testCase)) as Record<string, unknown>;
      for (const [key, value] of Object.entries(testCase.expected)) {
        if (!IGNORED_KEYS.has(key)) {
          expect(plan[key], key).toEqual(value);
        }
      }
      expect(plan.imageTag).toMatch(/^src-[0-9a-f]{16}$/);
    });
  }
});
