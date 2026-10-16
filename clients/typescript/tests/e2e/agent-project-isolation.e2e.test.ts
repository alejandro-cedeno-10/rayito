/**
 * Lo que un agente desplegado no puede hacer, en AWS real, un caso por runtime
 * (`agent-runtimes-byo`), espejo de
 * `clients/python/tests/e2e/test_agent_project_isolation.py`: no leer la
 * credencial del modelo, no encender trazas hacia fuera, hacer llamadas a
 * herramientas y emitir eventos mientras el egress directo sigue cerrado, y
 * desaparecer al parar.
 *
 * Variables y coste: cabeceras de `agent-project.e2e.test.ts` y del espejo de
 * Python.
 */

import { afterEach, describe, expect, it } from "vitest";
import {
  type AgentDeployment,
  type AgentEvent,
  LambdaMicrovmsControlPlane,
  Sandbox,
} from "../../src/index.js";
import { generateAccessToken } from "../../src/payload.js";
import {
  agentImage,
  BEDROCK_REGION,
  BEDROCK_SECRET,
  CASES,
  envDumpPrompt,
  modelCredential,
  PROMPT,
  projectFor,
  SANDBOX_WORKDIR,
  sandboxExposesKey,
  TEST_TIMEOUT_MS,
  TOOL_CASES,
  telemetryEnvNames,
  unsupportedReason,
} from "./agent-project-support.js";
import { e2eEnabled, e2eSettings, waitForTerminalState } from "./helpers.js";

describe.runIf(e2eEnabled() && Boolean(BEDROCK_SECRET))(
  "El agente desplegado no puede leer la credencial (Bedrock)",
  () => {
    const settings = e2eSettings();
    const region = settings.region ?? BEDROCK_REGION;
    const deployments: AgentDeployment[] = [];

    afterEach(async () => {
      for (const deployment of deployments.splice(0)) {
        await deployment.stop();
      }
    });

    it.for(CASES.map((item) => [item.id, item] as const))(
      "%s",
      { timeout: TEST_TIMEOUT_MS },
      async ([, item]) => {
        const template = await agentImage(item.image, region);
        if (unsupportedReason(item, template) !== undefined) {
          return;
        }
        const accessToken = generateAccessToken();
        const deployment = await projectFor(item, template).deploy({ region, accessToken });
        deployments.push(deployment);
        const sandbox = await Sandbox.connect(deployment.sandboxId, { region, accessToken });
        const key = await modelCredential(region);
        const envPath = `${SANDBOX_WORKDIR}/${item.id}-env.txt`;
        const usesTools = TOOL_CASES.includes(item.id);

        const events: AgentEvent[] = [];
        for await (const event of deployment.stream(usesTools ? envDumpPrompt(envPath) : PROMPT)) {
          events.push(event);
        }
        expect(events.at(-1)?.type).toBe("done");
        expect(events.some((event) => event.type === "agent_failed")).toBe(false);
        if (usesTools) {
          expect(
            events.some((event) => event.type === "tool_call" && event.status === "completed"),
          ).toBe(true);
          const envDump = await sandbox.files.read(envPath);
          expect(envDump.trim()).not.toBe("");
          expect(telemetryEnvNames(envDump)).toEqual([]);
          const inAgentEnv = envDump.includes(key);
          expect(inAgentEnv).toBe(false);
        }
        expect(await sandboxExposesKey(sandbox, key)).toEqual([]);

        await deployment.stop();
        deployments.pop();
        await waitForTerminalState(
          deployment.sandboxId,
          LambdaMicrovmsControlPlane.fromRegion(region),
        );
      },
    );
  },
);
