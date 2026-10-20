/**
 * Los agentes de ejemplo de la aceptación de 0.14 (`testdata/agent-samples/`):
 * dieciséis proyectos que ya existían, sin código de rayito, cada uno con su
 * lockfile, y el manifiesto que comparten los tests de Python y de TypeScript.
 * Todo es puro: sólo se leen ficheros.
 */

import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, sep } from "node:path";
import { describe, expect, test } from "vitest";

const ROOT = join(import.meta.dirname, "..", "..", "..", "..", "testdata", "agent-samples");
const SAMPLE_COUNT = 16;
const SKIPPED_DIRS = new Set([".venv", "node_modules", "__pycache__"]);
const LOCKFILE_NAMES = new Set(["uv.lock", "package-lock.json"]);
const SOURCE_SUFFIXES = new Set([".py", ".js"]);
const CONFIG_SUFFIXES = new Set([".py", ".js", ".yaml", ".json"]);
const NODE_FLOOR = ">=22";
/**
 * Patrones con forma de credencial, armados por partes para que este fichero
 * no contenga ningún valor con esa forma.
 */
const SECRET_PATTERNS = [
  new RegExp(`AK${"IA"}[0-9A-Z]{16}`),
  new RegExp(`sk-${"[A-Za-z0-9_-]{20,}"}`),
  new RegExp(`gh${"p_"}[A-Za-z0-9]{20,}`),
  /\b[0-9]{12}\b/,
];

interface Sample {
  readonly id: string;
  readonly dir: string;
  readonly lockfile: string | null;
  readonly lockfileReason?: string;
  readonly tools: readonly string[];
}

interface Manifest {
  readonly schema: string;
  readonly custom: Record<string, number>;
  readonly samples: readonly Sample[];
}

const manifest = JSON.parse(readFileSync(join(ROOT, "manifest.json"), "utf8")) as Manifest;

function sourceFiles(directory: string): string[] {
  const found: string[] = [];
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    if (SKIPPED_DIRS.has(entry.name)) continue;
    const path = join(directory, entry.name);
    if (entry.isDirectory()) found.push(...sourceFiles(path));
    else found.push(path);
  }
  return found.sort();
}

const textFiles = (directory: string): string[] =>
  sourceFiles(directory).filter((path) => !LOCKFILE_NAMES.has(path.split(sep).at(-1) ?? ""));
const suffixOf = (path: string): string => path.slice(path.lastIndexOf("."));
const read = (path: string): string => readFileSync(path, "utf8");

describe("agent samples", () => {
  test("the manifest lists the sixteen samples once", () => {
    expect(manifest.samples).toHaveLength(SAMPLE_COUNT);
    expect(new Set(manifest.samples.map((sample) => sample.id)).size).toBe(SAMPLE_COUNT);
    expect(manifest.schema).toBe("rayito.agent-samples/1");
  });

  test("every directory is in the manifest", () => {
    const nested = ["python", "typescript"].flatMap((language) =>
      readdirSync(join(ROOT, language), { withFileTypes: true })
        .filter((entry) => entry.isDirectory() && !SKIPPED_DIRS.has(entry.name))
        .map((entry) =>
          relative(ROOT, join(ROOT, language, entry.name))
            .split(sep)
            .join("/"),
        ),
    );
    expect([...nested, "opencode"].sort()).toEqual(
      manifest.samples.map((sample) => sample.dir).sort(),
    );
  });

  describe.each(manifest.samples)("$id", (sample) => {
    const directory = join(ROOT, sample.dir);

    test("has its own lockfile or says why not", () => {
      expect(statSync(directory).isDirectory()).toBe(true);
      if (sample.lockfile === null) {
        expect(sample.lockfileReason).toBeTruthy();
        expect(
          sourceFiles(directory).some((path) => LOCKFILE_NAMES.has(path.split(sep).at(-1) ?? "")),
        ).toBe(false);
      } else {
        expect(existsSync(join(directory, sample.lockfile))).toBe(true);
      }
    });

    test("never mentions rayito", () => {
      for (const path of textFiles(directory))
        expect(read(path).toLowerCase()).not.toContain("rayito");
    });

    test("holds no secret-like value and no env file", () => {
      expect(sourceFiles(directory).some((path) => path.endsWith(`${sep}.env`))).toBe(false);
      for (const path of textFiles(directory)) {
        for (const pattern of SECRET_PATTERNS) expect(pattern.test(read(path))).toBe(false);
      }
    });

    test("declares each tool in its source", () => {
      const source = textFiles(directory)
        .filter((path) => SOURCE_SUFFIXES.has(suffixOf(path)))
        .map(read)
        .join("\n");
      for (const name of sample.tools) {
        expect(new RegExp(`\\b${name}\\b|name: "${name}"`).test(source)).toBe(true);
      }
    });

    test("reads its model from the environment or configures a Claude model", () => {
      const sources = textFiles(directory)
        .filter((path) => CONFIG_SUFFIXES.has(suffixOf(path)))
        .map(read);
      const readsEnv = sources.some((text) => text.includes("MODEL_ID"));
      const configured = sources.some((text) => text.includes("model") && text.includes("claude"));
      expect(readsEnv || configured).toBe(true);
    });
  });

  test("python samples pin their project name in the lock", () => {
    for (const sample of manifest.samples.filter((entry) => entry.lockfile === "uv.lock")) {
      const directory = join(ROOT, sample.dir);
      const name = /^name = "([^"]+)"/m.exec(read(join(directory, "pyproject.toml")))?.[1];
      expect(name).toBeDefined();
      expect(read(join(directory, "uv.lock"))).toContain(`name = "${name}"`);
    }
  });

  test("node samples declare the node floor and ESM", () => {
    for (const sample of manifest.samples.filter(
      (entry) => entry.lockfile === "package-lock.json",
    )) {
      const pkg = JSON.parse(read(join(ROOT, sample.dir, "package.json"))) as {
        type: string;
        engines: { node: string };
      };
      expect(pkg.type).toBe("module");
      expect(pkg.engines.node).toBe(NODE_FLOOR);
    }
  });

  test("the custom options of the second pass are the documented ones", () => {
    expect(manifest.custom).toEqual({
      memoryMib: 4096,
      maxSessions: 2,
      minVms: 1,
      maxVms: 2,
      warmVms: 1,
    });
  });
});
