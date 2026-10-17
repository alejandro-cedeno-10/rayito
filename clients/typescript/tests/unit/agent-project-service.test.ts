/**
 * `build()`, `run()` y `deploy()` de un `AgentProject` (`agent/project-service.ts`)
 * sobre un sandbox de doble: los argumentos de `Sandbox.create()`, lo que
 * `deploy()` escribe y arranca, la espera del marcador de listo y la limpieza
 * (un `kill()`, nunca dos, nunca ninguno) ante cualquier fallo. Espejo de
 * `test_agent_project_service.py`; ninguna prueba llama a AWS.
 */

import { describe, expect, test, vi } from "vitest";
import { AgentDeployment } from "../../src/agent/deployment.js";
import { ServeOptions } from "../../src/agent/project.js";
import {
  AGENT_HOST_CONFIG_PATH,
  AGENT_SERVE_TAG,
  ProjectService,
} from "../../src/agent/project-service.js";
import { AGENT_HOST_DIR } from "../../src/agent/recipes.js";
import { AgentTemplate } from "../../src/agent/template.js";
import { AgentError, InvalidArgumentError, SandboxError, TimeoutError } from "../../src/errors.js";
import {
  AGENT_SERVICE_PORT,
  AGENT_SERVICE_READY_MARKER_PATH,
  AGENT_SERVICE_READY_TIMEOUT_SECONDS,
  AGENT_STARTUP_DIAGNOSTICS_MAX_BYTES,
} from "../../src/limits.js";
import { FakeAgentRuntime } from "./agent-fake.js";
import {
  FakeSandbox,
  FakeSandboxFactory,
  type FakeSandboxOptions,
  project,
  SANDBOX_ID,
} from "./agent-project-fake.js";

interface Harness {
  readonly sandbox: FakeSandbox;
  readonly factory: FakeSandboxFactory;
  readonly clock: { now: number; sleeps: number[] };
  service(the: ReturnType<typeof project>): ProjectService;
}

async function failureOf(promise: Promise<unknown>): Promise<Error> {
  try {
    await promise;
  } catch (error) {
    return error as Error;
  }
  throw new Error("se esperaba un fallo");
}

function harness(options: FakeSandboxOptions = {}): Harness {
  const sandbox = new FakeSandbox(options);
  const factory = new FakeSandboxFactory(sandbox);
  const clock = { now: 0, sleeps: [] as number[] };
  return {
    sandbox,
    factory,
    clock,
    service: (the) =>
      new ProjectService(the, {
        sandboxFactory: factory,
        now: () => clock.now,
        sleep: async (ms) => {
          clock.sleeps.push(ms);
          clock.now += ms;
        },
      }),
  };
}

describe("build", () => {
  test("composes the template of the project", async () => {
    const built = vi.spyOn(AgentTemplate.prototype, "build").mockResolvedValue({
      templateId: "t",
      buildId: "b",
      alias: "a",
    });
    const { service, factory } = harness();
    await service(
      project({ source: ".", exclude: [".venv"], memoryMib: 4096, template: "imagen-propia" }),
    ).build({ bucket: "amzn-s3-demo-bucket", force: true, region: "us-east-1" });
    const template = built.mock.contexts[0] as AgentTemplate;
    expect([template.name, template.base, template.memoryMib]).toEqual([
      "imagen-propia",
      "rayito-base-caps",
      4096,
    ]);
    expect([template.app, [...template.appExclude]]).toEqual([".", [".venv"]]);
    expect(template.runtimes).toEqual(["deepagents"]);
    expect(built).toHaveBeenCalledWith({
      bucket: "amzn-s3-demo-bucket",
      force: true,
      region: "us-east-1",
    });
    expect(factory.created).toEqual([]);
    built.mockRestore();
  });

  test("defaults the image name to the project name", async () => {
    const built = vi.spyOn(AgentTemplate.prototype, "build").mockResolvedValue({
      templateId: "t",
      buildId: "b",
      alias: "a",
    });
    await harness().service(project()).build({ bucket: "b" });
    expect((built.mock.contexts[0] as AgentTemplate).name).toBe("support-bot");
    built.mockRestore();
  });

  test("rejects a runtime that cannot be installed", async () => {
    const { service, factory } = harness();
    await expect(
      service(project({}, new FakeAgentRuntime())).build({ bucket: "b" }),
    ).rejects.toThrow(/InstallableRuntime/);
    expect(factory.created).toEqual([]);
  });
});

