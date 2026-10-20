/**
 * `AgentCoreFleet.invoke()` y `stream()` (ADR-031, D7): la misma API y las
 * mismas sesiones que `AgentFleet`. El id de sesión viaja sólo en la petición a
 * AWS (no en el cuerpo), el resultado devuelve el id usado, y la respuesta se
 * decodifica con los mismos decodificadores del despliegue de MicroVMs.
 */

import { describe, expect, it } from "vitest";
import { AgentCoreFleet, connectWith } from "../../src/agentcore/fleet.js";
import { AgentError, AuthenticationError, InvalidArgumentError } from "../../src/errors.js";
import { sse } from "./agent-project-fake.js";
import {
  AGENT,
  DESCRIPTION,
  desiredDetails,
  type FakeAccount,
  newAccount,
  REGION,
  RUNTIME_NAME,
} from "./agentcore-fakes.js";

const encoder = new TextEncoder();
const RESPONSE_SESSION = "rayito-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

function reply(account: FakeAccount, body: string): void {
  account.response = [encoder.encode(body.slice(0, 20)), encoder.encode(body.slice(20))];
}

async function fleetOf(account: FakeAccount): Promise<AgentCoreFleet> {
  account.control.runtime.listed = desiredDetails();
  account.control.runtime.details = desiredDetails();
  return connectWith(account.ports, AGENT, REGION);
}

function jsonBody(overrides: Record<string, unknown> = {}): string {
  return JSON.stringify({
    v: 1,
    session_id: RESPONSE_SESSION,
    text: "42",
    steps: 2,
    usage: { input: 10, output: 5 },
    ...overrides,
  });
}

describe("invoke", () => {
  it("manda el prompt sin session_id, la sesión en la petición y el plazo de 15 minutos", async () => {
    const account = newAccount();
    const fleet = await fleetOf(account);
    reply(account, jsonBody());
    const result = await fleet.invoke("¿Cuánto es 40 + 2?", {
      model: "amazon.nova-lite-v1:0",
      reasoning: true,
    });
    const [request] = account.invokes;
    expect(request).toMatchObject({
      agentName: AGENT,
      runtimeArn: desiredDetails().runtimeArn,
      accept: "application/json",
      requestTimeoutMs: 900_000,
    });
    expect(JSON.parse(new TextDecoder().decode(request?.payload))).toEqual({
      prompt: "¿Cuánto es 40 + 2?",
      model: "amazon.nova-lite-v1:0",
      reasoning: true,
    });
    expect(request?.sessionId).toMatch(/^rayito-[0-9a-f]{32}$/);
    expect(result.text).toBe("42");
    expect(result.steps).toBe(2);
    expect(result.usage.input).toBe(10);
  });

  it("el resultado devuelve el id de sesión usado, y pasarlo continúa la sesión", async () => {
    const account = newAccount();
    const fleet = await fleetOf(account);
    reply(account, jsonBody());
    const first = await fleet.invoke("hola");
    expect(first.sessionId).toBe(account.invokes[0]?.sessionId);
    reply(account, jsonBody());
    const second = await fleet.invoke("otra", { sessionId: first.sessionId });
    expect(account.invokes[1]?.sessionId).toBe(first.sessionId);
    expect(second.sessionId).toBe(first.sessionId);
  });

  it("un id del llamante se convierte siempre en el mismo id de AgentCore", async () => {
    const account = newAccount();
    const fleet = await fleetOf(account);
    reply(account, jsonBody());
    const first = await fleet.invoke("hola", { sessionId: "user-42" });
    reply(account, jsonBody());
    const second = await fleet.invoke("hola", { sessionId: "user-42" });
    expect(first.sessionId).toBe("rayito-6d894aa3ee802549d7f340e7c1cf0d1c");
    expect(second.sessionId).toBe(first.sessionId);
    expect(account.invokes[0]?.sessionId).toBe(first.sessionId);
  });

  it("un fallo del agente en el cuerpo es AgentError", async () => {
    const account = newAccount();
    const fleet = await fleetOf(account);
    reply(
      account,
      JSON.stringify({
        v: 1,
        failed: { reason: "model_error", detail_code: "ThrottlingException" },
      }),
    );
    await expect(fleet.invoke("hola")).rejects.toBeInstanceOf(AgentError);
  });

  it("un prompt vacío es un error de argumento y no llama a AWS", async () => {
    const account = newAccount();
    const fleet = await fleetOf(account);
    await expect(fleet.invoke("")).rejects.toBeInstanceOf(InvalidArgumentError);
    expect(account.invokes).toEqual([]);
  });

  it("lo que lanza el adaptador llega tal cual", async () => {
    const account = newAccount();
    const fleet = await fleetOf(account);
    account.invokeError = new AuthenticationError("sin permisos");
    await expect(fleet.invoke("hola")).rejects.toBeInstanceOf(AuthenticationError);
  });
});

