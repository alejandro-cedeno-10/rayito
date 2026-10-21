/**
 * Entrada estructurada en el SDK (ADR-033 D3): el cuerpo de
 * `POST /invocations` con `prompt` o `input`
 * (`testdata/agent-deploy/invocation-bodies.json`, compartido con Python), la
 * lectura de `output`, los mensajes por `detailCode`, el aviso de un host
 * anterior y `invoke({ input })` / `stream({ input })` en despliegues, flotas y
 * AgentCore. Espejo de `test_agent_structured_input.py`.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, test } from "vitest";
import {
  AgentDeployment,
  EventDecoder,
  HOST_TOO_OLD_MESSAGE,
  httpError,
  INPUT_NOT_OBJECT_MESSAGE,
  INPUT_TOO_LARGE_MESSAGE,
  invocationBody,
  PROMPT_OR_INPUT_MESSAGE,
  ResultBuilder,
  resultFromBody,
} from "../../src/agent/deployment.js";
import { AGENT_DETAIL_MESSAGES, type AgentEvent, failureMessage } from "../../src/agent/events.js";
import { connectWith } from "../../src/agentcore/fleet.js";
import { AgentError, InvalidArgumentError } from "../../src/errors.js";
import {
  AGENT_SERVICE_MAX_OUTPUT_BYTES,
  AGENT_SERVICE_MAX_REQUEST_BYTES,
} from "../../src/limits.js";
import { FleetHarness } from "./agent-fleet-fake.js";
import { FakeSandbox, type Scripted, scriptedFetch, sse } from "./agent-project-fake.js";
import { AGENT, desiredDetails, newAccount, REGION } from "./agentcore-fakes.js";

type Json = Record<string, unknown>;

const TESTDATA = join(import.meta.dirname, "..", "..", "..", "..", "testdata");
const VECTORS = JSON.parse(
  readFileSync(join(TESTDATA, "agent-deploy", "invocation-bodies.json"), "utf8"),
) as {
  readonly maxRequestBytesLimit: string;
  readonly messages: Record<string, string>;
  readonly detailCodes: Record<string, string>;
  readonly cases: readonly Json[];
  readonly resultCases: readonly Json[];
};
const CONTRACT = JSON.parse(
  readFileSync(join(TESTDATA, "agnostic-deploy", "contract.json"), "utf8"),
) as { readonly limits: readonly Json[] };
const TICKET = { ticket_id: "T-1" };
const OUTPUT = { priority: "high", summary: "printer offline" };
const SESSION = "rda_0123456789abcdef0123456789abcdef";

function inputOf(args: Json): unknown {
  const generator = args.inputGenerator as
    | { readonly key: string; readonly char: string; readonly length: number }
    | undefined;
  const number = args.inputNumber as { readonly key: string; readonly value: string } | undefined;
  if (number !== undefined) {
    return { [number.key]: Number(number.value) };
  }
  return generator === undefined
    ? args.input
    : { [generator.key]: generator.char.repeat(generator.length) };
}

function messageOf(error: Json): string {
  const fields = (error.fields ?? {}) as Record<string, unknown>;
  return Object.entries(fields).reduce(
    (text, [key, value]) => text.replace(`{${key}}`, String(value)),
    VECTORS.messages[error.messageKey as string] as string,
  );
}

describe("shared invocation bodies", () => {
  test.each(VECTORS.cases.map((item) => [item.name as string, item] as const))(
    "%s",
    (_name, item) => {
      const args = item.args as Json;
      const build = () =>
        invocationBody(args.prompt as string | undefined, {
          sessionId: args.sessionId as string | undefined,
          model: args.model as string | undefined,
          reasoning: args.reasoning as boolean | undefined,
          input: inputOf(args),
        });
      if (item.error !== undefined) {
        expect(build).toThrowError(new InvalidArgumentError(messageOf(item.error as Json)));
      } else {
        expect(build()).toBe(item.body);
      }
    },
  );

  test.each(VECTORS.resultCases.map((item) => [item.name as string, item] as const))(
    "reads %s",
    (_name, item) => {
      const result = resultFromBody(item.body as string);
      const expected = item.expected as Json;
      expect(result.text).toBe(expected.text);
      expect(result.output ?? null).toEqual(expected.output);
    },
  );

  test("the messages and the limit are the vector ones", () => {
    expect(PROMPT_OR_INPUT_MESSAGE).toBe(VECTORS.messages.prompt_or_input);
    expect(INPUT_NOT_OBJECT_MESSAGE).toBe(VECTORS.messages.input_not_object);
    expect(INPUT_TOO_LARGE_MESSAGE).toBe(VECTORS.messages.input_too_large);
    expect(HOST_TOO_OLD_MESSAGE).toBe(VECTORS.messages.host_too_old);
    expect({ ...AGENT_DETAIL_MESSAGES }).toEqual(VECTORS.detailCodes);
    expect(VECTORS.maxRequestBytesLimit).toBe("agentServiceMaxRequestBytes");
    const limit = CONTRACT.limits.find((item) => item.feature === "B");
    expect(limit).toMatchObject({
      key: "agentServiceMaxOutputBytes",
      value: AGENT_SERVICE_MAX_OUTPUT_BYTES,
    });
  });

  test("an input that cannot be encoded is not an object", () => {
    const circular: Json = {};
    circular.self = circular;
    for (const value of [circular, { big: 1n }]) {
      expect(() =>
        invocationBody(undefined, { input: value as unknown as Record<string, unknown> }),
      ).toThrowError(new InvalidArgumentError(INPUT_NOT_OBJECT_MESSAGE));
    }
  });

  test("an input that JSON.stringify would silently change is not an object", () => {
    const values: Json[] = [
      { missing: undefined },
      { run: () => 1 },
      { nested: { score: Number.NEGATIVE_INFINITY } },
      { list: [1, Number.NaN] },
      { tag: Symbol("x") },
    ];
    for (const value of values) {
      expect(() =>
        invocationBody(undefined, { input: value as unknown as Record<string, unknown> }),
      ).toThrowError(new InvalidArgumentError(INPUT_NOT_OBJECT_MESSAGE));
    }
  });

  test("an input that fits exactly is accepted", () => {
    const padding = AGENT_SERVICE_MAX_REQUEST_BYTES - JSON.stringify({ k: "" }).length;
    expect(invocationBody(undefined, { input: { k: "x".repeat(padding) } })).toMatch(
      /^\{"input":\{"k":"x/,
    );
  });
});

describe("structured failures and output", () => {
  test.each(Object.keys(VECTORS.detailCodes))("%s has its own message", (detail) => {
    expect(failureMessage("protocol_error", detail)).toBe(
      `${VECTORS.detailCodes[detail]} (${detail})`,
    );
    expect(failureMessage("runtime_error", detail)).toMatch(/^el runtime del agente/);
  });

  test("a bad request after input is a host that is too old", () => {
    expect(httpError(400, { sentInput: true })).toEqual(
      new InvalidArgumentError(HOST_TOO_OLD_MESSAGE),
    );
    expect(httpError(400).message).not.toBe(HOST_TOO_OLD_MESSAGE);
  });

  test("the done event and the result builder carry the output", () => {
    const decoder = new EventDecoder();
    const [done] = decoder.feed({
      event: "done",
      data: JSON.stringify({ v: 1, type: "done", session_id: SESSION, output: OUTPUT }),
    });
    expect(done).toMatchObject({ type: "done", output: OUTPUT });
    const builder = new ResultBuilder();
    builder.add(done as AgentEvent);
    expect(builder.result().output).toEqual(OUTPUT);
    const [plain] = new EventDecoder().feed({
      event: "done",
      data: JSON.stringify({ v: 1, type: "done", session_id: SESSION }),
    });
    expect(plain).not.toHaveProperty("output");
  });
});

async function deploymentWith(...responses: Scripted[]) {
  const sandbox = new FakeSandbox();
  const transport = scriptedFetch(...responses);
  const deployment = new AgentDeployment({
    sandbox,
    port: 8080,
    hostAccess: await sandbox.getHost(8080),
    project: "ticket-triage",
    fetch: transport.fetch,
  });
  return { deployment, transport };
}

const STRUCTURED_BODY = JSON.stringify({
  v: 1,
  text: "",
  session_id: SESSION,
  usage: {},
  usage_reported: false,
  steps: 0,
  failed: null,
  output: OUTPUT,
});

describe("AgentDeployment with input", () => {
  test("invoke sends the input and reads the output", async () => {
    const { deployment, transport } = await deploymentWith({ body: STRUCTURED_BODY });
    const result = await deployment.invoke({ input: TICKET }, { sessionId: SESSION });
    expect(result.output).toEqual(OUTPUT);
    expect(result.text).toBe("");
    expect(JSON.parse(transport.requests[0]?.body ?? "")).toEqual({
      input: TICKET,
      session_id: SESSION,
    });
  });

  test("stream ends with the output", async () => {
    const body = sse(["session", { session_id: SESSION }], ["done", { output: OUTPUT }]);
    const { deployment, transport } = await deploymentWith({ chunks: [body] });
    const events: AgentEvent[] = [];
    for await (const event of deployment.stream({ input: TICKET })) {
      events.push(event);
    }
    expect(events.at(-1)).toMatchObject({ type: "done", output: OUTPUT });
    expect(transport.requests[0]?.body).toContain('"input":{"ticket_id":"T-1"}');
  });

  test.each([
    ["neither", {}],
    ["a list", { input: ["T-1"] }],
  ])("validates %s before any request", async (_name, request) => {
    const { deployment, transport } = await deploymentWith();
    await expect(deployment.invoke(request as never)).rejects.toBeInstanceOf(InvalidArgumentError);
    expect(transport.requests).toEqual([]);
  });

  test("a host that rejects input asks for a new deploy", async () => {
    const { deployment } = await deploymentWith({ status: 400, body: "{}" });
    await expect(deployment.invoke({ input: TICKET })).rejects.toThrowError(
      new InvalidArgumentError(HOST_TOO_OLD_MESSAGE),
    );
  });

  test("a structured failure is an AgentError with its detail", async () => {
    const body = JSON.stringify({
      v: 1,
      text: "",
      session_id: SESSION,
      usage: {},
      steps: 0,
      failed: { reason: "protocol_error", detail_code: "input_required" },
    });
    const { deployment } = await deploymentWith({ body });
    const failure = await deployment.invoke("hola").catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(AgentError);
    expect((failure as AgentError).detailCode).toBe("input_required");
    expect((failure as AgentError).message).toContain(VECTORS.detailCodes.input_required);
  });
});

describe("fleets with input", () => {
  test("a fleet checks the input before placing a session", async () => {
    const h = new FleetHarness();
    const fleet = h.fleet();
    await expect(fleet.invoke({ input: ["x"] } as never)).rejects.toThrowError(
      new InvalidArgumentError(INPUT_NOT_OBJECT_MESSAGE),
    );
    await expect(fleet.stream(undefined as never)).rejects.toBeInstanceOf(InvalidArgumentError);
    expect(h.world.created).toEqual([]);
  });

  test("AgentCore sends the input without a session in the body", async () => {
    const account = newAccount();
    account.control.runtime.listed = desiredDetails();
    account.control.runtime.details = desiredDetails();
    const fleet = await connectWith(account.ports, AGENT, REGION);
    account.response = [new TextEncoder().encode(STRUCTURED_BODY)];
    const result = await fleet.invoke({ input: TICKET }, { model: "m" });
    const [request] = account.invokes;
    expect(JSON.parse(new TextDecoder().decode(request?.payload))).toEqual({
      input: TICKET,
      model: "m",
    });
    expect(result.output).toEqual(OUTPUT);
    await expect(fleet.invoke(undefined as never)).rejects.toBeInstanceOf(InvalidArgumentError);
    expect(account.invokes).toHaveLength(1);
  });
});
