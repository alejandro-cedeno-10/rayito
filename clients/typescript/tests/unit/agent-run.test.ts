/**
 * `sbx.agent.run/stream/prepare` (`ai-agent-core`, design.md §4, §7, §11):
 * aplica la configuración una sola vez por sha, trocea stdout en eventos,
 * impone `AgentLimits`, abre el span `rayito.agent.run` sólo con
 * `tracerProvider` y nunca llama a AWS ni a Connect (todo contra dobles de
 * `agent-fake.ts`). Espejo de `test_agent_run.py`/`test_agent_run_async.py`.
 */

import { describe, expect, test, vi } from "vitest";
import { AgentLimits, AgentModel, AgentSpec } from "../../src/agent/domain.js";
import { stopTreeCommand } from "../../src/agent/stream.js";
import { InvalidArgumentError, TimeoutError } from "../../src/errors.js";
import {
  ALLOWED_SPAN_ATTRIBUTES,
  instrumentationFor,
  type TracerProviderLike,
} from "../../src/otel.js";
import { Agent } from "../../src/sandbox/agent.js";
import {
  FAKE_PID,
  FAKE_SESSION_ID,
  FakeAgentRuntime,
  FakeCommandHandle,
  FakeCommands,
  FakeFilesystem,
  FakeSandbox,
  gatewayStatus,
  line,
} from "./agent-fake.js";

function spec(gateway = "bedrock"): AgentSpec {
  return new AgentSpec({
    model: new AgentModel({ provider: "bedrock", id: "model-x", gateway, region: "us-east-1" }),
  });
}

function sandboxWith(handle: FakeCommandHandle): FakeSandbox {
  return new FakeSandbox({
    commands: new FakeCommands({ handles: [handle] }),
    files: new FakeFilesystem(),
    gateways: { bedrock: gatewayStatus() },
  });
}

