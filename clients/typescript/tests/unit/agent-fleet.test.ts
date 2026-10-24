/**
 * `AgentFleet` (`agent/fleet.ts`) sobre dobles: el arranque todo o nada, el id
 * de sesión enrutable, la cola cliente y el 429, las sesiones dedicadas y su
 * índice, la salud y la sustitución, el escalado y la vida de los VMs
 * sobrantes, las plazas calientes, la reconexión y el cierre. Espejo de
 * `test_agent_fleet.py`; ninguna prueba llama a AWS.
 */

import { describe, expect, test, vi } from "vitest";
import { AgentFleet, SandboxDirectory } from "../../src/agent/fleet.js";
import {
  AGENT_FLEET_BUSY_RETRY_MS,
  AGENT_FLEET_PROBE_CONCURRENCY,
  AGENT_FLEET_SESSION_LABEL,
  sessionLabel,
  UNMANAGED_LIFETIME_MESSAGE,
  UNPROVEN_MEMBER_MESSAGE,
  UNREADABLE_MEMBER_MESSAGE,
} from "../../src/agent/fleet-base.js";
import { DEFAULT_QUEUE_TIMEOUT_MS, ServeOptions } from "../../src/agent/project.js";
import { requestTimeoutMs } from "../../src/agent/project-service.js";
import { decodeHandle, encodeHandle, ScalingOptions } from "../../src/agent/scaling-domain.js";
import {
  CapacityError,
  InvalidArgumentError,
  RateLimitError,
  SandboxError,
  TimeoutError,
} from "../../src/errors.js";
import { AGENT_FLEET_UNHEALTHY_PINGS } from "../../src/limits.js";
import type { SandboxListItem } from "../../src/models.js";
import { Sandbox } from "../../src/sandbox/sandbox.js";
import { FLEET_TOKEN, FleetHarness, sandboxIdOf } from "./agent-fleet-fake.js";
import { project } from "./agent-project-fake.js";

const SHARED_TWO = new ScalingOptions({ minVms: 1, maxVms: 2 });
const DEDICATED_TWO = new ScalingOptions({ minVms: 1, maxVms: 2, sessions: "dedicated" });
const MS_PER_SECOND = 1000;

/** Una flota de 1 a 2 VMs cuyo VM sobrante ya atendió una invocación
 * (`[120, hold, 120]`) y cuyo VM base queda ocupado por un stream: la
 * siguiente invocación va al sobrante. */
async function fleetWithAUsedExtraVm() {
  const h = new FleetHarness();
  const fleet = await h.started(
    new ScalingOptions({ minVms: 1, maxVms: 2, scaleInAfterMs: 120_000 }),
  );
  const held = await fleet.stream("primera");
  await fleet.invoke("segunda");
  const extra = h.world.sandboxes.get(fleet.sandboxIds[1] ?? "");
  if (extra === undefined) {
    throw new Error("falta el VM sobrante");
  }
  return { h, fleet, extra, held };
}

async function failureOf(promise: Promise<unknown>): Promise<Error> {
  try {
    await promise;
  } catch (error) {
    return error as Error;
  }
  throw new Error("se esperaba un fallo");
}

describe("start", () => {
  test("creates minVms with the fleet token and the deploy arguments", async () => {
    const h = new FleetHarness();
    const fleet = await h.started(new ScalingOptions({ minVms: 2, maxVms: 3 }));
    expect(fleet.sandboxIds).toHaveLength(2);
    expect(fleet.accessToken).toBe(FLEET_TOKEN);
    for (const { options } of h.world.created) {
      expect(options.template).toBe("support-bot");
      expect(options.accessToken).toBe(FLEET_TOKEN);
      expect(options.metadata?.["rayito.agent.deployment"]).toBe("support-bot");
    }
    expect(h.world.live.every((sandbox) => sandbox.served)).toBe(true);
  });

  test("a failed start terminates what it created", async () => {
    const h = new FleetHarness();
    const original = h.world.create.bind(h.world);
    let calls = 0;
    h.world.create = async (options) => {
      calls += 1;
      if (calls === 2) {
        throw new SandboxError("sin capacidad");
      }
      return original(options);
    };
    const fleet = h.fleet(new ScalingOptions({ minVms: 2, maxVms: 2 }));
    await failureOf(fleet.start());
    const created = [...h.world.sandboxes.values()];
    expect(created.length).toBeGreaterThan(0);
    expect(created.every((sandbox) => sandbox.state === "TERMINATED")).toBe(true);
  });

  test("a fleet without a project cannot start", async () => {
    const h = new FleetHarness();
    const fleet = h.fleet(undefined, { project: undefined, name: "support-bot" });
    expect(await failureOf(fleet.start())).toBeInstanceOf(InvalidArgumentError);
  });
});

