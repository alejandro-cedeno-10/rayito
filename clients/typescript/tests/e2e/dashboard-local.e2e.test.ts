/**
 * El panel local `rayito dashboard` de TypeScript (ADR-027, fase 8) contra AWS
 * real, de sólo lectura: los escenarios de `tests/shared/dashboard-scenarios.ts`
 * (los mismos que corren en local contra Docker + Floci) con la sesión real
 * (`createAwsServices`). No crea sandboxes ni ejecuta acciones: una acción sin
 * `--allow-actions` se rechaza antes de llamar a AWS, y el coste facturado
 * sigue apagado.
 *
 * Necesita `RAYITO_E2E=1` y `RAYITO_TEMPLATE` como el resto de los e2e. Sin
 * `RAYITO_E2E=1` se omite sin llamar a AWS.
 *
 * Coste: $0. Sólo lecturas gratuitas (`ListMicrovms`, `GetMicrovmImageVersion`,
 * `ListMicrovmImages`, `ListMicrovmImageVersions`, `ListServiceQuotas`); el
 * coste facturado (Cost Explorer, de pago) no se pide.
 */

import { describe } from "vitest";
import { createAwsServices } from "../../src/cli/aws-services.js";
import { defineDashboardReadScenarios } from "../shared/dashboard-scenarios.js";
import { e2eEnabled, TEMPLATE_VAR } from "./helpers.js";

function imageName(template: string): string {
  return template.startsWith("arn:") ? template.slice(template.lastIndexOf(":") + 1) : template;
}

describe.runIf(e2eEnabled())("rayito dashboard (AWS real, sólo lectura)", () => {
  defineDashboardReadScenarios({
    createServices: createAwsServices,
    env: process.env,
    imageName: imageName(process.env[TEMPLATE_VAR] as string),
  });
});
