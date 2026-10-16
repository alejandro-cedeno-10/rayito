/**
 * Adaptadores de Strands, Hermes y `CustomRuntime` (`agent-runtimes-byo`,
 * ADR-028): configuración del host, ficheros y script de una ejecución,
 * recetas de instalación, matriz de proveedores y valores seguros (D9),
 * contra los vectores que comparten con Python
 * (`testdata/agent/framework-*.json` y `provider-catalogue.json`). Espejo de
 * `test_agent_framework_runtimes.py`.
 */

import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, test } from "vitest";
import { CustomRuntime, HttpApp } from "../../src/agent/custom.js";
import { DeepAgents } from "../../src/agent/deepagents.js";
import { AgentModel, AgentPermissions, AgentSpec } from "../../src/agent/domain.js";
import {
  HERMES_DEFAULT_TOOLSETS,
  HERMES_PROVIDERS,
  HermesRuntime,
} from "../../src/agent/hermes.js";
import { AGENT_APP_DIR, nodeBridge } from "../../src/agent/recipes.js";
import {
  directWiring,
  gatewayWiring,
  installTarget,
  isInstallable,
  isServable,
  type RuntimeFile,
} from "../../src/agent/runtime.js";
import {
  AGENT_RUNTIMES,
  RUNTIME_OPTION_KEYS,
  resolveRuntime,
  runtimeFromOptions,
  runtimeToOptions,
} from "../../src/agent/runtimes.js";
import {
  harnessBuiltinTools,
  STRANDS_PROVIDERS,
  STRANDS_TS_PROVIDERS,
  StrandsRuntime,
} from "../../src/agent/strands.js";
import { InvalidArgumentError } from "../../src/errors.js";
import { MODEL_CREDENTIAL_PLACEHOLDER } from "../../src/limits.js";
import { Agent } from "../../src/sandbox/agent.js";
import {
  FakeCommandHandle,
  FakeCommands,
  FakeFilesystem,
  FakeSandbox,
  gatewayStatus,
} from "./agent-fake.js";

const REPO = join(import.meta.dirname, "..", "..", "..", "..");
const TESTDATA = join(REPO, "testdata", "agent");
const DECODER = new TextDecoder();
const SESSION_ID = "rda_0000000000000000000000000000000a";

// biome-ignore lint/suspicious/noExplicitAny: vectores JSON compartidos, sin esquema.
type Json = any;

function readJson(name: string): Json {
  return JSON.parse(readFileSync(join(TESTDATA, name), "utf8"));
}

const HOST_VECTORS = readJson("framework-host-config.json");
const INSTALL_VECTORS = readJson("framework-install-steps.json");
const CATALOGUE = readJson("provider-catalogue.json");
const SPIKE = readJson("strands-spike.json");

function specOf(data: Json): AgentSpec {
  return new AgentSpec({
    model: new AgentModel({
      provider: data.provider,
      id: data.model,
      gateway: "gw",
      region: data.region ?? undefined,
      basePath: data.base_path ?? undefined,
      promptCaching: data.prompt_caching ?? undefined,
    }),
    instructions: data.instructions ?? undefined,
    permissions: data.permissions ? new AgentPermissions(data.permissions) : undefined,
  });
}

function fileOf(entry: RuntimeFile): Json {
  const text = DECODER.decode(entry.data);
  return entry.path.endsWith(".json")
    ? { path: entry.path, mode: entry.mode, json: JSON.parse(text) }
    : { path: entry.path, mode: entry.mode, text };
}

function runtimeOf(runtime: Json) {
  return runtimeFromOptions(runtime.kind, runtime.options);
}

function asServable(runtime: ReturnType<typeof runtimeFromOptions>) {
  if (!isServable(runtime)) {
    throw new Error("el runtime no es servible");
  }
  return runtime;
}

function asInstallable(runtime: ReturnType<typeof runtimeFromOptions>) {
  if (!isInstallable(runtime)) {
    throw new Error("el runtime no es instalable");
  }
  return runtime;
}