describe("env and network", () => {
  test("run and deploy hand the project env and network to the sandbox", async () => {
    const { service, factory } = harness();
    const the = project({
      env: { LOG_LEVEL: "debug" },
      network: { allowOut: ["api.example.com"], denyOut: ["0.0.0.0/0"] },
    });
    await service(the).run("hola");
    await service(the).deploy();
    expect(factory.created).toHaveLength(2);
    for (const created of factory.created) {
      expect(created?.envs).toEqual({ LOG_LEVEL: "debug" });
      expect(created?.network).toEqual({ allowOut: ["api.example.com"], denyOut: ["0.0.0.0/0"] });
    }
  });

  test("a project without env or network sends neither", async () => {
    const { service, factory } = harness();
    await service(project()).deploy();
    const [created] = factory.created;
    expect(created).not.toHaveProperty("envs");
    expect(created).not.toHaveProperty("network");
  });
});

describe("run", () => {
  test("creates, runs and always kills", async () => {
    const { service, factory, sandbox } = harness({ agentResult: "listo" });
    const the = project({ egress: ["arn:aws:lambda:us-east-1:123456789012:connector/c"] });
    const result = await service(the).run("hola", {
      sessionId: "s",
      model: "m",
      reasoning: true,
      region: "us-west-2",
    });
    expect(result).toBe("listo");
    const [created] = factory.created;
    expect(created).toMatchObject({
      template: "support-bot",
      timeoutMs: 660_000,
      allowInternetAccess: false,
      egress: ["arn:aws:lambda:us-east-1:123456789012:connector/c"],
      region: "us-west-2",
    });
    expect(Object.keys(created?.gateways ?? {})).toEqual(["bedrock"]);
    expect(created).not.toHaveProperty("idle");
    expect(created).not.toHaveProperty("metadata");
    const [call] = sandbox.agentCalls;
    expect(call?.prompt).toBe("hola");
    expect(call?.options).toMatchObject({
      spec: the.spec,
      runtime: "deepagents",
      limits: the.limits,
      sessionId: "s",
      model: "m",
      reasoning: true,
    });
    expect(sandbox.kills).toBe(1);
  });

  test("forwards every event to onEvent and closes the stream", async () => {
    const { service, sandbox } = harness({ agentResult: "listo", agentEvents: ["uno", "dos"] });
    const seen: unknown[] = [];
    const result = await service(project()).run("hola", { onEvent: (event) => seen.push(event) });
    expect([result, seen]).toEqual(["listo", ["uno", "dos"]]);
    expect(sandbox.streamCloses.count).toBe(1);
    expect(sandbox.kills).toBe(1);
  });

  test("kills its sandbox when the agent fails", async () => {
    const error = new AgentError("falló", { reason: "model_error" });
    const { service, sandbox } = harness({ agentError: error });
    await expect(service(project()).run("hola")).rejects.toBe(error);
    expect(sandbox.kills).toBe(1);
  });

  test("a failing kill never hides the agent error", async () => {
    const error = new AgentError("falló", { reason: "timeout" });
    const { service } = harness({ agentError: error, killError: new SandboxError("no se pudo") });
    await expect(service(project()).run("hola")).rejects.toBe(error);
  });
});

