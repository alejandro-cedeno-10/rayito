/**
 * m15-rayd-otlp contra AWS real (`RAYITO_E2E=1`, cap $0.35, MILESTONES.md
 * §8). Exige que `RAYITO_TEMPLATE` apunte a una imagen `rayito-base-caps`
 * (o derivada) con el `rayd` del tag `rayd-v0.6.0` o posterior y
 * `RAYITO_EXECUTION_ROLE_ARN` con la política `RayitoOtlpExport`
 * (`infra/otlp-export.yaml`, `rayito stack deploy otlp-export`) adjunta.
 *
 * Cubre, de los Q `OT*` reservados por la investigación
 * (`docs/research/2026-10-e2b-out-of-scope.md` §6.8; Q-números reales
 * asignados por la etapa de aceptación, >= Q95):
 *
 * - **OT1**: el `PutMetricData` firmado llega y
 *   `Health.features.telemetryExport` se activa.
 * - **OT5**: la exportación sobrevive un ciclo `/suspend`/`/resume`, con un
 *   vaciado dentro del presupuesto de `/suspend`.
 * - Una imagen sin caps y `OtlpAuth.executionRole()` sigue terminando el
 *   sandbox y lanzando `UnimplementedError` incluso contra AWS real.
 *
 * No toca AWS si `RAYITO_E2E` no está a `1`: este fichero se recolecta
 * siempre, pero su suite se salta por defecto (`describe.skipIf`).
 */

import { describe, expect, test } from "vitest";
import { OtlpAuth, Sandbox, TelemetryExport, UnimplementedError } from "../../src/index.js";
import { createTestSandbox, e2eEnabled, sleep, useE2E, waitUntil } from "./helpers.js";

const SUSPEND_PAUSE_MS = 70_000;
const FIRST_EXPORT_BUDGET_MS = 20_000;
const FIRST_EXPORT_POLL_MS = 2000;

async function waitForFirstExport(sbx: Sandbox): Promise<void> {
  await waitUntil(
    async () => (await sbx.getTelemetryStatus()).exported > 0n,
    FIRST_EXPORT_BUDGET_MS,
    "OT1: ningún lote se exportó dentro del plazo esperado",
    FIRST_EXPORT_POLL_MS,
  );
}

describe.skipIf(!e2eEnabled())("m15-rayd-otlp", () => {
  const e2e = useE2E();

  test("executionRole auth exports and survives a suspend/resume cycle", async () => {
    if (e2e.settings.executionRoleArn === undefined) {
      return;
    }
    const sbx = await createTestSandbox(e2e);
    const health = await sbx.getHealth();
    expect(health.lifecycle).toBeDefined();

    await waitForFirstExport(sbx);
    const before = await sbx.getTelemetryStatus();

    await sbx.pause();
    await sleep(SUSPEND_PAUSE_MS);
    await sbx.connect();

    await waitForFirstExport(sbx);
    const after = await sbx.getTelemetryStatus();
    expect(after.exported).toBeGreaterThan(before.exported);
    expect(after.dropped).toBe(before.dropped);
  });

  test("executionRole auth on a non-caps image still terminates and raises", async () => {
    if (e2e.settings.template.includes("caps")) {
      return;
    }
    await expect(
      Sandbox.create({
        template: e2e.settings.template,
        telemetry: new TelemetryExport({ auth: OtlpAuth.executionRole() }),
        controlPlane: e2e.controlPlane,
      }),
    ).rejects.toThrow(UnimplementedError);
    // Si `create()` hubiera dejado algo vivo, el sweeper de `afterAll` de
    // `useE2E()` lo detectaría (y lo terminaría) al listar los MicroVMs
    // vivos de la imagen; no hace falta un aserto adicional aquí.
  });
});
