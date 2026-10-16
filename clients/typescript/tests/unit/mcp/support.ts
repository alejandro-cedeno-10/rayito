/**
 * Cableado de los tests del servidor MCP: un `rayd` falso y un plano de
 * control falso detrás de `createMcpApp`, y el cliente oficial del SDK de MCP
 * conectado por un par de transportes en memoria. Espejo de
 * `clients/python/tests/unit/mcp_support.py`. El `rayd` falso verifica
 * `x-access-token` y el servidor no acepta un token explícito, así que los
 * tests lo inyectan por `RAYITO_ACCESS_TOKEN`, la variable que `create()`
 * ya lee.
 */

import { Client } from "@modelcontextprotocol/client";
import { InMemoryTransport } from "@modelcontextprotocol/server";
import { afterEach, vi } from "vitest";
import { createStderrLogger } from "../../../src/mcp/logging.js";
import { loadMcpPeers, type McpPeers } from "../../../src/mcp/peers.js";
import { createMcpApp, type McpApp } from "../../../src/mcp/server.js";
import { McpSettings } from "../../../src/mcp/settings.js";
import { ACCESS_TOKEN_ENV_VAR } from "../../../src/sandbox/launch.js";
import { FakeControlPlane, IMAGE_ARN } from "../fake/control-plane.js";
import type { FakeRayd } from "../fake/server.js";
import { ACCESS_TOKEN, startRayd } from "../helpers.js";

const CLIENT_INFO = { name: "rayito-test-client", version: "0.0.0" };

export const FAST_TRANSPORT = { scheme: "http" as const, pingIdleConnection: false };

export interface Fixture {
  readonly app: McpApp;
  readonly rayd: FakeRayd;
  readonly plane: FakeControlPlane;
  readonly stderr: string[];
  readonly peers: McpPeers;
  connect(): Promise<Client>;
}

export interface FixtureOptions {
  readonly settings?: McpSettings;
}

const cleanups: Array<() => Promise<void>> = [];

afterEach(async () => {
  vi.unstubAllEnvs();
  for (const cleanup of cleanups.splice(0).reverse()) {
    await cleanup();
  }
});

export async function startFixture(options: FixtureOptions = {}): Promise<Fixture> {
  vi.stubEnv(ACCESS_TOKEN_ENV_VAR, ACCESS_TOKEN);
  const peers = await loadMcpPeers();
  const rayd = await startRayd();
  cleanups.push(() => rayd.close());
  const plane = new FakeControlPlane({ endpoint: rayd.host });
  const settings = options.settings ?? new McpSettings({ template: IMAGE_ARN, idleSeconds: 0 });
  const stderr: string[] = [];
  const logger = createStderrLogger(settings.logLevel, { write: (line) => stderr.push(line) });
  const app = createMcpApp(peers, settings, {
    controlPlane: plane,
    transport: rayd.transport,
    logger,
  });
  cleanups.push(() => app.lease.close());
  return {
    app,
    rayd,
    plane,
    stderr,
    peers,
    connect: () => connectClient(app),
  };
}

export async function connectClient(app: McpApp): Promise<Client> {
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  const server = app.createServer();
  await server.connect(serverTransport);
  const client = new Client(CLIENT_INFO);
  await client.connect(clientTransport);
  cleanups.push(async () => {
    await client.close();
    await server.close();
  });
  return client;
}

export interface ToolOutcome {
  readonly isError?: boolean | undefined;
  readonly content: ReadonlyArray<{ readonly type: string; readonly [key: string]: unknown }>;
  readonly structuredContent?: unknown;
}

export function firstText(result: ToolOutcome): string {
  const first = result.content[0];
  if (first?.type !== "text") {
    throw new Error("el primer bloque no es de texto");
  }
  return first.text as string;
}

export function firstJson(result: ToolOutcome): Record<string, unknown> {
  return JSON.parse(firstText(result)) as Record<string, unknown>;
}

export function errorText(result: ToolOutcome): string {
  if (result.isError !== true) {
    throw new Error("se esperaba un resultado isError");
  }
  return firstText(result);
}
