/**
 * El nombre de proyecto que sale de un directorio: vectores de
 * `testdata/agent-project/project-names.json`, los mismos que lee Python.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { projectNameOf } from "../../src/agent/project-file.js";

interface NameVectors {
  readonly maxLength: number;
  readonly defaultName: string;
  readonly cases: readonly { readonly directory: string; readonly name: string }[];
}

const VECTORS_PATH = join(
  import.meta.dirname,
  "../../../../testdata/agent-project/project-names.json",
);
const vectors = JSON.parse(readFileSync(VECTORS_PATH, "utf8")) as NameVectors;
const ROOT = "/project-name-vectors";

describe("projectNameOf", () => {
  it("usa los límites que declaran los vectores", () => {
    expect(vectors.maxLength).toBe(63);
    expect(vectors.defaultName).toBe("my-agent");
  });

  it.each(vectors.cases)("$directory -> $name", ({ directory, name }) => {
    expect(projectNameOf(join(ROOT, directory))).toBe(name);
  });
});