describe("deploy", () => {
  test("writes the host and starts it", async () => {
    const { service, factory, sandbox } = harness();
    const the = project({
      serve: new ServeOptions({
        timeoutMs: 1_800_400,
        idle: { maxIdleSeconds: 120, autoResume: true },
        metadata: { team: "soporte" },
        maxConcurrency: 2,
        queueMax: 3,
        queueTimeoutMs: 45_000,
      }),
      allowInternetAccess: true,
    });
    const deployment = await service(the).deploy();
    const [created] = factory.created;
    expect(created).toMatchObject({
      template: "support-bot",
      timeoutMs: 1_801_000,
      allowInternetAccess: true,
      metadata: {
        team: "soporte",
        "rayito.agent.deployment": "support-bot",
        "rayito.agent.port": String(AGENT_SERVICE_PORT),
      },
    });
    expect(created?.idle).toEqual({ maxIdleSeconds: 120, autoResume: true });
    const written = new Map(sandbox.written.map((entry) => [entry.path, entry]));
    const hostJson = JSON.parse(
      new TextDecoder().decode(written.get(AGENT_HOST_CONFIG_PATH)?.data),
    ) as {
      driver: string;
      max_concurrency: number;
      queue_max: number;
      queue_timeout_seconds: number;
      ready_marker: string;
      limits: { max_steps: number };
      options: { config_path: string };
    };
    expect(hostJson.driver).toBe("deepagents");
    expect(hostJson.max_concurrency).toBe(2);
    expect([hostJson.queue_max, hostJson.queue_timeout_seconds]).toEqual([3, 45]);
    expect(hostJson.ready_marker).toBe(AGENT_SERVICE_READY_MARKER_PATH);
    expect(hostJson.limits.max_steps).toBe(the.limits.maxSteps);
    expect(written.has(hostJson.options.config_path)).toBe(true);
    const [clear, call] = sandbox.commandCalls;
    expect(clear?.cmd.startsWith("rm -f ")).toBe(true);
    expect(clear?.cmd.match(/\/ready/g)).toHaveLength(2);
    expect(call?.cmd.startsWith("exec ")).toBe(true);
    expect(call?.cmd).toContain(
      `'${AGENT_HOST_DIR}' serve '${AGENT_HOST_CONFIG_PATH}' --port ${AGENT_SERVICE_PORT}`,
    );
    expect(call?.options).toMatchObject({
      background: true,
      timeoutMs: 0,
      user: "user",
      tag: AGENT_SERVE_TAG,
      killTree: true,
    });
    expect(call?.options.envs).toMatchObject({ HOME: "/home/user" });
    expect(sandbox.handle.disconnected).toBe(true);
    expect(deployment).toBeInstanceOf(AgentDeployment);
    expect(deployment.sandboxId).toBe(SANDBOX_ID);
    expect(deployment.port).toBe(AGENT_SERVICE_PORT);
    expect(sandbox.kills).toBe(0);
  });

  test("never writes a secret or a credential", async () => {
    const { service, sandbox } = harness();
    await service(project()).deploy();
    for (const entry of sandbox.written) {
      const text = new TextDecoder().decode(entry.data);
      expect(text).not.toContain("bedrock-key");
      expect(text).not.toContain("AKIA");
    }
  });

  test("waits for the ready marker", async () => {
    const { service, sandbox, clock } = harness({ readyAfter: 3 });
    await service(project()).deploy();
    expect(sandbox.readyPolls).toBe(4);
    expect(clock.sleeps).toHaveLength(3);
    expect(sandbox.kills).toBe(0);
  });

  test("kills the sandbox when the host never gets ready", async () => {
    const { service, sandbox, clock } = harness({ readyAfter: null });
    await expect(service(project()).deploy()).rejects.toBeInstanceOf(TimeoutError);
    expect(sandbox.kills).toBe(1);
    expect(clock.now).toBeGreaterThanOrEqual(AGENT_SERVICE_READY_TIMEOUT_SECONDS * 1000);
  });

  test("fails fast when the host left a failed marker", async () => {
    const { service, sandbox, clock } = harness({
      readyAfter: null,
      failedCode: "ModuleNotFoundError",
    });
    await expect(service(project()).deploy()).rejects.toThrow(/ModuleNotFoundError/);
    expect(sandbox.kills).toBe(1);
    expect(clock.now).toBe(0);
  });

  test("a failed startup shows the end of the app's stderr", async () => {
    const { service, sandbox } = harness({
      readyAfter: null,
      failedMarker: "app_exited\nTraceback (most recent call last):\nModuleNotFoundError: agent\n",
    });
    const error = await failureOf(service(project()).deploy());
    expect(error.message.startsWith("el host del agente no pudo arrancar (app_exited)")).toBe(true);
    expect(error.message).toContain("ModuleNotFoundError: agent");
    expect(sandbox.kills).toBe(1);
  });

  test("a failed marker is bounded again by the SDK", async () => {
    const { service } = harness({
      readyAfter: null,
      failedMarker: `app_exited\n${"inicio ".repeat(3000)}fin-visible`,
    });
    const error = await failureOf(service(project()).deploy());
    expect(error.message.endsWith("fin-visible")).toBe(true);
    expect(new TextEncoder().encode(error.message).length).toBeLessThan(
      AGENT_STARTUP_DIAGNOSTICS_MAX_BYTES + 200,
    );
  });

  test("keepOnFailure leaves the sandbox alive and names it", async () => {
    const { service, sandbox } = harness({
      readyAfter: null,
      failedMarker: "app_exited\nboom\n",
    });
    const error = await failureOf(
      service(project()).deploy({ accessToken: "dG9rZW4", keepOnFailure: true }),
    );
    expect(sandbox.kills).toBe(0);
    expect(error.message).toContain(SANDBOX_ID);
    expect(error.message).toContain("Sandbox.kill");
    expect(error.message).not.toContain("dG9rZW4");
  });

  test("keepOnFailure needs a token the caller knows", async () => {
    vi.stubEnv("RAYITO_ACCESS_TOKEN", "");
    const { service, factory } = harness();
    await expect(service(project()).deploy({ keepOnFailure: true })).rejects.toBeInstanceOf(
      InvalidArgumentError,
    );
    expect(factory.created).toEqual([]);
    vi.stubEnv("RAYITO_ACCESS_TOKEN", "dG9rZW4");
    await service(project()).deploy({ keepOnFailure: true });
    expect(factory.created).toHaveLength(1);
    vi.unstubAllEnvs();
  });

  test("keepOnFailure is off by default and a success never kills", async () => {
    const failing = harness({ readyAfter: null, failedCode: "AgentFailure" });
    await expect(failing.service(project()).deploy()).rejects.toBeInstanceOf(SandboxError);
    expect(failing.sandbox.kills).toBe(1);
    const ok = harness();
    await ok.service(project()).deploy({ accessToken: "dG9rZW4", keepOnFailure: true });
    expect(ok.sandbox.kills).toBe(0);
  });

  test("kills the sandbox on any step failure", async () => {
    const { service, sandbox } = harness({ writeError: new SandboxError("falló la escritura") });
    await expect(service(project()).deploy()).rejects.toBeInstanceOf(SandboxError);
    expect(sandbox.kills).toBe(1);
  });

  test("a runtime that cannot be served fails before any call", async () => {
    const { service, factory } = harness();
    await expect(service(project({}, new FakeAgentRuntime())).deploy()).rejects.toThrow(
      /ServableRuntime/,
    );
    expect(factory.created).toEqual([]);
  });

  test("a failing create leaves nothing to kill", async () => {
    const { service, factory, sandbox } = harness();
    factory.error = new SandboxError("sin capacidad");
    await expect(service(project()).deploy()).rejects.toBeInstanceOf(SandboxError);
    expect(sandbox.kills).toBe(0);
  });
});

describe("zero calls", () => {
  test("constructing the service creates nothing", () => {
    const { service, factory, sandbox } = harness();
    service(project());
    expect(factory.created).toEqual([]);
    expect(sandbox.kills).toBe(0);
  });
});
