import { describe, expect, test } from "vitest";
import limits from "../../../../limits.json" with { type: "json" };
import * as rendered from "../../src/limits.js";

const SET_KEYS = new Set(["terminalStates", "suspendedStates", "managedNetworkConnectors"]);
const CONSTANT_NAME_OVERRIDES: Record<string, string> = {
  s3BucketNameMin: "S3_BUCKET_NAME_MIN",
  s3BucketNameMax: "S3_BUCKET_NAME_MAX",
};

/** Mirrors scripts/gen_limits.py: a service name with its own digit (s3) keeps it attached. */
function constantName(key: string): string {
  const override = CONSTANT_NAME_OVERRIDES[key];
  if (override !== undefined) {
    return override;
  }
  return key
    .replace(/([a-zA-Z])(\d)/g, "$1_$2")
    .replace(/(\d)([a-zA-Z])/g, "$1_$2")
    .replace(/([a-z])([A-Z])/g, "$1_$2")
    .toUpperCase();
}

function normalise(value: unknown): unknown {
  if (value instanceof Set) {
    return [...value].sort();
  }
  return value;
}

function expected(key: string, value: unknown): unknown {
  if (Array.isArray(value) && SET_KEYS.has(key)) {
    return [...value].sort();
  }
  return value;
}

describe("limits.ts mirrors limits.json", () => {
  const entries = Object.entries(limits as Record<string, unknown>);

  test.each(entries.map(([key, value]) => [constantName(key), key, value] as const))(
    "%s equals limits.json",
    (name, key, value) => {
      const actual = (rendered as Record<string, unknown>)[name];
      expect(actual, `${name} no existe en limits.ts`).toBeDefined();
      expect(normalise(actual), `${name} difiere de limits.json`).toEqual(expected(key, value));
    },
  );

  test("limits.ts exports nothing the JSON does not define", () => {
    const exported = Object.keys(rendered).sort();
    const fromJson = entries.map(([key]) => constantName(key)).sort();
    expect(exported).toEqual(fromJson);
  });

  test("values are the ones of rayito 0.2.0", () => {
    expect(rendered.MAX_DURATION_SECONDS).toBe(28800);
    expect(rendered.API_TPS.SuspendMicrovm).toBe(2);
    expect(rendered.TERMINAL_STATES.has("TERMINATED")).toBe(true);
    expect(rendered.TOKEN_REFRESH_AFTER_MINUTES).toBe(45);
  });
});
