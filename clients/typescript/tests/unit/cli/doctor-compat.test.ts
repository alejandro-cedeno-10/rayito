/**
 * La tabla de compatibilidad SDK ↔ rayd y sus evaluaciones contra
 * `testdata/cli/compatibility.json`, que Python comprueba con
 * `tests/unit/cli/test_compat.py`; y que `docs/site/docs/limits.md` publica
 * la misma tabla.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import {
  assess,
  COMPATIBILITY,
  parseSemver,
  rowFor,
  seriesOf,
} from "../../../src/cli/doctor/compat.js";
import { VERSION } from "../../../src/version.js";
import { loadCliTestdata } from "./paths.js";

interface SharedCompatibility {
  table: Array<{ sdk_series: string; min_agent_version: string; note: string }>;
  assessments: Array<{
    sdkVersion: string;
    agentVersion: string;
    status: string;
    reason: string;
    row: string | null;
  }>;
}

const shared = loadCliTestdata<SharedCompatibility>("compatibility.json");
const LIMITS_MD = join(
  import.meta.dirname,
  "..",
  "..",
  "..",
  "..",
  "..",
  "docs",
  "site",
  "docs",
  "limits.md",
);
const SECTION_HEADING = "## Compatibilidad SDK ↔ rayd ↔ imagen";

describe("compatibilidad SDK ↔ rayd", () => {
  it("la tabla es la de testdata/cli/compatibility.json", () => {
    expect(
      COMPATIBILITY.map((row) => ({
        sdk_series: row.sdkSeries,
        min_agent_version: row.minAgentVersion,
        note: row.note,
      })),
    ).toEqual(shared.table);
  });

  it.each(shared.assessments)("evalúa SDK $sdkVersion con agente $agentVersion", (vector) => {
    const assessment = assess(vector.sdkVersion, vector.agentVersion);
    expect(assessment.status).toBe(vector.status);
    expect(assessment.reason).toBe(vector.reason);
    expect(assessment.row?.sdkSeries ?? null).toBe(vector.row);
  });

  it("la tabla de limits.md es la misma que la del código", () => {
    const section = readFileSync(LIMITS_MD, "utf8").split(SECTION_HEADING)[1] ?? "";
    const rows = section
      .split("\n")
      .filter((line) => line.startsWith("|") && !/^[|\s-]+$/.test(line))
      .map((line) =>
        line
          .trim()
          .replace(/^\||\|$/g, "")
          .split("|")
          .map((cell) => cell.trim().replace(/^`|`$/g, "")),
      )
      .filter((cells) => cells[0] !== "SDK");
    expect(rows.map(([series, minimum, note]) => ({ series, minimum, note }))).toEqual(
      COMPATIBILITY.map((row) => ({
        series: row.sdkSeries,
        minimum: row.minAgentVersion,
        note: row.note,
      })),
    );
  });

  it("cada serie es MAJOR.MINOR y el SDK instalado tiene fila", () => {
    for (const row of COMPATIBILITY) {
      expect(row.sdkSeries).toMatch(/^\d+\.\d+$/);
      parseSemver(row.minAgentVersion);
    }
    const row = rowFor(seriesOf(parseSemver(VERSION)));
    expect(row).toBeDefined();
    expect(assess(VERSION, row?.minAgentVersion ?? "").status).toBe("OK");
  });

  it("el parche puede divergir y un sufijo de pre-release se ignora", () => {
    expect(assess("0.1.3", "0.1.0").status).toBe("OK");
    expect(assess("0.1.0", "0.1.7-rc1").status).toBe("OK");
    expect(parseSemver("v1.2.3+build.5")).toEqual([1, 2, 3]);
  });

  it("una versión mal formada es RangeError al leerla y FAIL al evaluarla", () => {
    expect(() => parseSemver("abc")).toThrow(/MAJOR\.MINOR\.PATCH/);
    const failed = assess("0.1.0", "abc");
    expect(failed.status).toBe("FAIL");
    expect(failed.row).toBeUndefined();
  });
});