describe("sessions", () => {
  test("a single VM fleet keeps the session id unchanged", async () => {
    const h = new FleetHarness();
    const fleet = await h.started();
    const result = await fleet.invoke("hola");
    expect(result.sessionId.startsWith("rs1.")).toBe(false);
    expect((await fleet.invoke("otra", { sessionId: result.sessionId })).sessionId).toBe(
      result.sessionId,
    );
  });

  test("a routable fleet returns a handle that routes back to its VM", async () => {
    const h = new FleetHarness();
    const fleet = await h.started(SHARED_TWO);
    const first = await fleet.invoke("hola");
    const route = decodeHandle(first.sessionId);
    const second = await fleet.invoke("sigue", { sessionId: first.sessionId });
    expect(route?.sandboxId).toBe(fleet.sandboxIds[0]);
    expect(second.sessionId).toBe(first.sessionId);
    expect(h.host(1).invocations.map((item) => item.session_id)).toEqual([
      undefined,
      route?.sessionId,
    ]);
  });

  test("an unknown handle is a lost VM", async () => {
    const h = new FleetHarness();
    const fleet = await h.started(SHARED_TWO);
    const error = (await failureOf(
      fleet.invoke("hola", { sessionId: encodeHandle(sandboxIdOf(99), "ses_x") }),
    )) as SandboxError;
    expect(error.code).toBe("agent_vm_lost");
  });

  test("the handle of a VM that appeared later is found by listing", async () => {
    const h = new FleetHarness();
    const fleet = await h.started(SHARED_TWO);
    const later = h.world.add();
    const result = await fleet.invoke("hola", {
      sessionId: encodeHandle(later.sandboxId, "ses_later"),
    });
    expect(decodeHandle(result.sessionId)?.sandboxId).toBe(later.sandboxId);
    expect(fleet.sandboxIds).toContain(later.sandboxId);
  });
});