describe("framework host config vectors", () => {
  const names: string[] = HOST_VECTORS.cases.map((c: Json) => c.name);
  test.each(names)("%s", (name) => {
    const vector = HOST_VECTORS.cases.find((c: Json) => c.name === name);
    const runtime = runtimeOf(vector.runtime);
    const spec = specOf(vector.spec);
    const wiring =
      vector.wiring === "gateway"
        ? gatewayWiring({ gw: HOST_VECTORS.gateway_url })
        : directWiring();
    const servable = asServable(runtime);
    if (vector.expected.error !== undefined) {
      expect(() => servable.hostConfig(spec, { wiring, workdir: vector.workdir })).toThrow(
        InvalidArgumentError,
      );
      return;
    }
    const host = servable.hostConfig(spec, { wiring, workdir: vector.workdir });
    expect(host.driver).toBe(vector.expected.driver);
    expect(host.python).toBe(vector.expected.python);
    expect(host.options).toEqual(vector.expected.options);
    expect(host.envs).toEqual(vector.expected.envs);
    expect(host.files.files.map(fileOf)).toEqual(vector.expected.files);
    if (vector.wiring === "gateway") {
      const task = runtime.buildConfig(spec, {
        gatewayUrls: { gw: HOST_VECTORS.gateway_url },
        workdir: vector.workdir,
      });
      expect(task.files.map(fileOf)).toEqual(vector.expected.task_files);
    }
  });
});

describe("framework install vectors", () => {
  const names: string[] = INSTALL_VECTORS.cases.map((c: Json) => c.name);
  test.each(names)("%s", (name) => {
    const vector = INSTALL_VECTORS.cases.find((c: Json) => c.name === name);
    const runtime = asInstallable(runtimeOf(vector.runtime));
    for (const platform of ["microvm", "container"] as const) {
      const steps = runtime.installSteps(installTarget({ platform }));
      expect(steps.map((step) => ({ op: step.op, args: { ...step.args } }))).toEqual(
        vector[platform],
      );
    }
    const hashes: Record<string, string> = {};
    for (const [key, data] of Object.entries(runtime.contextFiles()).sort()) {
      hashes[key] = createHash("sha256").update(data, "utf8").digest("hex");
    }
    expect(hashes).toEqual(vector.context_files);
    expect(runtime.manifestEntry()).toEqual(vector.manifest_entry);
    expect(runtime.smokeChecks()).toEqual(vector.smoke_checks);
    expect(runtime.prefetchPaths()).toEqual(vector.prefetch_paths);
    const warmups = (
      runtime as unknown as { warmupSteps(): { cmd: string; background?: boolean }[] }
    )
      .warmupSteps()
      .map((step) => ({ cmd: step.cmd, background: step.background ?? false }));
    expect(warmups).toEqual(vector.warmup_steps);
  });

  test("the embedded bridge is the file Python ships", () => {
    const python = readFileSync(
      join(REPO, "clients", "python", "src", "rayito", "_agent", "_assets", "node_bridge.mjs"),
      "utf8",
    );
    expect(nodeBridge()).toBe(python);
  });
});

