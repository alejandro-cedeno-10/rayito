/**
 * La entrada `rayito/e2b` (D17): cada export resuelve, los alias son las
 * clases nativas y el empaquetado declara el subpath en `package.json`,
 * `tsdown.config.ts` y `scripts/pack-check.mjs`.
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, test } from "vitest";
import * as e2b from "../../src/e2b/index.js";
import * as native from "../../src/index.js";

const PACKAGE_DIR = fileURLToPath(new URL("../..", import.meta.url));

const VALUE_EXPORTS = [
  "Sandbox",
  "E2B",
  "ConnectionConfig",
  "ALL_TRAFFIC",
  "FileType",
  "FilesystemEventType",
  "Git",
  "getSignature",
  "Template",
  "Volume",
  "Secret",
] as const;

const ERROR_EXPORTS = [
  "SandboxError",
  "TimeoutError",
  "InvalidArgumentError",
  "NotEnoughSpaceError",
  "NotFoundError",
  "FileNotFoundError",
  "SandboxNotFoundError",
  "AuthenticationError",
  "GitAuthError",
  "GitUpstreamError",
  "TemplateError",
  "RateLimitError",
  "ServiceBusyError",
  "BuildError",
  "FileUploadError",
  "CommandExitError",
  "UnimplementedError",
] as const;

const exported: Readonly<Record<string, unknown>> = { ...e2b };
const nativeExported: Readonly<Record<string, unknown>> = { ...native };

function readText(relative: string): string {
  return readFileSync(`${PACKAGE_DIR}/${relative}`, "utf8");
}

describe("rayito/e2b exports", () => {
  test("every named value and error export resolves; the default export is Sandbox", () => {
    for (const name of [...VALUE_EXPORTS, ...ERROR_EXPORTS]) {
      expect(exported[name], name).toBeDefined();
    }
    expect(e2b.default).toBe(e2b.Sandbox);
    expect(e2b.ALL_TRAFFIC).toBe("0.0.0.0/0");
    for (const name of ["create", "connect", "kill", "getInfo", "list"] as const) {
      expect(typeof e2b.Sandbox[name], name).toBe("function");
    }
    expect(Object.keys(exported).filter((name) => name.includes("Pool"))).toEqual([]);
  });

  test("the aliases are the native bindings, so instanceof holds across entries", () => {
    expect(e2b.NotEnoughSpaceError).toBe(native.DiskFullError);
    expect(e2b.ServiceBusyError).toBe(native.CapacityError);
    expect(e2b.UnimplementedError).toBe(native.UnimplementedError);
    expect(e2b.FileUploadError).toBe(native.FileUploadError);
    expect(e2b.Git).toBe(native.Git);
    for (const name of ERROR_EXPORTS) {
      if (name in nativeExported) {
        expect(exported[name], name).toBe(nativeExported[name]);
      }
    }
    expect(new e2b.GitAuthError("x")).toBeInstanceOf(e2b.AuthenticationError);
    expect(new e2b.GitUpstreamError("x")).toBeInstanceOf(e2b.SandboxError);
    expect(new e2b.TemplateError("x")).toBeInstanceOf(e2b.SandboxError);
    const build = new e2b.BuildError("x");
    expect(build).toBeInstanceOf(Error);
    expect(build).not.toBeInstanceOf(e2b.SandboxError);
    expect(build.name).toBe("BuildError");
    expect(e2b.Sandbox).not.toBe(native.Sandbox);
  });

  test("package.json, tsdown and pack-check declare the ./e2b subpath", () => {
    const manifest = JSON.parse(readText("package.json")) as {
      exports: Record<string, unknown>;
      sideEffects: boolean;
    };
    expect(manifest.exports["./e2b"]).toEqual({
      types: "./dist/e2b.d.mts",
      import: "./dist/e2b.mjs",
      require: { types: "./dist/e2b.d.cts", default: "./dist/e2b.cjs" },
    });
    expect(manifest.sideEffects).toBe(false);
    expect(readText("tsdown.config.ts")).toContain(
      'entry: { index: "src/index.ts", e2b: "src/e2b/index.ts" }',
    );
    const packCheck = readText("scripts/pack-check.mjs");
    for (const file of ["e2b.mjs", "e2b.cjs", "e2b.d.mts", "e2b.d.cts"]) {
      expect(packCheck).toContain(`"package/dist/${file}"`);
    }
    expect(readText("src/e2b/index.ts")).toContain(
      '(Symbol as { asyncDispose?: symbol }).asyncDispose ??= Symbol.for("Symbol.asyncDispose");',
    );
  });
});
