/**
 * Paridad del texto de coste de los componentes del panel: lo que declara
 * TypeScript es, palabra por palabra, lo que declara Python
 * (`testdata/stacks/cost-statements.json`, generado por
 * `scripts/gen_stack_cost_statements.py`).
 */

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { COMPONENTS } from "../../src/stacks/registry.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const REFERENCE = JSON.parse(
  readFileSync(resolve(HERE, "../../../../testdata/stacks/cost-statements.json"), "utf8"),
) as Record<
  string,
  {
    creates: string[];
    idleMonthly: string;
    perUse: string[];
    removal: string;
    source: string;
    parameters: Record<string, string>;
  }
>;

describe("stack cost statements", () => {
  it("covers every dashboard component of the reference file", () => {
    expect(Object.keys(REFERENCE).length).toBeGreaterThan(0);
    for (const name of Object.keys(REFERENCE)) {
      expect(
        COMPONENTS.find((component) => component.name === name),
        name,
      ).toBeDefined();
    }
  });

  for (const [name, expected] of Object.entries(REFERENCE)) {
    it(`${name} says the same in TypeScript as in Python`, () => {
      const component = COMPONENTS.find((candidate) => candidate.name === name);
      expect(component?.cost.creates).toEqual(expected.creates);
      expect(component?.cost.idleMonthly).toBe(expected.idleMonthly);
      expect(component?.cost.perUse ?? []).toEqual(expected.perUse);
      expect(component?.cost.removal).toBe(expected.removal);
      expect(component?.cost.source).toBe(expected.source);
      const described = Object.fromEntries(
        (component?.parameters ?? [])
          .filter((parameter) => parameter.name in expected.parameters)
          .map((parameter) => [parameter.name, parameter.description]),
      );
      expect(described).toEqual(expected.parameters);
      expect(Object.keys(described).sort()).toEqual(Object.keys(expected.parameters).sort());
    });
  }
});
