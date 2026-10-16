/**
 * La CLI `rayito` de TypeScript contra el guest local y Floci (`make
 * local-e2e`): los mismos escenarios que corren contra AWS real
 * (`tests/e2e/cli*.e2e.test.ts`, definidos en `tests/shared/cli-scenarios.ts`)
 * con `runCli` en proceso. Lo que depende del proxy de AWS (`sandbox proxy`)
 * sólo lo cubre el e2e contra AWS real.
 */

import { afterAll, beforeAll, describe } from "vitest";
import {
  defineCliInteractiveScenarios,
  defineCliReadScenarios,
  defineCliSandboxScenarios,
} from "../shared/cli-scenarios.js";
import { localCliWorld } from "./cli-world.js";
import { type LocalHarness, localEnabled, localHarness, releaseGuest } from "./helpers.js";

const RUN_LABEL = `rayito-tscli-local-${Date.now()}`;

describe.skipIf(!localEnabled())("CLI rayito local", () => {
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

  afterAll(async () => {
    if (harness !== undefined) {
      await releaseGuest(harness);
    }
  });

  const world = localCliWorld(current);
  defineCliReadScenarios(world);
  defineCliSandboxScenarios(world, RUN_LABEL);
  defineCliInteractiveScenarios(world, RUN_LABEL);
});
