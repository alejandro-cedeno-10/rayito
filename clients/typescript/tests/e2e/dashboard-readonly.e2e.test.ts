/**
 * Panel de gestión (ADR-026) contra AWS real, de sólo lectura: `Quotas`,
 * `UsageCost` y `Fleet` leen la cuenta sin crear ni cambiar nada (sin
 * sandboxes nuevos, sin acciones). Necesita `RAYITO_E2E=1` y
 * `RAYITO_TEMPLATE` como el resto de los e2e; el coste facturado, que Cost
 * Explorer cobra por petición, sólo se lee con `RAYITO_E2E_BILLED_COST=1`
 * (cada fallo de caché cuesta $0,01 en us-east-1: una petición con la ventana
 * por defecto).
 *
 * Coste: $0 sin `RAYITO_E2E_BILLED_COST`; con ella, una petición de Cost
 * Explorer (~$0,01). Las lecturas de Service Quotas, `ListMicrovms`,
 * `ListMicrovmImages`, `ListMicrovmImageVersions` y `GetMicrovmImageVersion`
 * no se facturan.
 */

import { describe, expect, test } from "vitest";
import { Fleet, InvalidArgumentError, Quotas, UsageCost } from "../../src/index.js";
import { MICROVM_STATES } from "../../src/limits.js";
import { e2eEnabled, useE2E } from "./helpers.js";

const BILLED_COST_VAR = "RAYITO_E2E_BILLED_COST";
const TERMINAL_STATE = "TERMINATED";
/** `TERMINATING|TERMINATED`: un sandbox del filtro por imagen nunca está así. */
const TERMINAL_STATES = ["TERMINATING", "TERMINATED"];
const PLANNED_COUNT = 3;
const LEVELS = ["ok", "warning", "critical"];

describe.runIf(e2eEnabled())("Dashboard read-only services (AWS real)", () => {
  const e2e = useE2E();

  test("quotas report reads the applied quotas and the memory usage", async () => {
    const quotas = new Quotas({
      region: e2e.controlPlane.region,
      controlPlane: e2e.controlPlane,
    });

    const report = await quotas.report();

    expect(report.region).toBe(e2e.controlPlane.region);
    expect(report.entries.length).toBeGreaterThan(0);
    expect(report.entries.every((entry) => entry.increaseUrl.startsWith("https://"))).toBe(true);
    expect(report.memory.allocatedGb).toBeGreaterThanOrEqual(0);
    expect(report.memory.unresolved).toBeLessThanOrEqual(report.memory.sandboxes);
    expect(LEVELS).toContain(report.memory.level);
  });

  test("usage cost estimates for free", async () => {
    const cost = new UsageCost({
      region: e2e.controlPlane.region,
      controlPlane: e2e.controlPlane,
    });

    const estimate = await cost.estimate();
    expect(estimate.label).toBe("estimated");
    expect(estimate.totalUsd).toBeGreaterThanOrEqual(0);
    const listed = new Set<string>();
    for await (const item of e2e.controlPlane.listMicrovms({ states: MICROVM_STATES })) {
      listed.add(item.sandboxId);
    }
    expect(estimate.items.every((item) => listed.has(item.sandboxId))).toBe(true);
  });

  // Queda como omitido sin RAYITO_E2E_BILLED_COST=1: Cost Explorer cobra por petición.
  test.skipIf(process.env[BILLED_COST_VAR] !== "1")(
    "usage cost bills only behind the opt-in",
    async () => {
      const cost = new UsageCost({
        region: e2e.controlPlane.region,
        controlPlane: e2e.controlPlane,
      });

      const billed = await cost.billed();
      expect(billed.label).toBe("billed");
      expect(billed.requests).toBeGreaterThanOrEqual(1);
      expect(billed.totalUsd).toBeGreaterThanOrEqual(0);
      expect(await cost.billed()).toBe(billed);
    },
  );

  test("fleet lists sandboxes, images and versions and refuses actions by default", async () => {
    const fleet = new Fleet({
      region: e2e.controlPlane.region,
      controlPlane: e2e.controlPlane,
    });

    expect(fleet.allowActions).toBe(false);
    const sandboxes = await fleet.sandboxes();
    expect(sandboxes.every((view) => view.state !== TERMINAL_STATE)).toBe(true);
    const ofTemplate = await fleet.sandboxes({ image: e2e.templateArn });
    expect(ofTemplate.every((view) => view.template === e2e.templateArn)).toBe(true);
    expect(ofTemplate.every((view) => !TERMINAL_STATES.includes(view.state))).toBe(true);

    const images = await fleet.images();
    expect(images.map((image) => image.imageArn)).toContain(e2e.templateArn);
    expect((await fleet.imageVersions(e2e.settings.template)).length).toBeGreaterThan(0);
    expect(fleet.planBulk("kill", PLANNED_COUNT).count).toBe(PLANNED_COUNT);
    await expect(
      fleet.runBulk(
        "kill",
        ofTemplate.map((view) => view.sandboxId),
      ),
    ).rejects.toThrow(InvalidArgumentError);
  });
});
