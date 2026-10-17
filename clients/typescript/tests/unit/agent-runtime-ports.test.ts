/**
 * Los puertos opcionales `InstallableRuntime` y `ServableRuntime` (ADR-028):
 * quién los implementa, qué entrega cada adaptador y que el contrato de
 * `AgentRuntime` no se mueve. Espejo de `test_agent_runtime_ports.py`; los
 * vectores de configuración y comandos de OpenCode y deepagents siguen siendo
 * los de siempre.
 */

import { describe, expect, test } from "vitest";
import { DEEPAGENTS_CONFIG_PATH, DEEPAGENTS_PYTHON } from "../../src/agent/deepagents.js";
import { HermesRuntime } from "../../src/agent/hermes.js";
import { OPENCODE_CONFIG_PATH, OpenCodeRuntime } from "../../src/agent/opencode.js";
import {
  HOST_DRIVERS,
  hostConfig,
  requireInstallable,
  requireServable,
  STEP_COPY,
} from "../../src/agent/runtime.js";
import { AGENT_RUNTIMES, resolveRuntime, runtimeToOptions } from "../../src/agent/runtimes.js";
import { StrandsRuntime } from "../../src/agent/strands.js";
import {
  AgentModel,
  AgentSpec,
  DeepAgents,
  directWiring,
  gatewayWiring,
  InvalidArgumentError,
  installTarget,
  isInstallable,
  isServable,
  McpRemote,
  UnimplementedError,
} from "../../src/index.js";
import { MODEL_CREDENTIAL_PLACEHOLDER } from "../../src/limits.js";

const URLS = { bedrock: "http://127.0.0.1:18001", anthropic: "http://127.0.0.1:18002" };

function bedrock(): AgentSpec {
  return new AgentSpec({
    model: new AgentModel({
      provider: "bedrock",
      id: "us.anthropic.claude-haiku-4-5-20251001-v1:0",
      gateway: "bedrock",
      region: "us-east-1",
    }),
  });
}

function anthropic(): AgentSpec {
  return new AgentSpec({
    model: new AgentModel({ provider: "anthropic", id: "claude-haiku-4-5", gateway: "anthropic" }),
  });
}

const taskOnly = {
  name: "task-only",
  buildConfig: () => ({ files: [], configSha256: "0" }),
  command: () => ({ script: "", stdin: new Uint8Array() }),
  newState: () => ({}),
  parseLine: () => [],
  finish: () => ({ type: "done" }),
  templateSteps: () => [],
  warmupSteps: () => [],
};