describe("framework runtimes", () => {
  test("implement both optional ports and the registry resolves them", () => {
    const runtimes = [
      AGENT_RUNTIMES.strands,
      AGENT_RUNTIMES["strands-harness"],
      AGENT_RUNTIMES.hermes,
      new StrandsRuntime({ language: "typescript", entrypoint: "agent.ts" }),
      new CustomRuntime({ command: ["x"] }),
    ];
    for (const runtime of runtimes) {
      expect(isInstallable(runtime) && isServable(runtime)).toBe(true);
    }
    expect(resolveRuntime("strands").name).toBe("strands");
    expect(resolveRuntime("strands-harness").name).toBe("strands-harness");
    expect(resolveRuntime("hermes").name).toBe("hermes");
    expect(() => resolveRuntime("custom")).toThrow(InvalidArgumentError);
  });

  test.each([
    ["strands", {}],
    ["strands", { entrypoint: "agente:crea", requirements: "requirements.txt" }],
    ["strands-harness", { entrypoint: "agente:crea" }],
    ["strands", { language: "typescript", entrypoint: "agent.ts" }],
    ["langgraph", { entrypoint: "agente:crea", requirements: "requirements.txt" }],
    ["langgraph", { entrypoint: "agente:crea", requirements: "uv.lock", package_manager: "uv" }],
    ["hermes", {}],
    ["hermes", { toolsets: ["terminal"], skills_dir: "skills" }],
    ["custom", { command: ["./run.sh"], protocol: "text", install: ["echo hola"] }],
    ["custom", { entrypoint: "agente:responde", requirements: "r.txt", python: "python3" }],
    ["custom", { app: { command: ["serve"], port: 9000, protocol: "openai-chat" } }],
    [
      "custom",
      {
        app: {
          command: ["serve"],
          port: 9000,
          protocol: "json",
          request_field: "question",
          response_field: "data.answer",
          port_env: "PORT",
        },
      },
    ],
    ["custom", { entrypoint: "agent.ts#graph", package_manager: "pnpm", adapter: "langgraph" }],
    ["custom", { entrypoint: "agent.ts", package_manager: "npm", node_version: "24" }],
    [
      "custom",
      {
        entrypoint: "a:b",
        requirements: "uv.lock",
        package_manager: "uv",
        python_version: "3.13",
      },
    ],
    [
      "strands",
      {
        language: "typescript",
        entrypoint: "agent.ts",
        package_manager: "yarn",
        node_version: "22",
      },
    ],
  ])("options round trip: %s %j", (kind, options) => {
    const table = runtimeToOptions(runtimeFromOptions(kind, options));
    const { kind: tableKind, ...rest } = table;
    expect(tableKind).toBe(kind);
    expect(rest).toEqual(options);
    expect(Object.keys(rest).every((key) => RUNTIME_OPTION_KEYS[kind]?.includes(key))).toBe(true);
  });

  test.each([
    ["language", () => new StrandsRuntime({ language: "rust" as never })],
    ["ts without entrypoint", () => new StrandsRuntime({ language: "typescript" })],
    [
      "ts outside source",
      () => new StrandsRuntime({ language: "typescript", entrypoint: "../fuera.ts" }),
    ],
    [
      "ts harness",
      () => new StrandsRuntime({ language: "typescript", entrypoint: "a.ts", harness: true }),
    ],
    ["entrypoint form", () => new StrandsRuntime({ entrypoint: "sin-dos-puntos" })],
    ["requirements", () => new StrandsRuntime({ requirements: "../r.txt" })],
    ["empty toolsets", () => new HermesRuntime({ toolsets: [] })],
    ["repeated toolsets", () => new HermesRuntime({ toolsets: ["terminal", "terminal"] })],
    ["toolset form", () => new HermesRuntime({ toolsets: ["Terminal"] })],
    ["skills outside source", () => new HermesRuntime({ skillsDir: "../skills" })],
    ["custom nothing", () => new CustomRuntime()],
    ["custom two", () => new CustomRuntime({ command: ["a"], entrypoint: "m:f" })],
    ["custom protocol", () => new CustomRuntime({ entrypoint: "m:f", protocol: "text" })],
    ["custom entrypoint", () => new CustomRuntime({ entrypoint: "sin-forma" })],
    ["custom empty command", () => new CustomRuntime({ command: [] })],
    ["http port", () => new HttpApp({ command: ["x"], port: 0 })],
    ["http path", () => new HttpApp({ command: ["x"], port: 8000, healthPath: "ping" })],
    ["http protocol", () => new HttpApp({ command: ["x"], port: 8000, protocol: "soap" as never })],
    ["http request field", () => new HttpApp({ command: ["x"], port: 8000, requestField: "q" })],
    [
      "http response field",
      () => new HttpApp({ command: ["x"], port: 8000, protocol: "json", responseField: "a b" }),
    ],
    [
      "http request field name",
      () => new HttpApp({ command: ["x"], port: 8000, protocol: "json", requestField: "1q" }),
    ],
    ["http port env", () => new HttpApp({ command: ["x"], port: 8000, portEnv: "port" })],
    [
      "custom unknown manager",
      () => new CustomRuntime({ entrypoint: "a.ts#x", packageManager: "bun" }),
    ],
    [
      "custom node version alone",
      () => new CustomRuntime({ entrypoint: "a:b", nodeVersion: "22" }),
    ],
    [
      "custom unknown node version",
      () => new CustomRuntime({ entrypoint: "a.ts", packageManager: "npm", nodeVersion: "21" }),
    ],
    [
      "custom unknown python version",
      () => new CustomRuntime({ entrypoint: "a:b", packageManager: "uv", pythonVersion: "3.9" }),
    ],
    [
      "custom python version with node",
      () => new CustomRuntime({ entrypoint: "a.ts", packageManager: "npm", pythonVersion: "3.13" }),
    ],
    [
      "custom python and python version",
      () => new CustomRuntime({ entrypoint: "a:b", python: "python3.12", pythonVersion: "3.13" }),
    ],
    [
      "custom adapter without node",
      () => new CustomRuntime({ entrypoint: "a:b", adapter: "callable" }),
    ],
    [
      "custom unknown adapter",
      () => new CustomRuntime({ entrypoint: "a.ts", packageManager: "npm", adapter: "crewai" }),
    ],
    [
      "custom adapter with command",
      () => new CustomRuntime({ command: ["x"], packageManager: "npm", adapter: "callable" }),
    ],
    [
      "custom requirements with node",
      () => new CustomRuntime({ entrypoint: "a.ts", packageManager: "npm", requirements: "r.txt" }),
    ],
    [
      "custom lock with pip",
      () =>
        new CustomRuntime({ entrypoint: "a:b", requirements: "uv.lock", packageManager: "pip" }),
    ],
    [
      "custom pyproject without uv",
      () => new CustomRuntime({ entrypoint: "a:b", requirements: "pyproject.toml" }),
    ],
    [
      "custom python entrypoint with node",
      () => new CustomRuntime({ entrypoint: "a:b", packageManager: "npm" }),
    ],
    [
      "custom js export name",
      () => new CustomRuntime({ entrypoint: "a.ts#1x", packageManager: "npm" }),
    ],
    [
      "custom js entrypoint outside source",
      () => new CustomRuntime({ entrypoint: "../a.ts", packageManager: "npm" }),
    ],
    ["deepagents requirements without entrypoint", () => new DeepAgents({ requirements: "r.txt" })],
    [
      "deepagents lock without uv",
      () => new DeepAgents({ entrypoint: "a:b", requirements: "uv.lock" }),
    ],
    [
      "deepagents unknown manager",
      () => new DeepAgents({ entrypoint: "a:b", requirements: "r.txt", packageManager: "bun" }),
    ],
    ["deepagents manager alone", () => new DeepAgents({ entrypoint: "a:b", packageManager: "uv" })],
    [
      "deepagents requirements outside source",
      () => new DeepAgents({ entrypoint: "a:b", requirements: "../r.txt" }),
    ],
    ["strands manager with python", () => new StrandsRuntime({ packageManager: "npm" })],
    ["strands node version with python", () => new StrandsRuntime({ nodeVersion: "22" })],
    [
      "strands python manager",
      () =>
        new StrandsRuntime({
          language: "typescript",
          entrypoint: "a.ts",
          packageManager: "uv" as never,
        }),
    ],
    [
      "strands unknown node version",
      () => new StrandsRuntime({ language: "typescript", entrypoint: "a.ts", nodeVersion: "19" }),
    ],
  ])("invalid arguments fail: %s", (_label, factory) => {
    expect(factory).toThrow(InvalidArgumentError);
  });

  test("matrix in code matches the provider catalogue", () => {
    const supported = (runtime: string): Record<string, [string, string | null]> =>
      Object.fromEntries(
        Object.entries(CATALOGUE.runtimes[runtime] as Record<string, Json>)
          .filter(([, entry]) => entry.supported)
          .map(([name, entry]) => [name, [entry.client, entry.base_path]]),
      );
    const fromCode = (providers: Record<string, { client: string; basePath: string | null }>) =>
      Object.fromEntries(
        Object.entries(providers).map(([name, p]) => [name, [p.client, p.basePath]]),
      );
    expect(fromCode(STRANDS_PROVIDERS)).toEqual(supported("strands"));
    expect(fromCode(STRANDS_TS_PROVIDERS)).toEqual(supported("strands-ts"));
    expect(fromCode(HERMES_PROVIDERS)).toEqual(supported("hermes"));
    const known = new Set([...Object.keys(CATALOGUE.model_providers), "openai-compatible"]);
    for (const providers of Object.values(CATALOGUE.runtimes as Record<string, object>)) {
      expect(Object.keys(providers).every((name) => known.has(name))).toBe(true);
    }
  });

  test("the strands model clients come from the recorded spike", () => {
    for (const [language, providers] of [
      ["python", STRANDS_PROVIDERS],
      ["typescript", STRANDS_TS_PROVIDERS],
    ] as const) {
      for (const [provider, entry] of Object.entries(providers)) {
        const measured = SPIKE[language][provider === "openai-compatible" ? "openai" : provider];
        expect(measured.client).toBe(entry.client);
      }
    }
  });
});

