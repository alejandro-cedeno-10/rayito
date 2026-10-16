/**
 * Las operaciones de `AgentProject` (`build`, `run`, `deploy`, `export`): la
 * delegación en los servicios y que construir, cargar, exportar y crear un
 * cliente de despliegue no llaman a AWS. Espejo de
 * `test_agent_project_ops.py`.
 */

import { mkdtempSync, writeFileSync } from "node:fs";
import net from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, test, vi } from "vitest";
import { AgentDeployment } from "../../src/agent/deployment.js";
import { AgentProject } from "../../src/agent/project.js";
import { ProjectService } from "../../src/agent/project-service.js";
import { Sandbox } from "../../src/sandbox/sandbox.js";
import { Template } from "../../src/templates/dsl.js";
import { FakeSandbox, project } from "./agent-project-fake.js";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("operations delegate to the service", () => {
  test("build, run and deploy", async () => {
    const build = vi.spyOn(ProjectService.prototype, "build").mockResolvedValue("build" as never);
    const run = vi.spyOn(ProjectService.prototype, "run").mockResolvedValue("run" as never);
    const deploy = vi
      .spyOn(ProjectService.prototype, "deploy")
      .mockResolvedValue("deploy" as never);
    const the = project();
    expect(await the.build({ bucket: "b", force: true })).toBe("build");
    expect(await the.run("hola", { sessionId: "s", reasoning: true })).toBe("run");
    expect(await the.deploy({ region: "us-east-1" })).toBe("deploy");
    expect(build).toHaveBeenCalledWith({ bucket: "b", force: true });
    expect(run).toHaveBeenCalledWith("hola", { sessionId: "s", reasoning: true });
    expect(deploy).toHaveBeenCalledWith({ region: "us-east-1" });
  });
});

describe("zero calls", () => {
  function forbidAws(): { calls: string[] } {
    const calls: string[] = [];
    const trip = (name: string) => () => {
      calls.push(name);
      throw new Error(`${name} no debe llamarse`);
    };
    vi.spyOn(Sandbox, "create").mockImplementation(trip("Sandbox.create") as never);
    vi.spyOn(Template, "build").mockImplementation(trip("Template.build") as never);
    vi.spyOn(net.Socket.prototype, "connect").mockImplementation(trip("socket.connect") as never);
    vi.stubGlobal("fetch", trip("fetch"));
    return { calls };
  }

  test("construct, load, serialise and export", async () => {
    const { calls } = forbidAws();
    const directory = mkdtempSync(join(tmpdir(), "rayito-project-"));
    writeFileSync(
      join(directory, "rayito-agent.json"),
      JSON.stringify({
        schema: "rayito.agent-project/1",
        name: "support-bot",
        runtime: { kind: "deepagents" },
        model: {
          provider: "bedrock",
          id: "us.anthropic.claude-haiku-4-5-20251001-v1:0",
          region: "us-east-1",
          gateway: "bedrock",
        },
        gateways: {
          bedrock: {
            preset: "bedrock",
            secret: "bedrock-key",
            region: "us-east-1",
            models: ["us.anthropic.claude-haiku-4-5-20251001-v1:0"],
          },
        },
      }),
    );
    const loaded = await AgentProject.load(directory);
    expect(loaded.toObject().name).toBe("support-bot");
    new ProjectService(loaded);
    await loaded.export({ target: "docker", outDir: join(directory, "out") });
    await loaded.export({ target: "agentcore", outDir: join(directory, "out2") });
    expect(calls).toEqual([]);
  });

  test("creating a deployment client", async () => {
    const { calls } = forbidAws();
    const sandbox = new FakeSandbox();
    new AgentDeployment({ sandbox, port: 8088, hostAccess: await sandbox.getHost(8088) });
    expect(calls).toEqual([]);
  });
});