describe("runtime ports", () => {
  test.each([
    ["opencode", new OpenCodeRuntime()],
    ["deepagents", new DeepAgents()],
    ["deepagents with entrypoint", new DeepAgents({ entrypoint: "pkg.mod:build" })],
  ])("%s implements both ports", (_name, runtime) => {
    expect(isInstallable(runtime)).toBe(true);
    expect(isServable(runtime)).toBe(true);
  });

  test("a task-only runtime still resolves but has neither port", () => {
    expect(resolveRuntime(taskOnly as never)).toBe(taskOnly);
    expect(isInstallable(taskOnly)).toBe(false);
    expect(isServable(taskOnly)).toBe(false);
    expect(() => requireInstallable(taskOnly)).toThrow(/InstallableRuntime/);
    expect(() => requireServable(taskOnly)).toThrow(/ServableRuntime/);
    expect(() => requireInstallable(taskOnly)).toThrow(InvalidArgumentError);
  });

  test("registry names", () => {
    expect(AGENT_RUNTIMES.opencode).toBeInstanceOf(OpenCodeRuntime);
    const langgraph = resolveRuntime("langgraph");
    expect(langgraph).toBeInstanceOf(DeepAgents);
    expect((langgraph as DeepAgents).requireEntrypoint).toBe(true);
    expect((langgraph as DeepAgents).entrypoint).toBeUndefined();
    expect(resolveRuntime("deepagents")).not.toBe(langgraph);
  });

  test.each(["crewai", "toString"])("%s is unimplemented, not an opaque error", (name) => {
    expect(() => resolveRuntime(name)).toThrow(UnimplementedError);
    expect(() => resolveRuntime(name)).toThrow(new RegExp(`runtime: "${name}"`));
  });

  test("the framework runtimes are registered", () => {
    expect(resolveRuntime("strands")).toBeInstanceOf(StrandsRuntime);
    expect(resolveRuntime("strands-harness")).toMatchObject({ harness: true });
    expect(resolveRuntime("hermes")).toBeInstanceOf(HermesRuntime);
  });

  test("custom is not a registry name", () => {
    expect(() => resolveRuntime("custom")).toThrow(/CustomRuntime/);
    expect(() => resolveRuntime("custom")).toThrow(InvalidArgumentError);
  });

  test("langgraph needs an entrypoint to build anything", () => {
    const runtime = resolveRuntime("langgraph") as DeepAgents;
    expect(() =>
      runtime.buildConfig(anthropic(), { gatewayUrls: URLS, workdir: "/home/user" }),
    ).toThrow(/entrypoint/);
    expect(() =>
      runtime.hostConfig(anthropic(), { wiring: gatewayWiring(URLS), workdir: "/home/user" }),
    ).toThrow(/entrypoint/);
  });

  test("runtimeToOptions names the registry kind", () => {
    expect(runtimeToOptions("opencode")).toEqual({ kind: "opencode" });
    expect(runtimeToOptions(new OpenCodeRuntime())).toEqual({ kind: "opencode" });
    expect(runtimeToOptions(new DeepAgents())).toEqual({ kind: "deepagents" });
    expect(runtimeToOptions(new DeepAgents({ entrypoint: "pkg.mod:build" }))).toEqual({
      kind: "langgraph",
      entrypoint: "pkg.mod:build",
    });
    expect(() => runtimeToOptions(taskOnly as never)).toThrow(InvalidArgumentError);
  });

  test("install target is validated", () => {
    expect(installTarget().platform).toBe("microvm");
    expect(() => installTarget({ platform: "vm" as never })).toThrow(InvalidArgumentError);
    expect(() => installTarget({ installDir: "opt/agents" })).toThrow(InvalidArgumentError);
  });

  test("host config driver is a closed list", () => {
    const files = { files: [], configSha256: "0" };
    expect(HOST_DRIVERS).toContain("strands");
    expect(() => hostConfig({ driver: "bash" as never, python: "python3", files })).toThrow(
      InvalidArgumentError,
    );
  });

  test("opencode host config matches its run command", () => {
    const runtime = new OpenCodeRuntime();
    const spec = bedrock();
    const host = runtime.hostConfig(spec, {
      wiring: gatewayWiring(URLS),
      workdir: "/home/user",
    });
    const config = runtime.buildConfig(spec, { gatewayUrls: URLS, workdir: "/home/user" });
    const command = runtime.command({ spec, prompt: "hola", workdir: "/home/user" });
    expect(host.driver).toBe("opencode");
    expect(host.files).toEqual(config);
    expect(host.envs).toEqual(command.envs);
    expect(host.files.files[0]?.path).toBe(OPENCODE_CONFIG_PATH);
    expect(host.options).toEqual({ workdir: "/home/user" });
  });

  test("deepagents host config adds the standard provider variables", () => {
    const runtime = new DeepAgents();
    const spec = bedrock();
    const host = runtime.hostConfig(spec, {
      wiring: gatewayWiring(URLS),
      workdir: "/home/user",
    });
    const config = runtime.buildConfig(spec, { gatewayUrls: URLS, workdir: "/home/user" });
    const command = runtime.command({ spec, prompt: "hola", workdir: "/home/user" });
    expect(host.driver).toBe("deepagents");
    expect(host.python).toBe(DEEPAGENTS_PYTHON);
    expect(host.files).toEqual(config);
    expect(host.envs).toEqual({
      ...command.envs,
      AWS_ENDPOINT_URL_BEDROCK_RUNTIME: URLS.bedrock,
    });
    expect(host.options).toEqual({ config_path: DEEPAGENTS_CONFIG_PATH });
  });

  test("direct wiring drops the gateway and the placeholder", () => {
    const host = new DeepAgents().hostConfig(bedrock(), {
      wiring: directWiring(),
      workdir: "/home/user",
    });
    const config = new TextDecoder().decode(host.files.files[0]?.data);
    expect(config).toContain('"base_url": null');
    expect(config).toContain('"credential_placeholder": null');
    expect(host.envs.AWS_BEARER_TOKEN_BEDROCK).toBeUndefined();
    expect(host.envs.AWS_REGION).toBe("us-east-1");
    const opencode = new OpenCodeRuntime().hostConfig(bedrock(), {
      wiring: directWiring(),
      workdir: "/w",
    });
    const text = new TextDecoder().decode(opencode.files.files[0]?.data);
    expect(text).not.toContain("endpoint");
    expect(text).not.toContain(MODEL_CREDENTIAL_PLACEHOLDER);
    expect(opencode.envs.AWS_BEARER_TOKEN_BEDROCK).toBeUndefined();
  });

  test("direct wiring rejects what needs a gateway", () => {
    const compatible = new AgentSpec({
      model: new AgentModel({
        provider: "openai-compatible",
        id: "m",
        gateway: "x",
        basePath: "/v1",
      }),
    });
    for (const runtime of [new OpenCodeRuntime(), new DeepAgents()]) {
      expect(() =>
        runtime.hostConfig(compatible, { wiring: directWiring(), workdir: "/w" }),
      ).toThrow(/endpoint estándar/);
    }
    const remote = new AgentSpec({
      model: anthropic().model,
      mcp: { docs: new McpRemote({ gateway: "docs", path: "/mcp" }) },
    });
    expect(() =>
      new OpenCodeRuntime().hostConfig(remote, { wiring: directWiring(), workdir: "/w" }),
    ).toThrow(/McpRemote/);
  });

  test("wiring values are immutable", () => {
    const wiring = gatewayWiring({ a: "http://127.0.0.1:1" });
    expect(Object.isFrozen(wiring)).toBe(true);
    expect(Object.isFrozen(wiring.urls)).toBe(true);
  });

  test("install steps follow the install dir", () => {
    const target = installTarget({ installDir: "/srv/agents" });
    const steps = new OpenCodeRuntime().installSteps(target);
    expect(String(steps[0]?.args?.cmd)).toContain("/srv/agents/bin");
    const deep = new DeepAgents().installSteps(target);
    expect(deep[0]?.op).toBe(STEP_COPY);
    expect(deep[0]?.args?.dst).toBe("/srv/agents/requirements-deepagents.txt");
    expect(deep[2]?.args?.executable).toBe(true);
  });
});