describe("safe defaults (design D9)", () => {
  const bedrock = (permissions?: AgentPermissions): AgentSpec =>
    new AgentSpec({
      model: new AgentModel({
        provider: "bedrock",
        id: "m",
        gateway: "gw",
        region: "us-east-1",
      }),
      permissions,
    });
  const wiring = gatewayWiring({ gw: "http://127.0.0.1:18001" });

  test("strands has no OTEL exporter and never carries AWS credentials", () => {
    const host = new StrandsRuntime().hostConfig(bedrock(), { wiring, workdir: "/w" });
    expect(host.envs.OTEL_SDK_DISABLED).toBe("true");
    expect(host.envs.OTEL_TRACES_EXPORTER).toBe("none");
    expect(
      Object.keys(host.envs).some((key) => key.startsWith("OTEL") && key.includes("ENDPOINT")),
    ).toBe(false);
    for (const key of ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"]) {
      expect(host.envs).not.toHaveProperty(key);
    }
    expect(host.envs.AWS_BEARER_TOKEN_BEDROCK).toBe(MODEL_CREDENTIAL_PLACEHOLDER);
  });

  test("the harness turns off code execution and web search whatever the permissions", () => {
    const tools = harnessBuiltinTools(
      bedrock(
        new AgentPermissions({
          tools: { webfetch: "allow", websearch: "allow", bash: "allow" },
        }),
      ),
    );
    expect(tools.programmatic_tool_caller).toBe(false);
    expect(tools.web_search).toBe(false);
    expect(tools["*"]).toBe(false);
  });

  test("the harness builtin tools follow the agent permissions", () => {
    const names = new Set<string>(SPIKE.harness.builtin_tool_names);
    const enabled = (tools: Record<string, boolean>) =>
      Object.entries(tools)
        .filter(([name, on]) => on && name !== "*")
        .map(([name]) => name)
        .sort();
    const defaults = harnessBuiltinTools(bedrock());
    expect(Object.keys(defaults).every((name) => name === "*" || names.has(name))).toBe(true);
    expect(enabled(defaults)).toEqual(["edit", "read", "shell", "subagent", "write"]);
    const restricted = harnessBuiltinTools(
      bedrock(new AgentPermissions({ tools: { bash: "deny", edit: "deny", task: "deny" } })),
    );
    expect(enabled(restricted)).toEqual(["read"]);
  });

  test("the harness keeps its state under the agent state dir", () => {
    const host = new StrandsRuntime({ harness: true }).hostConfig(bedrock(), {
      wiring,
      workdir: "/w",
    });
    const config = JSON.parse(DECODER.decode(host.files.files[0]?.data));
    expect(config.sessions_dir.startsWith("/home/user/.rayito/agent/strands/")).toBe(true);
    expect(config.memory_dir.startsWith("/home/user/.rayito/agent/strands/")).toBe(true);
    expect(config).not.toHaveProperty("interventions");
  });

  test("hermes listens on loopback and never carries its api key", () => {
    const runtime = new HermesRuntime();
    const host = runtime.hostConfig(bedrock(), { wiring, workdir: "/w" });
    const task = runtime.buildConfig(bedrock(), {
      gatewayUrls: { gw: "http://127.0.0.1:18001" },
      workdir: "/w",
    });
    const command = runtime.command({ spec: bedrock(), prompt: "secreto", workdir: "/w" });
    expect(host.envs.API_SERVER_HOST).toBeUndefined();
    expect(host.envs.HERMES_HOME?.startsWith("/home/user/.rayito/agent/hermes/")).toBe(true);
    const everything = JSON.stringify([
      host.envs,
      task.files.map((f) => DECODER.decode(f.data)),
      command.envs,
      command.script,
    ]);
    expect(everything).not.toContain("API_SERVER_KEY");
    expect(everything).not.toContain("secreto");
    expect(DECODER.decode(command.stdin)).toContain("secreto");
  });

  test("hermes drops the toolsets the permissions deny and defaults to terminal and files", () => {
    expect([...new HermesRuntime().toolsets]).toEqual([...HERMES_DEFAULT_TOOLSETS]);
    expect([...HERMES_DEFAULT_TOOLSETS]).toEqual(["terminal", "file"]);
    const spec = new AgentSpec({
      model: new AgentModel({ provider: "anthropic", id: "m", gateway: "gw" }),
      permissions: new AgentPermissions({ tools: { bash: "deny" } }),
    });
    const host = new HermesRuntime({ toolsets: ["terminal", "file", "web"] }).hostConfig(spec, {
      wiring,
      workdir: "/w",
    });
    expect(JSON.parse(DECODER.decode(host.files.files[0]?.data)).toolsets).toEqual(["file"]);
  });

  test("a custom app runs from the source directory with its venv first", () => {
    const runtime = new CustomRuntime({
      requirements: "requirements.txt",
      app: new HttpApp({ command: ["python", "-m", "agent.server"], port: 9100 }),
    });
    const host = runtime.hostConfig(bedrock(), { wiring: directWiring(), workdir: "/home/user" });
    expect(host.options.app_dir).toBe(AGENT_APP_DIR);
    expect(host.options.path_prepend).toEqual([runtime.venvBin()]);
    expect(runtime.venvPython()).toBe(`${runtime.venvBin()}/python`);
    expect(host.python).toBe(runtime.venvPython());
  });

  test("without requirements there is no venv on the path", () => {
    const runtime = new CustomRuntime({
      app: new HttpApp({ command: ["python", "serve.py"], port: 9100 }),
    });
    const host = runtime.hostConfig(bedrock(), { wiring: directWiring(), workdir: "/home/user" });
    expect(host.options.app_dir).toBe(AGENT_APP_DIR);
    expect(host.options).not.toHaveProperty("path_prepend");
  });

  test("a custom command gets the same environment options", () => {
    const runtime = new CustomRuntime({
      command: ["python", "agent.py"],
      requirements: "requirements.txt",
    });
    const host = runtime.hostConfig(bedrock(), { wiring: directWiring(), workdir: "/home/user" });
    expect(host.options.app_dir).toBe(AGENT_APP_DIR);
    expect(host.options.path_prepend).toEqual([runtime.venvBin()]);
  });

  test.each([
    [new HttpApp({ command: ["x"], port: 9100 }), false],
    [new HttpApp({ command: ["x"], port: 9100, healthPath: "/health" }), true],
    [new HttpApp({ command: ["x"], port: 9100, invokePath: "/invoke" }), true],
    [new HttpApp({ command: ["x"], port: 9100, healthPath: "/health", protocol: "raw" }), false],
    [new HttpApp({ command: ["x"], port: 9100, invokePath: "/v1", protocol: "openai-chat" }), true],
  ])("an app hides its own routes unless the protocol is raw %#", (app, hidden) => {
    expect(app.hidesOwnRoutes).toBe(hidden);
  });

  test("hermes rejects command patterns it cannot enforce", () => {
    const spec = new AgentSpec({
      model: new AgentModel({ provider: "anthropic", id: "m", gateway: "gw" }),
      permissions: new AgentPermissions({ tools: { bash: { "git *": "allow", "*": "deny" } } }),
    });
    expect(() => new HermesRuntime().hostConfig(spec, { wiring, workdir: "/w" })).toThrow(
      InvalidArgumentError,
    );
  });

  test("the prompt never goes in the script and session ids must have the SDK form", () => {
    for (const runtime of [
      new StrandsRuntime(),
      new HermesRuntime(),
      new CustomRuntime({ command: ["x"] }),
      new CustomRuntime({ entrypoint: "m:f" }),
    ]) {
      const command = runtime.command({ spec: bedrock(), prompt: "el prompt", workdir: "/w" });
      expect(command.script).not.toContain("el prompt");
      expect(JSON.parse(DECODER.decode(command.stdin)).prompt).toBe("el prompt");
    }
    expect(() =>
      new StrandsRuntime().command({
        spec: bedrock(),
        prompt: "x",
        workdir: "/w",
        sessionId: "otra-forma",
      }),
    ).toThrow(InvalidArgumentError);
  });

  test("without a gateway there is no placeholder key or endpoint override", () => {
    const host = new StrandsRuntime().hostConfig(bedrock(), {
      wiring: directWiring(),
      workdir: "/w",
    });
    expect(host.envs).not.toHaveProperty("AWS_BEARER_TOKEN_BEDROCK");
    expect(host.envs).not.toHaveProperty("AWS_ENDPOINT_URL_BEDROCK_RUNTIME");
    const config = JSON.parse(DECODER.decode(host.files.files[0]?.data));
    expect(config.base_url).toBeNull();
    expect(config.credential_placeholder).toBeNull();
  });

  test("the custom runtime sees the model only through the gateway", () => {
    const envs = new CustomRuntime({ command: ["x"] }).hostConfig(bedrock(), {
      wiring,
      workdir: "/w",
    }).envs;
    expect(envs.RAYITO_MODEL_API_KEY).toBe(MODEL_CREDENTIAL_PLACEHOLDER);
    expect(envs.RAYITO_MODEL_BASE_URL).toBe("http://127.0.0.1:18001");
    expect(envs).not.toHaveProperty("AWS_ACCESS_KEY_ID");
  });
});

