/**
 * `AgentFleet` (`agent/fleet.ts`) sobre dobles: el arranque todo o nada, el id
 * de sesión enrutable, la cola cliente y el 429, las sesiones dedicadas y su
 * índice, la salud y la sustitución, el escalado y la vida de los VMs
 * sobrantes, las plazas calientes, la reconexión y el cierre. Espejo de
 * `test_agent_fleet.py`; ninguna prueba llama a AWS.
 */

import { describe, expect, test } from "vitest";
import { AgentFleet } from "../../src/agent/fleet.js";
import {
  AGENT_FLEET_BUSY_RETRY_MS,
  AGENT_FLEET_QUEUE_TIMEOUT_MS_DEFAULT,
  AGENT_FLEET_SESSION_LABEL,
  AGENT_FLEET_UNHEALTHY_PINGS,
  decodeHandle,
  encodeHandle,
  sessionLabel,
} from "../../src/agent/fleet-base.js";
import { ServeOptions } from "../../src/agent/project.js";
import { ScalingOptions } from "../../src/agent/scaling-domain.js";
import { CapacityError, InvalidArgumentError, SandboxError } from "../../src/errors.js";
import { FLEET_TOKEN, FleetHarness, sandboxIdOf } from "./agent-fleet-fake.js";
import { project } from "./agent-project-fake.js";

const SHARED_TWO = new ScalingOptions({ minVms: 1, maxVms: 2 });
const DEDICATED_TWO = new ScalingOptions({ minVms: 1, maxVms: 2, sessions: "dedicated" });
const MS_PER_SECOND = 1000;

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
    expect(extra?.timeouts).toEqual([120, 120]);
    expect(baseline?.timeouts).toEqual([]);
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
      AGENT_FLEET_QUEUE_TIMEOUT_MS_DEFAULT,
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

  test("status reports members without opening the unknown ones", async () => {
    const h = new FleetHarness();
    h.world.add();
    h.world.add();
    const fleet = await AgentFleet.connect("support-bot", {
      accessToken: FLEET_TOKEN,
      directory: h.world,
    });
    const status = await fleet.status();
    expect(status.members.map((member) => member.state)).toEqual(["unknown", "unknown"]);
    expect(h.world.connected).toEqual([]);
    expect([status.healthy, status.busy]).toEqual([0, 0]);
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