describe("shared mode", () => {
  test("a second concurrent session grows the fleet and limits the extra VM", async () => {
    const h = new FleetHarness();
    const fleet = await h.started(
      new ScalingOptions({ minVms: 1, maxVms: 2, scaleInAfterMs: 120_000 }),
    );
    const held = await fleet.stream("primera");
    const second = await fleet.invoke("segunda");
    await held.close();
    expect(fleet.sandboxIds).toHaveLength(2);
    expect(decodeHandle(second.sessionId)?.sandboxId).toBe(fleet.sandboxIds[1]);
    const extra = h.world.sandboxes.get(fleet.sandboxIds[1] ?? "");
    const baseline = h.world.sandboxes.get(fleet.sandboxIds[0] ?? "");
    const hold = Math.ceil(requestTimeoutMs(project()) / 1000);
    expect(extra?.timeouts).toEqual([120, hold, 120]);
    expect(baseline?.timeouts).toEqual([]);
  });

  test("a fleet that can grow creates every VM with a logical lifetime", async () => {
    const h = new FleetHarness();
    const fleet = await h.started(SHARED_TWO);
    const held = await fleet.stream("primera");
    await fleet.invoke("segunda");
    await held.close();
    expect(h.world.created).toHaveLength(2);
    for (const { options } of h.world.created) {
      expect(options.timeoutMs).toBe(3_600_000);
      expect(options.maxLifetimeMs).toBe(3_660_000);
      expect(options.onTimeout).toBe("kill");
    }
    expect([...h.world.sandboxes.values()].every((sandbox) => sandbox.managed)).toBe(true);
  });

  test("a single-VM fleet creates its VM as in 0.16", async () => {
    const h = new FleetHarness();
    await h.started();
    expect(h.world.created).toHaveLength(1);
    const options = h.world.created[0]?.options ?? {};
    expect("maxLifetimeMs" in options).toBe(false);
    expect("onTimeout" in options).toBe(false);
  });

  test("a VM without a logical lifetime warns once and is not retried", async () => {
    const h = new FleetHarness();
    const warn = vi.fn();
    const fleet = await h.started(SHARED_TWO, { logger: { warn } });
    const old = h.world.add({ managed: false });
    await fleet.refreshMembers();
    const held = await fleet.stream("primera");
    await fleet.invoke("segunda");
    await fleet.invoke("tercera");
    await held.close();
    expect(old.timeouts).toEqual([]);
    expect(
      warn.mock.calls.filter(([message]) => message === UNMANAGED_LIFETIME_MESSAGE),
    ).toHaveLength(1);
  });

  test("a VM promoted to base gets its whole lifetime back", async () => {
    const h = new FleetHarness();
    const fleet = await h.started(SHARED_TWO);
    const held = await fleet.stream("primera");
    await fleet.invoke("segunda");
    await held.close();
    const [base, extraId] = fleet.sandboxIds;
    const extra = h.world.sandboxes.get(extraId ?? "");
    await h.world.sandboxes.get(base ?? "")?.kill();
    await fleet.refreshMembers();
    await fleet.invoke("tercera");
    const restored = extra?.timeouts.at(-1) ?? 0;
    expect(restored).toBeGreaterThanOrEqual(3590);
    expect(restored).toBeLessThanOrEqual(3660 - 60 - 5);
    expect(extra?.beyondCap).toEqual([]);
  });

  test("an extra VM keeps its hold while another invocation is in flight", async () => {
    const h = new FleetHarness();
    const the = project({ serve: new ServeOptions({ maxConcurrency: 2 }) });
    const fleet = h.fleet(new ScalingOptions({ minVms: 1, maxVms: 2, scaleInAfterMs: 120_000 }), {
      project: the,
      service: h.service(the),
    });
    await fleet.start();
    const baseStreams = [await fleet.stream("a"), await fleet.stream("b")];
    const longRunning = await fleet.stream("larga");
    const extra = h.world.sandboxes.get(fleet.sandboxIds[1] ?? "");
    const hold = Math.ceil(requestTimeoutMs(the) / 1000);
    await fleet.invoke("corta");
    expect(extra?.timeouts).toEqual([120, hold, hold]);
    await longRunning.close();
    expect(extra?.timeouts).toEqual([120, hold, hold, 120]);
    for (const stream of baseStreams) {
      await stream.close();
    }
  });

  test("a fixed-size fleet never warns about a VM without a logical lifetime", async () => {
    const h = new FleetHarness();
    const warn = vi.fn();
    const fleet = await h.started(new ScalingOptions({ minVms: 1, maxVms: 1 }), {
      logger: { warn },
    });
    const old = h.world.add({ managed: false });
    await fleet.refreshMembers();
    const held = await fleet.stream("primera");
    await fleet.invoke("segunda");
    await held.close();
    expect(old.timeouts).toEqual([]);
    expect(warn.mock.calls.filter(([message]) => message === UNMANAGED_LIFETIME_MESSAGE)).toEqual(
      [],
    );
  });

  test("a late cap estimate is corrected from rayd and never marks the VM", async () => {
    const h = new FleetHarness();
    const warn = vi.fn();
    const fleet = await h.started(
      new ScalingOptions({ minVms: 1, maxVms: 2, scaleInAfterMs: 120_000 }),
      { logger: { warn } },
    );
    let held = await fleet.stream("primera");
    await fleet.invoke("segunda");
    const extra = h.world.sandboxes.get(fleet.sandboxIds[1] ?? "");
    if (extra === undefined) {
      throw new Error("falta el VM sobrante");
    }
    extra.maxLifetimeMs = 200_000;
    await held.close();
    held = await fleet.stream("primera");
    await fleet.invoke("tercera");
    await held.close();
    const hold = Math.ceil(requestTimeoutMs(project()) / 1000);
    expect(extra.beyondCap).toEqual([hold]);
    expect(extra.timeouts.at(-2)).toBeGreaterThan(120);
    expect(extra.timeouts.at(-2)).toBeLessThanOrEqual(200 - 60 - 5);
    expect(extra.timeouts.at(-1)).toBe(120);
    expect(warn.mock.calls.filter(([message]) => message === UNMANAGED_LIFETIME_MESSAGE)).toEqual(
      [],
    );
  });

  test.each([
    ["a 500", 500, 0],
    ["a 429 until the queue gives up", 200, 1_000],
  ])("%s gives the extra VM its scale-in deadline back", async (_label, status, busy) => {
    const { h, fleet, extra, held } = await fleetWithAUsedExtraVm();
    const hold = Math.ceil(requestTimeoutMs(project()) / MS_PER_SECOND);
    const host = h.backend.host(extra.sandboxId);
    host.invokeStatus = status;
    host.busyAnswers = busy;
    await failureOf(fleet.invoke("tercera"));
    await held.close();
    expect(extra.timeouts.slice(0, 4)).toEqual([120, hold, 120, hold]);
    expect(extra.timeouts.at(-1)).toBe(120);
  });

  test("a release never cuts the hold of an invocation that started meanwhile", async () => {
    const { fleet, extra, held } = await fleetWithAUsedExtraVm();
    const hold = Math.ceil(requestTimeoutMs(project()) / MS_PER_SECOND);
    const original = extra.setTimeout.bind(extra);
    let racing: Promise<unknown> | undefined;
    extra.setTimeout = async (timeoutMs: number) => {
      if (timeoutMs === 120_000 && racing === undefined) {
        racing = fleet.invoke("tercera");
        for (let tick = 0; tick < 50; tick += 1) {
          await Promise.resolve();
        }
      }
      await original(timeoutMs);
    };
    await fleet.invoke("cuarta");
    await racing;
    await held.close();
    expect(extra.timeouts.slice(-3, -1)).toEqual([120, hold]);
    expect(extra.timeouts.at(-1)).toBe(120);
  });

  test("sessions share a VM up to its concurrency", async () => {
    const h = new FleetHarness();
    const the = project({ serve: new ServeOptions({ maxConcurrency: 2 }) });
    const fleet = h.fleet(SHARED_TWO, { project: the, service: h.service(the) });
    await fleet.start();
    const held = await fleet.stream("primera");
    await fleet.invoke("segunda");
    await held.close();
    expect(fleet.sandboxIds).toHaveLength(1);
  });

  test("a full fleet queues and then reports capacity", async () => {
    const h = new FleetHarness();
    const fleet = await h.started();
    const held = await fleet.stream("primera");
    const error = (await failureOf(fleet.invoke("segunda"))) as CapacityError;
    await held.close();
    expect(error).toBeInstanceOf(CapacityError);
    expect(error.code).toBe("agent_capacity");
    expect(h.clock.sleeps.reduce((a, b) => a + b, 0)).toBeGreaterThanOrEqual(
      DEFAULT_QUEUE_TIMEOUT_MS,
    );
    expect(new Set(h.clock.sleeps)).toEqual(new Set([AGENT_FLEET_BUSY_RETRY_MS]));
  });

  test("a queued session gets the VM once it frees", async () => {
    const h = new FleetHarness();
    let held: Awaited<ReturnType<AgentFleet["stream"]>> | undefined;
    const fleet = h.fleet(undefined, {
      sleep: async (ms) => {
        const stream = held;
        held = undefined;
        await stream?.close();
        await h.clock.sleep(ms);
      },
    });
    await fleet.start();
    held = await fleet.stream("primera");
    const result = await fleet.invoke("segunda");
    expect(result.text).toBe("respuesta a segunda");
    expect(h.clock.sleeps).toHaveLength(1);
  });

  test("a 429 from the host is retried until it answers", async () => {
    const h = new FleetHarness();
    const fleet = await h.started();
    h.host(1).busyAnswers = 2;
    const result = await fleet.invoke("hola");
    expect(result.text).toBe("respuesta a hola");
    expect(h.host(1).invocations).toHaveLength(3);
    expect(h.clock.sleeps).toEqual([AGENT_FLEET_BUSY_RETRY_MS, AGENT_FLEET_BUSY_RETRY_MS]);
  });

  test("a host that never stops answering 429 is capacity", async () => {
    const h = new FleetHarness();
    const fleet = await h.started();
    h.host(1).busyAnswers = 10_000;
    const error = (await failureOf(fleet.invoke("hola"))) as CapacityError;
    expect(error.code).toBe("agent_capacity");
  });

  test("a 429 moves a new session to another VM", async () => {
    const h = new FleetHarness();
    const fleet = await h.started(new ScalingOptions({ minVms: 2, maxVms: 2 }));
    h.host(1).busyAnswers = 1;
    const result = await fleet.invoke("hola");
    expect(decodeHandle(result.sessionId)?.sandboxId).toBe(sandboxIdOf(2));
  });
});