describe("the task contract of AgentRuntime through the host", () => {
  test("a strands run decodes the recorded events into the SDK result", async () => {
    const expected = readJson("strands-expected-events.json").plain as Json[];
    const events = [
      { v: 1, type: "session", session_id: SESSION_ID },
      ...expected,
      { v: 1, type: "done", session_id: SESSION_ID },
    ];
    const handle = new FakeCommandHandle({
      lines: events.map((event) => new TextEncoder().encode(JSON.stringify(event))),
    });
    const sandbox = new FakeSandbox({
      commands: new FakeCommands({ handles: [handle] }),
      files: new FakeFilesystem(),
      gateways: { gw: gatewayStatus() },
    });
    const result = await new Agent(sandbox).run("suma 2 y 3", {
      spec: new AgentSpec({
        model: new AgentModel({ provider: "bedrock", id: "m", gateway: "gw", region: "us-east-1" }),
      }),
      runtime: new StrandsRuntime(),
    });
    expect(result.sessionId).toBe(SESSION_ID);
    expect(result.text.endsWith("El resultado es 5.")).toBe(true);
    expect(result.usage.input).toBe(300);
    expect(result.toolCalls.map((call) => call.name)).toEqual(["add"]);
    const written = (sandbox.files.writeFilesCalls[0] as { path: string }[]).map(
      (entry) => entry.path,
    );
    expect(written).toContain("/home/user/.rayito/agent/strands/wiring.env");
  });

  test.each([
    ["strands", () => new StrandsRuntime()],
    ["hermes", () => new HermesRuntime()],
    ["strands-ts", () => new StrandsRuntime({ language: "typescript", entrypoint: "a.ts" })],
  ])("an unsupported provider fails before any RPC: %s", async (_label, make) => {
    const sandbox = new FakeSandbox({
      commands: new FakeCommands({ handles: [] }),
      files: new FakeFilesystem(),
      gateways: { gw: gatewayStatus() },
    });
    const spec = new AgentSpec({
      model: new AgentModel({ provider: "azure", id: "gpt-4.1", gateway: "gw" }),
    });
    await expect(new Agent(sandbox).run("hola", { spec, runtime: make() })).rejects.toThrow(
      InvalidArgumentError,
    );
    expect(sandbox.commands.calls).toEqual([]);
    expect(sandbox.files.writeFilesCalls).toEqual([]);
  });

  test("the missing runtime line decodes as a failure", () => {
    const runtime = new StrandsRuntime();
    const state = runtime.newState();
    const events = runtime.parseLine(
      new TextEncoder().encode('{"v":1,"type":"rayito.runtime_missing"}'),
      state,
    );
    expect(events).toHaveLength(1);
    expect(runtime.finish(state, 0)).toMatchObject({ reason: "runtime_missing" });
  });
});
