/**
 * Vectores compartidos con el SDK Python
 * (`testdata/templates/dockerignore-vectors.json`): la semántica de
 * `.dockerignore` de Docker. Espejo de `test_dockerignore_vectors.py`: si los
 * dos pasan, los dos SDKs empaquetan los mismos ficheros.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, test } from "vitest";
import { cleanPattern, DockerIgnore } from "../../src/templates/dockerignore.js";

interface Vectors {
  readonly cases: ReadonlyArray<{
    readonly name: string;
    readonly dockerignore: string;
    readonly excluded: readonly string[];
    readonly included: readonly string[];
  }>;
}

const VECTORS = JSON.parse(
  readFileSync(
    join(
      import.meta.dirname,
      "..",
      "..",
      "..",
      "..",
      "testdata",
      "templates",
      "dockerignore-vectors.json",
    ),
    "utf8",
  ),
) as Vectors;

/** Lo bastante largo para que un emparejado exponencial se note. */
const ADVERSARIAL_SEGMENTS = 2_000;
/** `NAME_MAX` de Linux/macOS: ningún segmento de ruta real es más largo. */
const NAME_MAX = 255;
/** Un emparejado acotado tarda milisegundos; el margen absorbe un CI lento. */
const LINEAR_BUDGET_MS = 2_000;

describe("dockerignore shared vectors", () => {
  test.each(VECTORS.cases)("$name", ({ dockerignore, excluded, included }) => {
    const ignore = DockerIgnore.fromText(dockerignore);
    expect(excluded.filter((path) => !ignore.matches(path))).toEqual([]);
    expect(included.filter((path) => ignore.matches(path))).toEqual([]);
  });

  test("cleanPattern matches filepath.Clean", () => {
    expect(cleanPattern("./a//b/../c/")).toBe("a/c");
    expect(cleanPattern("/")).toBe("");
    expect(cleanPattern("../x")).toBe("../x");
  });

  test("a hostile pattern cannot trigger exponential matching", () => {
    const globstars = `${Array(ADVERSARIAL_SEGMENTS).fill("**").join("/")}/x`;
    const interleaved = `${"**/a/".repeat(ADVERSARIAL_SEGMENTS / 2)}x`;
    const path = `${Array(ADVERSARIAL_SEGMENTS).fill("a").join("/")}/y`;
    const stars = "*a".repeat(ADVERSARIAL_SEGMENTS);
    const ignore = DockerIgnore.fromText(`${globstars}\n${interleaved}\n${stars}b\n`);
    const started = performance.now();
    expect(ignore.matches(path)).toBe(false);
    expect(ignore.matches("a".repeat(NAME_MAX))).toBe(false);
    expect(performance.now() - started).toBeLessThan(LINEAR_BUDGET_MS);
  });
});
