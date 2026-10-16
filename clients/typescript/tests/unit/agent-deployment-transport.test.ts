/**
 * El transporte real de `AgentDeployment` (`fetch`) contra un servidor
 * `node:http` del loopback que habla `rayito.agent-service/1` (sin TLS: es lo
 * único que cambia respecto al endpoint de AWS): JSON, SSE en trozos, `429` y
 * que las cabeceras del proxy llegan al servidor.
 */

import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";
import { afterEach, describe, expect, test } from "vitest";
import { AgentDeployment } from "../../src/agent/deployment.js";
import type { AgentEvent } from "../../src/agent/events.js";
import { AgentError, SandboxError } from "../../src/errors.js";
import { AGENT_SERVICE_PORT } from "../../src/limits.js";
import { HostAccess } from "../../src/models.js";
import { FakeSandbox, RESULT_BODY, sse } from "./agent-project-fake.js";

const LOOPBACK = "127.0.0.1";
const servers: Server[] = [];

class LocalAccess extends HostAccess {
  readonly #base: string;

  constructor(base: string) {
    super(LOOPBACK, AGENT_SERVICE_PORT, () => "jwe-local");
    this.#base = base;
  }

  override get url(): string {
    return this.#base;
  }
}

async function listen(
  handler: (request: IncomingMessage, response: ServerResponse, body: string) => void,
): Promise<{ url: string; seen: { headers: IncomingMessage["headers"]; body: string }[] }> {
  const seen: { headers: IncomingMessage["headers"]; body: string }[] = [];
  const server = createServer((request, response) => {
    const chunks: Buffer[] = [];
    request.on("data", (chunk: Buffer) => chunks.push(chunk));
    request.on("end", () => {
      const body = Buffer.concat(chunks).toString("utf8");
      seen.push({ headers: request.headers, body });
      handler(request, response, body);
    });
  });
  servers.push(server);
  await new Promise<void>((resolve) => server.listen(0, LOOPBACK, resolve));
  return { url: `http://${LOOPBACK}:${(server.address() as AddressInfo).port}`, seen };
}

function deployment(url: string): AgentDeployment {
  return new AgentDeployment({
    sandbox: new FakeSandbox(),
    port: AGENT_SERVICE_PORT,
    hostAccess: new LocalAccess(url),
    project: "support-bot",
    requestTimeoutMs: 5000,
  });
}

afterEach(async () => {
  await Promise.all(
    servers.splice(0).map((server) => new Promise((resolve) => server.close(resolve))),
  );
});

describe("fetch transport", () => {
  test("invoke sends the proxy headers and reads the json result", async () => {
    const { url, seen } = await listen((request, response) => {
      if (request.url === "/ping") {
        response.setHeader("Content-Type", "application/json");
        response.end('{"status":"Healthy"}');
        return;
      }
      response.setHeader("Content-Type", "application/json");
      response.end(JSON.stringify(RESULT_BODY));
    });
    const target = deployment(url);
    expect((await target.status()).state).toBe("healthy");
    const result = await target.invoke("hola ñ", { sessionId: "ses_a" });
    expect(result.text).toBe("hola mundo");
    const sent = seen.at(-1);
    expect(sent?.headers["x-aws-proxy-auth"]).toBe("jwe-local");
    expect(sent?.headers["x-aws-proxy-port"]).toBe(String(AGENT_SERVICE_PORT));
    expect(sent?.headers.accept).toBe("application/json");
    expect(JSON.parse(sent?.body ?? "")).toEqual({ prompt: "hola ñ", session_id: "ses_a" });
  });

  test("stream reads sse messages that arrive in pieces", async () => {
    const body = sse(
      ["session", { session_id: "ses_a" }],
      ["step_started", { index: 1 }],
      ["text", { text: "listo" }],
      ["step_finished", { index: 1, usage: { input: 3 } }],
      ["done", { session_id: "ses_a" }],
    );
    const { url } = await listen((_request, response) => {
      response.writeHead(200, { "Content-Type": "text/event-stream" });
      response.write(body.slice(0, 40));
      setTimeout(() => response.end(body.slice(40)), 20);
    });
    const events: AgentEvent[] = [];
    for await (const event of deployment(url).stream("hola")) {
      events.push(event);
    }
    expect(events.map((event) => event.type)).toEqual([
      "step_started",
      "text",
      "step_finished",
      "done",
    ]);
    expect(events.at(-1)).toMatchObject({ sessionId: "ses_a" });
  });

  test("a 429 is busy", async () => {
    const { url } = await listen((_request, response) => {
      response.writeHead(429, { "Retry-After": "1" });
      response.end('{"error":"busy"}');
    });
    await expect(deployment(url).invoke("hola")).rejects.toMatchObject({
      reason: "busy",
      name: AgentError.name,
    });
  });

  test("an unreachable server is a sandbox error", async () => {
    const { url } = await listen(() => undefined);
    await Promise.all(
      servers.splice(0).map((server) => new Promise((resolve) => server.close(resolve))),
    );
    expect((await deployment(url).status()).state).toBe("unavailable");
    await expect(deployment(url).invoke("hola")).rejects.toBeInstanceOf(SandboxError);
  });
});