describe("dedicated mode", () => {
  test("each session gets its own VM", async () => {
    const h = new FleetHarness();
    const fleet = await h.started(DEDICATED_TWO);
    const first = await fleet.invoke("uno");
    const second = await fleet.invoke("dos");
    const routes = new Set(
      [first, second].map((result) => decodeHandle(result.sessionId)?.sandboxId),
    );
    expect(routes.size).toBe(2);
  });

  test("capacity is the number of VMs", async () => {
    const h = new FleetHarness();
    const fleet = await h.started(DEDICATED_TWO);
    await fleet.invoke("uno");
    await fleet.invoke("dos");
    expect(await failureOf(fleet.invoke("tres"))).toBeInstanceOf(CapacityError);
  });

  test("keeps a caller-chosen session on its VM within the process", async () => {
    const h = new FleetHarness();
    const fleet = await h.started(DEDICATED_TWO);
    const first = await fleet.invoke("uno", { sessionId: "chat-1" });
    const again = await fleet.invoke("dos", { sessionId: "chat-1" });
    expect(first.sessionId).toBe(again.sessionId);
    expect(fleet.sandboxIds).toHaveLength(1);
  });

  // Con maxVms 1 el id de sesión no lleva asa: la flota recuerda la sesión
  // que asignó el host para volver a su VM, que ya está reservado.
  test("with one VM brings the host's session back to its VM", async () => {
    const h = new FleetHarness();
    const fleet = await h.started(
      new ScalingOptions({ minVms: 1, maxVms: 1, sessions: "dedicated" }),
    );
    const first = await fleet.invoke("uno");
    const again = await fleet.invoke("dos", { sessionId: first.sessionId });
    expect(decodeHandle(first.sessionId)).toBeUndefined();
    expect(again.sessionId).toBe(first.sessionId);
    expect(fleet.sandboxIds).toHaveLength(1);
  });

  test("with an index labels the VM and finds it again", async () => {
    const h = new FleetHarness();
    const index = {} as never;
    const scaling = new ScalingOptions({ minVms: 1, maxVms: 3, sessions: "dedicated" });
    const fleet = await h.started(scaling, { sessionIndex: index });
    await fleet.invoke("uno");
    await fleet.invoke("hola", { sessionId: "chat-9" });
    const label = sessionLabel("chat-9");
    const created = h.world.created[h.world.created.length - 1]?.options;
    expect(created?.metadata?.[AGENT_FLEET_SESSION_LABEL]).toBe(label);
    expect(created?.index).toBe(index);
    expect(JSON.stringify(created?.metadata)).not.toContain("chat-9");
    const lastId = fleet.sandboxIds[fleet.sandboxIds.length - 1] ?? "";
    h.world.sessionHits.set(label, lastId);
    const fresh = h.fleet(scaling, { sessionIndex: index });
    await fresh.refreshMembers();
    const routed = await fresh.invoke("otra", { sessionId: "chat-9" });
    expect(decodeHandle(routed.sessionId)?.sandboxId).toBe(lastId);
  });
});

