/**
 * El panel local `rayito dashboard` de TypeScript contra Floci (`make
 * local-e2e`): los escenarios de sólo lectura de
 * `tests/shared/dashboard-scenarios.ts`, los mismos que corren contra AWS real
 * (`tests/e2e/dashboard-local.e2e.test.ts`), incluidas las guardas de
 * seguridad (sin sesión nada contesta, una acción sin `--allow-actions` y el
 * coste facturado se rechazan antes de llamar a AWS).
 */

import { beforeAll, describe } from "vitest";
import { defineDashboardReadScenarios } from "../shared/dashboard-scenarios.js";
import { localServices } from "./cli-world.js";
import { LOCAL_IMAGE_NAME, type LocalHarness, localEnabled, localHarness } from "./helpers.js";

describe.skipIf(!localEnabled())("rayito dashboard local (sólo lectura)", () => {
  let harness: LocalHarness | undefined;
  const current = (): LocalHarness => {
    if (harness === undefined) {
      throw new Error("el entorno local no está sembrado");
    }
    return harness;
  };

  beforeAll(async () => {
    harness = await localHarness();
  });

  defineDashboardReadScenarios({
    createServices: localServices(current),
    get env() {
      return { ...process.env, AWS_REGION: current().settings.region };
    },
    imageName: LOCAL_IMAGE_NAME,
  });
});
