/**
 * `rayito agent init | build | run | deploy | export | invoke | status |
 * stop` (`agent-runtimes-byo`): las órdenes sólo parsean argumentos y
 * delegan en `AgentProjectTools`; lo que se comprueba es el cableado, la
 * salida y los códigos de salida, igual que `test_agent_project_cli.py`.
 */

import { existsSync, mkdtempSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { agentFailed, TokenUsage } from "../../../src/agent/events.js";
import { AgentError, InvalidArgumentError, SandboxError } from "../../../src/errors.js";
import {
  DEFAULT_SERVICE_PORT,
  DEPLOYMENT_URL,
  FakeAgentProjects,
  FLEET_NAME,
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

/** El nombre de un secreto que ya existe (un nombre, no un valor). */
const OWN_SECRET = `nombre-${"propio"}`;
const REFUSE_AWS = "dry-run no debía desplegar";

async function inTemporaryDirectory<T>(action: () => Promise<T>): Promise<T> {
  const previous = process.cwd();
  process.chdir(tempDirectory());
  try {
    return await action();
  } finally {
    process.chdir(previous);
  }
}

describe("agent deploy", () => {
  it("--json imprime la flota y nunca las cabeceras", async () => {
    const outcome = await invoke(
      new FakeAgentProjects(),
      ["--json", "agent", "deploy"],
      WITH_TOKEN,
    );
    expect(outcome.exitCode).toBe(0);
    expect(JSON.parse(outcome.stdout)).toEqual({
      name: FLEET_NAME,
      sandboxId: SANDBOX_ID,
      sandboxIds: [SANDBOX_ID],
      url: DEPLOYMENT_URL,
      port: DEFAULT_SERVICE_PORT,
      tokenFile: null,
    });
    expect(outcome.stdout).not.toContain(PROXY_HEADER_VALUE);
    expect(outcome.stdout.toLowerCase()).not.toContain("authorization");
  });

  it("el texto conserva las claves de 0.13 y añade la flota", async () => {
    const outcome = await invoke(new FakeAgentProjects(), ["agent", "deploy"], WITH_TOKEN);
    expect(outcome.stdout).toContain(`sandbox_id=${SANDBOX_ID}`);
    expect(outcome.stdout).toContain(`url=${DEPLOYMENT_URL}`);
    expect(outcome.stdout).toContain(`port=${DEFAULT_SERVICE_PORT}`);
    expect(outcome.stdout).toContain(`name=${FLEET_NAME}`);
    expect(outcome.stdout).toContain("members=1");
  });

  it("toma la ruta como argumento o como --file, y los dos distintos son ambiguos", async () => {
    const projects = new FakeAgentProjects();
    await invoke(projects, ["agent", "deploy", "./mi-agente"], WITH_TOKEN);
    await invoke(projects, ["agent", "deploy", "--file", "./otro"], WITH_TOKEN);
    await invoke(projects, ["agent", "deploy"], WITH_TOKEN);
    const clash = await invoke(
      projects,
      ["agent", "deploy", "./uno", "--file", "./dos"],
      WITH_TOKEN,
    );
    expect(projects.deployRequests.map((request) => request.path)).toEqual([
      "./mi-agente",
      "./otro",
      ".",
    ]);
    expect(clash.exitCode).toBe(2);
  });

  it("pasa los overrides de la detección", async () => {
    const projects = new FakeAgentProjects();
    await invoke(
      projects,
      ["agent", "deploy", "--framework", "strands", "--entry", "a:b"],
      WITH_TOKEN,
    );
    expect(projects.deployRequests[0]?.overrides).toEqual({
      framework: "strands",
      language: undefined,
      entry: "a:b",
      protocol: undefined,
    });
  });

  it("mapea cada bandera de escalado y de servicio", async () => {
    const projects = new FakeAgentProjects();
    const outcome = await invoke(
      projects,
      [
        "agent", "deploy", "--memory", "4096", "--max-sessions", "2", "--sessions", "dedicated",
        "--min-vms", "1", "--max-vms", "3", "--warm", "1", "--idle", "120", "--queue", "8",
        "--queue-timeout", "45", "--env", "MODEL_ID=x", "--env", "A=b", "--allow-out",
        "api.example.com", "--port", "9100", "--timeout", "120",
      ],
      WITH_TOKEN,
    ); // biome-ignore format: una bandera por pareja
    expect(outcome.exitCode).toBe(0);
    expect(projects.deployRequests[0]?.flags).toEqual({
      port: 9100,
      timeoutMs: 120 * MS_PER_SECOND,
      memory: 4096,
      maxSessions: 2,
      idleSeconds: 120,
      queue: 8,
      queueTimeoutMs: 45 * MS_PER_SECOND,
      env: { MODEL_ID: "x", A: "b" },
      allowOut: ["api.example.com"],
      minVms: 1,
      maxVms: 3,
      warm: 1,
      sessions: "dedicated",
    });
  });

  it("sin banderas no cambia nada del proyecto", async () => {
    const projects = new FakeAgentProjects();
    await invoke(projects, ["agent", "deploy"], WITH_TOKEN);
    expect(projects.deployRequests[0]?.flags).toEqual({
      port: undefined,
      timeoutMs: undefined,
      memory: undefined,
      maxSessions: undefined,
      idleSeconds: undefined,
      queue: undefined,
      queueTimeoutMs: undefined,
      env: {},
      allowOut: [],
      minVms: undefined,
      maxVms: undefined,
      warm: undefined,
      sessions: undefined,
    });
  });

  it("rechaza --sessions desconocido y un --secret mal formado sin repetir su valor", async () => {
    const projects = new FakeAgentProjects();
    const sessions = await invoke(projects, ["agent", "deploy", "--sessions", "otro"], WITH_TOKEN);
    const secret = await invoke(
      projects,
      ["agent", "deploy", "--secret", "valor-pegado"],
      WITH_TOKEN,
    );
    expect(sessions.exitCode).toBe(2);
    expect(secret.exitCode).toBe(2);
    expect(secret.stderr).not.toContain("valor-pegado");
    expect(projects.deployRequests).toEqual([]);
  });

  it("pasa las opciones de AWS al SDK", async () => {
    const projects = new FakeAgentProjects();
    await invoke(
      projects,
      [
        "agent", "deploy", "--bucket", "amzn-s3-demo-bucket", "--secret", `bedrock=${OWN_SECRET}`,
        "--create-secret", "--no-build", "--keep-on-failure",
      ],
      WITH_TOKEN,
    ); // biome-ignore format: una bandera por pareja
    const { settings } = projects.deployRequests[0] ?? {};
    expect(settings?.bucket).toBe("amzn-s3-demo-bucket");
    expect(settings?.secretNames).toEqual({ bedrock: OWN_SECRET });
    expect(settings?.createSecretFromEnv).toBe(true);
    expect(settings?.buildImage).toBe(false);
    expect(settings?.keepOnFailure).toBe(true);
    expect(settings?.accessToken).toBe(ACCESS_TOKEN);
  });

  it("construye por defecto sólo si hay bucket (--build/--no-build son tres estados)", async () => {
    const projects = new FakeAgentProjects();
    await invoke(projects, ["agent", "deploy"], WITH_TOKEN);
    await invoke(projects, ["agent", "deploy", "--build"], WITH_TOKEN);
    await invoke(projects, ["agent", "deploy", "--no-build"], WITH_TOKEN);
    expect(projects.deployRequests.map((request) => request.settings.buildImage)).toEqual([
      undefined,
      true,
      false,
    ]);
  });

  it("avisa cuando las rutas de la app no se exponen", async () => {
    const projects = new FakeAgentProjects();
    projects.hiddenRoutes = {
      protocol: "agentcore",
      healthPath: "/health",
      invokePath: "/invoke",
    };
    const warned = await invoke(projects, ["agent", "deploy"], WITH_TOKEN);
    expect(warned.stderr).toContain('protocol = "raw"');
    expect(warned.stderr).toContain("/health");
    projects.hiddenRoutes = undefined;
    const quiet = await invoke(projects, ["agent", "deploy"], WITH_TOKEN);
    expect(quiet.stderr).not.toContain("aviso");
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
    expect(projects.deployRequests[0]?.settings.accessToken).toBe(token);
    expect(projects.deployRequests[0]?.settings.tokenFile).toBe(tokenFile);
  });

  it("sin token usa .rayito/agent-token con modo 0600 y lo reutiliza", async () => {
    await inTemporaryDirectory(async () => {
      const projects = new FakeAgentProjects();
      const first = await invoke(projects, ["agent", "deploy"]);
      await invoke(projects, ["agent", "deploy"]);
      const token = readFileSync(".rayito/agent-token", "utf8");
      expect(first.exitCode).toBe(0);
      expect(first.stdout).toContain("token_file=.rayito/agent-token");
      expect(first.stdout).not.toContain(token);
      expect(statSync(".rayito/agent-token").mode & 0o777).toBe(0o600);
      expect(projects.deployRequests.map((request) => request.settings.accessToken)).toEqual([
        token,
        token,
      ]);
      expect(projects.deployRequests[0]?.settings.tokenFile).toBe(".rayito/agent-token");
    });
  });

  it("no toca un --token-file que ya existe", async () => {
    const projects = new FakeAgentProjects();
    const tokenFile = join(tempDirectory(), "token");
    writeFileSync(tokenFile, "old");
    const outcome = await invoke(projects, ["agent", "deploy", "--token-file", tokenFile]);
    expect(outcome.exitCode).toBe(2);
    expect(readFileSync(tokenFile, "utf8")).toBe("old");
    expect(projects.deployRequests).toEqual([]);
  });

  it.each([
    ["un SandboxError", () => new SandboxError("el host no arrancó")],
    ["un InvalidArgumentError", () => new InvalidArgumentError("x")],
    ["cualquier otro error", () => new Error("boom")],
  ])(
    "%s borra el --token-file que creó el despliegue y el reintento funciona",
    async (_label, failure) => {
      const projects = new FakeAgentProjects();
      projects.deployFailure = failure();
      const tokenFile = join(tempDirectory(), "token");
      const failed = await invoke(projects, ["agent", "deploy", "--token-file", tokenFile]);
      expect(failed.exitCode).not.toBe(0);
      expect(existsSync(tokenFile)).toBe(false);
      projects.deployFailure = undefined;
      const retry = await invoke(projects, ["agent", "deploy", "--token-file", tokenFile]);
      expect(retry.exitCode).toBe(0);
      expect(projects.deployRequests.at(-1)?.settings.accessToken).toBe(
        readFileSync(tokenFile, "utf8"),
      );
    },
  );

  it("con --keep-on-failure conserva el token del sandbox que se queda", async () => {
    const projects = new FakeAgentProjects();
    projects.deployFailure = new SandboxError("el host no arrancó");
    const tokenFile = join(tempDirectory(), "token");
    const outcome = await invoke(projects, [
      "agent",
      "deploy",
      "--token-file",
      tokenFile,
      "--keep-on-failure",
    ]);
    expect(outcome.exitCode).toBe(1);
    expect(readFileSync(tokenFile, "utf8")).not.toBe("");
  });

  it("un fallo nunca borra el token de una ejecución anterior", async () => {
    await inTemporaryDirectory(async () => {
      const projects = new FakeAgentProjects();
      await invoke(projects, ["agent", "deploy"]);
      const earlier = readFileSync(".rayito/agent-token", "utf8");
      projects.deployFailure = new SandboxError("el host no arrancó");
      const outcome = await invoke(projects, ["agent", "deploy"]);
      expect(outcome.exitCode).toBe(1);
      expect(readFileSync(".rayito/agent-token", "utf8")).toBe(earlier);
    });
  });

  it("un fallo de despliegue imprime su causa y sale con 1", async () => {
    const projects = new FakeAgentProjects();
    projects.deployFailure = new SandboxError(
      "el host del agente no pudo arrancar (app_exited). Final de la salida de error de la app:\nModuleNotFoundError: agent",
    );
    const outcome = await invoke(projects, ["agent", "deploy"], WITH_TOKEN);
    expect(outcome.exitCode).toBe(1);
    expect(outcome.stderr).toContain("ModuleNotFoundError: agent");
  });

  it("un argumento inválido sale con 2", async () => {
    const projects = new FakeAgentProjects();
    projects.deployFailure = new InvalidArgumentError("pasarela sin configurar");
    const outcome = await invoke(projects, ["agent", "deploy"], WITH_TOKEN);
    expect(outcome.exitCode).toBe(2);
    expect(outcome.stderr).toContain("pasarela sin configurar");
  });

  it("imprime cada paso en stderr sin nombrar un secreto", async () => {
    const outcome = await invoke(new FakeAgentProjects(), ["agent", "deploy"], WITH_TOKEN);
    expect(outcome.stderr).toContain("imagen lista: versión 3.0");
  });

  it("imprime la detección antes de desplegar", async () => {
    const projects = new FakeAgentProjects();
    projects.detectionReport = { language: "python", framework: "langgraph", evidence: ["a"] };
    const outcome = await invoke(projects, ["agent", "deploy"], WITH_TOKEN);
    expect(outcome.stderr).toContain("detectado framework: langgraph");
    expect(outcome.stderr).not.toContain("evidence");
  });

  it("--dry-run no despliega, no pide AWS y no escribe el token", async () => {
    await inTemporaryDirectory(async () => {
      const projects = new FakeAgentProjects();
      const outcome = await invoke(projects, ["agent", "deploy", "--dry-run", "--max-vms", "2"]);
      expect(outcome.exitCode).toBe(0);
      expect(projects.deployRequests, REFUSE_AWS).toEqual([]);
      expect(projects.planRequests).toHaveLength(1);
      expect(() => statSync(".rayito")).toThrow();
      expect(outcome.stdout).toContain("VMs: máximo 2");
      expect(outcome.stdout).toContain("no se llamó a AWS");
    });
  });

  it("--dry-run --json es un documento", async () => {
    const outcome = await invoke(new FakeAgentProjects(), [
      "--json",
      "agent",
      "deploy",
      "--dry-run",
    ]);
    const document = JSON.parse(outcome.stdout);
    expect(document.dryRun).toBe(true);
    expect(document.plan.scaling.maxVms).toBe(1);
  });
});

describe("agent detect", () => {
  it("describe el proyecto sin AWS", async () => {
    const projects = new FakeAgentProjects();
    projects.detectionReport = { language: "typescript", framework: "strands-ts" };
    const outcome = await invoke(projects, [
      "agent",
      "detect",
      "./mi-agente",
      "--framework",
      "strands-ts",
    ]);
    expect(outcome.exitCode).toBe(0);
    expect(outcome.stdout).toContain("detectado language: typescript");
    expect(projects.calls).toEqual([["detect", "./mi-agente", "strands-ts", undefined]]);
  });

  it("--json y --write", async () => {
    const projects = new FakeAgentProjects();
    projects.detectionReport = { framework: "strands" };
    const outcome = await invoke(projects, ["--json", "agent", "detect", "--write"]);
    const document = JSON.parse(outcome.stdout);
    expect(document.detection).toEqual({ framework: "strands" });
    expect(document.written).toBe("rayito-agent.json");
  });

  it("un proyecto ambiguo sale con 2", async () => {
    const projects = new FakeAgentProjects();
    projects.loadFailure = new InvalidArgumentError("hay dos frameworks: usa --framework");
    const outcome = await invoke(projects, ["agent", "detect"]);
    expect(outcome.exitCode).toBe(2);
    expect(outcome.stderr).toContain("--framework");
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
    projects.deployed.events = [
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
    expect(projects.deployed.calls).toEqual([
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
