/**
 * Aceptación del servidor MCP en TypeScript (ADR-027, fase 6): `rayito-mcp`
 * (`dist/mcp.mjs`) como subproceso, manejado por el cliente oficial del SDK
 * de MCP por stdio y por `--http`, contra un MicroVM real. En cada transporte
 * prueba una a una las seis herramientas, la creación perezosa en la primera
 * llamada, el PNG de matplotlib como bloque de imagen, un error del kernel
 * como dato, `list_sandboxes` con `current` y, al parar el servidor (EOF en
 * stdin o SIGINT), que el MicroVM queda TERMINATING/TERMINATED. Espejo de
 * `clients/python/tests/e2e/test_m7_mcp.py`.
 *
 * Necesita `pnpm build` antes (usa `dist/mcp.mjs`), `RAYITO_E2E=1` y
 * `RAYITO_TEMPLATE` como el resto de los e2e; el subproceso hereda `AWS_*` y
 * `RAYITO_*`. Sin `RAYITO_E2E=1` se omite sin llamar a AWS.
 *
 * Coste: dos sandboxes de la imagen `RAYITO_TEMPLATE` (uno por transporte)
 * durante un par de minutos cada uno, terminados al parar el servidor.
 */

import { type ChildProcess, spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { createServer } from "node:net";
import { join } from "node:path";
import { Client, StreamableHTTPClientTransport } from "@modelcontextprotocol/client";
import { StdioClientTransport } from "@modelcontextprotocol/client/stdio";
import { describe, expect, test } from "vitest";
import { LambdaMicrovmsControlPlane } from "../../src/index.js";
import { exerciseTools } from "../shared/mcp-scenarios.js";
import { e2eEnabled, waitForTerminalState } from "./helpers.js";

const SERVER_ENTRY = join(import.meta.dirname, "../../dist/mcp.mjs");
const SERVER_TIMEOUT_SECONDS = "900";
const LISTEN_TIMEOUT_MS = 30_000;
const EXIT_TIMEOUT_MS = 60_000;
const POLL_MS = 50;

function serverEnvironment(): Record<string, string> {
  const inherited = Object.entries(process.env).filter(
    ([key, value]) => value !== undefined && /^(AWS_|RAYITO_)/.test(key),
  ) as Array<[string, string]>;
  return {
    PATH: process.env.PATH ?? "",
    ...Object.fromEntries(inherited),
    RAYITO_MCP_TIMEOUT_SECONDS: SERVER_TIMEOUT_SECONDS,
    RAYITO_MCP_IDLE_SECONDS: "0",
    RAYITO_MCP_LOG_LEVEL: "DEBUG",
  };
}

async function freePort(): Promise<number> {
  const probe = createServer();
  await new Promise<void>((resolve) => probe.listen(0, "127.0.0.1", resolve));
  const address = probe.address();
  await new Promise<void>((resolve) => probe.close(() => resolve()));
  return typeof address === "object" && address !== null ? address.port : 0;
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

describe.runIf(e2eEnabled())("rayito-mcp (AWS real)", () => {
  test("dist/mcp.mjs existe (ejecuta pnpm build antes)", () => {
    expect(existsSync(SERVER_ENTRY)).toBe(true);
  });

  test("seis herramientas por stdio y el sandbox termina al cerrar el cliente", async () => {
    const controlPlane = LambdaMicrovmsControlPlane.fromRegion(
      process.env.AWS_REGION || process.env.AWS_DEFAULT_REGION || undefined,
    );
    const templateArn = await controlPlane.resolveTemplateArn(process.env.RAYITO_TEMPLATE ?? "");
    const client = new Client({ name: "rayito-e2e-stdio", version: "0.0.0" });
    await client.connect(
      new StdioClientTransport({
        command: process.execPath,
        args: [SERVER_ENTRY],
        env: serverEnvironment(),
        stderr: "inherit",
      }),
    );
    let sandboxId: string | undefined;
    try {
      sandboxId = await exerciseTools(client, templateArn);
    } finally {
      await client.close();
    }
    await waitForTerminalState(sandboxId, controlPlane);
  });

  test("seis herramientas por --http y el sandbox termina al parar el servidor", async () => {
    const controlPlane = LambdaMicrovmsControlPlane.fromRegion(
      process.env.AWS_REGION || process.env.AWS_DEFAULT_REGION || undefined,
    );
    const templateArn = await controlPlane.resolveTemplateArn(process.env.RAYITO_TEMPLATE ?? "");
    const port = await freePort();
    const stderr: string[] = [];
    const child: ChildProcess = spawn(
      process.execPath,
      [SERVER_ENTRY, "--http", "--port", String(port)],
      {
        env: serverEnvironment(),
        stdio: ["ignore", "ignore", "pipe"],
      },
    );
    child.stderr?.setEncoding("utf8").on("data", (chunk: string) => stderr.push(chunk));
    const exited = new Promise<number | null>((resolve) => child.once("exit", resolve));
    let sandboxId: string | undefined;
    try {
      await until(() => stderr.join("").includes("escuchando"), LISTEN_TIMEOUT_MS, "listening");
      const client = new Client({ name: "rayito-e2e-http", version: "0.0.0" });
      await client.connect(
        new StreamableHTTPClientTransport(new URL(`http://127.0.0.1:${port}/mcp`)),
      );
      try {
        sandboxId = await exerciseTools(client, templateArn);
      } finally {
        await client.close();
      }
    } finally {
      child.kill("SIGINT");
    }
    expect(
      await Promise.race([
        exited,
        new Promise<string>((resolve) => setTimeout(() => resolve("timeout"), EXIT_TIMEOUT_MS)),
      ]),
    ).toBe(0);
    await waitForTerminalState(sandboxId as string, controlPlane);
    expect(stderr.join("")).not.toContain("echo hola");
  });
});
