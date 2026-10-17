/**
 * `AgentDeployment` (`agent/deployment.ts`) sobre un `fetch` de doble: la
 * invocación JSON y el stream SSE, los códigos del contrato, el reacuñado del
 * token del proxy ante un `403`, las apps `raw` y `connect()` desde las
 * etiquetas del sandbox. Espejo de `test_agent_deployment.py`.
 */

import { describe, expect, test, vi } from "vitest";
import {
  AGENT_DEPLOYMENT_LABEL,
  AGENT_DEPLOYMENT_PORT_LABEL,
  AGENT_DEPLOYMENT_PROTOCOL_LABEL,
  AgentDeployment,
  labelsFor,
} from "../../src/agent/deployment.js";
import type { AgentEvent } from "../../src/agent/events.js";
import {
  AgentError,
  AuthenticationError,
  InvalidArgumentError,
  SandboxError,
  TimeoutError,
  UnimplementedError,
} from "../../src/errors.js";
import { AGENT_SERVICE_PORT } from "../../src/limits.js";
import {
  ENDPOINT,
  FakeSandbox,
  RESULT_BODY,
  SANDBOX_ID,
  type Scripted,
  scriptedFetch,
  sse,
} from "./agent-project-fake.js";

const sandboxConnect = vi.hoisted(() => vi.fn());

vi.mock("../../src/sandbox/sandbox.js", () => ({ Sandbox: { connect: sandboxConnect } }));

const PROMPT = "prompt-secreto-del-usuario";
const SESSION = "ses_0000000000000000000000000a";

async function make(...responses: (Scripted | Error)[]): Promise<{
  deployment: AgentDeployment;
  sandbox: FakeSandbox;
  transport: ReturnType<typeof scriptedFetch>;
}>;
async function make(
  options: { raw: true },
  ...responses: (Scripted | Error)[]
): Promise<{
  deployment: AgentDeployment;
  sandbox: FakeSandbox;
  transport: ReturnType<typeof scriptedFetch>;
}>;
async function make(...args: unknown[]) {
  const raw = typeof args[0] === "object" && args[0] !== null && "raw" in args[0];
  const responses = (raw ? args.slice(1) : args) as (Scripted | Error)[];
  const sandbox = new FakeSandbox();
  const transport = scriptedFetch(...responses);
  const deployment = new AgentDeployment({
    sandbox,
    port: AGENT_SERVICE_PORT,
    hostAccess: await sandbox.getHost(AGENT_SERVICE_PORT),
    project: "support-bot",
    raw,
    requestTimeoutMs: 7000,
    fetch: transport.fetch,
  });
  return { deployment, sandbox, transport };
}

const json = (payload: unknown, status = 200): Scripted => ({
  status,
  body: JSON.stringify(payload),
});

async function drain(stream: AsyncIterable<AgentEvent>): Promise<AgentEvent[]> {
  const events: AgentEvent[] = [];
  for await (const event of stream) {
    events.push(event);
  }
  return events;
}

describe("url and headers", () => {
  test("are fresh proxy access", async () => {
    const { deployment, sandbox } = await make();
    expect(deployment.url).toBe(`https://${ENDPOINT}`);
    expect(deployment.sandboxId).toBe(SANDBOX_ID);
    expect(deployment.headers["x-aws-proxy-port"]).toBe(String(AGENT_SERVICE_PORT));
    const before = deployment.headers["x-aws-proxy-auth"];
    sandbox.token.generation += 1;
    expect(deployment.headers["x-aws-proxy-auth"]).not.toBe(before);
  });
});

