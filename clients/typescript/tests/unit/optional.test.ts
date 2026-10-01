/**
 * `loadOptionalPeer`: sólo se resuelve un peer opcional cuando ya se activó
 * una función (ADR-014).
 */

import { mkdirSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterAll, beforeAll, describe, expect, test } from "vitest";
import { InvalidArgumentError } from "../../src/errors.js";
import { loadOptionalPeer } from "../../src/optional.js";

const MISSING_SPECIFIER = "rayito-test-definitely-not-installed";

const MISSING_NESTED_IMPORT_FIXTURE = new URL(
  "./fixtures/optional-peer-with-missing-nested-import.mjs",
  import.meta.url,
).href;

// Peers opcionales de AWS SDK v3 (secretos, M13a; índice de metadatos, M14):
// ya están en `peerDependencies` (opcionales), pero `src/**/*.ts` no debe
// importarlos de forma estática nunca: sólo `loadOptionalPeer` los carga, con
// `import()` dinámico, y sólo dentro de la función ya activada por su opción. `@opentelemetry/api`
// (M13b) ya es una dependencia real, pero sólo como tipo: la comprueba el
// describe de más abajo, con su propia regla (permite `import type`).
const LAZY_OPTIONAL_PEERS = ["@aws-sdk/client-secrets-manager", "@aws-sdk/client-dynamodb"];

// Peer "instalado" de verdad bajo node_modules (resuelto por especificador
// desnudo, como `@aws-sdk/client-dynamodb`), cuyo index reexporta un paquete
// que no existe. Node nombra en el mensaje la ruta del importador, que
// contiene el propio especificador del peer.
const FAKE_SCOPE_DIR = fileURLToPath(
  new URL("../../node_modules/@rayito-test-fake", import.meta.url),
);
const FAKE_PEER = "@rayito-test-fake/peer";
const FAKE_NESTED = "rayito-test-fake-nested-missing-pkg";

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

  describe("con un peer desnudo instalado en node_modules", () => {
    beforeAll(() => {
      const peerDir = join(FAKE_SCOPE_DIR, "peer");
      mkdirSync(peerDir, { recursive: true });
      writeFileSync(
        join(peerDir, "package.json"),
        JSON.stringify({ name: FAKE_PEER, type: "module", main: "index.js" }),
      );
      writeFileSync(join(peerDir, "index.js"), `export * from "${FAKE_NESTED}";\n`);
    });

    afterAll(() => {
      rmSync(FAKE_SCOPE_DIR, { recursive: true, force: true });
    });

    test("a broken transitive dependency of an installed bare peer propagates the original error", async () => {
      await expect(loadOptionalPeer(FAKE_PEER, "una función")).rejects.toSatisfy(
        (error: unknown) => {
          expect(error).not.toBeInstanceOf(InvalidArgumentError);
          expect((error as { code?: unknown }).code).toBe("ERR_MODULE_NOT_FOUND");
          expect((error as Error).message).toContain(FAKE_NESTED);
          return true;
        },
      );
    });

    test("a missing subpath of an uninstalled scoped peer is still reported as a missing peer", async () => {
      await expect(
        loadOptionalPeer("@rayito-test-fake-absent/peer/sub", "una función"),
      ).rejects.toBeInstanceOf(InvalidArgumentError);
    });
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

describe("peerDependencies opcionales cargadas sólo con loadOptionalPeer", () => {
  test("ningún fichero de src/ importa de forma estática un peer opcional de AWS SDK", () => {
    const offenders: string[] = [];
    for (const file of listTsFilesRecursively(SRC_ROOT)) {
      const content = readFileSync(file, "utf-8");
      for (const peer of LAZY_OPTIONAL_PEERS) {
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

describe("@opentelemetry/api sólo se importa como tipo", () => {
  test("todo import/export/require de @opentelemetry/api en src/ es `import type` (se borra en el build, M13b)", () => {
    const offenders: string[] = [];
    const pattern =
      /(^|\n)\s*(import(?:\s+type)?|export(?:\s+type)?)\b[^\n]*['"]@opentelemetry\/api['"]|require\(\s*['"]@opentelemetry\/api['"]\s*\)/g;
    for (const file of listTsFilesRecursively(SRC_ROOT)) {
      const content = readFileSync(file, "utf-8");
      for (const match of content.matchAll(pattern)) {
        const statement = match[0].trim();
        if (!/^import\s+type\b/.test(statement)) {
          offenders.push(`${file} -> ${statement}`);
        }
      }
    }
    expect(offenders).toEqual([]);
  });
});
