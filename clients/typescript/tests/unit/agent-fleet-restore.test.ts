/**
 * Un VM que esta flota acortó y que `markExtras` asciende a base recupera toda
 * su vida aunque la flota no conozca su tope (`listedCap` sin estimación): pide
 * el máximo de la plataforma, `rayd` lo rechaza informando su tope y se
 * reintenta con él, una sola vez: después deja de darlo por acortado. Espejo
 * de `test_a_promoted_vm_whose_cap_is_unknown_still_gets_its_whole_lifetime_back`.
 */

import { describe, expect, test, vi } from "vitest";
import { ScalingOptions } from "../../src/agent/scaling-domain.js";
import { MAX_DURATION_SECONDS } from "../../src/limits.js";
import { FleetHarness } from "./agent-fleet-fake.js";

vi.mock("../../src/agent/fleet-base.js", async (importOriginal) => {
  const original = await importOriginal<typeof import("../../src/agent/fleet-base.js")>();
  return { ...original, listedCap: () => undefined };
});

async function promotedExtraVm() {
  const h = new FleetHarness();
  const fleet = await h.started(new ScalingOptions({ minVms: 1, maxVms: 2 }));
  const held = await fleet.stream("primera");
  await fleet.invoke("segunda");
  await held.close();
  const [base, extraId] = fleet.sandboxIds;
  const extra = h.world.sandboxes.get(extraId ?? "");
  if (extra === undefined) {
    throw new Error("falta el VM sobrante");
  }
  await h.world.sandboxes.get(base ?? "")?.kill();
  await fleet.refreshMembers();
  return { fleet, extra };
}

describe("a VM promoted to base", () => {
  test("with an unknown cap still gets its whole lifetime back", async () => {
    const { fleet, extra } = await promotedExtraVm();
    await fleet.invoke("tercera");
    expect(extra.beyondCap).toEqual([MAX_DURATION_SECONDS]);
    const restored = extra.timeouts.at(-1) ?? 0;
    expect(restored).toBeGreaterThanOrEqual(3590);
    expect(restored).toBeLessThanOrEqual(3660 - 60 - 5);
    const sent = extra.timeouts.length;
    await fleet.invoke("cuarta");
    expect(extra.timeouts).toHaveLength(sent);
  });
});