describe("stream", () => {
  it("pide SSE con el plazo de 60 minutos y decodifica los eventos", async () => {
    const account = newAccount();
    const fleet = await fleetOf(account);
    reply(
      account,
      sse(
        ["step_started", { index: 1 }],
        ["text_delta", { text: "4" }],
        ["text", { text: "42" }],
        ["step_finished", { index: 1, usage: { input: 4, output: 1 }, finish_reason: "stop" }],
        ["done", { session_id: RESPONSE_SESSION }],
      ),
    );
    const stream = await fleet.stream("hola");
    const types: string[] = [];
    for await (const event of stream) {
      types.push(event.type);
    }
    expect(types).toEqual(["step_started", "text_delta", "text", "step_finished", "done"]);
    expect(account.invokes[0]).toMatchObject({
      accept: "text/event-stream",
      requestTimeoutMs: 3_600_000,
    });
    expect(stream.sessionId).toBe(account.invokes[0]?.sessionId);
  });

  it("result() devuelve el texto y la sesión usada aunque el host nombre otra", async () => {
    const account = newAccount();
    const fleet = await fleetOf(account);
    reply(
      account,
      sse(
        ["step_started", { index: 1 }],
        ["text", { text: "42" }],
        ["done", { session_id: "otra" }],
      ),
    );
    const result = await (await fleet.stream("hola", { sessionId: "user-42" })).result();
    expect(result.text).toBe("42");
    expect(result.sessionId).toBe("rayito-6d894aa3ee802549d7f340e7c1cf0d1c");
  });

  it("un error al abrir la petición sale de stream(), no al iterar", async () => {
    const account = newAccount();
    const fleet = await fleetOf(account);
    account.invokeError = new AuthenticationError("sin permisos");
    await expect(fleet.stream("hola")).rejects.toBeInstanceOf(AuthenticationError);
  });
});

describe("status, stopSession y connect", () => {
  it("status lee el runtime por su id y nombra el grupo de logs", async () => {
    const account = newAccount();
    const fleet = await fleetOf(account);
    const status = await fleet.status();
    expect(status).toMatchObject({
      name: AGENT,
      region: REGION,
      runtimeName: RUNTIME_NAME,
      status: "READY",
      runtimeVersion: "2",
    });
    expect(status.logGroup).toBe(`/aws/bedrock-agentcore/runtimes/${status.runtimeId}-DEFAULT`);
  });

  it("stopSession para la sesión con el id derivado", async () => {
    const account = newAccount();
    const fleet = await fleetOf(account);
    expect(await fleet.stopSession("user-42")).toBe(true);
    expect(account.calls.at(-1)).toEqual({
      operation: "StopRuntimeSession",
      input: "rayito-6d894aa3ee802549d7f340e7c1cf0d1c",
    });
  });

  it("connect de un agente que no existe es runtime_not_found", async () => {
    const account = newAccount();
    await expect(connectWith(account.ports, AGENT, REGION)).rejects.toMatchObject({
      code: "runtime_not_found",
    });
  });

  it("connect no encuentra un runtime con el nombre pero sin la marca de Rayito", async () => {
    const account = newAccount({
      runtime: { listed: desiredDetails({ description: "otro" }), details: desiredDetails() },
    });
    await expect(connectWith(account.ports, AGENT, REGION)).rejects.toMatchObject({
      code: "runtime_not_found",
    });
    expect(DESCRIPTION).toContain("rayito.agentcore/1");
  });

  it("connect valida el nombre del agente", async () => {
    const account = newAccount();
    await expect(connectWith(account.ports, "Support.Bot", REGION)).rejects.toMatchObject({
      code: "name_invalid",
    });
  });

  it("connect sin región ni entorno es un error de argumento", async () => {
    const saved = [process.env.AWS_REGION, process.env.AWS_DEFAULT_REGION];
    delete process.env.AWS_REGION;
    delete process.env.AWS_DEFAULT_REGION;
    try {
      await expect(AgentCoreFleet.connect(AGENT)).rejects.toBeInstanceOf(InvalidArgumentError);
    } finally {
      if (saved[0] !== undefined) process.env.AWS_REGION = saved[0];
      if (saved[1] !== undefined) process.env.AWS_DEFAULT_REGION = saved[1];
    }
  });
});