describe("health", () => {
  test("a VM that stops answering is replaced to keep minVms", async () => {
    const h = new FleetHarness();
    const fleet = await h.started();
    const original = fleet.sandboxIds[0] ?? "";
    h.host(1).pingStatus = 503;
    for (let count = 0; count < AGENT_FLEET_UNHEALTHY_PINGS; count += 1) {
      await fleet.status();
    }
    expect(fleet.sandboxIds).not.toContain(original);
    expect(fleet.sandboxIds).toHaveLength(1);
    expect(h.world.sandboxes.get(original)?.state).toBe("TERMINATED");
  });

  test("status never opens a suspended member", async () => {
    const h = new FleetHarness();
    h.world.add({ state: "SUSPENDED" });
    h.world.add({ state: "SUSPENDED" });
    const fleet = await AgentFleet.connect("support-bot", {
      accessToken: FLEET_TOKEN,
      directory: h.world,
      fetch: h.backend.fetch,
    });
    const status = await fleet.status();
    expect(status.members.map((member) => member.state)).toEqual(["unknown", "unknown"]);
    expect(h.world.connected).toEqual([]);
    expect([status.healthy, status.busy]).toEqual([0, 0]);
  });

  test("status right after connect reports the real state of running members", async () => {
    const h = new FleetHarness();
    h.world.add();
    h.world.add();
    h.world.add({ state: "SUSPENDED" });
    const fleet = await AgentFleet.connect("support-bot", {
      accessToken: FLEET_TOKEN,
      scaling: SHARED_TWO,
      directory: h.world,
      fetch: h.backend.fetch,
    });
    const status = await fleet.status();
    expect(status.members.map((member) => member.state)).toEqual(["healthy", "healthy", "unknown"]);
    expect(status.healthy).toBe(2);
    expect([...h.world.connected].sort()).toEqual([sandboxIdOf(1), sandboxIdOf(2)]);
    expect(fleet.deployments).toEqual([]);
    expect([...h.world.sandboxes.values()].every((sandbox) => sandbox.timeouts.length === 0)).toBe(
      true,
    );
  });

  test("status never replaces an unopened member it only asked", async () => {
    const h = new FleetHarness();
    h.world.add({ served: false });
    h.world.add();
    h.host(2).pingStatus = 503;
    const fleet = await AgentFleet.connect("support-bot", {
      accessToken: FLEET_TOKEN,
      scaling: SHARED_TWO,
      directory: h.world,
      fetch: h.backend.fetch,
    });
    let states: string[] = [];
    for (let count = 0; count < AGENT_FLEET_UNHEALTHY_PINGS; count += 1) {
      states = (await fleet.status()).members.map((member) => member.state);
    }
    expect(states).toEqual(["unknown", "unavailable"]);
    expect(fleet.sandboxIds).toEqual([sandboxIdOf(1), sandboxIdOf(2)]);
    expect([...h.world.sandboxes.values()].every((sandbox) => sandbox.state === "RUNNING")).toBe(
      true,
    );
    expect(fleet.deployments).toEqual([]);
  });

  test("a transport failure is not enough to drop the VM", async () => {
    const h = new FleetHarness();
    const fleet = await h.started();
    h.host(1).invokeStatus = 500;
    await failureOf(fleet.invoke("hola"));
    expect(fleet.sandboxIds).toEqual([sandboxIdOf(1)]);
  });
});

describe("scaling", () => {
  test("scale up creates VMs and scale down terminates the free extras", async () => {
    const h = new FleetHarness();
    const fleet = await h.started(new ScalingOptions({ minVms: 1, maxVms: 3 }));
    await fleet.scale({ minVms: 3 });
    expect(fleet.sandboxIds).toHaveLength(3);
    await fleet.scale({ minVms: 1, maxVms: 1 });
    expect(fleet.sandboxIds).toEqual([sandboxIdOf(1)]);
    expect(h.world.live).toHaveLength(1);
    expect(fleet.scaling).toEqual(new ScalingOptions({ minVms: 1, maxVms: 1 }));
  });

  test("scale validates like ScalingOptions", async () => {
    const h = new FleetHarness();
    const fleet = await h.started();
    expect(await failureOf(fleet.scale({ minVms: 5, maxVms: 2 }))).toBeInstanceOf(
      InvalidArgumentError,
    );
  });
});

describe("warm pool", () => {
  test("warm slots come from a pool with the fleet token", async () => {
    const h = new FleetHarness();
    const fleet = await h.started(new ScalingOptions({ minVms: 1, maxVms: 3, warmVms: 1 }));
    const pool = h.pools[0];
    expect(pool?.started).toBe(true);
    expect(pool?.config.size).toBe(1);
    expect(pool?.config.accessToken).toBe(FLEET_TOKEN);
    expect(await fleet.waitWarm(5 * MS_PER_SECOND, 100)).toBe(true);
    const held = await fleet.stream("primera");
    await fleet.invoke("segunda");
    await held.close();
    expect(pool?.taken).toHaveLength(1);
    expect(Object.keys((pool?.taken[0]?.gateways ?? {}) as object)).toContain("bedrock");
    expect(fleet.sandboxIds).toHaveLength(2);
    expect(pool?.config.maxLifetimeMs).toBe(3_660_000);
    expect(pool?.config.onTimeout).toBe("kill");
    const taken = h.world.sandboxes.get(fleet.sandboxIds[1] ?? "");
    expect(taken?.timeouts[0]).toBe(900);
  });

  test("the parked slots are not members until claimed", async () => {
    const h = new FleetHarness();
    const fleet = await h.started(new ScalingOptions({ minVms: 1, maxVms: 3, warmVms: 1 }));
    await fleet.refreshMembers();
    expect(fleet.sandboxIds).toHaveLength(1);
  });

  test("stop drains the pool and terminates every VM", async () => {
    const h = new FleetHarness();
    const fleet = await h.started(new ScalingOptions({ minVms: 2, maxVms: 3, warmVms: 1 }));
    await fleet.stop();
    expect(h.pools[0]?.closed).toEqual([true]);
    expect(h.world.live).toEqual([]);
    expect(fleet.sandboxIds).toEqual([]);
  });

  test("detach keeps the VMs and the parked slots with a persistent backend", async () => {
    const h = new FleetHarness();
    const fleet = await h.started(new ScalingOptions({ minVms: 1, maxVms: 2, warmVms: 1 }), {
      poolBackend: {} as never,
    });
    await fleet.detach();
    expect(h.pools[0]?.closed).toEqual([false]);
    expect(h.world.live).toHaveLength(2);
  });

  test("detach without a persistent backend drains the pool", async () => {
    const h = new FleetHarness();
    const fleet = await h.started(new ScalingOptions({ minVms: 1, maxVms: 2, warmVms: 1 }));
    await fleet.detach();
    expect(h.pools[0]?.closed).toEqual([true]);
  });
});

