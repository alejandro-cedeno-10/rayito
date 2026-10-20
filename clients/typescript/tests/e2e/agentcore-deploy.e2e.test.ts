/**
 * Los agentes de ejemplo de TypeScript (y `python-strands`) desplegados en AWS
 * real con `target: "agentcore"` (ADR-031): `deployAgent`, `AgentCoreFleet` y el
 * rol del runtime. Por cada ejemplo: despliegue, estado `READY`, `invoke` y
 * `stream` con la respuesta del manifiesto, la sesión que continúa con el id
 * devuelto, un redespliegue sin cambios (tres `reuse`, sin build), `stopSession`
 * y `delete`; el rol sólo puede invocar su modelo y no lee secretos. `python-strands`
 * añade el redespliegue con un cambio de entorno (un `update` y un solo runtime) y
 * el `name_conflict` de un runtime sin la marca; `typescript-express-custom`, el
 * cambio de código (build nuevo y `update`). El guest y el host se prueban una
 * vez, en Python.
 *
 * Variables, coste y tope: cabecera de `agentcore-deploy-support.ts`. Nunca corre
 * sin `RAYITO_E2E=1` y `RAYITO_E2E_AGENTCORE=1`; no lo ejecuta ningún gate local.
 */

import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { SdkRuntimeControl } from "../../src/agentcore/aws.js";
import { runtimeNameFor } from "../../src/agentcore/names.js";
import { AgentCoreFleet, deployAgent, planDeploy } from "../../src/index.js";
import {
  agentcoreE2eReady,
  agentNameOf,
  BUCKET,
  EXPECTED_ANSWER,
  OTHER_MODEL_ARN_TEMPLATE,
  PROMPT,
  REGION,
  runtimeRoleOf,
  type Sample,
  SECRET_ARN_PATTERN_TEMPLATE,
  selectedSamples,
  simulatedDecision,
  stagedAgent,
  TEST_TIMEOUT_MS,
  tagsFromEnvironment,
  touchSource,
} from "./agentcore-deploy-support.js";

const READY = agentcoreE2eReady();
const SAMPLES = selectedSamples();
const ENVIRONMENT_SAMPLE = "python-strands";
const CODE_SAMPLE = "typescript-express-custom";
const CODE_FILE = "package.json";
const ALLOWED = "allowed";

const control = new SdkRuntimeControl({ region: REGION });

async function deployed(directory: string, kinds: string[]): Promise<AgentCoreFleet> {
  return deployAgent(directory, {
    target: "agentcore",
    region: REGION,
    options: {
      bucket: BUCKET,
      onStep: (step) => kinds.push(step.kind),
    },
    agentcore: { tags: tagsFromEnvironment() },
  });
}

async function runtimesNamed(name: string): Promise<number> {
  const runtimeName = runtimeNameFor(name);
  return (await control.listRuntimes()).filter((runtime) => runtime.runtimeName === runtimeName)
    .length;
}

async function checkAnswers(fleet: AgentCoreFleet): Promise<string> {
  const status = await fleet.status();
  expect(status.status).toBe("READY");
  const result = await fleet.invoke(PROMPT);
  expect(result.text).toContain(EXPECTED_ANSWER);
  const stream = await fleet.stream(PROMPT);
  const events: string[] = [];
  for await (const event of stream) {
    events.push(event.type);
  }
  expect(events.at(-1)).toBe("done");
  expect((await stream.result()).text).toContain(EXPECTED_ANSWER);
  const next = await fleet.invoke(PROMPT, { sessionId: result.sessionId });
  expect(next.sessionId).toBe(result.sessionId);
  return result.sessionId;
}

async function checkRole(name: string): Promise<void> {
  const role = await runtimeRoleOf(name);
  const otherModel = OTHER_MODEL_ARN_TEMPLATE.replace("{region}", REGION);
  const anySecret = SECRET_ARN_PATTERN_TEMPLATE.replace("{region}", REGION);
  expect(await simulatedDecision(role, "bedrock:InvokeModel", otherModel)).not.toBe(ALLOWED);
  expect(await simulatedDecision(role, "secretsmanager:GetSecretValue", anySecret)).not.toBe(
    ALLOWED,
  );
}

