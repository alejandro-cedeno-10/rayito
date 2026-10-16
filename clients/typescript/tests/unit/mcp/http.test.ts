/**
 * Streamable HTTP de verdad: el manejador del SDK servido con `node:http` en
 * un puerto libre de loopback, el cliente oficial por URL, la defensa contra
 * DNS rebinding (`Host` y `Origin` exactos) y la terminación del sandbox al
 * parar. Espejo de `test_mcp_http.py`.
 */

import { request as httpRequest } from "node:http";
import { networkInterfaces } from "node:os";
import { Client, StreamableHTTPClientTransport } from "@modelcontextprotocol/client";
import { describe, expect, it } from "vitest";
import { guardRequest } from "../../../src/mcp/http.js";
import { createStderrLogger } from "../../../src/mcp/logging.js";
import { startMcp } from "../../../src/mcp/run.js";
import { startFixture } from "./support.js";

const AUTHORITY = "127.0.0.1:8000";
const LOOPBACK = "127.0.0.1";
const EPHEMERAL_PORT = 0;

async function startHttp(host = LOOPBACK) {
  const fixture = await startFixture();
  const lines: string[] = [];
  const logger = createStderrLogger("INFO", { write: (line) => lines.push(line) });
  const running = await startMcp(
    { http: true, host, port: EPHEMERAL_PORT },
    { peers: fixture.peers, app: fixture.app, logger },
  );
  return { fixture, running, lines };
}

async function rawPost(
  url: string,
  headers: Record<string, string>,
): Promise<{ status: number; body: string }> {
  const target = new URL(url);
  return new Promise((resolve, reject) => {
    const req = httpRequest(
      {
        host: target.hostname,
        port: target.port,
        path: target.pathname,
        method: "POST",
        headers: { "content-type": "application/json", ...headers },
      },
      (res) => {
        let body = "";
        res.setEncoding("utf8");
        res.on("data", (chunk) => {
          body += chunk;
        });
        res.on("end", () => resolve({ status: res.statusCode ?? 0, body }));
      },
    );
    req.on("error", reject);
    req.end("{}");
  });
}

describe("guardRequest", () => {
  it("deja pasar un Host exacto sin Origin", () => {
    expect(guardRequest({ host: AUTHORITY }, "/mcp", AUTHORITY)).toBeUndefined();
  });

  it("deja pasar un Origin igual a la autoridad", () => {
    expect(
      guardRequest({ host: AUTHORITY, origin: `http://${AUTHORITY}` }, "/mcp", AUTHORITY),
    ).toBeUndefined();
  });

  it.each([
    ["sin Host", {}],
    ["otro host", { host: "evil.example:8000" }],
    ["otro puerto", { host: "127.0.0.1:8001" }],
    ["sólo el nombre sin puerto", { host: "127.0.0.1" }],
  ])("%s da 421", (_name, headers) => {
    expect(guardRequest(headers, "/mcp", AUTHORITY)?.status).toBe(421);
  });

  it.each([
    ["otro origen", "http://evil.example"],
    ["el mismo host con otro puerto", "http://127.0.0.1:9"],
    ["origen null", "null"],
    ["otro esquema", `https://${AUTHORITY}`],
  ])("%s da 403", (_name, origin) => {
    expect(guardRequest({ host: AUTHORITY, origin }, "/mcp", AUTHORITY)?.status).toBe(403);
  });

  it("otra ruta da 404 sólo con Host válido", () => {
    expect(guardRequest({ host: AUTHORITY }, "/otra", AUTHORITY)?.status).toBe(404);
    expect(guardRequest({ host: "evil:1" }, "/otra", AUTHORITY)?.status).toBe(421);
  });

  it("compara Host y Origin sin distinguir mayúsculas", () => {
    expect(
      guardRequest(
        { host: "LocalHost:8000", origin: "HTTP://LOCALHOST:8000" },
        "/mcp",
        "localhost:8000",
      ),
    ).toBeUndefined();
  });
});

describe("streamable HTTP", () => {
  it("sirve las seis herramientas por URL y termina el sandbox al parar", async () => {
    const { fixture, running } = await startHttp();
    const client = new Client({ name: "http-test", version: "0.0.0" });
    await client.connect(new StreamableHTTPClientTransport(new URL(running.url as string)));
    const tools = await client.listTools();
    const result = await client.callTool({ name: "run_command", arguments: { cmd: "echo http" } });
    await client.close();
    expect(tools.tools).toHaveLength(6);
    expect(result.isError).toBeFalsy();
    expect(result.structuredContent).toMatchObject({ stdout: "http\n", exit_code: 0 });
    await running.stop();
    expect(fixture.plane.callsTo("terminateMicrovm")).toHaveLength(1);
  });

  it("rechaza un Host ajeno con 421 y un Origin ajeno con 403", async () => {
    const { running } = await startHttp();
    const url = running.url as string;
    const foreignHost = await rawPost(url, { host: "evil.example" });
    const foreignOrigin = await rawPost(url, {
      host: new URL(url).host,
      origin: "http://evil.example",
    });
    const nullOrigin = await rawPost(url, { host: new URL(url).host, origin: "null" });
    await running.stop();
    expect(foreignHost.status).toBe(421);
    expect(foreignOrigin.status).toBe(403);
    expect(nullOrigin.status).toBe(403);
  });

  it("responde 404 en otra ruta", async () => {
    const { running } = await startHttp();
    const other = new URL("/otra", running.url as string).toString();
    const response = await rawPost(other, {});
    await running.stop();
    expect(response.status).toBe(404);
  });

  const external = Object.values(networkInterfaces())
    .flat()
    .find((address) => address?.family === "IPv4" && !address.internal)?.address;

  it.skipIf(external === undefined)(
    "avisa de que no hay autenticación fuera de loopback",
    async () => {
      const { running, lines } = await startHttp(external);
      await running.stop();
      expect(lines.join("")).toContain("sin autenticación");
    },
  );

  it("no avisa en loopback", async () => {
    const { running, lines } = await startHttp();
    await running.stop();
    expect(lines.join("")).not.toContain("sin autenticación");
  });
});
