/**
 * `AgentFleet.connect(nombre)` con la receta de la flota (ADR-034 D2, D3):
 * sin `project` lee la receta (un `GetParameter`) y crea VMs iguales a los del
 * despliegue; con `project` o `useRecipe: false` no toca Parameter Store (la
 * traza de 0.16); `status()` dice de dónde sale el escalado y `stop()` borra
 * sólo la receta que leyó o guardó. También `deployAgent()` guardándola.
 * Espejo de `test_fleet_connect_recipe.py`. Sobre dobles: nada llama a AWS.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, test } from "vitest";
import type { DeployAws, SecretStorePort } from "../../src/agent/aws-resolve.js";
import { deployWith, planDeploy } from "../../src/agent/deploy.js";
import {
  AgentDeployOptions,
  type AgentDeployOptionsInput,
  type AgentDeployStep,
} from "../../src/agent/deploy-plan.js";
import { AgentFleet } from "../../src/agent/fleet.js";
import {
  decodeRecipe,
  encodeRecipe,
  type FleetRecipe,
  recipeDocument,
  recipeOf,
} from "../../src/agent/fleet-recipe.js";
import { AgentProject } from "../../src/agent/project.js";
import { ScalingOptions } from "../../src/agent/scaling-domain.js";
import {
  AgentCoreError,
  InvalidArgumentError,
  SandboxError,
  SecretError,
} from "../../src/errors.js";
import { FLEET_TOKEN, FleetHarness } from "./agent-fleet-fake.js";
import { MODEL_ID } from "./agent-project-fake.js";
import { denied, MemoryRecipeStore } from "./fleet-recipe-fake.js";

interface LoadFailure {
  readonly id: string;
  readonly awsCode: string | null;
  readonly warning: string;
}

const RECIPE_VECTORS = JSON.parse(
  readFileSync(join(import.meta.dirname, "../../../../testdata/fleet-recipe/recipes.json"), "utf8"),
) as { readonly loadFailures: readonly LoadFailure[] };

const NAME = "support-bot";
const PARAMETER = `/rayito/agents/${NAME}/recipe`;
const RECIPE_SCALING = new ScalingOptions({
  minVms: 2,
  maxVms: 5,
  warmVms: 0,
  scaleInAfterMs: 300_000,
});

function recipeProject(): AgentProject {
  return AgentProject.fromObject(
    {
      schema: "rayito.agent-project/1",
      name: NAME,
      runtime: { kind: "deepagents" },
      model: { provider: "bedrock", id: MODEL_ID, gateway: "bedrock", region: "us-east-1" },
      gateways: {
        bedrock: {
          preset: "bedrock",
          secret: "bedrock-key",
          region: "us-east-1",
          models: [MODEL_ID],
        },
      },
      env: { LOG_LEVEL: "debug" },
      network: { allow_out: ["api.example.com"] },
    },
    { baseDir: "." },
  );
}

function savedRecipe(renewGatewayKeys = false): FleetRecipe {
  return recipeOf(recipeProject(), {
    name: NAME,
    imageVersion: "7",
    scaling: RECIPE_SCALING,
    renewGatewayKeys,
  });
}

function storeWith(recipe?: FleetRecipe): MemoryRecipeStore {
  const store = new MemoryRecipeStore();
  if (recipe !== undefined) {
    store.values.set(PARAMETER, encodeRecipe(recipeDocument(recipe)) ?? "");
  }
  return store;
}

function connect(
  h: FleetHarness,
  options: Parameters<typeof AgentFleet.connect>[1] = {},
): Promise<AgentFleet> {
  return AgentFleet.connect(NAME, {
    accessToken: FLEET_TOKEN,
    directory: h.world,
    fetch: h.backend.fetch,
    now: h.clock.clock,
    sleep: h.clock.sleep,
    serviceOptions: {
      sandboxFactory: h.world,
      fetch: h.backend.fetch,
      sleep: h.clock.sleep,
      now: h.clock.clock,
    },
    ...options,
  });
}

/** Un renovador que siempre falla con `error`, y cuántas veces se llamó. */
function failingRenewer(error: Error): { calls: number; renew: () => Promise<boolean> } {
  const renewer = {
    calls: 0,
    renew: async (): Promise<boolean> => {
      renewer.calls += 1;
      throw error;
    },
  };
  return renewer;
}

