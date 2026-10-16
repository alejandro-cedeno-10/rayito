/**
 * La línea de comandos de `rayito-mcp`: argumentos, códigos de salida, el
 * mensaje cuando faltan los peers opcionales y un arranque completo en modo
 * HTTP. Espejo de `test_mcp_main.py`.
 */

import { createServer } from "node:net";
import { Client, StreamableHTTPClientTransport } from "@modelcontextprotocol/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { InvalidArgumentError } from "../../../src/errors.js";
import { MISSING_PEER_MESSAGE, runMcpCli } from "../../../src/mcp/cli.js";
import {
  DEFAULT_HTTP_HOST,
  DEFAULT_HTTP_PORT,
  HELP_TEXT,
  McpUsageError,
  parseCommandLine,
} from "../../../src/mcp/options.js";
import { MCP_NPX_COMMAND } from "../../../src/mcp/peers.js";
import * as optional from "../../../src/optional.js";
import { FakeControlPlane, IMAGE_ARN } from "../fake/control-plane.js";
import { ACCESS_TOKEN, startRayd } from "../helpers.js";

const FAST_POLL_MS = 10;
const STARTUP_BUDGET_MS = 10_000;

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllEnvs();
});

describe("parseCommandLine", () => {
  it("sin argumentos usa stdio", () => {
    expect(parseCommandLine([])).toEqual({
      kind: "run",
      options: { http: false, host: DEFAULT_HTTP_HOST, port: DEFAULT_HTTP_PORT },
    });
  });

  it("--http usa 127.0.0.1:8000", () => {
    expect(parseCommandLine(["--http"])).toEqual({
      kind: "run",
      options: { http: true, host: "127.0.0.1", port: 8000 },
    });
  });

  it("--http con host y puerto", () => {
    expect(parseCommandLine(["--http", "--host", "192.168.1.5", "--port", "9000"])).toEqual({
      kind: "run",
      options: { http: true, host: "192.168.1.5", port: 9000 },
    });
  });

  it("--help pide la ayuda", () => {
    expect(parseCommandLine(["--help"])).toEqual({ kind: "help" });
    expect(parseCommandLine(["-h"])).toEqual({ kind: "help" });
  });

  it.each(["0.0.0.0", "::"])("rechaza el host comodín %s", (host) => {
    expect(() => parseCommandLine(["--http", "--host", host])).toThrow(McpUsageError);
    expect(() => parseCommandLine(["--http", "--host", host])).toThrow("Host");
  });

  it.each([
    ["--port", "1"],
    ["--host", "192.168.1.5"],
  ])("%s sin --http es un error de uso", (...argv) => {
    expect(() => parseCommandLine(argv)).toThrow("--http");
  });

  it.each(["0", "65536", "abc", "-1", "80.5"])("rechaza el puerto %s", (port) => {
    expect(() => parseCommandLine(["--http", "--port", port])).toThrow("--port");
  });

  it("rechaza opciones desconocidas y posicionales", () => {
    expect(() => parseCommandLine(["--nope"])).toThrow(McpUsageError);
    expect(() => parseCommandLine(["extra"])).toThrow(McpUsageError);
  });
});

interface Harness {
  readonly stdout: string[];
  readonly stderr: string[];
  readonly shutdown: AbortController;
}

function harness(): Harness {
  return { stdout: [], stderr: [], shutdown: new AbortController() };
}

function deps(h: Harness, env: Record<string, string> = {}) {
  return {
    env,
    stdout: { write: (line: string) => h.stdout.push(line) },
    stderr: { write: (line: string) => h.stderr.push(line) },
    shutdown: h.shutdown.signal,
  };
}

async function freePort(): Promise<number> {
  const probe = createServer();
  await new Promise<void>((resolve) => probe.listen(0, "127.0.0.1", resolve));
  const address = probe.address();
  await new Promise<void>((resolve) => probe.close(() => resolve()));
  return typeof address === "object" && address !== null ? address.port : 0;
}

