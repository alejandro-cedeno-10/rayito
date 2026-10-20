/**
 * `rayito/cdk` no cuesta nada a quien no lo usa ni al sintetizar (ADR-032
 * D8 y D14): ninguna entrada del paquete (`index`, `e2b`, `cli`, `mcp`)
 * alcanza `src/cdk/` ni importa `aws-cdk-lib` o `constructs`, sólo
 * `src/cdk/` los importa, y sintetizar los casos del contrato sin
 * credenciales no carga ningún cliente de AWS. `pack-check.mjs` comprueba lo
 * mismo sobre `dist/`.
 */

import { mkdtempSync, readdirSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, relative, resolve } from "node:path";
import { describe, expect, it, vi } from "vitest";
import { loadCases, synthesizeCase } from "./cdk-contract-harness.js";

const observed = vi.hoisted(() => ({ peers: [] as string[] }));

vi.mock("../../src/optional.js", async (importOriginal) => {
  const original = await importOriginal<typeof import("../../src/optional.js")>();
  return {
    ...original,
    loadOptionalPeer: vi.fn((specifier: string, feature: string) => {
      observed.peers.push(specifier);
      return original.loadOptionalPeer(specifier, feature);
    }),
  };
});

const SRC = join(import.meta.dirname, "..", "..", "src");
const CDK_DIR = join(SRC, "cdk");
const ENTRIES = ["index.ts", "e2b/index.ts", "cli/main.ts", "mcp/main.ts"];
const CDK_PACKAGE = /^(?:aws-cdk-lib(?:\/.*)?|constructs)$/;
const IMPORT_SPECIFIER =
  /(?:import|export)\s+(?:type\s+)?(?:[^"';]*?\s+from\s+)?["']([^"']+)["']|import\(\s*["']([^"']+)["']\s*\)/g;

function specifiers(
  file: string,
): Array<{ readonly specifier: string; readonly typeOnly: boolean }> {
  const text = readFileSync(file, "utf8");
  return [...text.matchAll(IMPORT_SPECIFIER)].map((match) => ({
    specifier: (match[1] ?? match[2]) as string,
    typeOnly: /^(?:import|export)\s+type\s/.test(match[0]),
  }));
}

function resolveLocal(from: string, specifier: string): string {
  return resolve(dirname(from), specifier.replace(/\.js$/, ".ts"));
}

/** Los ficheros de `src/` que una entrada alcanza en tiempo de ejecución. */
function reachable(entry: string): Set<string> {
  const seen = new Set<string>();
  const pending = [join(SRC, entry)];
  for (let file = pending.pop(); file !== undefined; file = pending.pop()) {
    if (seen.has(file)) {
      continue;
    }
    seen.add(file);
    for (const { specifier, typeOnly } of specifiers(file)) {
      if (!typeOnly && specifier.startsWith(".")) {
        pending.push(resolveLocal(file, specifier));
      }
    }
  }
  return seen;
}

function sources(directory: string): string[] {
  return readdirSync(directory, { recursive: true, withFileTypes: true })
    .filter((entry) => entry.isFile() && entry.name.endsWith(".ts"))
    .map((entry) => join(entry.parentPath, entry.name));
}

describe("rayito/cdk: coste cero", () => {
  for (const entry of ENTRIES) {
    it(`${entry} no alcanza src/cdk ni importa aws-cdk-lib o constructs`, () => {
      const files = [...reachable(entry)];
      expect(files.length).toBeGreaterThan(1);
      expect(files.filter((file) => file.startsWith(`${CDK_DIR}/`))).toEqual([]);
      const offenders = files.filter((file) =>
        specifiers(file).some(({ specifier }) => CDK_PACKAGE.test(specifier)),
      );
      expect(offenders.map((file) => relative(SRC, file))).toEqual([]);
    });
  }

  it("sólo src/cdk importa aws-cdk-lib o constructs", () => {
    const offenders = sources(SRC)
      .filter((file) => !file.startsWith(`${CDK_DIR}/`))
      .filter((file) => specifiers(file).some(({ specifier }) => CDK_PACKAGE.test(specifier)));
    expect(offenders.map((file) => relative(SRC, file))).toEqual([]);
  });

  it("sintetizar todos los casos sin credenciales no carga ningún cliente de AWS", () => {
    for (const name of ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_PROFILE"]) {
      vi.stubEnv(name, "");
    }
    try {
      for (const [, testCase] of loadCases()) {
        if (testCase.expected !== "template") {
          continue;
        }
        const scratch = mkdtempSync(join(tmpdir(), "rayito-cdk-zero-"));
        try {
          synthesizeCase(testCase, scratch);
        } finally {
          rmSync(scratch, { recursive: true, force: true });
        }
      }
      expect(observed.peers).toEqual([]);
    } finally {
      vi.unstubAllEnvs();
    }
  });
});
