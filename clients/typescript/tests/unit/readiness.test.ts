import { create } from "@bufbuild/protobuf";
import { Code, ConnectError } from "@connectrpc/connect";
import { describe, expect, test } from "vitest";
import {
  SandboxError,
  SandboxNotFoundError,
  SandboxNotReadyError,
  SandboxStateError,
} from "../../src/errors.js";
import { HealthResponseSchema } from "../../src/gen/rayito/v1/health_pb.js";
import {
  alreadySuspended,
  healthFromProto,
  healthReconnected,
  isSuspendingReason,
  notReadyError,
  ReadinessPoll,
  ReconnectBudget,
  ReconnectPoll,
  reconnectFailure,
  terminalStateError,
  terminatedDuringBootError,
} from "../../src/sandbox/readiness.js";
import { FakeControlPlane } from "./fake/control-plane.js";

class ManualClock {
  now = 1000;
  tick(): number {
    return this.now;
  }
  advance(ms: number): void {
    this.now += ms;
  }
}

describe("ReadinessPoll", () => {
  test("0.25 s doubling to 2 s, state checks every 5 s, rpc timeout clamped", () => {
    const clock = new ManualClock();
    const poll = new ReadinessPoll({ timeoutMs: 90_000, now: () => clock.tick() });
    const delays = [];
    for (let i = 0; i < 6; i += 1) {
      delays.push(poll.nextDelayMs());
    }
    expect(delays).toEqual([250, 500, 1000, 2000, 2000, 2000]);
    expect(poll.rpcTimeoutMs()).toBe(5000);
    expect(poll.shouldCheckState()).toBe(false);
    clock.advance(5000);
    expect(poll.shouldCheckState()).toBe(true);
    expect(poll.shouldCheckState()).toBe(false);
    clock.advance(84_600);
    expect(poll.rpcTimeoutMs()).toBe(500);
    expect(poll.remainingMs()).toBe(400);
    expect(poll.nextDelayMs()).toBe(400);
    clock.advance(400);
    expect(poll.timedOut()).toBe(true);
    expect(poll.rpcTimeoutMs()).toBe(500);
    expect(poll.elapsedMs()).toBe(90_000);
  });
});

describe("ReconnectPoll", () => {
  test("0.5 s doubling to 4 s with ±25 % jitter from an injected random", () => {
    const poll = new ReconnectPoll({ timeoutMs: 60_000, now: () => 0, random: () => 1 });
    expect([
      poll.nextDelayMs(),
      poll.nextDelayMs(),
      poll.nextDelayMs(),
      poll.nextDelayMs(),
      poll.nextDelayMs(),
    ]).toEqual([625, 1250, 2500, 5000, 5000]);
    const low = new ReconnectPoll({ timeoutMs: 60_000, now: () => 0, random: () => 0 });
    expect(low.nextDelayMs()).toBe(375);
    const mid = new ReconnectPoll({ timeoutMs: 60_000, now: () => 0, random: () => 0.5 });
    expect([
      mid.nextDelayMs(),
      mid.nextDelayMs(),
      mid.nextDelayMs(),
      mid.nextDelayMs(),
      mid.nextDelayMs(),
    ]).toEqual([500, 1000, 2000, 4000, 4000]);
  });

  test("static timing is shared by every instance (tests shorten it)", () => {
    const previous = ReconnectPoll.stateCheckIntervalMs;
    ReconnectPoll.stateCheckIntervalMs = 10;
    try {
      const clock = new ManualClock();
      const poll = new ReconnectPoll({ timeoutMs: 1000, now: () => clock.tick() });
      clock.advance(10);
      expect(poll.shouldCheckState()).toBe(true);
    } finally {
      ReconnectPoll.stateCheckIntervalMs = previous;
    }
    expect(ReadinessPoll.stateCheckIntervalMs).toBe(5000);
  });
});

describe("ReconnectBudget", () => {
  test("three futile reconnects are allowed, the fourth is not", () => {
    const budget = new ReconnectBudget();
    const futile = { resumed: true, generationChanged: false, resumeGeneration: 0 };
    expect([budget.allows(futile), budget.allows(futile), budget.allows(futile)]).toEqual([
      true,
      true,
      true,
    ]);
    expect(budget.allows(futile)).toBe(false);
    expect(budget.allows({ resumed: true, generationChanged: true, resumeGeneration: 1 })).toBe(
      true,
    );
    expect(budget.futile).toBe(0);
  });
});