async function failureOf(promise: Promise<unknown>): Promise<unknown> {
  try {
    await promise;
  } catch (error) {
    return error;
  }
  return undefined;
}

describe("connect by name", () => {
  test("without project it creates VMs from the recipe", async () => {
    const h = new FleetHarness();
    const store = storeWith(savedRecipe());
    const fleet = await connect(h, { recipeStore: store });
    const result = await fleet.invoke("hola");
    expect(result.text).toBe("respuesta a hola");
    expect(store.calls).toEqual([["load", PARAMETER]]);
    const created = h.world.created[0]?.options as Record<string, unknown>;
    expect(created.templateVersion).toBe("7");
    expect((created.envs as Record<string, string>).LOG_LEVEL).toBe("debug");
    expect((created.network as { allowOut: string[] }).allowOut).toEqual(["api.example.com"]);
    expect(Object.keys(created.gateways as object)).toEqual(["bedrock"]);
  });

  test("status reports the scaling of the recipe", async () => {
    const h = new FleetHarness();
    const fleet = await connect(h, { recipeStore: storeWith(savedRecipe()) });
    const status = await fleet.status();
    expect([status.minVms, status.maxVms, status.scalingSource]).toEqual([2, 5, "recipe"]);
    expect(fleet.scaling.scaleInAfterMs).toBe(300_000);
  });

  test("an explicit scaling wins over the recipe", async () => {
    const h = new FleetHarness();
    const fleet = await connect(h, {
      recipeStore: storeWith(savedRecipe()),
      scaling: new ScalingOptions({ minVms: 1, maxVms: 2 }),
    });
    const status = await fleet.status();
    expect([status.minVms, status.maxVms, status.scalingSource]).toEqual([1, 2, "explicit"]);
  });

  test("an explicit project never reads Parameter Store", async () => {
    const h = new FleetHarness();
    const store = storeWith(savedRecipe());
    const fleet = await connect(h, { recipeStore: store, project: recipeProject() });
    const status = await fleet.status();
    await fleet.stop();
    expect(store.calls).toEqual([]);
    expect(status.scalingSource).toBe("project");
  });

  test("useRecipe false is the 0.16 fleet", async () => {
    const h = new FleetHarness();
    const store = storeWith(savedRecipe());
    const fleet = await connect(h, { recipeStore: store, useRecipe: false });
    expect((await fleet.status()).scalingSource).toBe("default");
    expect(store.calls).toEqual([]);
    expect(await failureOf(fleet.invoke("hola"))).toBeInstanceOf(InvalidArgumentError);
  });

  test("without a recipe the fleet is the 0.16 one", async () => {
    const h = new FleetHarness();
    const store = storeWith();
    const fleet = await connect(h, { recipeStore: store });
    expect((await fleet.status()).scalingSource).toBe("default");
    await fleet.stop();
    expect(store.operations).toEqual(["load"]);
  });

  test("a recipe without permission warns once and is the 0.16 fleet", async () => {
    const h = new FleetHarness();
    const store = storeWith(savedRecipe());
    store.failures.set("load", denied());
    const warnings: unknown[] = [];
    const fleet = await connect(h, {
      recipeStore: store,
      logger: { warn: (message, fields) => warnings.push({ message, fields }) },
    });
    expect((await fleet.status()).scalingSource).toBe("default");
    expect(warnings).toEqual([
      { message: "receta de la flota", fields: { code: "recipe_access_denied" } },
    ]);
  });

  test.each(RECIPE_VECTORS.loadFailures)(
    "a Parameter Store failure ($id) is a warning and the 0.16 fleet",
    async ({ awsCode, warning }) => {
      const h = new FleetHarness();
      const store = storeWith(savedRecipe());
      store.failures.set(
        "load",
        new SandboxError("Parameter Store falló", awsCode === null ? {} : { awsCode }),
      );
      const warnings: unknown[] = [];
      const fleet = await connect(h, {
        recipeStore: store,
        logger: { warn: (message, fields) => warnings.push({ message, fields }) },
      });
      expect((await fleet.status()).scalingSource).toBe("default");
      expect(warnings).toEqual([{ message: "receta de la flota", fields: { code: warning } }]);
      await fleet.stop();
    },
  );

  test("stop deletes the recipe it read, unless it keeps it", async () => {
    const h = new FleetHarness();
    const store = storeWith(savedRecipe());
    await (await connect(h, { recipeStore: store })).stop();
    expect(store.operations).toEqual(["load", "delete"]);
    const kept = storeWith(savedRecipe());
    await (await connect(h, { recipeStore: kept })).stop({ keepRecipe: true });
    expect(kept.operations).toEqual(["load"]);
    expect(kept.values.has(PARAMETER)).toBe(true);
  });

  test("a renewing recipe without the generator peer still lists, stops and creates", async () => {
    const h = new FleetHarness();
    const keys = failingRenewer(
      new InvalidArgumentError("renewGatewayKeys necesita el paquete opcional"),
    );
    const warnings: unknown[] = [];
    const fleet = await connect(h, {
      recipeStore: storeWith(savedRecipe(true)),
      keyRenewer: keys,
      logger: { warn: (message, fields) => warnings.push({ message, fields }) },
    });
    await fleet.status();
    expect((await fleet.invoke("hola")).text).toBe("respuesta a hola");
    await fleet.scale({ minVms: 2, maxVms: 2 });
    await fleet.stop();
    expect(keys.calls).toBe(1);
    expect(warnings).toContainEqual({
      message: "la flota no renovó la clave de su pasarela (receta)",
      fields: { code: "missing_extra" },
    });
  });

  test("an inherited renewal without permission only warns", async () => {
    const h = new FleetHarness();
    const keys = failingRenewer(new SecretError("sin permiso", { code: "permission_denied" }));
    const fleet = await connect(h, {
      recipeStore: storeWith(savedRecipe(true)),
      keyRenewer: keys,
    });
    await fleet.invoke("hola");
    await fleet.scale({ minVms: 2, maxVms: 2 });
    expect(keys.calls).toBe(1);
  });

  test("an explicit renewal without permission fails", async () => {
    const h = new FleetHarness();
    const fleet = await connect(h, {
      recipeStore: storeWith(savedRecipe(true)),
      renewGatewayKeys: true,
      keyRenewer: failingRenewer(new SecretError("sin permiso", { code: "permission_denied" })),
    });
    expect(await failureOf(fleet.invoke("hola"))).toBeInstanceOf(SecretError);
  });

  test("concurrent renewals share the one in flight", async () => {
    const h = new FleetHarness();
    let calls = 0;
    const fleet = await connect(h, {
      recipeStore: storeWith(savedRecipe(true)),
      scaling: new ScalingOptions({ minVms: 3, maxVms: 3 }),
      keyRenewer: {
        renew: async () => {
          calls += 1;
          await Promise.resolve();
          return false;
        },
      },
    });
    await fleet.start();
    expect(calls).toBe(1);
  });

  test("an explicit renewGatewayKeys false wins over the recipe", async () => {
    const h = new FleetHarness();
    const fleet = await connect(h, {
      recipeStore: storeWith(savedRecipe(true)),
      renewGatewayKeys: false,
      keyRenewer: {
        renew: async () => {
          throw new Error("no debía renovar");
        },
      },
    });
    await fleet.invoke("hola");
  });
});

