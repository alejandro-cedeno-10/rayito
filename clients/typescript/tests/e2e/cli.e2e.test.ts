/**
 * La CLI `rayito` de TypeScript (ADR-027, fase 1) contra AWS real: un sandbox
 * de punta a punta con `create --detach`, `info`, `exec`, `metrics`, `list`
 * y `kill`, más lo de sólo lectura (`stack list|status`, `image list|sizes`).
 * Corre `runCli` en proceso con la sesión real de AWS (`createAwsServices`),
 * como `node dist/cli.mjs`. Los escenarios son los mismos que corren en local
 * contra Docker + Floci (`tests/shared/cli-scenarios.ts`).
 *
 * Necesita `RAYITO_E2E=1` y `RAYITO_TEMPLATE` como el resto de los e2e. Sin
 * `RAYITO_E2E=1` se omite sin llamar a AWS.
 *
 * Coste: un sandbox de la imagen `RAYITO_TEMPLATE` durante un par de minutos
 * (un `create`, tres `exec` y un `kill`); el resto son lecturas gratuitas
 * (`ListMicrovms`, `ListMicrovmImages`, `DescribeStacks`).
 */

import { describe } from "vitest";
import { defineCliReadScenarios, defineCliSandboxScenarios } from "../shared/cli-scenarios.js";
import { realAwsCliWorld } from "./cli-world.js";
import { e2eEnabled } from "./helpers.js";

const RUN_LABEL = `rayito-tscli-e2e-${Date.now()}`;

describe.runIf(e2eEnabled())("CLI rayito (AWS real)", () => {
  const world = realAwsCliWorld();
  defineCliReadScenarios(world);
  defineCliSandboxScenarios(world, RUN_LABEL);
});
