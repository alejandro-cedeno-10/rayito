/**
 * `loadOptionalPeer`: sólo se resuelve un peer opcional cuando ya se activó
 * una función (ADR-014).
 */

import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, test } from "vitest";
import { InvalidArgumentError } from "../../src/errors.js";
import { loadOptionalPeer } from "../../src/optional.js";

const MISSING_SPECIFIER = "rayito-test-definitely-not-installed";

const MISSING_NESTED_IMPORT_FIXTURE = new URL(
  "./fixtures/optional-peer-with-missing-nested-import.mjs",
  import.meta.url,
).href;

// Peers opcionales que ningún grupo ha añadido todavía como dependencia real
// (los añadirán M13a/M13b/M14). `src/**/*.ts` no debe importarlos de forma
// estática nunca: sólo `loadOptionalPeer` los carga, con `import()` dinámico,
// y sólo dentro de la función ya activada por su opción.
const FUTURE_OPTIONAL_PEERS = [
  "@aws-sdk/client-secrets-manager",
  "@aws-sdk/client-dynamodb",
  "@opentelemetry/api",
];

const SRC_ROOT = fileURLToPath(new URL("../../src", import.meta.url));

function listTsFilesRecursively(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const fullPath = join(dir, entry.name);
    if (entry.isDirectory()) {
      return listTsFilesRecursively(fullPath);
    }
    return entry.name.endsWith(".ts") ? [fullPath] : [];
  });
}

describe("loadOptionalPeer", () => {
  test("a specifier that does not exist is rejected with InvalidArgumentError naming the package", async () => {
    await expect(loadOptionalPeer(MISSING_SPECIFIER, "las trazas OTel")).rejects.toSatisfy(
      (error: unknown) => {
        expect(error).toBeInstanceOf(InvalidArgumentError);
        const message = (error as Error).message;
        expect(message).toContain(MISSING_SPECIFIER);
        expect(message).toContain("las trazas OTel");
        expect(message).toContain(`npm install ${MISSING_SPECIFIER}`);
        return true;
      },
    );
  });

  test("the InvalidArgumentError chains the original resolution failure as its cause", async () => {
    await expect(loadOptionalPeer(MISSING_SPECIFIER, "una función")).rejects.toSatisfy(
      (error: unknown) => {
        expect((error as { cause?: unknown }).cause).toBeInstanceOf(Error);
        return true;
      },
    );
  });

  test("an existing specifier resolves", async () => {
    const module = await loadOptionalPeer<typeof import("node:util")>("node:util", "una función");
    expect(typeof module.inspect).toBe("function");
  });

  test("a missing nested dependency of an otherwise resolvable specifier propagates as-is, not as InvalidArgumentError", async () => {
    await expect(loadOptionalPeer(MISSING_NESTED_IMPORT_FIXTURE, "una función")).rejects.toSatisfy(
      (error: unknown) => {
        expect(error).not.toBeInstanceOf(InvalidArgumentError);
        const code = (error as { code?: unknown } | null)?.code;
        expect(code === "ERR_MODULE_NOT_FOUND" || code === "MODULE_NOT_FOUND").toBe(true);
        const message = (error as Error).message;
        expect(message).toContain("this-nested-dependency-does-not-exist");
        return true;
      },
    );
  });
});

// Escapa TODOS los metacaracteres de regex (no sólo `/`) antes de incrustar
// una cadena arbitraria en un patrón construido dinámicamente.
function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

describe("peerDependencies opcionales futuros", () => {
  test("ningún fichero de src/ importa de forma estática un peer opcional que aún no es dependencia real", () => {
    const offenders: string[] = [];
    for (const file of listTsFilesRecursively(SRC_ROOT)) {
      const content = readFileSync(file, "utf-8");
      for (const peer of FUTURE_OPTIONAL_PEERS) {
        const escapedPeer = escapeRegExp(peer);
        // Import/export estático o `require`; el `import()` dinámico de
        // `loadOptionalPeer` no cuenta como estático y no debe casar aquí.
        const staticImportPattern = new RegExp(
          `(^|\\n)\\s*(import|export)\\b[^\\n]*['"]${escapedPeer}['"]|require\\(\\s*['"]${escapedPeer}['"]\\s*\\)`,
        );
        if (staticImportPattern.test(content)) {
          offenders.push(`${file} -> ${peer}`);
        }
      }
    }
    expect(offenders).toEqual([]);
  });
});