/** `deployWith()` sin build (la imagen ya existe) sobre los dobles de la
 * flota, con la tienda de recetas de la prueba. */
async function deployed(
  store: MemoryRecipeStore,
  options: AgentDeployOptionsInput = {},
): Promise<{ readonly fleet: AgentFleet; readonly steps: AgentDeployStep[] }> {
  const h = new FleetHarness();
  const steps: AgentDeployStep[] = [];
  const aws: DeployAws = {
    resolvedRegion: () => "us-east-1",
    accountId: async () => "123456789012",
    bucketExists: async () => false,
    artifactStackBucket: async () => undefined,
    imageExists: async () => true,
  };
  const secrets: SecretStorePort = {
    exists: async () => true,
    create: async () => undefined,
  };
  const fleet = await deployWith(
    {
      aws,
      store: secrets,
      environ: {},
      serviceOptions: {
        sandboxFactory: h.world,
        fetch: h.backend.fetch,
        sleep: h.clock.sleep,
        now: h.clock.clock,
      },
      fleetOptions: {
        directory: h.world,
        fetch: h.backend.fetch,
        now: h.clock.clock,
        sleep: h.clock.sleep,
        recipeStore: store,
      },
    },
    ".",
    {
      project: recipeProject(),
      options: { buildImage: false, onStep: (step) => steps.push(step), ...options },
    },
  );
  return { fleet, steps };
}