describe("invoke", () => {
  test("posts to /invocations and returns the result", async () => {
    const { deployment, transport } = await make(json(RESULT_BODY));
    const result = await deployment.invoke(PROMPT, {
      sessionId: SESSION,
      model: "m",
      reasoning: true,
    });
    expect(result).toMatchObject({ sessionId: SESSION, text: "hola mundo", steps: 2, exitCode: 0 });
    expect(result.usage.total).toBe(15);
    const [sent] = transport.requests;
    expect(sent?.method).toBe("POST");
    expect(sent?.url).toBe(`https://${ENDPOINT}/invocations`);
    expect(sent?.headers["x-aws-proxy-auth"]).toBe("jwe-1");
    expect(sent?.headers.Accept).toBe("application/json");
    expect(sent?.body).toContain('"prompt":"prompt-secreto-del-usuario"');
    expect(sent?.body).toContain(`"session_id":"${SESSION}"`);
    expect(sent?.body).toContain('"model":"m"');
    expect(sent?.body).toContain('"reasoning":true');
  });

  test("sends only what was asked", async () => {
    const { deployment, transport } = await make(json(RESULT_BODY));
    await deployment.invoke("hola");
    expect(transport.requests[0]?.body).toBe('{"prompt":"hola"}');
  });

  test("a failed result is an AgentError", async () => {
    const { deployment } = await make(
      json({ ...RESULT_BODY, text: "", failed: { reason: "max_steps", detail_code: null } }),
    );
    const error = await deployment.invoke("hola").catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(AgentError);
    expect(error).toMatchObject({ reason: "max_steps", sessionId: SESSION });
    expect((error as AgentError).usage?.total).toBe(15);
  });

  test.each([
    [429, AgentError, "busy"],
    [503, TimeoutError, undefined],
    [400, InvalidArgumentError, undefined],
    [413, InvalidArgumentError, undefined],
    [401, AuthenticationError, undefined],
    [500, SandboxError, undefined],
  ] as const)("status %i maps to its error", async (status, errorClass, reason) => {
    const { deployment } = await make({ status, body: "cuerpo del servidor" });
    const error = await deployment.invoke(PROMPT).catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(errorClass);
    expect((error as { reason?: string }).reason).toBe(reason);
    const message = (error as Error).message;
    expect(message).not.toContain(PROMPT);
    expect(message).not.toContain(ENDPOINT);
    expect(message).not.toContain("cuerpo del servidor");
  });

  test("a body outside the contract is a sandbox error", async () => {
    const { deployment } = await make({ body: "<html>" });
    await expect(deployment.invoke("hola")).rejects.toBeInstanceOf(SandboxError);
  });

  test("a network failure is a sandbox error without the cause text", async () => {
    const { deployment } = await make(new Error(`fallo con ${ENDPOINT}`));
    const error = await deployment.invoke("hola").catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(SandboxError);
    expect((error as Error).message).not.toContain(ENDPOINT);
  });

  test("an empty prompt is rejected before any request", async () => {
    const { deployment, transport } = await make();
    await expect(deployment.invoke("")).rejects.toBeInstanceOf(InvalidArgumentError);
    expect(transport.requests).toEqual([]);
  });
});

describe("403 from the AWS proxy", () => {
  test("is reminted once and then succeeds", async () => {
    const { deployment, sandbox, transport } = await make({ status: 403 }, json(RESULT_BODY));
    expect((await deployment.invoke("hola")).text).toBe("hola mundo");
    const [first, second] = transport.requests;
    expect([first?.headers["x-aws-proxy-auth"], second?.headers["x-aws-proxy-auth"]]).toEqual([
      "jwe-1",
      "jwe-2",
    ]);
    expect(sandbox.refreshed).toBe(1);
  });

  test("a second 403 is an authentication error", async () => {
    const { deployment, sandbox, transport } = await make({ status: 403 }, { status: 403 });
    const error = await deployment.invoke("hola").catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(AuthenticationError);
    expect((error as AuthenticationError).proxyRejected).toBe(true);
    expect(sandbox.refreshed).toBe(1);
    expect(transport.requests).toHaveLength(2);
  });
});

describe("stream", () => {
  test("decodes events and ends in done", async () => {
    const body = sse(
      ["session", { session_id: SESSION }],
      ["step_started", { index: 1 }],
      ["text_delta", { text: "ho" }],
      ["text", { text: "hola" }],
      ["step_finished", { index: 1, usage: { input: 4, output: 1 }, finish_reason: "stop" }],
      ["done", { session_id: SESSION }],
    );
    const { deployment, transport } = await make({ chunks: [body.slice(0, 30), body.slice(30)] });
    const stream = deployment.stream(PROMPT);
    const events = await drain(stream);
    expect(events.map((event) => event.type)).toEqual([
      "step_started",
      "text_delta",
      "text",
      "step_finished",
      "done",
    ]);
    const final = events.at(-1);
    expect(final).toMatchObject({ type: "done", sessionId: SESSION });
    expect((final as { usage: { input: number } }).usage.input).toBe(4);
    expect(stream.sessionId).toBe(SESSION);
    expect(transport.requests[0]?.headers.Accept).toBe("text/event-stream");
  });

  test("result() collects the text and steps", async () => {
    const body = sse(
      ["step_started", { index: 1 }],
      ["text", { text: "listo" }],
      ["step_finished", { index: 1, usage: { input: 2 } }],
      ["done", { session_id: SESSION }],
    );
    const { deployment } = await make({ body });
    const result = await deployment.stream("hola").result();
    expect([result.text, result.steps, result.sessionId]).toEqual(["listo", 1, SESSION]);
  });

  test("keeps the limit reasons of the service", async () => {
    const body = sse(
      ["step_started", { index: 1 }],
      ["agent_failed", { reason: "timeout", detail_code: null, session_id: SESSION }],
    );
    const { deployment } = await make({ body }, { body });
    const events = await drain(deployment.stream("hola"));
    expect(events.at(-1)).toMatchObject({
      type: "agent_failed",
      reason: "timeout",
      sessionId: SESSION,
    });
    await expect(deployment.stream("hola").result()).rejects.toMatchObject({ reason: "timeout" });
  });

  test("without a terminal event it is a protocol error", async () => {
    const { deployment } = await make({ body: sse(["step_started", { index: 1 }]) });
    await expect(deployment.stream("hola").result()).rejects.toMatchObject({
      reason: "protocol_error",
    });
  });

  test("an http error raises when iteration starts", async () => {
    const { deployment } = await make({ status: 429 });
    await expect(drain(deployment.stream("hola"))).rejects.toMatchObject({ reason: "busy" });
  });
});