describe("reconnect helpers", () => {
  const plane = new FakeControlPlane({ endpoint: "127.0.0.1" });

  test("alreadySuspended and isSuspendingReason", () => {
    expect(alreadySuspended(plane.info("SUSPENDED"))).toBe(true);
    expect(alreadySuspended(plane.info("SUSPENDING"))).toBe(true);
    expect(alreadySuspended(plane.info("RUNNING"))).toBe(false);
    expect(isSuspendingReason(new ConnectError("suspending", Code.Unavailable))).toBe(true);
    expect(isSuspendingReason(new ConnectError("HTTP 502", Code.Unavailable))).toBe(false);
    expect(isSuspendingReason(new SandboxStateError("suspending"))).toBe(true);
    expect(isSuspendingReason(new SandboxError("x"))).toBe(false);
  });

  test("reconnectFailure matrix", () => {
    const reason = new ConnectError("HTTP 502", Code.Unavailable);
    expect(reconnectFailure(reason, { info: plane.info("TERMINATED") })).toBeInstanceOf(
      SandboxNotFoundError,
    );
    expect(reconnectFailure(reason, { info: plane.info("TERMINATING") })).toBeInstanceOf(
      SandboxNotFoundError,
    );
    plane.idle = { maxIdleSeconds: 60, suspendedDurationSeconds: 60, autoResume: false };
    const noAutoResume = reconnectFailure(reason, { info: plane.info("SUSPENDED"), wake: true });
    expect(noAutoResume).toBeInstanceOf(SandboxStateError);
    expect(noAutoResume?.message).toContain("resume()");
    expect(
      reconnectFailure(reason, { info: plane.info("SUSPENDED"), wake: false }),
    ).toBeUndefined();
    plane.idle = { maxIdleSeconds: 60, suspendedDurationSeconds: 60, autoResume: true };
    expect(reconnectFailure(reason, { info: plane.info("SUSPENDED"), wake: true })).toBeUndefined();
    expect(reconnectFailure(reason, { info: plane.info("RUNNING") })).toBeUndefined();
    expect(reconnectFailure(reason)).toBeUndefined();
    const deadline = reconnectFailure(reason, { timeoutMs: 60_000 });
    expect(deadline).toBeInstanceOf(SandboxError);
    expect(deadline).not.toBeInstanceOf(SandboxStateError);
    expect(deadline?.message).toContain("60 s");
    expect(deadline?.cause).toBe(reason);
    const suspending = reconnectFailure(new ConnectError("suspending", Code.Unavailable), {
      timeoutMs: 2500,
    });
    expect(suspending).toBeInstanceOf(SandboxStateError);
    expect(suspending?.message).toContain("suspending");
    expect(suspending?.message).toContain("2.5 s");
    const inStream = reconnectFailure(new SandboxStateError("cut"), { timeoutMs: 1000 });
    expect(inStream?.message).toContain("suspending");
  });

  test("healthReconnected requires readiness and, after a suspend, a new generation", () => {
    const ready = create(HealthResponseSchema, {
      agentReady: true,
      kernelReady: true,
      resumeGeneration: 1n,
    });
    const notReady = create(HealthResponseSchema, {
      agentReady: true,
      kernelReady: false,
      resumeGeneration: 1n,
    });
    expect(healthReconnected(undefined, { seenGeneration: 0, suspending: false })).toBe(false);
    expect(healthReconnected(notReady, { seenGeneration: 0, suspending: false })).toBe(false);
    expect(healthReconnected(ready, { seenGeneration: 1, suspending: false })).toBe(true);
    expect(healthReconnected(ready, { seenGeneration: 1, suspending: true })).toBe(false);
    expect(healthReconnected(ready, { seenGeneration: 0, suspending: true })).toBe(true);
  });

  test("healthFromProto converts bigints", () => {
    const health = healthFromProto(
      create(HealthResponseSchema, {
        agentReady: true,
        kernelReady: true,
        agentVersion: "1",
        uptimeMs: 12n,
        sandboxId: "s",
        resumeGeneration: 2n,
        clockOffsetMs: -3n,
        kernelStateLost: true,
      }),
    );
    expect(health).toEqual({
      agentReady: true,
      kernelReady: true,
      agentVersion: "1",
      uptimeMs: 12,
      sandboxId: "s",
      resumeGeneration: 2,
      clockOffsetMs: -3,
      kernelStateLost: true,
    });
  });

  test("error constructors", () => {
    const notReady = notReadyError(plane.info("RUNNING"), {
      readyTimeoutMs: 2000,
      terminated: true,
    });
    expect(notReady).toBeInstanceOf(SandboxNotReadyError);
    expect(notReady.state).toBe("RUNNING");
    expect(notReady.message).toContain("2 s");
    expect(notReady.message).toContain("terminado");
    expect(
      notReadyError(undefined, { readyTimeoutMs: 90_000, terminated: false }).state,
    ).toBeUndefined();
    plane.stateReason = "boot failed";
    expect(terminalStateError(plane.info("TERMINATED")).message).toContain("boot failed");
    const boot = terminatedDuringBootError(plane.info("TERMINATING"));
    expect(boot).toBeInstanceOf(SandboxNotReadyError);
    expect(boot.state).toBe("TERMINATING");
  });
});