describe("deploy and plan", () => {
  test("a deploy saves the recipe once after the fleet starts", async () => {
    const store = new MemoryRecipeStore();
    const { fleet, steps } = await deployed(store, {
      scaling: new ScalingOptions({ minVms: 1, maxVms: 3 }),
    });
    expect(store.calls).toEqual([["save", PARAMETER]]);
    expect(decodeRecipe(store.values.get(PARAMETER) ?? "").scaling.maxVms).toBe(3);
    expect(steps.map((step) => step.kind)).toEqual(["fleet"]);
    await fleet.stop();
  });

  test("saveRecipe false makes no SSM call", async () => {
    const store = new MemoryRecipeStore();
    await deployed(store, { saveRecipe: false });
    expect(store.calls).toEqual([]);
  });

  test("a recipe that cannot be saved never fails the deploy", async () => {
    const store = new MemoryRecipeStore();
    store.failures.set("save", denied());
    const { fleet, steps } = await deployed(store);
    expect(fleet.sandboxIds.length).toBe(1);
    expect(steps.at(-1)).toEqual({ kind: "recipe", detail: "recipe_access_denied" });
  });

  test("scale-in reaches the fleet and the recipe", async () => {
    const store = new MemoryRecipeStore();
    const { fleet } = await deployed(store, { scaleInAfterMs: 300_000 });
    expect(fleet.scaling.scaleInAfterMs).toBe(300_000);
    expect(decodeRecipe(store.values.get(PARAMETER) ?? "").scaling.scaleInAfterMs).toBe(300_000);
  });

  test("starting a fleet directly saves nothing", async () => {
    const h = new FleetHarness();
    const store = new MemoryRecipeStore();
    const fleet = h.fleet(undefined, { project: recipeProject(), recipeStore: store });
    await fleet.start();
    await fleet.stop();
    expect(store.calls).toEqual([]);
  });

  test("the deploy-time save writes the recipe of the running fleet", async () => {
    const h = new FleetHarness();
    const store = new MemoryRecipeStore();
    const fleet = h.fleet(new ScalingOptions({ minVms: 1, maxVms: 3 }), {
      project: recipeProject(),
      recipeStore: store,
    });
    await fleet.start();
    expect(await fleet.saveRecipeForDeploy()).toBe("saved");
    const recipe = decodeRecipe(store.values.get(PARAMETER) ?? "");
    expect(recipe.scaling.maxVms).toBe(3);
    expect(recipe.gatewayPresets).toEqual({ bedrock: "bedrock" });
    expect((recipe.project.env as Record<string, string>).LOG_LEVEL).toBe("debug");
    await fleet.stop();
    expect(store.operations).toEqual(["save", "delete"]);
  });

  test("a scale-in below the minimum fails before anything", () => {
    expect(() => new AgentDeployOptions({ scaleInAfterMs: 10_000 })).toThrow(InvalidArgumentError);
  });

  test("the plan shows the recipe step", async () => {
    expect((await planDeploy(".", { project: recipeProject() })).recipe).toBe("save");
    const off = await planDeploy(".", { project: recipeProject(), options: { saveRecipe: false } });
    expect(off.recipe).toBe("off");
  });

  test.each([
    [{ saveRecipe: false }, "saveRecipe"],
    [{ renewGatewayKeys: true }, "renewGatewayKeys"],
    [{ scaleInAfterMs: 300_000 }, "scaleInAfterMs"],
  ])("%o does not apply to agentcore", async (options, name) => {
    const caught = await failureOf(
      planDeploy(".", { project: recipeProject(), options, target: "agentcore" }),
    );
    expect(caught).toBeInstanceOf(AgentCoreError);
    expect((caught as AgentCoreError).code).toBe("option_not_supported");
    expect((caught as Error).message).toContain(name);
  });
});
