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
  AGENT_FLEET_PING_TTL_MS,
  eventWithHandle,
  fleetSettingsOf,
  isLost,
  markExtras,
  newMember,
  planPlacement,
  poolConfigFor,
  recordBusy,
  recordPing,
  sessionLabel,
  surplus,
  withHandle,
} from "../../src/agent/fleet-base.js";
import { DEFAULT_QUEUE_TIMEOUT_MS, ServeOptions } from "../../src/agent/project.js";
import { replacementsNeeded, ScalingOptions } from "../../src/agent/scaling-domain.js";
import { InvalidArgumentError } from "../../src/errors.js";
import { generateAccessToken } from "../../src/payload.js";
import { slotAccessToken, validatePoolConfig } from "../../src/pool/config.js";
import { project, SANDBOX_ID } from "./agent-project-fake.js";

const VECTORS = join(import.meta.dirname, "..", "..", "..", "..", "testdata", "agent-fleet");
const SESSION_LABELS = JSON.parse(readFileSync(join(VECTORS, "session-labels.json"), "utf8"));
const PLACEMENT = JSON.parse(readFileSync(join(VECTORS, "placement.json"), "utf8"));
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
});
