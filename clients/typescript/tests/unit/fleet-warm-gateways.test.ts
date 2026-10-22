/**
 * Una plaza aparcada abre las pasarelas del proyecto (ADR-034 D6, design D4):
 * la flota reconectada, al abrir una plaza que nadie sirvió, pide al
 * directorio que abra sus `gateways` (el mismo camino que
 * `SandboxPool.take({ gateways })`, `applyGateways`) antes de arrancar el host;
 * si no puede, la sustituye. Espejo de `test_fleet_warm_gateways.py`.
 */

import { describe, expect, test } from "vitest";
import { AgentFleet } from "../../src/agent/fleet.js";
import { SandboxError } from "../../src/errors.js";
import { gatewaySections } from "../../src/pool/pool.js";
import { FLEET_TOKEN, FleetHarness, sandboxIdOf } from "./agent-fleet-fake.js";
import { project } from "./agent-project-fake.js";

async function reconnect(h: FleetHarness): Promise<AgentFleet> {
  const the = project();
  return AgentFleet.connect("support-bot", {
    accessToken: FLEET_TOKEN,
    project: the,
    directory: h.world,
    service: h.service(the),
    fetch: h.backend.fetch,
    now: h.clock.clock,
    sleep: h.clock.sleep,
  });
}

describe("warm slots get the project gateways", () => {
  test("a parked slot gets the gateways before the host starts", async () => {
    const h = new FleetHarness();
    const parked = h.world.add({ served: false, state: "SUSPENDED" });
    const fleet = await reconnect(h);
    const result = await fleet.invoke("hola");
    expect(result.text).toBe("respuesta a hola");
    expect(parked.configured).toEqual([["bedrock"]]);
    expect(parked.served).toBe(true);
  });

  test("a served VM is not configured again", async () => {
    const h = new FleetHarness();
    const served = h.world.add();
    const fleet = await reconnect(h);
    await fleet.invoke("hola");
    expect(served.configured).toEqual([]);
  });

  test("a parked slot that cannot get its gateways is replaced", async () => {
    const h = new FleetHarness();
    const parked = h.world.add({ served: false, state: "SUSPENDED" });
    parked.configureError = new SandboxError("ConfigureSandbox falló");
    const fleet = await reconnect(h);
    const result = await fleet.invoke("hola");
    expect(result.text).toBe("respuesta a hola");
    expect(parked.state).toBe("TERMINATED");
    expect(fleet.sandboxIds).not.toContain(sandboxIdOf(1));
    expect(h.world.created.length).toBe(1);
  });

  test("the gateway plan is the one take() applies", () => {
    expect(gatewaySections(undefined)).toEqual([]);
    expect(gatewaySections({ ...project().gateways }).length).toBe(1);
  });
});