describe("reconnect", () => {
  test("connect lists the image and opens a VM only when it is used", async () => {
    const h = new FleetHarness();
    h.world.add();
    h.world.add();
    const fleet = await AgentFleet.connect("support-bot", {
      accessToken: FLEET_TOKEN,
      scaling: SHARED_TWO,
      directory: h.world,
      fetch: h.backend.fetch,
    });
    expect(fleet.sandboxIds).toEqual([sandboxIdOf(1), sandboxIdOf(2)]);
    expect(h.world.connected).toEqual([]);
    const result = await fleet.invoke("hola");
    expect(h.world.connected).toEqual([sandboxIdOf(1)]);
    expect(decodeHandle(result.sessionId)?.sandboxId).toBe(sandboxIdOf(1));
  });

  test("a reconnected fleet starts a parked VM that has no service", async () => {
    const h = new FleetHarness();
    h.world.add({ served: false, state: "SUSPENDED" });
    const the = project();
    const fleet = await AgentFleet.connect("support-bot", {
      accessToken: FLEET_TOKEN,
      project: the,
      directory: h.world,
      service: h.service(the),
      fetch: h.backend.fetch,
    });
    const result = await fleet.invoke("hola");
    expect(result.text).toBe("respuesta a hola");
    expect(h.world.sandboxes.get(sandboxIdOf(1))?.served).toBe(true);
  });

  test("a reconnected fleet creates new VMs with the version of its members", async () => {
    const h = new FleetHarness();
    const older = h.world.add();
    const newer = h.world.add();
    h.world.versions.set(older.sandboxId, "1");
    h.world.versions.set(newer.sandboxId, "2");
    const the = project();
    const fleet = await AgentFleet.connect("support-bot", {
      accessToken: FLEET_TOKEN,
      project: the,
      directory: h.world,
      service: h.service(the),
      fetch: h.backend.fetch,
    });
    await fleet.scale({ minVms: 3, maxVms: 3 });
    expect(h.world.created.map(({ options }) => options.templateVersion)).toEqual(["2"]);
  });

  test("a version the fleet already has is never replaced by the members", async () => {
    const h = new FleetHarness();
    const member = h.world.add();
    h.world.versions.set(member.sandboxId, "1");
    const the = project();
    const service = h.service(the);
    service.adoptImageVersion("7");
    const fleet = await AgentFleet.connect("support-bot", {
      accessToken: FLEET_TOKEN,
      project: the,
      directory: h.world,
      service,
      fetch: h.backend.fetch,
    });
    await fleet.scale({ minVms: 2, maxVms: 2 });
    expect(h.world.created.map(({ options }) => options.templateVersion)).toEqual(["7"]);
  });

  test("a reconnected fleet without a project cannot start a VM", async () => {
    const h = new FleetHarness();
    h.world.add({ served: false, state: "SUSPENDED" });
    const fleet = await AgentFleet.connect("support-bot", {
      accessToken: FLEET_TOKEN,
      directory: h.world,
    });
    expect(await failureOf(fleet.invoke("hola"))).toBeInstanceOf(InvalidArgumentError);
  });

  test("a fleet reconnected without a token can be listed but not used", async () => {
    const h = new FleetHarness();
    h.world.add();
    const fleet = await AgentFleet.connect("support-bot", { directory: h.world });
    expect(fleet.sandboxIds).toEqual([sandboxIdOf(1)]);
    expect(() => fleet.accessToken).toThrow(InvalidArgumentError);
    expect(await failureOf(fleet.invoke("hola"))).toBeInstanceOf(InvalidArgumentError);
  });
});

