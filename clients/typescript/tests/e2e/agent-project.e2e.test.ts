/**
 * `AgentProject.run()` y `AgentProject.deploy()` contra AWS real, un caso por
 * runtime (`agent-runtimes-byo`), espejo de
 * `clients/python/tests/e2e/test_agent_project_runs.py`: deepagents,
 * LangGraph, Strands (SDK y harness), un comando propio, OpenCode, Hermes y
 * Strands sobre Node, con Claude Haiku 4.5 por `bedrockGateway` y el egress
 * cerrado. Cada runtime hace un `run()` y un `deploy()` con `status()`,
 * `invoke()`, `stream()` y `stop()`.
 *
 * Además de `RAYITO_E2E=1` y `RAYITO_TEMPLATE`:
 * - `RAYITO_E2E_BEDROCK_SECRET`: secreto con `Bearer <clave de API de Bedrock
 *   de corta duración>`; sin él se salta todo.
 * - `RAYITO_E2E_AGENT_PROJECT_PREFIX` y `RAYITO_E2E_AGENT_PROJECT_BUCKET`: si
 *   están, la suite construye `<prefijo>-py` y `<prefijo>-cli` con
 *   `AgentTemplate` y el código de `testdata/agent-project-e2e/`. El prefijo
 *   lleva el id de la ejecución y el carril (`rayito-v014-<id>-tslane`).
 * - `RAYITO_E2E_AGENT_PROJECT_BASE` (opcional): imagen base con capabilities
 *   de la que parten las dos imágenes.
 * - `RAYITO_E2E_AGENT_PROJECT_PY_TEMPLATE` y
 *   `RAYITO_E2E_AGENT_PROJECT_CLI_TEMPLATE`: imágenes ya construidas.
 * - `RAYITO_E2E_ANTHROPIC_SECRET` (opcional): para Hermes si su matriz no
 *   admite Bedrock.
 *
 * Coste: cabecera del espejo de Python. Los despliegues se paran en
 * `afterEach`; las imágenes y lo que quede lo borra el inventario de quien
 * lanza la ejecución.
 */

import { afterEach, describe, expect, it } from "vitest";
import type { AgentDeployment } from "../../src/index.js";
import {
  agentImage,
  BEDROCK_REGION,
  BEDROCK_SECRET,
  CASES,
  PROMPT,
  projectFor,
  TEST_TIMEOUT_MS,
  unsupportedReason,
} from "./agent-project-support.js";
import { e2eEnabled, e2eSettings } from "./helpers.js";

describe.runIf(e2eEnabled() && Boolean(BEDROCK_SECRET))(
  "AgentProject en AWS real (Bedrock)",
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
      "run: %s",
      { timeout: TEST_TIMEOUT_MS },
      async ([, item]) => {
        const template = await agentImage(item.image, region);
        const reason = unsupportedReason(item, template);
        if (reason !== undefined) {
          return;
        }
        const result = await projectFor(item, template).run(PROMPT, { region });
        expect(result.text.trim()).not.toBe("");
        if (item.callsTheModel) {
          expect(result.usage.total).toBeGreaterThan(0);
        }
      },
    );

    it.for(CASES.map((item) => [item.id, item] as const))(
      "deploy, invoke, stream y stop: %s",
      { timeout: TEST_TIMEOUT_MS },
      async ([, item]) => {
        const template = await agentImage(item.image, region);
        if (unsupportedReason(item, template) !== undefined) {
          return;
        }
        const deployment = await projectFor(item, template).deploy({ region });
        deployments.push(deployment);
        expect((await deployment.status()).ready).toBe(true);
        expect(deployment.url.startsWith("https://")).toBe(true);
        expect((await deployment.invoke(PROMPT)).text.trim()).not.toBe("");
        const events = [];
        for await (const event of deployment.stream(PROMPT)) {
          events.push(event);
        }
        expect(events.at(-1)?.type).toBe("done");
        expect(events.some((event) => event.type === "agent_failed")).toBe(false);
        expect(events.some((event) => event.type === "text_delta" || event.type === "text")).toBe(
          true,
        );
        await deployment.stop();
        deployments.pop();
      },
    );
  },
);
