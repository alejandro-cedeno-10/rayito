/**
 * `rayito agent run|deploy|invoke|status|stop` contra AWS real, un caso por
 * runtime (`agent-runtimes-byo`), espejo de
 * `clients/python/tests/e2e/test_agent_project_cli.py`: la CLI de TypeScript
 * con `runCli` en proceso y la sesión real de AWS, sobre un `rayito-agent.toml`
 * escrito para la imagen ya construida del caso.
 *
 * Cada runtime hace un `run --stream --json` (los eventos, una línea por evento,
 * terminan en `done` con el uso de tokens) y un ciclo `deploy` / `status` /
 * `invoke` / `stop`, con el access token en un `--token-file` como lo haría
 * otro proceso. `deploy` va con `--no-build`: reutiliza la imagen compartida
 * del caso, ya construida con todos sus runtimes, en vez de construir encima
 * una versión con uno solo. `stop` va por el nombre de la flota, así que también borra su
 * receta de Parameter Store (`/rayito/agents/<nombre>/recipe`), y la prueba
 * comprueba que no queda. Las cabeceras de acceso del despliegue no salen por
 * ninguna salida.
 *
 * Variables y coste: cabeceras de `agent-project.e2e.test.ts` y del espejo de
 * Python.
 */

import { cpSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { recipeParameterName } from "../../src/agent/fleet-recipe.js";
import { defaultRecipeStore } from "../../src/agent/fleet-recipe-aws.js";
import { type Outcome, rayito } from "../shared/cli-scenarios.js";
import {
  agentImage,
  appFixture,
  BEDROCK_REGION,
  BEDROCK_SECRET,
  CASES,
  PROMPT,
  projectFileText,
  TEST_TIMEOUT_MS,
  unsupportedReason,
} from "./agent-project-support.js";
import { realAwsCliWorld } from "./cli-world.js";
import { e2eEnabled, e2eSettings } from "./helpers.js";

const PROJECT_FILE_NAME = "rayito-agent.toml";

/** Si la receta de la flota `name` sigue en Parameter Store (la misma lectura
 * que hace `AgentFleet.connect`). */
async function recipeLeft(name: string, region: string): Promise<boolean> {
  const parameter = recipeParameterName(name);
  expect(parameter).toBeDefined();
  return (await defaultRecipeStore({ region }).load(parameter ?? "")) !== undefined;
}

function jsonOf(outcome: Outcome): Record<string, unknown> {
  expect(outcome.exitCode, outcome.stderr.slice(-400)).toBe(0);
  return JSON.parse(outcome.stdout) as Record<string, unknown>;
}

describe.runIf(e2eEnabled() && Boolean(BEDROCK_SECRET))(
  "rayito agent en AWS real (Bedrock)",
  () => {
    const world = realAwsCliWorld();
    const region = e2eSettings().region ?? BEDROCK_REGION;
    const deployed: string[] = [];

    afterEach(async () => {
      for (const name of deployed.splice(0)) {
        await rayito(world, ["agent", "stop", name]);
      }
    });

    /** El proyecto del caso: el código de su tipo de imagen y el
     * `rayito-agent.toml`. Va en un subdirectorio para que el fichero del
     * token, junto a él, nunca entre en el código que `deploy` empaqueta. */
    async function projectDir(item: (typeof CASES)[number]): Promise<string | undefined> {
      const template = await agentImage(item.image, region);
      if (unsupportedReason(item, template) !== undefined) {
        return undefined;
      }
      const directory = join(mkdtempSync(join(tmpdir(), "rayito-agent-e2e-")), "project");
      cpSync(appFixture(item.image), directory, { recursive: true });
      writeFileSync(join(directory, PROJECT_FILE_NAME), projectFileText(item, template));
      return directory;
    }

    it.for(CASES.map((item) => [item.id, item] as const))(
      "run --stream: %s",
      { timeout: TEST_TIMEOUT_MS },
      async ([, item]) => {
        const directory = await projectDir(item);
        if (directory === undefined) {
          return;
        }
        const outcome = await rayito(world, [
          "--json",
          "agent",
          "run",
          "--stream",
          "--file",
          directory,
          PROMPT,
        ]);
        expect(outcome.exitCode, outcome.stderr.slice(-400)).toBe(0);
        const events = outcome.stdout
          .split("\n")
          .filter((line) => line.trim() !== "")
          .map(
            (line) =>
              JSON.parse(line) as {
                type: string;
                sessionId?: string;
                usage?: { input: number; output: number };
              },
          );
        const kinds = events.map((event) => event.type);
        expect(kinds.at(-1)).toBe("done");
        expect(kinds).not.toContain("agent_failed");
        expect(kinds.some((kind) => kind === "text" || kind === "text_delta")).toBe(true);
        const done = events.at(-1);
        expect(done?.sessionId).toBeTruthy();
        if (item.callsTheModel) {
          expect((done?.usage?.input ?? 0) + (done?.usage?.output ?? 0)).toBeGreaterThan(0);
        }
      },
    );

    it.for(CASES.map((item) => [item.id, item] as const))(
      "deploy, status, invoke y stop: %s",
      { timeout: TEST_TIMEOUT_MS },
      async ([, item]) => {
        const directory = await projectDir(item);
        if (directory === undefined) {
          return;
        }
        const tokenFile = join(dirname(directory), "agent.token");
        const deploy = jsonOf(
          await rayito(world, [
            "--json",
            "agent",
            "deploy",
            "--file",
            directory,
            "--no-build",
            "--token-file",
            tokenFile,
          ]),
        );
        const name = String(deploy.name);
        deployed.push(name);
        const sandboxId = String(deploy.sandboxId);
        expect(String(deploy.url).startsWith("https://")).toBe(true);
        const status = jsonOf(
          await rayito(world, ["--json", "agent", "status", sandboxId, "--token-file", tokenFile]),
        );
        expect(status.ready).toBe(true);
        const reply = jsonOf(
          await rayito(world, [
            "--json",
            "agent",
            "invoke",
            sandboxId,
            PROMPT,
            "--token-file",
            tokenFile,
          ]),
        );
        expect(String(reply.text).trim()).not.toBe("");
        for (const output of [deploy, status, reply]) {
          expect(JSON.stringify(output).toLowerCase()).not.toContain("authorization");
        }
        const stopped = jsonOf(await rayito(world, ["--json", "agent", "stop", name]));
        expect(stopped).toEqual({ name, stopped: (deploy.sandboxIds as unknown[]).length });
        deployed.pop();
        expect(await recipeLeft(name, region)).toBe(false);
      },
    );
  },
);