describe("sbx.agent.run", () => {
  test("devuelve el resultado de una ejecución correcta", async () => {
    const handle = new FakeCommandHandle({
      lines: [
        line({ event: "step_started", index: 1 }),
        line({ event: "text", text: "hola" }),
        line({ event: "step_finished", index: 1, usage: { input: 10, output: 5 } }),
        line({ event: "done", sessionId: FAKE_SESSION_ID, exitCode: 0, usage: {} }),
      ],
    });
    const sandbox = sandboxWith(handle);
    const agent = new Agent(sandbox);
    const result = await agent.run("hola agente", {
      spec: spec(),
      runtime: new FakeAgentRuntime(),
    });
    expect(result.sessionId).toBe(FAKE_SESSION_ID);
    expect(result.text).toBe("hola");
    expect(result.steps).toBe(1);
    expect(result.usage.input).toBe(10);
    expect(result.exitCode).toBe(0);
    expect(new TextDecoder().decode(handle.stdin)).toBe("hola agente");
    expect(handle.stdinClosed).toBe(true);
  });

  test("lanza AgentError cuando el último evento es agent_failed", async () => {
    const handle = new FakeCommandHandle({
      lines: [line({ event: "agent_failed", reason: "model_error", detailCode: "APIError" })],
    });
    const agent = new Agent(sandboxWith(handle));
    await expect(
      agent.run("hola", { spec: spec(), runtime: new FakeAgentRuntime() }),
    ).rejects.toMatchObject({ reason: "model_error", detailCode: "APIError" });
  });

  test("falla antes de cualquier RPC si falta una pasarela", async () => {
    const sandbox = new FakeSandbox({
      commands: new FakeCommands(),
      files: new FakeFilesystem(),
      gateways: {},
    });
    const agent = new Agent(sandbox);
    await expect(
      agent.run("hola", { spec: spec(), runtime: new FakeAgentRuntime() }),
    ).rejects.toBeInstanceOf(InvalidArgumentError);
    expect(sandbox.commands.calls).toEqual([]);
    expect(sandbox.files.writeFilesCalls).toEqual([]);
  });

  test("escribe la configuración una sola vez por sha", async () => {
    const runtime = new FakeAgentRuntime();
    const sandbox = new FakeSandbox({
      commands: new FakeCommands({
        handles: [
          new FakeCommandHandle({ lines: [line({ event: "done", exitCode: 0, usage: {} })] }),
          new FakeCommandHandle({ lines: [line({ event: "done", exitCode: 0, usage: {} })] }),
        ],
      }),
      files: new FakeFilesystem(),
      gateways: { bedrock: gatewayStatus() },
    });
    const agent = new Agent(sandbox);
    await agent.run("uno", { spec: spec(), runtime });
    await agent.run("dos", { spec: spec(), runtime });
    expect(sandbox.files.writeFilesCalls).toHaveLength(1);
  });

  test("reescribe la configuración cuando cambia el sha", async () => {
    const sandbox = new FakeSandbox({
      commands: new FakeCommands({
        handles: [
          new FakeCommandHandle({ lines: [line({ event: "done", exitCode: 0, usage: {} })] }),
          new FakeCommandHandle({ lines: [line({ event: "done", exitCode: 0, usage: {} })] }),
        ],
      }),
      files: new FakeFilesystem(),
      gateways: { bedrock: gatewayStatus() },
    });
    const agent = new Agent(sandbox);
    await agent.run("uno", {
      spec: spec(),
      runtime: new FakeAgentRuntime("fake", { configSha: "a" }),
    });
    await agent.run("dos", {
      spec: spec(),
      runtime: new FakeAgentRuntime("fake", { configSha: "b" }),
    });
    expect(sandbox.files.writeFilesCalls).toHaveLength(2);
  });

  test("maxSteps lo impone el SDK", async () => {
    const handle = new FakeCommandHandle({
      lines: [line({ event: "step_started", index: 1 }), line({ event: "step_started", index: 2 })],
    });
    const agent = new Agent(sandboxWith(handle));
    await expect(
      agent.run("hola", {
        spec: spec(),
        runtime: new FakeAgentRuntime(),
        limits: new AgentLimits({ maxSteps: 1 }),
      }),
    ).rejects.toMatchObject({ reason: "max_steps" });
  });

  test("el presupuesto de tokens se comprueba tras un step_finished", async () => {
    const handle = new FakeCommandHandle({
      lines: [
        line({ event: "step_started", index: 1 }),
        line({ event: "step_finished", index: 1, usage: { input: 100, output: 50 } }),
      ],
    });
    const agent = new Agent(sandboxWith(handle));
    await expect(
      agent.run("hola", {
        spec: spec(),
        runtime: new FakeAgentRuntime(),
        limits: new AgentLimits({ maxTotalTokens: 10 }),
      }),
    ).rejects.toMatchObject({ reason: "token_budget" });
  });

  test("un timeout durante el stream se convierte en AgentError", async () => {
    const handle = new FakeCommandHandle({
      lines: [line({ event: "step_started", index: 1 })],
      raiseOnIterate: new TimeoutError("venció"),
    });
    const agent = new Agent(sandboxWith(handle));
    await expect(
      agent.run("hola", { spec: spec(), runtime: new FakeAgentRuntime() }),
    ).rejects.toMatchObject({ reason: "timeout" });
  });

  test("stream() nunca lanza por un fallo del agente", async () => {
    const handle = new FakeCommandHandle({
      lines: [line({ event: "agent_failed", reason: "busy" })],
    });
    const agent = new Agent(sandboxWith(handle));
    const stream = await agent.stream("hola", { spec: spec(), runtime: new FakeAgentRuntime() });
    const events = [];
    for await (const event of stream) {
      events.push(event);
    }
    await stream.close();
    const last = events.at(-1);
    expect(last?.type).toBe("agent_failed");
    expect((last as { reason: string }).reason).toBe("busy");
  });

  test("abort() corre abortCommand y luego mata el handle", async () => {
    const handle = new FakeCommandHandle({ lines: [line({ event: "step_started", index: 1 })] });
    const sandbox = new FakeSandbox({
      commands: new FakeCommands({ handles: [handle], foregroundResults: [undefined, undefined] }),
      files: new FakeFilesystem(),
      gateways: { bedrock: gatewayStatus() },
    });
    const agent = new Agent(sandbox);
    const runtime = new FakeAgentRuntime("fake", {
      abortCmd: "curl -X POST http://127.0.0.1:4096/session/x/abort",
    });
    const stream = await agent.stream("hola", { spec: spec(), runtime });
    await stream.abort();
    expect(handle.killed).toBe(true);
    expect(sandbox.commands.calls.slice(1).map((call) => call.cmd)).toEqual([
      "curl -X POST http://127.0.0.1:4096/session/x/abort",
      stopTreeCommand(FAKE_PID),
    ]);
    await expect(stream.result()).rejects.toMatchObject({ reason: "aborted" });
  });

  test("un AbortSignal aborta el stream", async () => {
    const handle = new FakeCommandHandle({
      lines: Array.from({ length: 20 }, () => line({ event: "step_started", index: 1 })),
    });
    const agent = new Agent(sandboxWith(handle));
    const controller = new AbortController();
    const stream = await agent.stream("hola", {
      spec: spec(),
      runtime: new FakeAgentRuntime(),
      signal: controller.signal,
    });
    controller.abort();
    await vi.waitFor(() => expect(handle.killed).toBe(true));
    await stream.close();
  });

  test("prepare() dispara los pasos de calentamiento en segundo plano", async () => {
    const handleA = new FakeCommandHandle({ lines: [] });
    const handleB = new FakeCommandHandle({ lines: [] });
    const sandbox = new FakeSandbox({
      commands: new FakeCommands({ handles: [handleA, handleB] }),
      files: new FakeFilesystem(),
    });
    const agent = new Agent(sandbox);
    const runtime = new FakeAgentRuntime("fake", {
      warmup: [{ cmd: "echo a", background: true }, { cmd: "echo b" }],
    });
    await agent.prepare({ runtime });
    expect(sandbox.commands.calls.map((call) => call.cmd)).toEqual(["echo a", "echo b"]);
    expect(handleA.disconnected).toBe(true);
    expect(handleB.disconnected).toBe(true);
  });

  test("tocar sbx.agent no hace ninguna llamada", () => {
    const sandbox = new FakeSandbox({ commands: new FakeCommands(), files: new FakeFilesystem() });
    const agent = new Agent(sandbox);
    expect(agent).toBeInstanceOf(Agent);
    expect(sandbox.commands.calls).toEqual([]);
    expect(sandbox.files.writeFilesCalls).toEqual([]);
  });

  test("sin tracerProvider no se crea ningún span", async () => {
    const handle = new FakeCommandHandle({
      lines: [line({ event: "done", exitCode: 0, usage: {} })],
    });
    const sandbox = sandboxWith(handle);
    const agent = new Agent(sandbox);
    const result = await agent.run("hola", { spec: spec(), runtime: new FakeAgentRuntime() });
    expect(result.exitCode).toBe(0);
  });

  test("los atributos del span son un subconjunto de la lista permitida", async () => {
    const handle = new FakeCommandHandle({
      lines: [
        line({ event: "step_started", index: 1 }),
        line({ event: "step_finished", index: 1, usage: { input: 1, output: 1 } }),
        line({ event: "done", sessionId: FAKE_SESSION_ID, exitCode: 0, usage: {} }),
      ],
    });
    const sandbox = sandboxWith(handle);
    const spans: [string, Record<string, unknown>][] = [];
    const provider = {
      getTracer: () => ({
        startActiveSpan: (
          name: string,
          options: { attributes: Record<string, unknown> },
          fn: (span: unknown) => unknown,
        ) => {
          spans.push([name, options.attributes]);
          return fn({
            setAttribute: () => undefined,
            recordException: () => undefined,
            setStatus: () => undefined,
            end: () => undefined,
          });
        },
      }),
    };
    sandbox.setInstrumentation(instrumentationFor(provider as unknown as TracerProviderLike));
    const agent = new Agent(sandbox);
    const result = await agent.run("hola", { spec: spec(), runtime: new FakeAgentRuntime() });
    expect(result.exitCode).toBe(0);
    expect(spans).toHaveLength(1);
    const [name, attributes] = spans[0] as [string, Record<string, unknown>];
    expect(name).toBe("rayito.agent.run");
    for (const key of Object.keys(attributes)) {
      expect(ALLOWED_SPAN_ATTRIBUTES).toContain(key);
    }
    expect(attributes["gen_ai.operation.name"]).toBe("invoke_agent");
    expect(attributes["gen_ai.provider.name"]).toBe("aws.bedrock");
  });

  test.each([
    {
      limits: new AgentLimits({ maxSteps: 1 }),
      lines: [line({ event: "step_started", index: 1 }), line({ event: "step_started", index: 2 })],
      reason: "max_steps",
    },
    {
      limits: new AgentLimits({ maxTotalTokens: 10 }),
      lines: [
        line({ event: "step_started", index: 1 }),
        line({ event: "step_finished", index: 1, usage: { input: 100, output: 50 } }),
      ],
      reason: "token_budget",
    },
  ])(
    "un límite del SDK ($reason) para el runtime y su árbol de procesos",
    async ({ limits, lines, reason }) => {
      const handle = new FakeCommandHandle({
        lines: [...lines, line({ event: "step_started", index: 3 })],
      });
      const sandbox = sandboxWith(handle);
      const agent = new Agent(sandbox);
      const stream = await agent.stream("hola", {
        spec: spec(),
        runtime: new FakeAgentRuntime(),
        limits,
      });
      const events = [];
      for await (const event of stream) {
        events.push(event);
      }
      expect(events.at(-1)).toMatchObject({ type: "agent_failed", reason });
      expect(handle.killed).toBe(true);
      expect(sandbox.commands.calls.slice(1).map((call) => call.cmd)).toEqual([
        stopTreeCommand(FAKE_PID),
      ]);
    },
  );

  test("un timeout que sólo dice el EndEvent se convierte en timeout", async () => {
    const handle = new FakeCommandHandle({
      lines: [line({ event: "step_started", index: 1 })],
      exitCode: -1,
      raiseOnWait: new TimeoutError("venció"),
    });
    const agent = new Agent(sandboxWith(handle));
    await expect(
      agent.run("hola", { spec: spec(), runtime: new FakeAgentRuntime() }),
    ).rejects.toMatchObject({ reason: "timeout" });
  });

  test("stopTreeCommand rechaza pids que nunca debe señalar", () => {
    for (const pid of [0, 1, -5, 1.5]) {
      expect(() => stopTreeCommand(pid)).toThrow(InvalidArgumentError);
    }
  });
});
