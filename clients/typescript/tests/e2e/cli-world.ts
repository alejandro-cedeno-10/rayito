/** El `CliWorld` de los e2e contra AWS real: la sesión real y `Sandbox.kill` sin transporte propio. */

import { createAwsServices } from "../../src/cli/aws-services.js";
import { Sandbox, SandboxNotFoundError } from "../../src/index.js";
import type { CliWorld } from "../shared/cli-scenarios.js";
import { TEMPLATE_VAR } from "./helpers.js";

async function killRealSandbox(sandboxId: string): Promise<void> {
  try {
    await Sandbox.kill(sandboxId);
  } catch (error) {
    if (!(error instanceof SandboxNotFoundError)) {
      throw error;
    }
  }
}

export function realAwsCliWorld(): CliWorld {
  return {
    template: process.env[TEMPLATE_VAR] ?? "",
    createServices: createAwsServices,
    env: process.env,
    killQuietly: killRealSandbox,
  };
}
