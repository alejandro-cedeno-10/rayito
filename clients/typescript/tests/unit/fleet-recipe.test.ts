/**
 * La receta de la flota (ADR-034 D1-D2) contra los vectores compartidos con
 * Python (`testdata/fleet-recipe/recipes.json` y `contract.json`): los bytes
 * exactos del JSON, el sobre `gzip+base64` que escribe cada SDK y lee el otro,
 * lo que se rechaza y el nombre del parámetro. Espejo de `test_fleet_recipe.py`.
 */

import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, test } from "vitest";
import {
  decodeRecipe,
  encodeRecipe,
  RECIPE_ENCODING_GZIP,
  RECIPE_UNREADABLE,
  RECIPE_WARNING_CODES,
  RecipeWarning,
  recipeDocument,
  recipeFromDocument,
  recipeOf,
  recipeParameterName,
  recipeProject,
} from "../../src/agent/fleet-recipe.js";
import { loadRecipe, RECIPE_SAVED, storeRecipe } from "../../src/agent/fleet-recipe-aws.js";
import { ScalingOptions } from "../../src/agent/scaling-domain.js";
import * as limits from "../../src/limits.js";
import { denied, MemoryRecipeStore } from "./fleet-recipe-fake.js";

const TESTDATA = join(import.meta.dirname, "..", "..", "..", "..", "testdata", "fleet-recipe");
const VECTORS = JSON.parse(readFileSync(join(TESTDATA, "recipes.json"), "utf8"));
const CONTRACT = JSON.parse(readFileSync(join(TESTDATA, "contract.json"), "utf8"));

interface RecipeCase {
  readonly id: string;
  readonly document?: Record<string, unknown>;
  readonly instructions?: {
    readonly repeat?: string;
    readonly times?: number;
    readonly sha256HexChars?: number;
  };
  readonly encoding: string | null;
  readonly value?: string | null;
  readonly warning?: string;
}

const CASES = new Map<string, RecipeCase>(
  (VECTORS.cases as RecipeCase[]).map((item) => [item.id, item]),
);
const SMALL = CASES.get("small-bedrock")?.document as Record<string, unknown>;

function sha256Hex(chars: number): string {
  let text = "";
  for (let index = 0; text.length < chars; index += 1) {
    text += createHash("sha256").update(String(index)).digest("hex");
  }
  return text.slice(0, chars);
}

function documentOf(item: RecipeCase): Record<string, unknown> {
  if (item.document !== undefined) {
    return item.document;
  }
  const document = structuredClone(SMALL) as Record<string, Record<string, unknown>>;
  const spec = item.instructions ?? {};
  const text =
    spec.repeat === undefined
      ? sha256Hex(spec.sha256HexChars ?? 0)
      : spec.repeat.repeat(spec.times ?? 0);
  const project = document.project as Record<string, unknown>;
  project.agent = { ...(project.agent as object | undefined), instructions: text };
  return document;
}

function caseOf(id: string): RecipeCase {
  const found = CASES.get(id);
  if (found === undefined) {
    throw new Error(`falta el vector ${id}`);
  }
  return found;
}

