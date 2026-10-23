/**
 * Las banderas de la receta de la flota en `rayito agent deploy | status |
 * stop` (ADR-034, `testdata/fleet-recipe/contract.json` `cliFlags`): llegan
 * al SDK, `--env` rechaza lo que parece un secreto
 * (`testdata/fleet-recipe/env-rejection.json`), `status` no inventa un
 * escalado y `stop --keep-recipe` conserva la receta. Espejo de
 * `test_cli_agent_recipe.py`. Con dobles: nada toca AWS.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, test } from "vitest";
import {
  ENV_LOOKS_SECRET_MESSAGE,
  envFlagLooksSecret,
  KEEP_RECIPE_HELP,
  RENEW_GATEWAY_KEYS_HELP,
  SAVE_RECIPE_HELP,
  SCALE_IN_AFTER_HELP,
  STATUS_UNKNOWN_SCALING,
} from "../../src/cli/agent-deploy.js";
import type {
  AgentDeployRequest,
  DeployedFleetView,
  FleetStatusView,
} from "../../src/cli/ops-ports.js";
import { generateAccessToken } from "../../src/payload.js";
import {
  FakeAgentProjects,
  FakeFleetClient,
  FLEET_NAME,
  SANDBOX_ID,
} from "./cli/agent-project-fakes.js";
import { buildWorld } from "./cli/case-world.js";
import { runWithServices } from "./cli/run-case.js";

const TESTDATA = join(import.meta.dirname, "..", "..", "..", "..", "testdata", "fleet-recipe");
const CONTRACT = JSON.parse(readFileSync(join(TESTDATA, "contract.json"), "utf8"));
const ENV_VECTORS = JSON.parse(readFileSync(join(TESTDATA, "env-rejection.json"), "utf8"));
const EXIT_USAGE = 2;

class RecipeFleet extends FakeFleetClient {
  scalingSource = "default";
  readonly stops: unknown[] = [];

  override async status(): Promise<FleetStatusView> {
    return {
      name: FLEET_NAME,
      minVms: 1,
      maxVms: 1,
      scalingSource: this.scalingSource,
      members: [{ sandboxId: SANDBOX_ID, state: "unknown" }],
    };
  }

  override async stop(options?: { readonly keepRecipe?: boolean | undefined }): Promise<number> {
    this.stops.push(options ?? {});
    return 1;
  }
}

class RecipeProjects extends FakeAgentProjects {
  override readonly fleet = new RecipeFleet();

  override async deployFleet(request: AgentDeployRequest): Promise<DeployedFleetView> {
    const deployed = await super.deployFleet(request);
    request.settings.onStep?.({ kind: "recipe", detail: "recipe_access_denied" });
    return deployed;
  }
}

async function run(projects: FakeAgentProjects, args: string[]) {
  const built = buildWorld({});
  return runWithServices({
    args,
    services: built.services,
    agentProjects: projects,
    env: { RAYITO_ACCESS_TOKEN: generateAccessToken() },
  });
}

describe("rayito agent with the fleet recipe", () => {
  test("the help texts and messages are the contract ones", () => {
    const helps = new Map(
      (CONTRACT.cliFlags as { command: string; flag: string; help: string }[]).map((flag) => [
        `${flag.command} ${flag.flag}`,
        flag.help,
      ]),
    );
    expect(helps.get("agent deploy --scale-in-after")).toBe(SCALE_IN_AFTER_HELP);
    expect(helps.get("agent deploy --no-save-recipe")).toBe(SAVE_RECIPE_HELP);
    expect(helps.get("agent deploy --renew-gateway-keys")).toBe(RENEW_GATEWAY_KEYS_HELP);
    expect(helps.get("agent stop --keep-recipe")).toBe(KEEP_RECIPE_HELP);
    expect(CONTRACT.messages.envLooksSecret).toBe(ENV_LOOKS_SECRET_MESSAGE);
    expect(CONTRACT.messages.statusUnknownScaling).toBe(STATUS_UNKNOWN_SCALING);
  });

  test.each(ENV_VECTORS.rejected as string[])("--env %s looks like a secret", (name) => {
    expect(envFlagLooksSecret(name)).toBe(true);
  });

  test.each(ENV_VECTORS.accepted as string[])("--env %s is accepted", (name) => {
    expect(envFlagLooksSecret(name)).toBe(false);
  });

  test("deploy rejects a secret-looking env before any call", async () => {
    const projects = new RecipeProjects();
    const outcome = await run(projects, ["agent", "deploy", "--env", "API_TOKEN=x"]);
    expect(outcome.exitCode).toBe(EXIT_USAGE);
    expect(outcome.stderr).toContain(ENV_LOOKS_SECRET_MESSAGE.replaceAll("{name}", "API_TOKEN"));
    expect(projects.deployRequests).toEqual([]);
  });

  test("the recipe flags reach the deploy request", async () => {
    const projects = new RecipeProjects();
    const outcome = await run(projects, [
      "agent",
      "deploy",
      "--scale-in-after",
      "300",
      "--no-save-recipe",
      "--renew-gateway-keys",
      "--env",
      "LOG_LEVEL=debug",
    ]);
    expect(outcome.exitCode).toBe(0);
    const [request] = projects.deployRequests as [AgentDeployRequest];
    expect(request.flags.scaleInAfter).toBe(300);
    expect(request.settings.saveRecipe).toBe(false);
    expect(request.settings.renewGatewayKeys).toBe(true);
    expect(request.flags.env).toEqual({ LOG_LEVEL: "debug" });
  });

  test("by default the recipe is saved and its warnings reach --json", async () => {
    const projects = new RecipeProjects();
    const outcome = await run(projects, ["--json", "agent", "deploy"]);
    expect(outcome.exitCode).toBe(0);
    const [request] = projects.deployRequests as [AgentDeployRequest];
    expect(request.settings.saveRecipe).toBe(true);
    expect(request.settings.renewGatewayKeys).toBe(false);
    expect(JSON.parse(outcome.stdout).warnings).toEqual(["recipe_access_denied"]);
  });

  test("a fleet without members prints without a first VM", async () => {
    const projects = new RecipeProjects();
    projects.sandboxIds = [];
    const json = await run(projects, ["--json", "agent", "deploy", "--min-vms", "0"]);
    expect(json.exitCode).toBe(0);
    const document = JSON.parse(json.stdout);
    expect([document.sandboxId, document.url, document.sandboxIds]).toEqual([null, null, []]);
    const text = await run(projects, ["agent", "deploy", "--min-vms", "0"]);
    expect(text.exitCode).toBe(0);
    expect(text.stdout).not.toContain("sandbox_id=");
    expect(text.stdout).not.toContain("url=");
    expect(text.stdout).toContain("members=0");
  });

  test.each([["--scale-in-after", "300"], ["--no-save-recipe"], ["--renew-gateway-keys"]])(
    "%s is microvm only",
    async (...flags) => {
      const outcome = await run(new RecipeProjects(), [
        "agent",
        "deploy",
        "--target",
        "agentcore",
        ...flags,
      ]);
      expect(outcome.exitCode).toBe(EXIT_USAGE);
      expect(outcome.stderr).toContain(flags[0]);
    },
  );

  test("status without project or recipe says the scaling is unknown", async () => {
    const projects = new RecipeProjects();
    const text = await run(projects, ["agent", "status", FLEET_NAME]);
    const json = await run(projects, ["--json", "agent", "status", FLEET_NAME]);
    expect(text.stdout).toContain("members: 1 (escalado desconocido: sin proyecto ni receta)");
    expect(text.stdout).not.toContain("min 1, max 1");
    expect(JSON.parse(json.stdout).scalingSource).toBe("default");
  });

  test("status from the recipe prints its scaling", async () => {
    const projects = new RecipeProjects();
    projects.fleet.scalingSource = "recipe";
    const text = await run(projects, ["agent", "status", FLEET_NAME]);
    expect(text.stdout).toContain("members: 1 (min 1, max 1)");
  });

  test.each([
    [[], {}],
    [["--keep-recipe"], { keepRecipe: true }],
  ])("stop %o keeps the recipe only on request", async (flags, expected) => {
    const projects = new RecipeProjects();
    const outcome = await run(projects, ["agent", "stop", FLEET_NAME, ...(flags as string[])]);
    expect(outcome.exitCode).toBe(0);
    expect(projects.fleet.stops).toEqual([expected]);
  });
});
