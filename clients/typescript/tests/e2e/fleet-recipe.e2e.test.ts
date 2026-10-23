/**
 * La receta de la flota y las plazas calientes con pasarelas (ADR-034,
 * openspec `fleet-recipe`) contra AWS real, para Q191 y Q194 de
 * `AWS_API_NOTES.md`. Espejo de `test_fleet_recipe.py`: `deployAgent()` de un
 * ejemplo con `warmVms: 1`, sesiones dedicadas y plazas persistentes; otra
 * flota se reconecta sólo con el nombre y el token (`AgentFleet.connect`, que
 * lee la receta), invoca dos sesiones nuevas, sin id propio porque el host
 * sólo acepta los suyos (la segunda cae en la plaza aparcada y
 * necesita la pasarela del modelo), `status()` da el escalado de la receta y
 * `stop()` borra la receta. Q193 (la renovación de claves) se prueba una vez,
 * en Python.
 *
 * Variables: las de `agent-zero-config-support.ts` (`RAYITO_E2E=1`,
 * `RAYITO_E2E_RUN_ID`, `RAYITO_E2E_ZC_BUCKET`, `RAYITO_E2E_ZC_BASE`,
 * `RAYITO_E2E_BEDROCK_SECRET`) más `RAYITO_E2E_RECIPE=1`. Coste: dos VMs de
 * 4 GB unos minutos y una versión de imagen (menos de 0,30 USD). Todo se para
 * en `finally`.
 */

import { randomBytes } from "node:crypto";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { AgentFleet, deployAgent, ScalingOptions } from "../../src/index.js";
import { JsonFilePoolBackend } from "../../src/pool/backend.js";
import {
  BASE,
  BEDROCK_REGION,
  BEDROCK_SECRET,
  BUCKET,
  GATEWAY_NAME,
  RUN_ID,
  selectedSamples,
  stagedSample,
  TEST_TIMEOUT_MS,
} from "./agent-zero-config-support.js";
import { e2eEnabled } from "./helpers.js";

const READY =
  e2eEnabled() &&
  process.env.RAYITO_E2E_RECIPE === "1" &&
  Boolean(BEDROCK_SECRET && BUCKET && BASE && RUN_ID);
const RECIPE_VARIANT = "r";

describe.skipIf(!READY)("fleet recipe (Q191, Q194)", () => {
  it(
    "a reconnected fleet uses the recipe and the warm slot",
    async () => {
      const [sample] = selectedSamples();
      expect(sample).toBeDefined();
      const root = join(tmpdir(), `rayito-recipe-${RUN_ID}`);
      const path = stagedSample(sample as NonNullable<typeof sample>, root, RECIPE_VARIANT);
      const accessToken = randomBytes(32).toString("base64url");
      const fleet = await deployAgent(path, {
        region: BEDROCK_REGION,
        options: {
          bucket: BUCKET,
          secretNames: { [GATEWAY_NAME]: BEDROCK_SECRET ?? "" },
          accessToken,
          scaling: new ScalingOptions({ minVms: 1, maxVms: 2, warmVms: 1, sessions: "dedicated" }),
          scaleInAfterMs: 300_000,
          poolBackend: new JsonFilePoolBackend(join(root, "pool.json")),
        },
      });
      const name = fleet.name;
      try {
        await fleet.detach();
        const reconnected = await AgentFleet.connect(name, {
          accessToken,
          connection: { region: BEDROCK_REGION },
        });
        const first = await reconnected.invoke("Responde sólo: uno");
        const second = await reconnected.invoke("Responde sólo: dos");
        expect(first.text).not.toBe("");
        expect(second.text).not.toBe("");
        const status = await reconnected.status();
        expect([status.minVms, status.maxVms, status.scalingSource]).toEqual([1, 2, "recipe"]);
        await reconnected.stop();
        const after = await AgentFleet.connect(name, { connection: { region: BEDROCK_REGION } });
        expect((await after.status()).scalingSource).toBe("default");
      } finally {
        await (
          await AgentFleet.connect(name, {
            connection: { region: BEDROCK_REGION },
            useRecipe: false,
          })
        ).stop();
      }
    },
    TEST_TIMEOUT_MS,
  );
});