describe("fleet recipe vectors", () => {
  test("the limits are the contract ones", () => {
    const expected = CONTRACT.limits;
    expect(limits.AGENT_RECIPE_PARAMETER_TEMPLATE).toBe(expected.agentRecipeParameterTemplate);
    expect(limits.AGENT_RECIPE_SCHEMA).toBe(expected.agentRecipeSchema);
    expect(limits.AGENT_RECIPE_MAX_BYTES).toBe(expected.agentRecipeMaxBytes);
    expect(limits.AGENT_RECIPE_DESCRIPTION).toBe(expected.agentRecipeDescription);
    expect(limits.GATEWAY_KEY_RENEW_AFTER_SECONDS).toBe(expected.gatewayKeyRenewAfterSeconds);
    expect(limits.GATEWAY_KEY_EXPIRY_SECONDS).toBe(expected.gatewayKeyExpirySeconds);
  });

  test("the warning codes are the contract ones", () => {
    const codes = (CONTRACT.warningCodes as string[]).filter((code) => code.startsWith("recipe_"));
    expect([...RECIPE_WARNING_CODES]).toEqual(codes);
  });

  test("a small recipe is byte for byte the value Python wrote", () => {
    const item = caseOf("small-bedrock");
    expect(item.encoding).toBe("json");
    expect(item.value).toEqual(expect.any(String));
    expect(encodeRecipe(SMALL)).toBe(item.value);
  });

  test("a large recipe becomes the gzip envelope and reads back", () => {
    const item = caseOf("large-instructions");
    const document = documentOf(item);
    const value = encodeRecipe(document) ?? "";
    const envelope = JSON.parse(value);
    expect(envelope.schema).toBe(limits.AGENT_RECIPE_SCHEMA);
    expect(envelope.encoding).toBe(RECIPE_ENCODING_GZIP);
    expect(Buffer.byteLength(value, "utf8")).toBeLessThanOrEqual(limits.AGENT_RECIPE_MAX_BYTES);
    expect(recipeDocument(decodeRecipe(value))).toEqual(document);
  });

  test.each(["small-bedrock", "large-instructions"])("the value of %s reads back", (id) => {
    const item = caseOf(id);
    const recipe = decodeRecipe(item.value ?? "");
    expect(recipeDocument(recipe)).toEqual(documentOf(item));
    expect(recipeProject(recipe).gatewayPresets).toEqual({ model: "bedrock" });
  });

  test("a recipe that does not fit even compressed is not encoded", () => {
    const item = caseOf("too-large");
    expect(item.warning).toBe("recipe_too_large");
    expect(encodeRecipe(documentOf(item))).toBeUndefined();
  });

  test.each(VECTORS.rejected as { id: string; value: string; warning: string }[])(
    "$id is unreadable",
    ({ value, warning }) => {
      let caught: unknown;
      try {
        decodeRecipe(value);
      } catch (error) {
        caught = error;
      }
      expect(caught).toBeInstanceOf(RecipeWarning);
      expect((caught as RecipeWarning).code).toBe(warning);
      expect(warning).toBe(RECIPE_UNREADABLE);
    },
  );

  test.each(VECTORS.names as { fleet: string; parameter: string | null }[])(
    "the parameter of $fleet",
    ({ fleet, parameter }) => {
      expect(recipeParameterName(fleet)).toBe(parameter ?? undefined);
    },
  );

  test("a recipe from a project keeps names and never secret values", () => {
    const project = recipeProject(recipeFromDocument(SMALL));
    const rebuilt = recipeOf(project, {
      name: "my-agent",
      imageVersion: "3",
      scaling: new ScalingOptions({ minVms: 1, maxVms: 3, warmVms: 1 }),
      renewGatewayKeys: false,
      savedBy: SMALL.savedBy as string,
    });
    expect(recipeDocument(rebuilt)).toEqual(SMALL);
    expect(encodeRecipe(recipeDocument(rebuilt))).toBe(caseOf("small-bedrock").value);
  });
});

describe("recipe store port", () => {
  test("store and load round trip", async () => {
    const store = new MemoryRecipeStore();
    expect(await storeRecipe(store, recipeFromDocument(SMALL))).toBe(RECIPE_SAVED);
    const loaded = await loadRecipe(store, "my-agent");
    expect(loaded === undefined ? undefined : recipeDocument(loaded.recipe)).toEqual(SMALL);
    expect(store.calls).toEqual([
      ["save", "/rayito/agents/my-agent/recipe"],
      ["load", "/rayito/agents/my-agent/recipe"],
    ]);
  });

  test("a too large recipe deletes the previous one", async () => {
    const store = new MemoryRecipeStore();
    store.values.set("/rayito/agents/my-agent/recipe", "{}");
    const recipe = recipeFromDocument(documentOf(caseOf("too-large")));
    expect(await storeRecipe(store, recipe)).toBe("recipe_too_large");
    expect(store.operations).toEqual(["delete"]);
    expect(store.values.size).toBe(0);
  });

  test("a save without permission is a warning with only the code", async () => {
    const store = new MemoryRecipeStore();
    store.failures.set("save", denied());
    const warnings: unknown[] = [];
    const logger = {
      warn: (message: string, fields?: unknown) => warnings.push([message, fields]),
    };
    expect(await storeRecipe(store, recipeFromDocument(SMALL), logger)).toBe(
      "recipe_access_denied",
    );
    expect(JSON.stringify(warnings)).not.toContain("/rayito/agents");
    expect(JSON.stringify(warnings)).toContain("recipe_access_denied");
  });

  test("a fleet name outside the SSM rule is not saved", async () => {
    const store = new MemoryRecipeStore();
    const recipe = recipeFromDocument({ ...SMALL, name: "bad name" });
    expect(await storeRecipe(store, recipe)).toBe("recipe_not_saved");
    expect(store.calls).toEqual([]);
  });

  test("an unreadable value loads as no recipe", async () => {
    const store = new MemoryRecipeStore();
    store.values.set("/rayito/agents/my-agent/recipe", "not json");
    expect(await loadRecipe(store, "my-agent")).toBeUndefined();
  });
});
