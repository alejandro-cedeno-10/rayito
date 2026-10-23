/**
 * `SsmRecipeStore` (ADR-034, `AWS_API_NOTES.md` «Receta de la flota y claves de
 * corta duración de Bedrock») sobre un cliente de doble con la forma del SDK
 * v3: sólo las tres operaciones y los parámetros del contrato, y los errores
 * mapeados por código. Espejo de `test_fleet_recipe_aws.py`.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, test } from "vitest";
import { RecipeWarning } from "../../src/agent/fleet-recipe.js";
import { type SsmApi, SsmRecipeStore } from "../../src/agent/fleet-recipe-aws.js";
import { SandboxError } from "../../src/errors.js";
import { AGENT_RECIPE_DESCRIPTION } from "../../src/limits.js";

const PARAMETER = "/rayito/agents/my-agent/recipe";
const VALUE = '{"schema":"rayito.agent-recipe/1"}';
const CONTRACT = JSON.parse(
  readFileSync(
    join(import.meta.dirname, "..", "..", "..", "..", "testdata", "fleet-recipe", "contract.json"),
    "utf8",
  ),
);

class AwsFailure extends Error {
  constructor(name: string, message = "fallo") {
    super(message);
    this.name = name;
  }
}

class FakeSsm implements SsmApi {
  readonly calls: [string, Record<string, unknown>][] = [];
  readonly failures = new Map<string, Error>();
  value: string | undefined = VALUE;

  async putParameter(input: Parameters<SsmApi["putParameter"]>[0]): Promise<unknown> {
    return this.#call("PutParameter", input, {});
  }

  async getParameter(
    input: Parameters<SsmApi["getParameter"]>[0],
  ): Promise<{ Parameter?: { Value?: string | undefined } }> {
    return this.#call("GetParameter", input, { Parameter: { Value: this.value } });
  }

  async deleteParameter(input: { Name: string }): Promise<unknown> {
    return this.#call("DeleteParameter", input, {});
  }

  #call<T>(operation: string, input: object, response: T): T {
    this.calls.push([operation, { ...input }]);
    const failure = this.failures.get(operation);
    if (failure !== undefined) {
      throw failure;
    }
    return response;
  }
}

async function failureOf(promise: Promise<unknown>): Promise<unknown> {
  try {
    await promise;
  } catch (error) {
    return error;
  }
  return undefined;
}

describe("SsmRecipeStore", () => {
  test("save sends exactly the contract parameters", async () => {
    const client = new FakeSsm();
    await new SsmRecipeStore({ client }).save(PARAMETER, VALUE);
    const [[operation, input]] = client.calls as [[string, Record<string, unknown>]];
    expect(operation).toBe("PutParameter");
    expect(input).toEqual({
      Name: PARAMETER,
      Value: VALUE,
      Type: "SecureString",
      Tier: "Standard",
      Overwrite: true,
      Description: AGENT_RECIPE_DESCRIPTION,
    });
    expect(Object.keys(input).sort()).toEqual([...CONTRACT.awsOperations.ssm.PutParameter].sort());
  });

  test("load decrypts and delete sends only the name", async () => {
    const client = new FakeSsm();
    const store = new SsmRecipeStore({ client });
    expect(await store.load(PARAMETER)).toBe(VALUE);
    await store.delete(PARAMETER);
    expect(client.calls).toEqual([
      ["GetParameter", { Name: PARAMETER, WithDecryption: true }],
      ["DeleteParameter", { Name: PARAMETER }],
    ]);
    expect(CONTRACT.awsOperations.ssm.GetParameter).toEqual(["Name", "WithDecryption"]);
    expect(CONTRACT.awsOperations.ssm.DeleteParameter).toEqual(["Name"]);
  });

  test("a missing parameter is no recipe and deleting it is not an error", async () => {
    const client = new FakeSsm();
    client.failures.set("GetParameter", new AwsFailure("ParameterNotFound"));
    client.failures.set("DeleteParameter", new AwsFailure("ParameterNotFound"));
    const store = new SsmRecipeStore({ client });
    expect(await store.load(PARAMETER)).toBeUndefined();
    await store.delete(PARAMETER);
  });

  test.each([
    ["PutParameter", (store: SsmRecipeStore) => store.save(PARAMETER, VALUE)],
    ["GetParameter", (store: SsmRecipeStore) => store.load(PARAMETER)],
    ["DeleteParameter", (store: SsmRecipeStore) => store.delete(PARAMETER)],
  ])("access denied on %s is the access warning", async (operation, call) => {
    const client = new FakeSsm();
    client.failures.set(operation, new AwsFailure("AccessDeniedException"));
    const caught = await failureOf(call(new SsmRecipeStore({ client })));
    expect(caught).toBeInstanceOf(RecipeWarning);
    expect((caught as RecipeWarning).code).toBe("recipe_access_denied");
  });

  test.each([
    "ParameterLimitExceeded",
    "ParameterPatternMismatchException",
    "HierarchyLevelLimitExceededException",
    "TooManyUpdates",
    "ThrottlingException",
  ])("%s on save is not saved", async (code) => {
    const client = new FakeSsm();
    client.failures.set("PutParameter", new AwsFailure(code));
    const caught = await failureOf(new SsmRecipeStore({ client }).save(PARAMETER, VALUE));
    expect((caught as RecipeWarning).code).toBe("recipe_not_saved");
  });

  test("any other read error propagates without the AWS message", async () => {
    const client = new FakeSsm();
    client.failures.set("GetParameter", new AwsFailure("InternalServerError", PARAMETER));
    const caught = await failureOf(new SsmRecipeStore({ client }).load(PARAMETER));
    expect(caught).toBeInstanceOf(SandboxError);
    expect((caught as SandboxError).awsCode).toBe("InternalServerError");
    expect((caught as Error).message).not.toContain(PARAMETER);
    expect((caught as Error).message).toContain("ssm:GetParameter");
  });

  test("construction makes no call", () => {
    const client = new FakeSsm();
    new SsmRecipeStore({ client });
    expect(client.calls).toEqual([]);
  });
});