describe("SandboxDirectory", () => {
  const AgentLabels = { "rayito.agent.deployment": "support-bot", "rayito.agent.port": "8088" };

  function item(sandboxId: string, state: string, metadata?: Record<string, string>) {
    return {
      sandboxId,
      state,
      template: "arn:aws:lambda:us-east-1:123456789012:microvm-image:support-bot",
      templateVersion: "1",
      startedAt: new Date(0),
      templateName: "support-bot",
      metadata,
    } satisfies SandboxListItem;
  }

  async function* iterate(items: SandboxListItem[]): AsyncGenerator<SandboxListItem> {
    yield* items;
  }

  function probeFrom(
    metadata: Record<string, Record<string, string> | undefined>,
    failing: readonly string[] = [],
    states: Record<string, string> = {},
  ) {
    const probed: string[] = [];
    const probe = async (sandboxId: string) => {
      probed.push(sandboxId);
      if (failing.includes(sandboxId)) {
        throw new SandboxError("Health no respondió");
      }
      return { sandboxId, state: states[sandboxId] ?? "RUNNING", metadata: metadata[sandboxId] };
    };
    return { probe, probed };
  }

  test("never counts a running VM that is not an agent", async () => {
    const spy = vi
      .spyOn(Sandbox, "list")
      .mockImplementation(() =>
        iterate([item("mine", "RUNNING"), item("other", "RUNNING"), item("parked", "SUSPENDED")]),
      );
    const { probe } = probeFrom({ mine: AgentLabels, other: {} });
    const members = await new SandboxDirectory({}, { probeInfo: probe }).listMembers("support-bot");
    spy.mockRestore();
    expect(members.map((member) => member.sandboxId)).toEqual(["mine"]);
  });

  test("keeps a suspended VM while the image only has agents", async () => {
    const spy = vi
      .spyOn(Sandbox, "list")
      .mockImplementation(() => iterate([item("mine", "RUNNING"), item("parked", "SUSPENDED")]));
    const { probe, probed } = probeFrom({ mine: AgentLabels });
    const members = await new SandboxDirectory({}, { probeInfo: probe }).listMembers("support-bot");
    spy.mockRestore();
    expect(members.map((member) => member.sandboxId)).toEqual(["mine", "parked"]);
    expect(probed).toEqual(["mine"]);
  });

  test("leaves out a VM whose Health fails while listing, as terminating", async () => {
    const spy = vi
      .spyOn(Sandbox, "list")
      .mockImplementation(() =>
        iterate([item("alive", "RUNNING"), item("dying", "RUNNING"), item("parked", "SUSPENDED")]),
      );
    const { probe } = probeFrom({ alive: AgentLabels, dying: AgentLabels }, ["dying"]);
    const warnings: { message: string; fields?: Readonly<Record<string, unknown>> }[] = [];
    const logger = {
      warn: (message: string, fields?: Readonly<Record<string, unknown>>) =>
        warnings.push({ message, ...(fields === undefined ? {} : { fields }) }),
    };
    const members = await new SandboxDirectory({}, { probeInfo: probe, logger }).listMembers(
      "support-bot",
    );
    spy.mockRestore();
    expect(members.map((member) => member.sandboxId)).toEqual(["alive", "parked"]);
    expect(warnings).toEqual([
      { message: UNREADABLE_MEMBER_MESSAGE, fields: { sandboxId: "dying", error: "SandboxError" } },
    ]);
  });

  test("a VM that left RUNNING before its probe is not a member", async () => {
    const spy = vi
      .spyOn(Sandbox, "list")
      .mockImplementation(() => iterate([item("alive", "RUNNING"), item("leaving", "RUNNING")]));
    const { probe } = probeFrom({ alive: AgentLabels, leaving: AgentLabels }, [], {
      leaving: "TERMINATING",
    });
    const members = await new SandboxDirectory({}, { probeInfo: probe }).listMembers("support-bot");
    spy.mockRestore();
    expect(members.map((member) => member.sandboxId)).toEqual(["alive"]);
  });

  test.each([new RateLimitError("limitado"), new TimeoutError("plazo")])(
    "a control-plane error while probing still fails the listing: %s",
    async (error) => {
      const spy = vi
        .spyOn(Sandbox, "list")
        .mockImplementation(() => iterate([item("a", "RUNNING")]));
      const probe = async () => {
        throw error;
      };
      const failure = await failureOf(
        new SandboxDirectory({}, { probeInfo: probe }).listMembers("support-bot"),
      );
      spy.mockRestore();
      expect(failure).toBe(error);
    },
  );

  test("an error that is not a sandbox error still fails the listing", async () => {
    const spy = vi.spyOn(Sandbox, "list").mockImplementation(() => iterate([item("a", "RUNNING")]));
    const probe = async () => {
      throw new TypeError("sin credenciales");
    };
    const failure = await failureOf(
      new SandboxDirectory({}, { probeInfo: probe }).listMembers("support-bot"),
    );
    spy.mockRestore();
    expect(failure).toBeInstanceOf(TypeError);
  });
});

describe("SandboxDirectory concurrency", () => {
  test("probes at most AGENT_FLEET_PROBE_CONCURRENCY VMs at a time, in listing order", async () => {
    const ids = Array.from(
      { length: AGENT_FLEET_PROBE_CONCURRENCY + 2 },
      (_, index) => `vm-${index}`,
    );
    const spy = vi.spyOn(Sandbox, "list").mockImplementation(async function* () {
      for (const sandboxId of ids) {
        yield {
          sandboxId,
          state: "RUNNING",
          template: "arn:aws:lambda:us-east-1:123456789012:microvm-image:support-bot",
          templateVersion: "1",
          startedAt: new Date(0),
          templateName: "support-bot",
        } satisfies SandboxListItem;
      }
    });
    let probing = 0;
    let mostProbing = 0;
    const probe = async (sandboxId: string) => {
      probing += 1;
      mostProbing = Math.max(mostProbing, probing);
      await new Promise((resolve) =>
        setTimeout(resolve, 5 * (ids.length - ids.indexOf(sandboxId))),
      );
      probing -= 1;
      if (sandboxId !== "vm-0") {
        throw new SandboxError("Health no respondió");
      }
      return {
        sandboxId,
        state: "RUNNING",
        metadata: { "rayito.agent.deployment": "support-bot", "rayito.agent.port": "8088" },
      };
    };
    const warned: unknown[] = [];
    const logger = {
      warn: (_: string, fields?: Readonly<Record<string, unknown>>) =>
        warned.push(fields?.sandboxId),
    };
    const members = await new SandboxDirectory({}, { probeInfo: probe, logger }).listMembers(
      "support-bot",
    );
    spy.mockRestore();
    expect(members.map((member) => member.sandboxId)).toEqual(["vm-0"]);
    expect(mostProbing).toBe(AGENT_FLEET_PROBE_CONCURRENCY);
    expect(warned).toEqual(ids.slice(1));
  });
});