describe("raw apps", () => {
  test("cannot be invoked or streamed", async () => {
    const { deployment, transport } = await make({ raw: true });
    await expect(deployment.invoke("hola")).rejects.toBeInstanceOf(UnimplementedError);
    expect(() => deployment.stream("hola")).toThrow(UnimplementedError);
    expect(transport.requests).toEqual([]);
    expect(deployment.url.startsWith("https://")).toBe(true);
  });
});

describe("status", () => {
  test.each([
    [{ body: '{"status":"Healthy","time_of_last_update":1}' }, "healthy", true, false],
    [{ body: '{"status":"HealthyBusy"}' }, "busy", true, true],
    [{ status: 503, body: "{}" }, "unavailable", false, false],
    [{ body: "no es json" }, "unavailable", false, false],
    [new SandboxError("sin conexión"), "unavailable", false, false],
  ] as const)("reads ping %#", async (scripted, state, ready, busy) => {
    const { deployment, transport } = await make(scripted as Scripted | Error);
    const status = await deployment.status();
    expect([status.state, status.ready, status.busy]).toEqual([state, ready, busy]);
    expect([status.port, status.project]).toEqual([AGENT_SERVICE_PORT, "support-bot"]);
    expect(transport.requests[0]?.url).toBe(`https://${ENDPOINT}/ping`);
  });
});

describe("access token, pause and resume", () => {
  test("exposes the access token of the sandbox to reconnect", async () => {
    const { deployment, sandbox } = await make();
    expect(deployment.accessToken).toBe(sandbox.accessToken);
  });

  test("pause and resume map to the sandbox", async () => {
    const { deployment, sandbox } = await make();
    expect(await deployment.pause()).toBe(true);
    await deployment.pause({ wait: false });
    await deployment.resume();
    await deployment.resume({ wait: false });
    expect(sandbox.pauseCalls).toEqual([{}, { wait: false }]);
    expect(sandbox.resumeCalls).toEqual([{}, { wait: false }]);
    expect(sandbox.kills).toBe(0);
  });
});

describe("usage reported", () => {
  const Zero = { input: 0, output: 0 };

  test("the result says when the agent reported no usage", async () => {
    const { deployment } = await make(json({ ...RESULT_BODY, usage: Zero, usage_reported: false }));
    const result = await deployment.invoke(PROMPT);
    expect(result.usageReported).toBe(false);
    expect(result.usage.total).toBe(0);
  });

  test("an older service without the field reports usage", async () => {
    const { deployment } = await make(json(RESULT_BODY));
    expect((await deployment.invoke(PROMPT)).usageReported).toBe(true);
  });

  test("a stream without usage in its steps is unreported", async () => {
    const unreported = sse(
      ["step_started", { index: 1 }],
      ["text", { text: "listo" }],
      ["step_finished", { index: 1, usage: null }],
      ["done", { session_id: SESSION }],
    );
    const reported = sse(
      ["step_started", { index: 1 }],
      ["step_finished", { index: 1, usage: { input: 3 } }],
      ["done", { session_id: SESSION }],
    );
    const first = await make({ body: unreported });
    expect((await first.deployment.stream("hola").result()).usageReported).toBe(false);
    const second = await make({ body: reported });
    expect((await second.deployment.stream("hola").result()).usageReported).toBe(true);
  });
});

describe("stop", () => {
  test("kills the sandbox", async () => {
    const { deployment, sandbox } = await make();
    await deployment.stop();
    expect(sandbox.kills).toBe(1);
  });
});

describe("labels and connect", () => {
  test("labels round trip", () => {
    expect(labelsFor("support-bot", 8088, { raw: false })).toEqual({
      [AGENT_DEPLOYMENT_LABEL]: "support-bot",
      [AGENT_DEPLOYMENT_PORT_LABEL]: "8088",
    });
    expect(labelsFor("x", 9, { raw: true })[AGENT_DEPLOYMENT_PROTOCOL_LABEL]).toBe("raw");
  });

  test("connect rebuilds the client from the labels", async () => {
    const sandbox = new FakeSandbox({ metadata: labelsFor("support-bot", 9100, { raw: false }) });
    sandboxConnect.mockResolvedValueOnce(sandbox);
    const deployment = await AgentDeployment.connect(SANDBOX_ID, { region: "us-east-1" });
    expect(sandboxConnect).toHaveBeenCalledWith(SANDBOX_ID, { region: "us-east-1" });
    expect(deployment.port).toBe(9100);
    expect(deployment.headers["x-aws-proxy-port"]).toBe("9100");
  });

  test.each([{}, { [AGENT_DEPLOYMENT_LABEL]: "x" }, { otra: "cosa" }])(
    "connect rejects a sandbox that is not a deployment %#",
    async (metadata) => {
      sandboxConnect.mockResolvedValueOnce(new FakeSandbox({ metadata }));
      await expect(AgentDeployment.connect(SANDBOX_ID)).rejects.toBeInstanceOf(
        InvalidArgumentError,
      );
    },
  );
});
