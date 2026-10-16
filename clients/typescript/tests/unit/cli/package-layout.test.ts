/**
 * El ejecutable `rayito` convive con la biblioteca sin romperla (ADR-027,
 * D1): `exports` no cambia, `sideEffects` sigue en `false` y la biblioteca
 * nunca importa el código de la CLI.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const PACKAGE_DIR = join(import.meta.dirname, "..", "..", "..");
const manifest = JSON.parse(readFileSync(join(PACKAGE_DIR, "package.json"), "utf8")) as {
  bin: Record<string, string>;
  exports: Record<string, unknown>;
  peerDependencies: Record<string, string>;
  peerDependenciesMeta: Record<string, { optional?: boolean }>;
  sideEffects: boolean;
};

describe("paquete npm", () => {
  it("declara los ejecutables rayito y rayito-mcp sobre un .mjs de dist", () => {
    expect(manifest.bin).toEqual({ rayito: "./dist/cli.mjs", "rayito-mcp": "./dist/mcp.mjs" });
  });

  it("no cambia las entradas públicas de la biblioteca", () => {
    expect(Object.keys(manifest.exports)).toEqual([".", "./e2b", "./package.json"]);
  });

  it("mantiene sideEffects en false", () => {
    expect(manifest.sideEffects).toBe(false);
  });

  it.each(["src/index.ts", "src/e2b/index.ts"])(
    "%s no importa la CLI ni el servidor MCP",
    (entry) => {
      const source = readFileSync(join(PACKAGE_DIR, entry), "utf8");
      expect(source).not.toMatch(/from\s+["'][^"']*\/(cli|mcp)\//);
    },
  );

  it.each(["@modelcontextprotocol/server", "zod"])("%s es un peer opcional", (peer) => {
    expect(manifest.peerDependencies[peer]).toBeDefined();
    expect(manifest.peerDependenciesMeta[peer]?.optional).toBe(true);
  });

  it("tsdown construye el ejecutable sólo en ESM", () => {
    const config = readFileSync(join(PACKAGE_DIR, "tsdown.config.ts"), "utf8");
    expect(config).toContain('cli: "src/cli/main.ts"');
    expect(config).toContain('mcp: "src/mcp/main.ts"');
    expect(config).not.toMatch(/cjs:[^}]*(cli|mcp)/s);
  });
});