describe("unproven members", () => {
  test("stop never terminates a VM it could not prove is a member", async () => {
    const h = new FleetHarness();
    const mine = h.world.add();
    const parked = h.world.add({ state: "SUSPENDED" });
    h.world.unproven.add(parked.sandboxId);
    const warnings: unknown[] = [];
    const fleet = await AgentFleet.connect("support-bot", {
      accessToken: FLEET_TOKEN,
      directory: h.world,
      logger: { warn: (message, fields) => warnings.push({ message, fields }) },
    });
    expect(await fleet.stop()).toBe(1);
    expect(mine.state).toBe("TERMINATED");
    expect(parked.kills).toBe(0);
    expect(parked.state).toBe("SUSPENDED");
    expect(warnings).toContainEqual({
      message: UNPROVEN_MEMBER_MESSAGE,
      fields: { sandboxId: parked.sandboxId },
    });
  });

  test("reconcile never trims a VM it could not prove is a member", async () => {
    const h = new FleetHarness();
    h.world.add();
    const parked = h.world.add({ state: "SUSPENDED" });
    h.world.unproven.add(parked.sandboxId);
    const fleet = await AgentFleet.connect("support-bot", {
      accessToken: FLEET_TOKEN,
      directory: h.world,
      scaling: new ScalingOptions({ minVms: 1, maxVms: 1 }),
    });
    await fleet.reconcile();
    expect(parked.kills).toBe(0);
  });

  test("an unreadable VM leaves the suspended ones unproven", async () => {
    const spy = vi.spyOn(Sandbox, "list").mockImplementation(async function* () {
      for (const [sandboxId, state] of [
        ["mine", "RUNNING"],
        ["dying", "RUNNING"],
        ["parked", "SUSPENDED"],
      ] as const) {
        yield {
          sandboxId,
          state,
          template: "arn:aws:lambda:us-east-1:123456789012:microvm-image:support-bot",
          templateVersion: "1",
          startedAt: new Date(0),
          templateName: "support-bot",
        } satisfies SandboxListItem;
      }
    });
    const probe = async (sandboxId: string) => {
      if (sandboxId === "dying") {
        throw new SandboxError("Health no respondió");
      }
      return {
        sandboxId,
        state: "RUNNING",
        metadata: { "rayito.agent.deployment": "support-bot", "rayito.agent.port": "8088" },
      };
    };
    const members = await new SandboxDirectory({}, { probeInfo: probe }).listMembers("support-bot");
    spy.mockRestore();
    expect(members.map(({ sandboxId, proven }) => [sandboxId, proven !== false])).toEqual([
      ["mine", true],
      ["parked", false],
    ]);
  });
});

describe("stop", () => {
  test("terminates without opening the members and raises the first failure", async () => {
    const h = new FleetHarness();
    const first = h.world.add();
    const second = h.world.add();
    first.killError = new SandboxError("falló");
    const fleet = await AgentFleet.connect("support-bot", {
      accessToken: FLEET_TOKEN,
      directory: h.world,
    });
    const error = await failureOf(fleet.stop());
    expect(error.message).toBe("falló");
    expect(h.world.connected).toEqual([]);
    expect(second.state).toBe("TERMINATED");
    expect(first.kills).toBe(1);
  });
});

describe("stream", () => {
  test("a routable stream carries the handle and frees the VM", async () => {
    const h = new FleetHarness();
    const fleet = await h.started(SHARED_TWO);
    const stream = await fleet.stream("hola");
    const events = [];
    for await (const event of stream) {
      events.push(event);
    }
    const done = events.find((event) => event.type === "done");
    expect(events.some((event) => event.type === "text")).toBe(true);
    expect(done?.type === "done" && decodeHandle(done.sessionId) !== undefined).toBe(true);
    expect(stream.sessionId).toBe(done?.type === "done" ? done.sessionId : undefined);
  });

  test("result of a stream carries the handle", async () => {
    const h = new FleetHarness();
    const fleet = await h.started(SHARED_TWO);
    const result = await (await fleet.stream("hola")).result();
    expect(decodeHandle(result.sessionId)).toBeDefined();
    expect(result.text).toBe("respuesta a hola");
  });

  test("a single VM stream keeps the plain session id", async () => {
    const h = new FleetHarness();
    const fleet = await h.started();
    const stream = await fleet.stream("hola");
    const events = [];
    for await (const event of stream) {
      events.push(event);
    }
    const done = events.find((event) => event.type === "done");
    expect(done?.type === "done" && done.sessionId.startsWith("rs1.")).toBe(false);
  });
});
