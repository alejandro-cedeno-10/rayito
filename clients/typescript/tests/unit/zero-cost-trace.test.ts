/**
 * Traza dorada de "coste cero" (espejo de `test_m15_zero_cost.py`): con
 * ninguna opción de coste activada, `create -> commands.run -> files.write ->
 * pause -> resume -> commands.run -> kill -> list` produce exactamente la
 * misma secuencia de operaciones del plano de control (con el
 * `runHookPayload` de `RunMicrovm`) y de métodos gRPC que la traza congelada
 * de Python (`fixtures/zero_cost_0_5_trace.json` del SDK Python: un único
 * fichero para los dos SDKs). Cualquier cambio que la rompa
 * debe justificar por qué el SDK, sin activar nada, deja de hacer lo mismo.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, test } from "vitest";
import { Sandbox } from "../../src/sandbox/sandbox.js";
import type { FakeControlPlane } from "./fake/control-plane.js";
import { createTestSandbox } from "./helpers.js";

interface GoldenTrace {
  readonly boto3_operations: readonly string[];
  readonly run_microvm_payload: Readonly<Record<string, unknown>>;
  readonly grpc_methods: readonly string[];
}

const GOLDEN = JSON.parse(
  readFileSync(
    join(
      import.meta.dirname,
      "..",
      "..",
      "..",
      "python",
      "tests",
      "unit",
      "fixtures",
      "zero_cost_0_5_trace.json",
    ),
    "utf8",
  ),
) as GoldenTrace;

/** Búsqueda local del ARN de una plantilla: no es una operación del servicio. */
const NOT_A_SERVICE_OPERATION = new Set(["resolveTemplateArn"]);

/** Las operaciones del fake que se llaman distinto en el servicio de AWS. */
const SERVICE_OPERATION_NAMES: ReadonlyMap<string, string> = new Map([
  ["createAuthToken", "CreateMicrovmAuthToken"],
  ["listMicrovmsPage", "ListMicrovms"],
]);

/** `runMicrovm` -> `RunMicrovm`: el nombre de la operación del servicio. */
function operationName(operation: string): string {
  return (
    SERVICE_OPERATION_NAMES.get(operation) ?? operation.charAt(0).toUpperCase() + operation.slice(1)
  );
}

function controlPlaneOperations(plane: FakeControlPlane): string[] {
  return plane.calls
    .map((call) => call.operation)
    .filter((operation) => !NOT_A_SERVICE_OPERATION.has(operation))
    .map(operationName);
}

describe("traza dorada de coste cero", () => {
  test("el camino por defecto coincide con la traza congelada", async () => {
    const { sandbox, plane, rayd } = await createTestSandbox();
    await sandbox.commands.run("echo hola");
    await sandbox.files.write("/home/user/f.txt", "contenido");
    plane.setStates(["RUNNING", "SUSPENDED"]);
    await sandbox.pause();
    rayd.suspend();
    rayd.resume();
    plane.setStates(["RUNNING"]);
    await sandbox.resume();
    await sandbox.commands.run("echo de nuevo");
    await sandbox.kill();
    for await (const item of Sandbox.list({ controlPlane: plane })) {
      expect(item).toBeDefined();
    }

    const launched = plane.launches[0];
    expect(launched).toBeDefined();
    expect({
      boto3_operations: controlPlaneOperations(plane),
      run_microvm_payload: JSON.parse(launched?.runHookPayload ?? "{}"),
      grpc_methods: rayd.paths,
    }).toEqual(GOLDEN);
  });
});

describe("fleet recipe and key renewal outside deployAgent", () => {
  test("building, starting, reconnecting and stopping a fleet reach no recipe or key", async () => {
    const { AgentFleet } = await import("../../src/agent/fleet.js");
    const { planDeploy } = await import("../../src/agent/deploy.js");
    const { FLEET_TOKEN, FleetHarness } = await import("./agent-fleet-fake.js");
    const { project } = await import("./agent-project-fake.js");
    const { MemoryRecipeStore } = await import("./fleet-recipe-fake.js");
    const h = new FleetHarness();
    const store = new MemoryRecipeStore();
    const renewals: string[] = [];
    const keyRenewer = {
      renew: async () => {
        renewals.push("renew");
        return false;
      },
    };
    const fleet = h.fleet(undefined, { recipeStore: store, keyRenewer });
    await fleet.start();
    await fleet.status();
    await fleet.stop();
    for (const options of [{ project: project() }, { useRecipe: false }]) {
      const reconnected = await AgentFleet.connect("support-bot", {
        accessToken: FLEET_TOKEN,
        directory: h.world,
        recipeStore: store,
        keyRenewer,
        ...options,
      });
      await reconnected.stop();
    }
    await planDeploy(".", { project: project() });
    expect(store.calls).toEqual([]);
    expect(renewals).toEqual([]);
  });
});
