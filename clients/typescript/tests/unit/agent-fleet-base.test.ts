/**
 * La lógica pura de la flota (`agent/fleet-base.ts`, `agent/scaling-domain.ts`):
 * el id de sesión enrutable, la etiqueta de sesión y la colocación de una sesión
 * nueva, contra los vectores compartidos con Python (`testdata/agent-fleet/`).
 * Espejo de `test_agent_fleet_base.py`. Ninguna prueba llama a AWS.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, test } from "vitest";
import { TokenUsage } from "../../src/agent/events.js";
import {
  AGENT_FLEET_BUSY_RETRY_MS,
  AGENT_FLEET_ON_TIMEOUT,
  AGENT_FLEET_PING_TTL_MS,
  correctedCap,
  eventWithHandle,
  type FleetLifetime,
  fleetLifetime,
  fleetSettingsOf,
  holdMs,
  isLost,
  lifetimeMs,
  listedCap,
  mapBounded,
  markExtras,
  memberOf,
  newMember,
  ownedIds,
  planPlacement,
  poolConfigFor,
  recordBusy,
  recordPing,
  restoreMs,
  sandboxCap,
  selectMembers,
  sessionLabel,
  surplus,
  withHandle,
} from "../../src/agent/fleet-base.js";
import { DEFAULT_QUEUE_TIMEOUT_MS, ServeOptions } from "../../src/agent/project.js";
import { replacementsNeeded, ScalingOptions } from "../../src/agent/scaling-domain.js";
import { InvalidArgumentError } from "../../src/errors.js";
import { generateAccessToken } from "../../src/payload.js";
import { slotAccessToken, validatePoolConfig } from "../../src/pool/config.js";
import { beyondCapError } from "../../src/sandbox/lifecycle.js";
import { project, SANDBOX_ID } from "./agent-project-fake.js";

const VECTORS = join(import.meta.dirname, "..", "..", "..", "..", "testdata", "agent-fleet");
const SESSION_LABELS = JSON.parse(readFileSync(join(VECTORS, "session-labels.json"), "utf8"));
const PLACEMENT = JSON.parse(readFileSync(join(VECTORS, "placement.json"), "utf8"));
const MEMBERS = JSON.parse(readFileSync(join(VECTORS, "members.json"), "utf8"));
const LIFETIME = JSON.parse(readFileSync(join(VECTORS, "lifetime.json"), "utf8"));
const NOW = 1_000_000;
const MS_PER_SECOND = 1000;

describe("handles", () => {
  test("session labels hash the caller-chosen id", () => {
    for (const item of SESSION_LABELS.sessionLabels) {
      expect(sessionLabel(item.sessionId)).toBe(item.label);
      expect(item.label).not.toContain(item.sessionId);
    }
  });

  test("the result and the terminal events carry the handle", () => {
    const usage = new TokenUsage();
    const result = withHandle(
      {
        sessionId: "ses_1",
        text: "hola",
        steps: 1,
        usage,
        exitCode: 0,
        toolCalls: [],
        droppedLines: 0,
      },
      SANDBOX_ID,
    );
    const done = eventWithHandle(
      { type: "done", sessionId: "ses_1", exitCode: 0, usage },
      SANDBOX_ID,
    );
    const failed = eventWithHandle(
      {
        type: "agent_failed",
        reason: "timeout",
        exitCode: undefined,
        detailCode: undefined,
        sessionId: "ses_1",
      },
      SANDBOX_ID,
    );
    const bare = eventWithHandle(
      {
        type: "agent_failed",
        reason: "timeout",
        exitCode: undefined,
        detailCode: undefined,
        sessionId: undefined,
      },
      SANDBOX_ID,
    );
    const handle = `rs1.${SANDBOX_ID}.ses_1`;
    expect(result.sessionId).toBe(handle);
    expect((done as { sessionId: string }).sessionId).toBe(handle);
    expect((failed as { sessionId: string }).sessionId).toBe(handle);
    expect((bare as { sessionId?: string }).sessionId).toBeUndefined();
  });
});

interface PlacementCase {
  name: string;
  scaling: { minVms: number; maxVms: number; warmVms: number; sessions: "shared" | "dedicated" };
  growing: number;
  maxConcurrency: number;
  members: {
    id: string;
    inflight: number;
    health: "healthy" | "busy" | "unavailable" | "unknown";
    bound: string[];
    pingAgeSeconds: number | null;
    busyRemainingSeconds: number | null;
  }[];
  expected: { kind: string; memberId: string | null };
}

describe("placement", () => {
  test.each(PLACEMENT.cases as PlacementCase[])("$name", (item) => {
    const raw = item.scaling;
    const scaling = new ScalingOptions({
      minVms: raw.minVms,
      maxVms: raw.maxVms,
      warmVms: raw.warmVms,
      sessions: raw.sessions,
    });
    const members = item.members.map((data) => {
      const member = newMember<unknown>(data.id);
      member.inflight = data.inflight;
      member.health = data.health;
      member.bound.clear();
      for (const bound of data.bound) {
        member.bound.add(bound);
      }
      member.pingedAt =
        data.pingAgeSeconds === null ? undefined : NOW - data.pingAgeSeconds * MS_PER_SECOND;
      member.busyUntil =
        data.busyRemainingSeconds === null ? 0 : NOW + data.busyRemainingSeconds * MS_PER_SECOND;
      return member;
    });
    const plan = planPlacement(members, item.growing, scaling, item.maxConcurrency, NOW);
    expect(plan.kind).toBe(item.expected.kind);
    expect(plan.member?.sandboxId).toBe(item.expected.memberId ?? undefined);
  });
});

describe("health", () => {
  test("pings count failures and a healthy one resets them", () => {
    const member = newMember<unknown>("microvm-a");
    recordPing(member, "unavailable", NOW);
    recordPing(member, "unavailable", NOW);
    expect(isLost(member)).toBe(false);
    recordPing(member, "healthy", NOW);
    expect(member.failedPings).toBe(0);
    for (let count = 0; count < 3; count += 1) {
      recordPing(member, "unavailable", NOW);
    }
    expect(isLost(member)).toBe(true);
  });

  test("a busy answer marks the member without counting a failure", () => {
    const member = newMember<unknown>("microvm-a");
    recordBusy(member, NOW);
    expect(member.health).toBe("busy");
    expect(member.failedPings).toBe(0);
    expect(member.busyUntil).toBe(NOW + AGENT_FLEET_BUSY_RETRY_MS);
  });

  test("a busy ping lasts until the ping expires and a healthy one clears it", () => {
    const member = newMember<unknown>("microvm-a");
    recordPing(member, "busy", NOW);
    expect(member.busyUntil).toBe(NOW + AGENT_FLEET_PING_TTL_MS);
    recordPing(member, "healthy", NOW);
    expect(member.busyUntil).toBe(0);
  });
});

describe("scaling", () => {
  test("the fleet keeps minVms and marks the rest as extra", () => {
    const scaling = new ScalingOptions({ minVms: 2, maxVms: 4 });
    const members = [1, 2, 3, 4].map((number) => newMember<unknown>(`microvm-${number}`));
    markExtras(members, scaling);
    expect(members.map((member) => member.extra)).toEqual([false, false, true, true]);
    expect(replacementsNeeded(1, scaling)).toBe(1);
    expect(replacementsNeeded(3, scaling)).toBe(0);
    expect(
      surplus(members, new ScalingOptions({ minVms: 1, maxVms: 3 })).map((m) => m.sandboxId),
    ).toEqual(["microvm-4"]);
  });

  test("defaults are the single VM of deploy", () => {
    const scaling = new ScalingOptions();
    expect([scaling.minVms, scaling.maxVms, scaling.warmVms]).toEqual([1, 1, 0]);
    expect(scaling.sessions).toBe("shared");
    expect(scaling.routesByHandle).toBe(false);
    expect(new ScalingOptions({ maxVms: 2 }).routesByHandle).toBe(true);
  });

  test.each([
    { minVms: 2, maxVms: 1 },
    { maxVms: 1, warmVms: 1 },
    { maxVms: 65 },
    { sessions: "otra" },
    { scaleInAfterMs: 1000 },
    { warmVms: -1 },
    { minVms: 1.5 },
  ])("rejects %j", (options) => {
    expect(() => new ScalingOptions(options as never)).toThrow(InvalidArgumentError);
  });
});

describe("pool token", () => {
  test("a fixed access token is validated and shared by every slot", () => {
    const token = generateAccessToken();
    expect(slotAccessToken(validatePoolConfig({ size: 2, accessToken: token }))).toBe(token);
    expect(slotAccessToken(validatePoolConfig({ size: 2 }))).not.toBe(
      slotAccessToken(validatePoolConfig({ size: 2 })),
    );
    expect(() => validatePoolConfig({ size: 1, accessToken: "demasiado-corto" })).toThrow(
      InvalidArgumentError,
    );
  });
});

describe("settings", () => {
  test("read the project and let an explicit scaling win", () => {
    const the = project({ serve: new ServeOptions({ maxConcurrency: 3 }) });
    const explicit = new ScalingOptions({ maxVms: 2 });
    const settings = fleetSettingsOf(the, explicit);
    expect(settings.scaling).toBe(explicit);
    expect(settings.maxConcurrency).toBe(3);
    expect(settings.queueTimeoutMs).toBe(DEFAULT_QUEUE_TIMEOUT_MS);
    expect(fleetSettingsOf(undefined).scaling).toEqual(new ScalingOptions());
  });

  test("the warm pool launches like a served VM with the fleet token", () => {
    const the = project({
      allowInternetAccess: false,
      serve: new ServeOptions({ timeoutMs: 3_600_000 }),
    });
    const token = generateAccessToken();
    const config = poolConfigFor(
      the,
      new ScalingOptions({ minVms: 1, maxVms: 3, warmVms: 2 }),
      token,
      {
        "rayito.agent.deployment": "support-bot",
      },
    );
    expect(config.size).toBe(2);
    expect(config.template).toBe("support-bot");
    expect(config.timeoutMs).toBe(3_600_000);
    expect(config.accessToken).toBe(token);
    expect(config.allowInternetAccess).toBe(false);
    expect(config.metadata).toEqual({ "rayito.agent.deployment": "support-bot" });
  });

  test("the warm pool launches the pinned image version, if any", () => {
    const scaling = new ScalingOptions({ minVms: 1, maxVms: 3, warmVms: 2 });
    const token = generateAccessToken();
    expect(poolConfigFor(project(), scaling, token, {}, "2.0").templateVersion).toBe("2.0");
    expect(poolConfigFor(project(), scaling, token, {})).not.toHaveProperty("templateVersion");
  });

  test("the warm pool carries the project env and network", () => {
    const the = project({
      env: { LOG_LEVEL: "debug" },
      network: { allowOut: ["api.example.com"], denyOut: ["0.0.0.0/0"] },
    });
    const config = poolConfigFor(
      the,
      new ScalingOptions({ maxVms: 2, warmVms: 1 }),
      generateAccessToken(),
      {},
    );
    expect(config.envs).toEqual({ LOG_LEVEL: "debug" });
    expect(config.network).toEqual({ allowOut: ["api.example.com"], denyOut: ["0.0.0.0/0"] });
  });

  test("the warm pool sends no env or network by default", () => {
    const config = poolConfigFor(
      project(),
      new ScalingOptions({ maxVms: 2, warmVms: 1 }),
      generateAccessToken(),
      {},
    );
    expect(config).not.toHaveProperty("envs");
    expect(config).not.toHaveProperty("network");
  });

  test.each(
    MEMBERS.ownedIds as {
      name: string;
      items: { id: string; metadata: Record<string, string> | null }[];
      expected: string[];
    }[],
  )("only agent labels prove ownership: $name", (item) => {
    const probed = item.items.map((entry) => ({
      sandboxId: entry.id,
      metadata: entry.metadata ?? undefined,
    }));
    expect([...ownedIds(probed)]).toEqual(item.expected);
  });

  test.each(
    MEMBERS.selectMembers as {
      name: string;
      items: { id: string; state: string }[];
      owned: string[];
      unreadable?: string[];
      unproven?: string[];
      expected: string[];
    }[],
  )("a vm is a member only when the image is not shared: $name", (item) => {
    const listing = item.items.map((entry) => ({
      sandboxId: entry.id,
      state: entry.state,
      startedAt: new Date(0),
    }));
    const selected = selectMembers(listing, new Set(item.owned), new Set(item.unreadable ?? []));
    expect(selected.map((entry) => entry.sandboxId)).toEqual(item.expected);
    if (item.unproven !== undefined) {
      expect(
        selected.filter((entry) => entry.proven === false).map((entry) => entry.sandboxId),
      ).toEqual(item.unproven);
    }
  });
});

interface CreateArgsCase {
  readonly name: string;
  readonly serveTimeoutSeconds: number;
  readonly minVms: number;
  readonly maxVms: number;
  readonly expected: { readonly maxLifetimeSeconds: number; readonly onTimeout: string } | null;
}

interface LifetimeSecondsCase {
  readonly name: string;
  readonly desiredSeconds: number | null;
  readonly capInSeconds: number | null;
  readonly expected: number | null;
}

interface ListedCapCase {
  readonly name: string;
  readonly maxLifetimeSeconds: number | null;
  readonly expectedCapAfterStartSeconds: number | null;
}

interface SandboxCapCase {
  readonly name: string;
  readonly platformCapInSeconds: number | null;
  readonly reportedCapInSeconds: number | null;
  readonly expected: number | null;
}

interface PromotedRestoreCase {
  readonly name: string;
  readonly maxLifetimeSeconds: number;
  readonly startedSecondsAgo: number;
  readonly desiredSeconds: number | null;
  readonly expected: number | null;
}

interface CorrectedCapCase {
  readonly name: string;
  readonly currentCapInSeconds: number | null;
  readonly reportedCapInSeconds: number | null;
  readonly expected: number | null;
}

const LIFETIME_NOW = Date.UTC(2026, 9, 22);

function inSeconds(seconds: number | null): Date | undefined {
  return seconds === null ? undefined : new Date(LIFETIME_NOW + seconds * 1000);
}

function fleetLifetimeOf(maxLifetimeSeconds: number): FleetLifetime {
  return { maxLifetimeMs: maxLifetimeSeconds * 1000, onTimeout: AGENT_FLEET_ON_TIMEOUT };
}

interface RestoreSecondsCase {
  readonly name: string;
  readonly capInSeconds: number | null;
  readonly expected: number | null;
}

interface HoldSecondsCase {
  readonly name: string;
  readonly scaleInSeconds: number;
  readonly requestTimeoutSeconds: number | null;
  readonly expected: number;
}

describe("fleet lifetime", () => {
  test.each(LIFETIME.createArgs as CreateArgsCase[])("$name", (entry) => {
    const the = project({
      serve: new ServeOptions({ timeoutMs: entry.serveTimeoutSeconds * 1000 }),
    });
    const scaling = new ScalingOptions({ minVms: entry.minVms, maxVms: entry.maxVms });
    const lifetime = fleetLifetime(the, scaling);
    if (entry.expected === null) {
      expect(lifetime).toBeUndefined();
      return;
    }
    expect(lifetime).toEqual({
      maxLifetimeMs: entry.expected.maxLifetimeSeconds * 1000,
      onTimeout: entry.expected.onTimeout,
    });
  });

  test.each(LIFETIME.lifetimeSeconds as LifetimeSecondsCase[])("$name", (entry) => {
    const now = Date.UTC(2026, 9, 22);
    const cap = entry.capInSeconds === null ? undefined : new Date(now + entry.capInSeconds * 1000);
    const desired = entry.desiredSeconds === null ? undefined : entry.desiredSeconds * 1000;
    const expected = entry.expected === null ? undefined : entry.expected * 1000;
    expect(lifetimeMs(desired, cap, now)).toBe(expected);
  });

  test.each(LIFETIME.restoreSeconds as RestoreSecondsCase[])("$name", (entry) => {
    const now = Date.UTC(2026, 9, 22);
    const cap = entry.capInSeconds === null ? undefined : new Date(now + entry.capInSeconds * 1000);
    const expected = entry.expected === null ? undefined : entry.expected * 1000;
    expect(restoreMs(cap, now)).toBe(expected);
  });

  test.each(LIFETIME.holdSeconds as HoldSecondsCase[])("$name", (entry) => {
    const scaling = new ScalingOptions({ maxVms: 2, scaleInAfterMs: entry.scaleInSeconds * 1000 });
    const request =
      entry.requestTimeoutSeconds === null ? undefined : entry.requestTimeoutSeconds * 1000;
    expect(holdMs(scaling, request)).toBe(entry.expected * 1000);
  });

  test.each(LIFETIME.listedCap as ListedCapCase[])("$name", (entry) => {
    const started = new Date(LIFETIME_NOW);
    const lifetime =
      entry.maxLifetimeSeconds === null ? undefined : fleetLifetimeOf(entry.maxLifetimeSeconds);
    expect(listedCap(started, lifetime)).toEqual(inSeconds(entry.expectedCapAfterStartSeconds));
  });

  test.each(LIFETIME.sandboxCap as SandboxCapCase[])("$name", (entry) => {
    const info = {
      platformExpiresAt: inSeconds(entry.platformCapInSeconds),
      lifecycle: { cap: inSeconds(entry.reportedCapInSeconds) },
    };
    expect(sandboxCap({ info })).toEqual(inSeconds(entry.expected));
  });

  test.each(LIFETIME.promotedRestore as PromotedRestoreCase[])("$name", (entry) => {
    const started = new Date(LIFETIME_NOW - entry.startedSecondsAgo * 1000);
    const cap = listedCap(started, fleetLifetimeOf(entry.maxLifetimeSeconds));
    const desired = entry.desiredSeconds === null ? undefined : entry.desiredSeconds * 1000;
    const expected = entry.expected === null ? undefined : entry.expected * 1000;
    expect(lifetimeMs(desired, cap, LIFETIME_NOW)).toBe(expected);
  });

  test.each(LIFETIME.correctedCap as CorrectedCapCase[])("$name", (entry) => {
    const reported = inSeconds(entry.reportedCapInSeconds);
    const error = beyondCapError(1000, reported?.getTime());
    expect(correctedCap(error, inSeconds(entry.currentCapInSeconds))).toEqual(
      inSeconds(entry.expected),
    );
  });

  test("the warm pool of a fleet that can grow carries the lifetime", () => {
    const config = poolConfigFor(
      project(),
      new ScalingOptions({ maxVms: 3, warmVms: 1 }),
      generateAccessToken(),
      {},
    );
    expect(config.maxLifetimeMs).toBe(3_660_000);
    expect(config.onTimeout).toBe("kill");
  });
});

describe("mapBounded", () => {
  test("an empty input does no work", async () => {
    let calls = 0;
    expect(await mapBounded([], 8, async () => (calls += 1))).toEqual([]);
    expect(calls).toBe(0);
  });

  test("a failure starts no more work and rejects with that error", async () => {
    const started: number[] = [];
    const failure = new Error("limitado");
    const items = Array.from({ length: 20 }, (_, index) => index);
    await expect(
      mapBounded(items, 2, async (item) => {
        started.push(item);
        if (item === 0) {
          throw failure;
        }
        await new Promise((resolve) => setTimeout(resolve, 5));
        return item;
      }),
    ).rejects.toBe(failure);
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(started.length).toBeLessThan(items.length);
  });

  test("a limit below one still runs one at a time, in order", async () => {
    expect(await mapBounded([3, 1, 2], 0, async (item) => item * 2)).toEqual([6, 2, 4]);
    expect(await mapBounded([3, 1, 2], Number.NaN, async (item) => item * 2)).toEqual([6, 2, 4]);
  });
});

describe("memberOf", () => {
  test("a known member becomes proven when a listing proves it", () => {
    const listed = { sandboxId: "a", state: "SUSPENDED", startedAt: new Date(0) };
    const known = { ...newMember("a"), proven: false };
    expect(memberOf(known, { ...listed, proven: false }, undefined)).toBe(known);
    expect(known.proven).toBe(false);
    memberOf(known, listed, undefined);
    expect(known.proven).toBe(true);
    memberOf(known, { ...listed, proven: false }, undefined);
    expect(known.proven).toBe(true);
    expect(memberOf(undefined, { ...listed, proven: false }, undefined).proven).toBe(false);
  });
});