async function checkUnchangedRedeploy(directory: string): Promise<void> {
  const kinds: string[] = [];
  await deployed(directory, kinds);
  expect(kinds).toContain("stackNoChanges");
  expect(kinds).toContain("imageReused");
  expect(kinds).toContain("runtimeReuse");
  expect(kinds).not.toContain("buildStart");
  expect(kinds).not.toContain("runtimeCreate");
}

async function extraChecks(
  sample: Sample,
  directory: string,
  fleet: AgentCoreFleet,
): Promise<void> {
  const name = agentNameOf(sample);
  if (sample.id === ENVIRONMENT_SAMPLE) {
    const kinds: string[] = [];
    await deployed(
      stagedAgent(sample, join(tmpdir(), `agentcore-env-${name}`), { E2E_MARKER: "1" }),
      kinds,
    );
    expect(kinds).toContain("runtimeUpdate");
    expect(kinds).not.toContain("runtimeCreate");
    expect(await runtimesNamed(name)).toBe(1);
    await checkNameConflict(name, fleet);
  }
  if (sample.id === CODE_SAMPLE) {
    touchSource(directory, CODE_FILE, "");
    const kinds: string[] = [];
    await deployed(directory, kinds);
    expect(kinds).toContain("buildStart");
    expect(kinds).toContain("runtimeUpdate");
    expect(await runtimesNamed(name)).toBe(1);
  }
}

async function checkNameConflict(name: string, fleet: AgentCoreFleet): Promise<void> {
  const details = await control.getRuntime(fleet.runtimeId);
  if (details === undefined) {
    throw new Error("el runtime desplegado ya no existe");
  }
  const rewrite = (description: string) =>
    control.updateRuntime({
      runtimeId: details.runtimeId,
      containerUri: details.containerUri,
      roleArn: details.roleArn,
      environmentVariables: details.environmentVariables,
      idleRuntimeSessionTimeout: details.idleRuntimeSessionTimeout,
      maxLifetime: details.maxLifetime,
      description,
      requireMMDSV2: true,
      baseVersion: details.runtimeVersion,
    });
  await rewrite("creado a mano");
  try {
    await expect(deployed(join(tmpdir(), `agentcore-env-${name}`), [])).rejects.toMatchObject({
      code: "name_conflict",
    });
  } finally {
    await rewrite(details.description);
  }
}

describe.skipIf(!READY)("AgentCore: agentes de ejemplo desplegados en AWS real", () => {
  it("--dry-run no llama a AWS y nombra el agente de la ejecución", async () => {
    const [first] = SAMPLES;
    if (first === undefined) {
      return;
    }
    const plan = await planDeploy(stagedAgent(first, join(tmpdir(), "agentcore-plan")), {
      target: "agentcore",
      region: REGION,
      options: { bucket: BUCKET },
    });
    expect(plan.runtimeName).toBe(runtimeNameFor(agentNameOf(first)));
    expect(plan.steps.map((step) => step.action)).toEqual(["planned", "planned", "planned"]);
  });

  for (const sample of SAMPLES) {
    it(
      `${sample.id}: despliega, responde, continúa la sesión, no cambia al redesplegar y se borra`,
      async () => {
        const directory = stagedAgent(sample, join(tmpdir(), "agentcore-e2e"));
        const name = agentNameOf(sample);
        const kinds: string[] = [];
        let fleet: AgentCoreFleet | undefined;
        try {
          fleet = await deployed(directory, kinds);
          expect(kinds).toContain("runtimeCreate");
          const sessionId = await checkAnswers(fleet);
          await checkRole(name);
          await checkUnchangedRedeploy(directory);
          await extraChecks(sample, directory, fleet);
          expect(await fleet.stopSession(sessionId)).toBe(true);
        } finally {
          await (
            fleet ?? (await AgentCoreFleet.connect(name, { region: REGION }).catch(() => undefined))
          )?.delete();
        }
        expect(await runtimesNamed(name)).toBe(0);
      },
      TEST_TIMEOUT_MS,
    );
  }
});
