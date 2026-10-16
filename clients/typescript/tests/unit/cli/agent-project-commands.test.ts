/**
 * `rayito agent init | build | run | deploy | export | invoke | status |
 * stop` (`agent-runtimes-byo`): las órdenes sólo parsean argumentos y
 * delegan en `AgentProjectTools`; lo que se comprueba es el cableado, la
 * salida y los códigos de salida, igual que `test_agent_project_cli.py`.
 */

import { mkdtempSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { agentFailed, TokenUsage } from "../../../src/agent/events.js";
import { AgentError, InvalidArgumentError } from "../../../src/errors.js";
import {
  DEFAULT_SERVICE_PORT,
  DEPLOYMENT_URL,
  FakeAgentProjects,
  RESULT,
  SANDBOX_ID,
  USAGE,
} from "./agent-project-fakes.js";
import { buildWorld } from "./case-world.js";
import { runWithServices } from "./run-case.js";

const PROXY_HEADER_VALUE = "eyJhbGciOiJkaXIifQ..fake.jwe";
const MS_PER_SECOND = 1000;
const ACCESS_TOKEN = "fake-access-token";
const WITH_TOKEN = { RAYITO_ACCESS_TOKEN: ACCESS_TOKEN };
const REFUSE_SERVICES = "el comando no debía abrir la sesión de AWS";

async function invoke(
  projects: FakeAgentProjects,
  args: string[],
  env: Readonly<Record<string, string | undefined>> = {},
) {
  const built = buildWorld({});
  const services = new Proxy(built.services, {
    get() {
      throw new Error(REFUSE_SERVICES);
    },
  });
  return runWithServices({ args, services, agentProjects: projects, env });
}

function tempDirectory(): string {
  return mkdtempSync(join(tmpdir(), "rayito-agent-"));
}

describe("agent init", () => {
  it("escribe el fichero de proyecto y el andamiaje sin pedir AWS", async () => {
    const directory = join(tempDirectory(), "Support Bot");
    const outcome = await invoke(new FakeAgentProjects(), [
      "agent",
      "init",
      directory,
      "--runtime",
      "strands",
      "--region",
      "us-east-1",
    ]);
    expect(outcome.exitCode).toBe(0);
    const projectFile = readFileSync(join(directory, "rayito-agent.toml"), "utf8");
    expect(projectFile).toContain('name = "support-bot"');
    expect(projectFile).toContain('kind = "strands"');
    expect(readFileSync(join(directory, "agent.py"), "utf8")).toContain("def agent(ctx)");
  });

  it("no sobrescribe sin --force: sale con 2 y deja el fichero como estaba", async () => {
    const directory = tempDirectory();
    writeFileSync(join(directory, "rayito-agent.toml"), "mine");
    const outcome = await invoke(new FakeAgentProjects(), [
      "agent",
      "init",
      directory,
      "--runtime",
      "strands",
    ]);
    expect(outcome.exitCode).toBe(2);
    expect(outcome.stderr).toContain("--force");
    expect(readFileSync(join(directory, "rayito-agent.toml"), "utf8")).toBe("mine");
  });

  it("--force sobrescribe", async () => {
    const directory = tempDirectory();
    writeFileSync(join(directory, "rayito-agent.toml"), "mine");
    const outcome = await invoke(new FakeAgentProjects(), [
      "agent",
      "init",
      directory,
      "--runtime",
      "custom",
      "--force",
    ]);
    expect(outcome.exitCode).toBe(0);
    expect(readFileSync(join(directory, "rayito-agent.toml"), "utf8")).toMatch(/^schema =/);
  });

  it("custom escribe una app con [runtime.app], requisitos e install, y explica raw", async () => {
    const directory = tempDirectory();
    const outcome = await invoke(new FakeAgentProjects(), [
      "agent",
      "init",
      directory,
      "--runtime",
      "custom",
    ]);
    expect(outcome.exitCode).toBe(0);
    expect(outcome.stdout).toContain('protocol = "raw"');
    const projectFile = readFileSync(join(directory, "rayito-agent.toml"), "utf8");
    for (const key of ['requirements = "requirements.txt"', "install = []", "[runtime.app]"]) {
      expect(projectFile).toContain(key);
    }
    expect(readFileSync(join(directory, "serve.py"), "utf8")).toContain("/invoke");
  });

  it("langgraph escribe un grafo de LangGraph a secas", async () => {
    const directory = tempDirectory();
    await invoke(new FakeAgentProjects(), ["agent", "init", directory, "--runtime", "langgraph"]);
    const graph = readFileSync(join(directory, "graph.py"), "utf8");
    expect(graph).toContain("create_react_agent");
    expect(graph).not.toContain("deepagents");
  });

  it("--json lista los ficheros escritos", async () => {
    const outcome = await invoke(new FakeAgentProjects(), [
      "--json",
      "agent",
      "init",
      tempDirectory(),
      "--runtime",
      "langgraph",
    ]);
    expect(JSON.parse(outcome.stdout).files).toEqual([
      "rayito-agent.toml",
      "AGENTS.md",
      "graph.py",
    ]);
  });

  it.each([
    ["--runtime", "opencode", "--language", "typescript"],
    ["--runtime", "strands", "--provider", "openai"],
    ["--runtime", "nope"],
    ["--runtime", "strands", "--provider", "nope"],
  ])("rechaza %s antes de escribir", async (...flags) => {
    const outcome = await invoke(new FakeAgentProjects(), [
      "agent",
      "init",
      tempDirectory(),
      ...flags,
    ]);
    expect(outcome.exitCode).toBe(2);
  });
});

describe("agent build", () => {
  it("reenvía las opciones y imprime los ids", async () => {
    const projects = new FakeAgentProjects();
    const outcome = await invoke(projects, [
      "--json",
      "agent",
      "build",
      "--bucket",
      "amzn-s3-demo-bucket",
      "--file",
      "p",
    ]);
    expect(JSON.parse(outcome.stdout)).toEqual({
      templateId: "tpl-1",
      buildId: "bld-1",
      alias: "support-bot",
    });
    expect(projects.calls[0]).toEqual(["load", "p"]);
    expect(projects.project.calls).toEqual([
      ["build", "amzn-s3-demo-bucket", false, 1800 * MS_PER_SECOND],
    ]);
  });
});

describe("agent run", () => {
  it("imprime el texto y el resumen, y reenvía sesión y modelo", async () => {
    const projects = new FakeAgentProjects();
    const outcome = await invoke(projects, [
      "agent",
      "run",
      "hola",
      "--session-id",
      "s",
      "--model",
      "m",
    ]);
    expect(outcome.exitCode).toBe(0);
    expect(outcome.stdout).toBe("hola\n");
    expect(outcome.stderr).toContain("steps=2 tokens=15");
    expect(projects.project.calls).toEqual([["run", "hola", "s", "m", false]]);
  });

  it("--json es un documento con el uso", async () => {
    const outcome = await invoke(new FakeAgentProjects(), ["--json", "agent", "run", "hola"]);
    expect(JSON.parse(outcome.stdout)).toEqual({
      sessionId: "ses_1",
      text: "hola",
      steps: 2,
      usage: { input: 10, output: 5, reasoning: 0, cacheRead: 0, cacheWrite: 0, total: 15 },
      usageReported: true,
      exitCode: 0,
    });
  });

  it("dice desconocido cuando el agente no informó de tokens", async () => {
    const projects = new FakeAgentProjects();
    projects.project.runResult = {
      ...RESULT,
      usage: new TokenUsage(),
      usageReported: false,
    };
    const text = await invoke(projects, ["agent", "run", "hola"]);
    expect(text.stderr).toContain("tokens=desconocido");
    const document = await invoke(projects, ["--json", "agent", "run", "hola"]);
    expect(JSON.parse(document.stdout).usageReported).toBe(false);
  });

  it("--stream escribe los deltas y un salto de línea", async () => {
    const projects = new FakeAgentProjects();
    projects.project.runEvents = [
      { type: "step_started", index: 1 },
      { type: "text_delta", text: "ho" },
      { type: "text_delta", text: "la" },
      { type: "text", text: "hola" },
      { type: "done", sessionId: "ses_1", exitCode: 0, usage: USAGE },
    ];
    const outcome = await invoke(projects, ["agent", "run", "hola", "--stream"]);
    expect(outcome.exitCode).toBe(0);
    expect(outcome.stdout).toBe("hola\n");
  });

  it("--stream con --json es una línea por evento", async () => {
    const projects = new FakeAgentProjects();
    projects.project.runEvents = [
      {
        type: "tool_call",
        callId: "c1",
        name: "read",
        status: "completed",
        input: undefined,
        output: undefined,
        outputTruncated: false,
      },
      { type: "done", sessionId: "ses_1", exitCode: 0, usage: new TokenUsage() },
    ];
    const outcome = await invoke(projects, ["--json", "agent", "run", "hola", "--stream"]);
    const lines = outcome.stdout
      .trimEnd()
      .split("\n")
      .map((line) => JSON.parse(line));
    expect(lines.map((line) => line.type)).toEqual(["tool_call", "done"]);
    expect(lines[0].callId).toBe("c1");
  });

  it("un agent_failed en el stream sale con 1", async () => {
    const projects = new FakeAgentProjects();
    projects.project.runEvents = [agentFailed("model_error")];
    const outcome = await invoke(projects, ["agent", "run", "hola", "--stream"]);
    expect(outcome.exitCode).toBe(1);
  });

  it("un AgentError sale con 1 y lo dice", async () => {
    const projects = new FakeAgentProjects();
    projects.project.runFailure = new AgentError("falló", { reason: "model_error" });
    const outcome = await invoke(projects, ["agent", "run", "hola"]);
    expect(outcome.exitCode).toBe(1);
    expect(outcome.stderr).toContain("falló");
  });

  it("un fichero de proyecto inválido sale con 2", async () => {
    const projects = new FakeAgentProjects();
    projects.loadFailure = new InvalidArgumentError("runtime.entrpoint: clave desconocida");
    const outcome = await invoke(projects, ["agent", "run", "hola"]);
    expect(outcome.exitCode).toBe(2);
    expect(outcome.stderr).toContain("runtime.entrpoint");
  });
});

describe("agent deploy", () => {
  it("--json imprime sandboxId, url y puerto, y nunca las cabeceras", async () => {
    const outcome = await invoke(
      new FakeAgentProjects(),
      ["--json", "agent", "deploy"],
      WITH_TOKEN,
    );
    expect(outcome.exitCode).toBe(0);
    expect(JSON.parse(outcome.stdout)).toEqual({
      sandboxId: SANDBOX_ID,
      url: DEPLOYMENT_URL,
      port: DEFAULT_SERVICE_PORT,
    });
    expect(outcome.stdout).not.toContain(PROXY_HEADER_VALUE);
    expect(outcome.stdout.toLowerCase()).not.toContain("authorization");
  });

  it("--keep-on-failure sólo se reenvía si se pide", async () => {
    const projects = new FakeAgentProjects();
    await invoke(projects, ["agent", "deploy"], WITH_TOKEN);
    await invoke(projects, ["agent", "deploy", "--keep-on-failure"], WITH_TOKEN);
    expect(projects.project.calls.map((call) => call[4])).toEqual([false, true]);
  });

  it("avisa cuando las rutas de la app no se exponen", async () => {
    const projects = new FakeAgentProjects();
    projects.project.hiddenRoutes = {
      protocol: "agentcore",
      healthPath: "/health",
      invokePath: "/invoke",
    };
    const warned = await invoke(projects, ["agent", "deploy"], WITH_TOKEN);
    expect(warned.stderr).toContain('protocol = "raw"');
    expect(warned.stderr).toContain("/health");
    projects.project.hiddenRoutes = undefined;
    const quiet = await invoke(projects, ["agent", "deploy"], WITH_TOKEN);
    expect(quiet.stderr).not.toContain("aviso");
  });

  it("sólo cambia lo que se pasó", async () => {
    const projects = new FakeAgentProjects();
    await invoke(projects, ["agent", "deploy"], WITH_TOKEN);
    await invoke(projects, ["agent", "deploy", "--port", "9100", "--timeout", "120"], WITH_TOKEN);
    expect(projects.project.calls).toEqual([
      ["deploy", undefined, undefined, ACCESS_TOKEN, false],
      ["deploy", 9100, 120 * MS_PER_SECOND, ACCESS_TOKEN, false],
    ]);
  });

  it("guarda un token nuevo en --token-file y lo pasa al despliegue", async () => {
    const projects = new FakeAgentProjects();
    const tokenFile = join(tempDirectory(), "token");
    const outcome = await invoke(projects, ["agent", "deploy", "--token-file", tokenFile]);
    expect(outcome.exitCode).toBe(0);
    const token = readFileSync(tokenFile, "utf8");
    expect(token).not.toBe("");
    expect(outcome.stdout).not.toContain(token);
    expect(statSync(tokenFile).mode & 0o777).toBe(0o600);
    expect(projects.project.calls).toEqual([["deploy", undefined, undefined, token, false]]);
  });

  it("sin forma de reconectarse no crea nada y sale con 2", async () => {
    const projects = new FakeAgentProjects();
    const outcome = await invoke(projects, ["agent", "deploy"]);
    expect(outcome.exitCode).toBe(2);
    expect(outcome.stderr).toContain("--token-file");
    expect(projects.project.calls).toEqual([]);
  });

  it("no toca un --token-file que ya existe", async () => {
    const projects = new FakeAgentProjects();
    const tokenFile = join(tempDirectory(), "token");
    writeFileSync(tokenFile, "old");
    const outcome = await invoke(projects, ["agent", "deploy", "--token-file", tokenFile]);
    expect(outcome.exitCode).toBe(2);
    expect(readFileSync(tokenFile, "utf8")).toBe("old");
    expect(projects.project.calls).toEqual([]);
  });
});

describe("agent export", () => {
  it("escribe el contexto sin pedir AWS", async () => {
    const projects = new FakeAgentProjects();
    const outcome = await invoke(projects, [
      "--json",
      "agent",
      "export",
      "--target",
      "agentcore",
      "--out",
      "build",
    ]);
    expect(outcome.exitCode).toBe(0);
    expect(JSON.parse(outcome.stdout).port).toBe(8080);
    expect(projects.project.calls).toEqual([["export", "agentcore", "build", false]]);
  });

  it("rechaza un destino desconocido con 2", async () => {
    const outcome = await invoke(new FakeAgentProjects(), [
      "agent",
      "export",
      "--target",
      "ecs",
      "--out",
      "build",
    ]);
    expect(outcome.exitCode).toBe(2);
  });
});

describe("agent invoke | status | stop", () => {
  it("invoke llama a invoke o a stream", async () => {
    const projects = new FakeAgentProjects();
    projects.project.deployed.events = [
      { type: "text_delta", text: "hi" },
      { type: "done", sessionId: "ses_1", exitCode: 0, usage: USAGE },
    ];
    const plain = await invoke(
      projects,
      ["agent", "invoke", SANDBOX_ID, "hola", "--session-id", "s"],
      WITH_TOKEN,
    );
    const streamed = await invoke(
      projects,
      ["agent", "invoke", SANDBOX_ID, "hola", "--stream"],
      WITH_TOKEN,
    );
    expect(plain.stdout).toBe("hola\n");
    expect(streamed.stdout).toBe("hi\n");
    expect(projects.project.deployed.calls).toEqual([
      ["invoke", "hola", "s"],
      ["stream", "hola", undefined],
    ]);
  });

  it("invoke y status leen el token de --token-file", async () => {
    const projects = new FakeAgentProjects();
    const tokenFile = join(tempDirectory(), "token");
    writeFileSync(tokenFile, `${ACCESS_TOKEN}\n`);
    const invoked = await invoke(projects, [
      "agent",
      "invoke",
      SANDBOX_ID,
      "hola",
      "--token-file",
      tokenFile,
    ]);
    const status = await invoke(projects, [
      "agent",
      "status",
      SANDBOX_ID,
      "--token-file",
      tokenFile,
    ]);
    expect(invoked.exitCode).toBe(0);
    expect(status.exitCode).toBe(0);
    expect(projects.calls).toEqual([
      ["connect", SANDBOX_ID, ACCESS_TOKEN],
      ["connect", SANDBOX_ID, ACCESS_TOKEN],
    ]);
  });

  it.each([
    ["invoke", [SANDBOX_ID, "hola"]],
    ["status", [SANDBOX_ID]],
  ])("%s sin token sale con 2 y no se conecta", async (command, rest) => {
    const projects = new FakeAgentProjects();
    const outcome = await invoke(projects, ["agent", command ?? "", ...rest]);
    expect(outcome.exitCode).toBe(2);
    expect(outcome.stderr).toContain("--token-file");
    expect(projects.calls).toEqual([]);
  });

  it("status", async () => {
    const projects = new FakeAgentProjects();
    const status = await invoke(projects, ["--json", "agent", "status", SANDBOX_ID], WITH_TOKEN);
    expect(JSON.parse(status.stdout).ready).toBe(true);
  });

  it.each([true, false])("stop no necesita token y es idempotente (existe: %s)", async (found) => {
    const projects = new FakeAgentProjects();
    projects.stopResult = found;
    const stop = await invoke(projects, ["--json", "agent", "stop", SANDBOX_ID]);
    expect(stop.exitCode).toBe(0);
    expect(JSON.parse(stop.stdout)).toEqual({ sandboxId: SANDBOX_ID, stopped: found });
    expect(projects.calls).toEqual([["stop", SANDBOX_ID]]);
  });
});
