/**
 * Los peers opcionales del servidor MCP (ADR-027, D5): el SDK oficial
 * `@modelcontextprotocol/server` (v2) y `zod`. Se cargan bajo demanda con
 * `loadOptionalPeer`, nunca de forma estática: `rayito` sin ellos instalados
 * sigue funcionando y sólo `rayito-mcp` los necesita.
 */

import { loadOptionalPeer } from "../optional.js";

export type McpSdk = typeof import("@modelcontextprotocol/server");
export type McpStdio = typeof import("@modelcontextprotocol/server/stdio");
export type Zod = typeof import("zod");

export const MCP_SDK_PACKAGE = "@modelcontextprotocol/server";
export const MCP_STDIO_SPECIFIER = "@modelcontextprotocol/server/stdio";
export const ZOD_PACKAGE = "zod";
export const MCP_FEATURE = "rayito-mcp";

/** La orden que arranca el servidor sin instalar nada, para el mensaje de peer ausente. */
export const MCP_NPX_COMMAND = `npx -y -p rayito -p ${MCP_SDK_PACKAGE} -p ${ZOD_PACKAGE} rayito-mcp`;

export interface McpPeers {
  readonly sdk: McpSdk;
  readonly stdio: McpStdio;
  readonly zod: Zod;
}

export async function loadMcpPeers(): Promise<McpPeers> {
  const [sdk, stdio, zod] = await Promise.all([
    loadOptionalPeer<McpSdk>(MCP_SDK_PACKAGE, MCP_FEATURE),
    loadOptionalPeer<McpStdio>(MCP_STDIO_SPECIFIER, MCP_FEATURE),
    loadOptionalPeer<Zod>(ZOD_PACKAGE, MCP_FEATURE),
  ]);
  return { sdk, stdio, zod };
}
