/**
 * El servidor MCP de TypeScript contra el guest local (`make local-e2e`).
 *
 * Por stdio (`rayito-mcp` como subproceso, con el cliente oficial del SDK de
 * MCP): el protocolo y el contrato compartido `testdata/mcp/tools.json`, el
 * error de una herramienta sin `RAYITO_TEMPLATE`, el rechazo de una opción
 * desconocida y la parada con el fin de `stdin`. Ese subproceso no sabe
 * hablar con el guest local, así que las seis herramientas contra un sandbox
 * real corren en proceso por `--http` con el plano de control y el transporte
 * del guest inyectados, con los mismos escenarios que el e2e contra AWS real
 * (`tests/shared/mcp-scenarios.ts`). El transporte stdio y el HTTP sirven las
 * mismas herramientas.
 *
 * Necesita el paquete construido: el runner lo deja en `RAYITO_TS_DIST`
 * (`dev/local/run-e2e.sh`), y sin esa variable se usa `dist/`.
 */

import { type ChildProcess, spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { join } from "node:path";
import { Client, StreamableHTTPClientTransport } from "@modelcontextprotocol/client";
import { StdioClientTransport } from "@modelcontextprotocol/client/stdio";
import { afterAll, beforeAll, describe, expect, test } from "vitest";
import { runMcpCli } from "../../src/mcp/cli.js";
import { describeServer, GOLDEN } from "../shared/mcp-golden.js";
import { CALL_TIMEOUT_MS, exerciseTools, TOOL_NAMES } from "../shared/mcp-scenarios.js";
import { freePort } from "../unit/cli/proxy-harness.js";
import { type LocalHarness, localEnabled, localHarness, releaseGuest } from "./helpers.js";

const DIST = process.env.RAYITO_TS_DIST ?? join(import.meta.dirname, "../../dist");
const SERVER_ENTRY = join(DIST, "mcp.mjs");
const SERVER_TIMEOUT_SECONDS = "900";
const USAGE_EXIT_CODE = 2;
const EXIT_TIMEOUT_MS = 30_000;
const TERMINATE_BUDGET_MS = 30_000;
const POLL_MS = 100;

function stdioEnvironment(extra: Record<string, string> = {}): Record<string, string> {
  return {
    PATH: process.env.PATH ?? "",
    RAYITO_MCP_TIMEOUT_SECONDS: SERVER_TIMEOUT_SECONDS,
    RAYITO_MCP_IDLE_SECONDS: "0",
    ...extra,
  };
}

function lineSink(lines: string[]) {
  return { write: (line: string) => lines.push(line) };
}

async function until(condition: () => boolean, budgetMs: number, label: string): Promise<void> {
  const deadline = Date.now() + budgetMs;
  while (!condition()) {
    if (Date.now() > deadline) {
      throw new Error(`no ocurrió a tiempo: ${label}`);
    }
    await new Promise((resolve) => setTimeout(resolve, POLL_MS));
  }
}

describe.skipIf(!localEnabled())("rayito-mcp local", () => {
  test("el paquete construido trae rayito-mcp", () => {
    expect(existsSync(SERVER_ENTRY)).toBe(true);
  });

  test("por stdio anuncia el contrato compartido y las seis herramientas", async () => {
    const client = new Client({ name: "rayito-local-stdio", version: "0.0.0" });
    await client.connect(
      new StdioClientTransport({
        command: process.execPath,
        args: [SERVER_ENTRY],
        env: stdioEnvironment(),
        stderr: "pipe",
      }),
    );
    try {
      const described = await describeServer(client);
      expect(described).toEqual(GOLDEN);
      expect((described.tools as Array<{ name: string }>).map((tool) => tool.name)).toEqual(
        TOOL_NAMES,
      );
    } finally {
      await client.close();
    }
  });

  test("por stdio una herramienta sin RAYITO_TEMPLATE falla con un mensaje que lo dice", async () => {
    const client = new Client({ name: "rayito-local-stdio", version: "0.0.0" });
    await client.connect(
      new StdioClientTransport({
        command: process.execPath,
        args: [SERVER_ENTRY],
        env: stdioEnvironment(),
        stderr: "pipe",
      }),
    );
    try {
      const result = await client.callTool(
        { name: "run_command", arguments: { cmd: "echo hola" } },
        { timeout: CALL_TIMEOUT_MS },
      );
      expect(result.isError).toBe(true);
      expect(JSON.stringify(result.content)).toContain("RAYITO_TEMPLATE");
    } finally {
      await client.close();
    }
  });

  test("una opción desconocida sale con 2 y sin traza", async () => {
    const stderr: string[] = [];
    const child = spawn(process.execPath, [SERVER_ENTRY, "--no-existe"], {
      env: stdioEnvironment(),
      stdio: ["ignore", "ignore", "pipe"],
    });
    child.stderr?.setEncoding("utf8").on("data", (chunk: string) => stderr.push(chunk));
    const code = await new Promise<number | null>((resolve) => child.once("exit", resolve));
    expect(code).toBe(USAGE_EXIT_CODE);
    expect(stderr.join("")).not.toContain("    at ");
  });

  test("al cerrarse stdin el servidor sale con 0", async () => {
    const child: ChildProcess = spawn(process.execPath, [SERVER_ENTRY], {
      env: stdioEnvironment(),
      stdio: ["pipe", "ignore", "ignore"],
    });
    const exited = new Promise<number | null>((resolve) => child.once("exit", resolve));
    child.stdin?.end();
    expect(
      await Promise.race([
        exited,
        new Promise<string>((resolve) => setTimeout(() => resolve("timeout"), EXIT_TIMEOUT_MS)),
      ]),
    ).toBe(0);
  });

  describe("las seis herramientas contra el guest", () => {
    let harness: LocalHarness | undefined;
    const stderr: string[] = [];
    const stop = new AbortController();
    let served: Promise<number>;
    let url = "";

    beforeAll(async () => {
      harness = await localHarness();
      const port = await freePort();
      url = `http://127.0.0.1:${port}/mcp`;
      served = runMcpCli(["--http", "--port", String(port)], {
        env: stdioEnvironment({ RAYITO_TEMPLATE: harness.templateArn }),
        stdout: lineSink([]),
        stderr: lineSink(stderr),
        shutdown: stop.signal,
        environment: {
          controlPlane: harness.controlPlane,
          transport: harness.settings.address.transport(),
        },
      });
      await until(() => stderr.join("").includes("escuchando"), EXIT_TIMEOUT_MS, "listening");
    });

    afterAll(async () => {
      stop.abort();
      await served;
      if (harness !== undefined) {
        await releaseGuest(harness);
      }
    });

    test("run_code, run_command, archivos y list_sandboxes; el sandbox termina al parar", async () => {
      const current = harness as LocalHarness;
      const client = new Client({ name: "rayito-local-http", version: "0.0.0" });
      await client.connect(new StreamableHTTPClientTransport(new URL(url)));
      let sandboxId: string;
      try {
        sandboxId = await exerciseTools(client, current.templateArn);
      } finally {
        await client.close();
      }
      expect(current.controlPlane.liveSandboxId).toBe(sandboxId);
      stop.abort();
      expect(await served).toBe(0);
      await until(
        () => current.controlPlane.liveSandboxId === undefined,
        TERMINATE_BUDGET_MS,
        "terminate",
      );
      expect(stderr.join("")).not.toContain("echo hola");
    });
  });
});