async function until(condition: () => boolean): Promise<void> {
  const deadline = Date.now() + STARTUP_BUDGET_MS;
  while (!condition()) {
    if (Date.now() > deadline) {
      throw new Error("el servidor no arrancó a tiempo");
    }
    await new Promise((resolve) => setTimeout(resolve, FAST_POLL_MS));
  }
}

describe("runMcpCli", () => {
  it("imprime la ayuda en stdout y sale con 0", async () => {
    const h = harness();
    expect(await runMcpCli(["--help"], deps(h))).toBe(0);
    expect(h.stdout.join("")).toBe(HELP_TEXT);
    expect(h.stderr).toEqual([]);
  });

  it("un argumento inválido sale con 2 y lo dice en stderr", async () => {
    const h = harness();
    expect(await runMcpCli(["--port", "1"], deps(h))).toBe(2);
    expect(h.stderr.join("")).toBe("rayito-mcp: --host y --port sólo tienen sentido con --http\n");
    expect(h.stdout).toEqual([]);
  });

  it("un entorno malformado sale con 2 y nombra la variable", async () => {
    const h = harness();
    expect(await runMcpCli([], deps(h, { RAYITO_MCP_IDLE_SECONDS: "abc" }))).toBe(2);
    expect(h.stderr.join("")).toContain("RAYITO_MCP_IDLE_SECONDS");
  });

  it("sin los peers opcionales sale con 2 y da la orden npx exacta", async () => {
    vi.spyOn(optional, "loadOptionalPeer").mockRejectedValue(
      new InvalidArgumentError("falta el paquete"),
    );
    const h = harness();
    expect(await runMcpCli([], deps(h))).toBe(2);
    expect(h.stderr.join("")).toBe(`${MISSING_PEER_MESSAGE}\n`);
    expect(MISSING_PEER_MESSAGE).toContain(
      "npx -y -p rayito -p @modelcontextprotocol/server -p zod rayito-mcp",
    );
    expect(MCP_NPX_COMMAND).toContain("rayito-mcp");
  });

  it("arranca en HTTP, sirve una herramienta y al parar termina el sandbox", async () => {
    vi.stubEnv("RAYITO_ACCESS_TOKEN", ACCESS_TOKEN);
    const rayd = await startRayd();
    const plane = new FakeControlPlane({ endpoint: rayd.host });
    const h = harness();
    const port = await freePort();
    const exit = runMcpCli(["--http", "--port", String(port)], {
      ...deps(h, { RAYITO_TEMPLATE: IMAGE_ARN, RAYITO_MCP_IDLE_SECONDS: "0" }),
      environment: { controlPlane: plane, transport: rayd.transport },
    });
    await until(() => h.stderr.join("").includes("escuchando"));
    const client = new Client({ name: "cli-test", version: "0.0.0" });
    await client.connect(
      new StreamableHTTPClientTransport(new URL(`http://127.0.0.1:${port}/mcp`)),
    );
    const result = await client.callTool({ name: "run_command", arguments: { cmd: "echo cli" } });
    await client.close();
    h.shutdown.abort();
    expect(await exit).toBe(0);
    await rayd.close();
    expect(result.structuredContent).toMatchObject({ stdout: "cli\n" });
    expect(plane.callsTo("terminateMicrovm")).toHaveLength(1);
  });

  it("un puerto ocupado sale con 1 y lo dice", async () => {
    const holder = createServer();
    await new Promise<void>((resolve) => holder.listen(0, "127.0.0.1", resolve));
    const address = holder.address();
    const port = typeof address === "object" && address !== null ? address.port : 0;
    const h = harness();
    const code = await runMcpCli(["--http", "--port", String(port)], deps(h));
    await new Promise<void>((resolve) => holder.close(() => resolve()));
    expect(code).toBe(1);
    expect(h.stderr.join("")).toContain("no se pudo arrancar");
  });
});
