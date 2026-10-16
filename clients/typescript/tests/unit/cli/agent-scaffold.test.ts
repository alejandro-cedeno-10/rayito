/**
 * El andamiaje de `rayito agent init` contra los vectores compartidos
 * (`testdata/cli/agent-scaffold/cases.json`), los mismos que lee la CLI de
 * Python: las dos escriben exactamente los mismos ficheros.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { scaffoldFiles } from "../../../src/cli/agent-scaffold.js";

interface ScaffoldCase {
  readonly case: string;
  readonly request: {
    readonly name: string;
    readonly runtime: string;
    readonly language: string;
    readonly provider: string;
    readonly model_id: string;
    readonly region: string;
  };
  readonly files: Readonly<Record<string, string>>;
}

const VECTORS = join(import.meta.dirname, "../../../../../testdata/cli/agent-scaffold/cases.json");
const cases = JSON.parse(readFileSync(VECTORS, "utf8")) as ScaffoldCase[];

describe("agent scaffold", () => {
  it("tiene un caso por runtime", () => {
    expect(new Set(cases.map((entry) => entry.request.runtime))).toEqual(
      new Set([
        "opencode",
        "deepagents",
        "langgraph",
        "strands",
        "strands-harness",
        "hermes",
        "custom",
      ]),
    );
  });

  it.each(cases.map((entry) => [entry.case, entry] as const))(
    "escribe lo mismo que Python: %s",
    (_name, entry) => {
      const { request } = entry;
      const files = scaffoldFiles({
        name: request.name,
        runtime: request.runtime,
        language: request.language,
        provider: request.provider,
        modelId: request.model_id,
        region: request.region,
      });
      expect(files).toEqual(entry.files);
      expect(Object.keys(files)).toEqual(Object.keys(entry.files));
    },
  );
});
