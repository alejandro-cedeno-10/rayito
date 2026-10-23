/**
 * `rayito agent invoke | status | stop` con el nombre de una flota
 * (`agent/fleet.ts`): las órdenes sólo parsean argumentos y delegan en
 * `AgentProjectTools`. Espejo de las pruebas de flota de
 * `test_agent_deploy_cli.py`.
 */

import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { defaultToken, RENEW_NEEDS_FLEET_MESSAGE } from "../../../src/cli/agent-deploy.js";
import { CapacityError } from "../../../src/errors.js";
import { AGENT_TOKEN_FILE_DEFAULT } from "../../../src/limits.js";
import { generateAccessToken } from "../../../src/payload.js";
import { FakeAgentProjects, FLEET_NAME, SANDBOX_ID } from "./agent-project-fakes.js";
import { buildWorld } from "./case-world.js";
import { runWithServices } from "./run-case.js";

async function invoke(
  projects: FakeAgentProjects,
  args: string[],
  env: Readonly<Record<string, string | undefined>> = {},
) {
  const built = buildWorld({});
  return runWithServices({ args, services: built.services, agentProjects: projects, env });
}

describe("agent con una flota", () => {
  it("invoke con el nombre conecta la flota con el token", async () => {
    const projects = new FakeAgentProjects();
    const token = generateAccessToken();
    const outcome = await invoke(
      projects,
      ["agent", "invoke", FLEET_NAME, "hola", "--session-id", "s"],
      { RAYITO_ACCESS_TOKEN: token },
    );
    expect(outcome.exitCode).toBe(0);
    expect(projects.calls).toEqual([["connectFleet", FLEET_NAME, token]]);
    expect(projects.fleet.calls).toEqual([["invoke", "hola", "s"]]);
    expect(outcome.stderr).toContain(`session_id=rs1.${SANDBOX_ID}.ses_1`);
  });

  it("invoke --renew-gateway-keys pide la renovación al reconectar la flota", async () => {
    const projects = new FakeAgentProjects();
    const token = generateAccessToken();
    const outcome = await invoke(
      projects,
      ["agent", "invoke", FLEET_NAME, "hola", "--renew-gateway-keys"],
      { RAYITO_ACCESS_TOKEN: token },
    );
    expect(outcome.exitCode).toBe(0);
    expect(projects.calls).toEqual([
      ["connectFleet", FLEET_NAME, token, { renewGatewayKeys: true }],
    ]);
  });

  it("invoke --renew-gateway-keys con un id de sandbox sale con 2 sin conectar", async () => {
    const projects = new FakeAgentProjects();
    const outcome = await invoke(
      projects,
      ["agent", "invoke", SANDBOX_ID, "hola", "--renew-gateway-keys"],
      { RAYITO_ACCESS_TOKEN: generateAccessToken() },
    );
    expect(outcome.exitCode).toBe(2);
    expect(outcome.stderr).toContain(RENEW_NEEDS_FLEET_MESSAGE);
    expect(projects.calls).toEqual([]);
  });

  it("invoke --renew-gateway-keys con --target agentcore es option_not_supported", async () => {
    const projects = new FakeAgentProjects();
    const outcome = await invoke(projects, [
      "agent",
      "invoke",
      FLEET_NAME,
      "hola",
      "--target",
      "agentcore",
      "--renew-gateway-keys",
    ]);
    expect(outcome.exitCode).toBe(2);
    expect(outcome.stderr).toContain("--renew-gateway-keys");
    expect(projects.calls).toEqual([]);
  });

  it("invoke con un id de sandbox sigue hablando con un solo VM", async () => {
    const projects = new FakeAgentProjects();
    await invoke(projects, ["agent", "invoke", SANDBOX_ID, "hola"], {
      RAYITO_ACCESS_TOKEN: generateAccessToken(),
    });
    expect(projects.calls.map((call) => call[0])).toEqual(["connect"]);
    expect(projects.fleet.calls).toEqual([]);
  });

  it("una flota sin cupo sale con 1", async () => {
    const projects = new FakeAgentProjects();
    projects.fleet.busy = new CapacityError("sin cupo", { code: "agent_capacity" });
    const outcome = await invoke(projects, ["agent", "invoke", FLEET_NAME, "hola"], {
      RAYITO_ACCESS_TOKEN: generateAccessToken(),
    });
    expect(outcome.exitCode).toBe(1);
    expect(outcome.stderr).toContain("sin cupo");
  });

  it("invoke usa el fichero de token por defecto si no hay otro", async () => {
    const previous = process.cwd();
    process.chdir(mkdtempSync(join(tmpdir(), "rayito-fleet-")));
    try {
      const token = defaultToken(AGENT_TOKEN_FILE_DEFAULT);
      const projects = new FakeAgentProjects();
      await invoke(projects, ["agent", "invoke", FLEET_NAME, "hola"]);
      expect(projects.calls).toEqual([["connectFleet", FLEET_NAME, token]]);
    } finally {
      process.chdir(previous);
    }
  });

  it("invoke sin ningún token sale con 2", async () => {
    const previous = process.cwd();
    process.chdir(mkdtempSync(join(tmpdir(), "rayito-fleet-")));
    try {
      const outcome = await invoke(new FakeAgentProjects(), [
        "agent",
        "invoke",
        FLEET_NAME,
        "hola",
      ]);
      expect(outcome.exitCode).toBe(2);
    } finally {
      process.chdir(previous);
    }
  });

  it("status de una flota lista sus miembros", async () => {
    const projects = new FakeAgentProjects();
    const env = { RAYITO_ACCESS_TOKEN: generateAccessToken() };
    const text = await invoke(projects, ["agent", "status", FLEET_NAME], env);
    const json = await invoke(projects, ["--json", "agent", "status", FLEET_NAME], env);
    expect(text.stdout).toContain("members: 2 (min 1, max 2)");
    expect(text.stdout).toContain(`sandbox_id=${SANDBOX_ID} state=unknown`);
    expect(JSON.parse(json.stdout).members[1].state).toBe("healthy");
  });

  it("stop de una flota no necesita token y termina cada VM", async () => {
    const projects = new FakeAgentProjects();
    const outcome = await invoke(projects, ["agent", "stop", FLEET_NAME]);
    expect(outcome.exitCode).toBe(0);
    expect(projects.calls).toEqual([["connectFleet", FLEET_NAME, undefined]]);
    expect(projects.fleet.calls).toEqual([["stop"]]);
    expect(outcome.stdout).toContain("parados 2 VM(s)");
  });

  it("stop de un id de sandbox no cambia", async () => {
    const projects = new FakeAgentProjects();
    const outcome = await invoke(projects, ["agent", "stop", SANDBOX_ID]);
    expect(outcome.stdout).toContain(`parado sandbox_id=${SANDBOX_ID}`);
  });
});
