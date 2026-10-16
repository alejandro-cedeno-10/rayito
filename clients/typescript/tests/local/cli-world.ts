/**
 * El `CliWorld` de los e2e locales (`make local-e2e`): los servicios de la CLI
 * sobre Floci para lo que Floci emula (pilas, imágenes, cuotas) y sobre el
 * plano de control y el transporte del guest local para los sandboxes, como
 * `createAwsServices` los armaría en AWS real. Los escenarios son los de
 * `tests/shared`, los mismos que corren contra AWS real.
 */

import { createAwsServices, SdkSandboxGateway } from "../../src/cli/aws-services.js";
import type { ServicesFactory } from "../../src/cli/services.js";
import { SandboxNotFoundError } from "../../src/index.js";
import { TERMINAL_STATES } from "../../src/limits.js";
import type { CliWorld } from "../shared/cli-scenarios.js";
import type { LocalHarness } from "./helpers.js";

export function localServices(harness: () => LocalHarness): ServicesFactory {
  return async (session) => {
    const { controlPlane, settings } = harness();
    const aws = await createAwsServices(session);
    return {
      ...aws,
      controlPlane,
      sandboxes: new SdkSandboxGateway(
        controlPlane,
        aws.region,
        session.logger,
        settings.address.transport(),
      ),
    };
  };
}

/** Los campos de `CliWorld` se leen al correr cada test, cuando `beforeAll` ya sembró la imagen. */
export function localCliWorld(harness: () => LocalHarness): CliWorld {
  return {
    get template() {
      return harness().templateArn;
    },
    createServices: localServices(harness),
    get env() {
      return { ...process.env, AWS_REGION: harness().settings.region };
    },
    killQuietly: async (sandboxId) => {
      const { controlPlane } = harness();
      try {
        const info = await controlPlane.getMicrovm(sandboxId);
        if (!TERMINAL_STATES.has(info.state)) {
          await controlPlane.terminateMicrovm(sandboxId);
        }
      } catch (error) {
        if (!(error instanceof SandboxNotFoundError)) {
          throw error;
        }
      }
    },
  };
}
