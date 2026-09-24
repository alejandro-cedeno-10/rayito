import { create } from "@bufbuild/protobuf";
import { describe, expect, test } from "vitest";
import {
  InvalidArgumentError,
  LifecycleUnsupportedError,
  SandboxLifetimeError,
} from "../../src/errors.js";
import {
  LifecyclePhase,
  LifecycleStateSchema,
  TimeoutAction,
} from "../../src/gen/rayito/v1/lifecycle_pb.js";
import type { SandboxLifecycle } from "../../src/models.js";
import {
  autoResumeReopenMs,
  beyondCapError,
  capFromDetail,
  connectExtension,
  defaultMaxLifetimeMs,
  lifecycleBlockToWire,
  lifecycleFromProto,
  olderAgentError,
  pauseTriggerDelayMs,
  resolveLifecycle,
  validateSetTimeoutMs,
} from "../../src/sandbox/lifecycle.js";

const NOW = Date.UTC(2026, 8, 22, 12, 0, 0);

function lifecycle(overrides: Partial<SandboxLifecycle> = {}): SandboxLifecycle {
  return {
    phase: "active",
    deadline: new Date(NOW + 90_000),
    cap: new Date(NOW + 600_000),
    timeoutMs: 60_000,
    onTimeout: "kill",
    autoResume: false,
    extensions: 0,
    ...overrides,
  };
}

describe("resolveLifecycle", () => {
  test("no block without maxLifetimeMs or onTimeout: the pre-M9 launch", () => {
    const plan = resolveLifecycle({
      timeoutSeconds: 900,
      maxLifetimeMs: undefined,
      onTimeout: undefined,
      idle: undefined,
    });
    expect(plan.block).toBeUndefined();
    expect(plan.platformDurationSeconds).toBe(900);
    expect(plan.idle).toEqual({
      maxIdleSeconds: 300,
      suspendedDurationSeconds: 600,
      autoResume: true,
    });
  });

  test("kill block and platform duration", () => {
    const plan = resolveLifecycle({
      timeoutSeconds: 60,
      maxLifetimeMs: 900_000,
      onTimeout: "kill",
      idle: null,
    });
    expect(plan.block).toEqual({ timeoutS: 60, capS: 900, onTimeout: "kill", autoResume: false });
    expect(plan.platformDurationSeconds).toBe(900);
    expect(plan.idle).toBeUndefined();
    const withIdle = resolveLifecycle({
      timeoutSeconds: 60,
      maxLifetimeMs: 900_000,
      onTimeout: undefined,
      idle: { maxIdleSeconds: 120, autoResume: true },
    });
    expect(withIdle.block?.onTimeout).toBe("kill");
    expect(withIdle.block?.autoResume).toBe(false);
    expect(withIdle.idle).toEqual({
      maxIdleSeconds: 120,
      suspendedDurationSeconds: 780,
      autoResume: true,
    });
  });

  test("the default maxLifetimeMs is timeout plus the 60 s margin, at least 120 s", () => {
    expect(defaultMaxLifetimeMs(60)).toBe(120_000);
    expect(defaultMaxLifetimeMs(300)).toBe(360_000);
    expect(defaultMaxLifetimeMs(28_800)).toBe(28_800_000);
    const plan = resolveLifecycle({
      timeoutSeconds: 300,
      maxLifetimeMs: undefined,
      onTimeout: "kill",
      idle: null,
    });
    expect(plan.block).toEqual({ timeoutS: 300, capS: 360, onTimeout: "kill", autoResume: false });
  });

  test("pause requires idle", () => {
    expect(() =>
      resolveLifecycle({
        timeoutSeconds: 60,
        maxLifetimeMs: 900_000,
        onTimeout: "pause",
        idle: null,
      }),
    ).toThrow(InvalidArgumentError);
    expect(() =>
      resolveLifecycle({
        timeoutSeconds: 60,
        maxLifetimeMs: 900_000,
        onTimeout: "pause",
        idle: null,
      }),
    ).toThrow(/idle/);
  });

  test("pause forces platform auto-resume and keeps the logical flag", () => {
    const plan = resolveLifecycle({
      timeoutSeconds: 60,
      maxLifetimeMs: 900_000,
      onTimeout: "pause",
      idle: { autoResume: false },
    });
    expect(plan.platformDurationSeconds).toBe(900);
    expect(plan.idle).toEqual({
      maxIdleSeconds: 300,
      suspendedDurationSeconds: 600,
      autoResume: true,
    });
    expect(lifecycleBlockToWire(plan.block as NonNullable<typeof plan.block>)).toEqual({
      auto_resume: false,
      cap_s: 900,
      on_timeout: "pause",
      timeout_s: 60,
    });
    const defaults = resolveLifecycle({
      timeoutSeconds: 60,
      maxLifetimeMs: 900_000,
      onTimeout: "pause",
      idle: undefined,
    });
    expect(defaults.block?.autoResume).toBe(true);
  });

  test("pause rejects an explicit suspended duration and an idle longer than the cap", () => {
    expect(() =>
      resolveLifecycle({
        timeoutSeconds: 60,
        maxLifetimeMs: 900_000,
        onTimeout: "pause",
        idle: { suspendedDurationSeconds: 10 },
      }),
    ).toThrow(InvalidArgumentError);
    expect(() =>
      resolveLifecycle({
        timeoutSeconds: 60,
        maxLifetimeMs: 240_000,
        onTimeout: "pause",
        idle: { maxIdleSeconds: 300 },
      }),
    ).toThrow(/maxIdleSeconds/);
  });

  test("maxLifetimeMs bounds", () => {
    const base = { timeoutSeconds: 60, onTimeout: "kill", idle: null } as const;
    expect(() => resolveLifecycle({ ...base, maxLifetimeMs: 28_801_000 })).toThrow(
      SandboxLifetimeError,
    );
    expect(() => resolveLifecycle({ ...base, maxLifetimeMs: 28_801_000 })).toThrow(/28800/);
    expect(() => resolveLifecycle({ ...base, maxLifetimeMs: 28_801_000 })).toThrow(/maxLifetimeMs/);
    expect(() => resolveLifecycle({ ...base, maxLifetimeMs: 119_000 })).toThrow(
      InvalidArgumentError,
    );
    expect(() => resolveLifecycle({ ...base, maxLifetimeMs: 900_500 })).toThrow(
      InvalidArgumentError,
    );
    expect(() => resolveLifecycle({ ...base, maxLifetimeMs: Number.NaN })).toThrow(
      InvalidArgumentError,
    );
    expect(() =>
      resolveLifecycle({ ...base, timeoutSeconds: 901, maxLifetimeMs: 900_000 }),
    ).toThrow(/no puede superar maxLifetimeMs/);
    expect(resolveLifecycle({ ...base, maxLifetimeMs: 28_800_000 }).platformDurationSeconds).toBe(
      28_800,
    );
  });

  test("onTimeout outside kill and pause is refused", () => {
    expect(() =>
      resolveLifecycle({
        timeoutSeconds: 60,
        maxLifetimeMs: undefined,
        onTimeout: "freeze",
        idle: null,
      }),
    ).toThrow(/onTimeout debe ser 'kill' o 'pause'/);
  });
});

describe("reading the LifecycleState", () => {
  test("lifecycleFromProto is undefined on an older agent", () => {
    expect(lifecycleFromProto(undefined)).toBeUndefined();
  });

  test("a managed state maps every field in camelCase", () => {
    const parsed = lifecycleFromProto(
      create(LifecycleStateSchema, {
        phase: LifecyclePhase.RESUME_GRACE,
        deadlineUnixMs: BigInt(NOW + 90_000),
        capUnixMs: BigInt(NOW + 600_000),
        timeoutMs: 60_000n,
        onTimeout: TimeoutAction.PAUSE,
        autoResume: true,
        extensions: 2,
      }),
    );
    expect(parsed).toEqual({
      phase: "resumeGrace",
      deadline: new Date(NOW + 90_000),
      cap: new Date(NOW + 600_000),
      timeoutMs: 60_000,
      onTimeout: "pause",
      autoResume: true,
      extensions: 2,
    });
  });

  test("unmanaged reports no instants and no action", () => {
    const parsed = lifecycleFromProto(
      create(LifecycleStateSchema, { phase: LifecyclePhase.UNMANAGED }),
    );
    expect(parsed).toMatchObject({
      phase: "unmanaged",
      deadline: undefined,
      cap: undefined,
      onTimeout: undefined,
    });
    expect(
      lifecycleFromProto(create(LifecycleStateSchema, { phase: LifecyclePhase.EXPIRED }))?.phase,
    ).toBe("expired");
  });
});

describe("moving the deadline", () => {
  test("validateSetTimeoutMs", () => {
    expect(validateSetTimeoutMs(150_000)).toBe(150_000);
    expect(validateSetTimeoutMs(1000)).toBe(1000);
    expect(() => validateSetTimeoutMs(999)).toThrow(InvalidArgumentError);
    expect(() => validateSetTimeoutMs(1.5)).toThrow(InvalidArgumentError);
    expect(() => validateSetTimeoutMs("60")).toThrow(InvalidArgumentError);
    expect(() => validateSetTimeoutMs(28_800_001)).toThrow(/maxLifetimeMs/);
  });

  test("the connect extension table", () => {
    expect(connectExtension(undefined, undefined, NOW)).toBeUndefined();
    expect(() => connectExtension(undefined, 300_000, NOW)).toThrow(LifecycleUnsupportedError);
    const unmanaged = lifecycle({ phase: "unmanaged", deadline: undefined, cap: undefined });
    expect(connectExtension(unmanaged, undefined, NOW)).toBeUndefined();
    expect(() => connectExtension(unmanaged, 300_000, NOW)).toThrow(/maxLifetimeMs/);
    expect(() => connectExtension(unmanaged, 300_000, NOW)).not.toThrow(LifecycleUnsupportedError);
    expect(connectExtension(lifecycle(), 300_000, NOW)).toBe(300_000);
    expect(connectExtension(lifecycle(), undefined, NOW)).toBeUndefined();
    for (const phase of ["resumeGrace", "expired"] as const) {
      expect(connectExtension(lifecycle({ phase }), 120_000, NOW)).toBe(120_000);
      expect(connectExtension(lifecycle({ phase }), undefined, NOW)).toBe(60_000);
      expect(
        connectExtension(lifecycle({ phase, cap: new Date(NOW + 30_000) }), undefined, NOW),
      ).toBe(25_000);
      expect(() =>
        connectExtension(lifecycle({ phase, cap: new Date(NOW + 5500) }), undefined, NOW),
      ).toThrow(SandboxLifetimeError);
      expect(() =>
        connectExtension(lifecycle({ phase, cap: new Date(NOW + 5500) }), undefined, NOW),
      ).toThrow(/reincarnate\(\)/);
    }
  });

  test("the pause trigger delay", () => {
    expect(pauseTriggerDelayMs(undefined, NOW)).toBeUndefined();
    expect(pauseTriggerDelayMs(lifecycle(), NOW)).toBeUndefined();
    const pause = lifecycle({ onTimeout: "pause" });
    expect(pauseTriggerDelayMs(pause, NOW)).toBe(91_000);
    expect(pauseTriggerDelayMs({ ...pause, deadline: new Date(NOW - 5000) }, NOW)).toBe(1000);
    expect(pauseTriggerDelayMs({ ...pause, phase: "expired" }, NOW)).toBe(0);
    expect(pauseTriggerDelayMs({ ...pause, phase: "resumeGrace" }, NOW)).toBeUndefined();
    expect(pauseTriggerDelayMs({ ...pause, phase: "unmanaged" }, NOW)).toBeUndefined();
  });

  test("the auto-resume reopen after a resumed deadline pause", () => {
    const expired = lifecycle({ phase: "expired", onTimeout: "pause", autoResume: true });
    const reopen = (state: SandboxLifecycle | undefined, paused: number | undefined, now = 1) =>
      autoResumeReopenMs(state, { pausedGeneration: paused, generation: now, nowUnixMs: NOW });
    expect(reopen(expired, 0)).toBe(300_000);
    expect(reopen({ ...expired, timeoutMs: 450_000 }, 2, 3)).toBe(450_000);
    expect(reopen({ ...expired, cap: new Date(NOW + 100_000) }, 0)).toBe(95_000);
    expect(reopen({ ...expired, cap: new Date(NOW + 5500) }, 0)).toBeUndefined();
    expect(reopen(expired, undefined)).toBeUndefined();
    expect(reopen(expired, 1)).toBeUndefined();
    expect(reopen(undefined, 0)).toBeUndefined();
    expect(reopen({ ...expired, phase: "active" }, 0)).toBeUndefined();
    expect(reopen({ ...expired, phase: "resumeGrace" }, 0)).toBeUndefined();
    expect(reopen({ ...expired, autoResume: false }, 0)).toBeUndefined();
    expect(reopen({ ...expired, onTimeout: "kill" }, 0)).toBeUndefined();
  });

  test("the beyond-cap message names maxLifetimeMs, 28800 and reincarnate()", () => {
    const error = beyondCapError(2_000_000, NOW + 840_000);
    expect(error).toBeInstanceOf(InvalidArgumentError);
    expect(error.message).toContain("maxLifetimeMs");
    expect(error.message).toContain("28800");
    expect(error.message).toContain("reincarnate()");
    expect(error.message).toContain(new Date(NOW + 840_000).toISOString());
    expect(capFromDetail(`timeout beyond cap; cap_unix_ms=${NOW}`)).toBe(NOW);
    expect(capFromDetail("timeout beyond cap")).toBeUndefined();
  });

  test("the older-agent error names the template, its agent version and M9", () => {
    const error = olderAgentError("rayito-base", "0.2.0");
    expect(error).toBeInstanceOf(LifecycleUnsupportedError);
    expect(error).toBeInstanceOf(InvalidArgumentError);
    expect(error.message).toContain("rayito-base");
    expect(error.message).toContain("0.2.0");
    expect(error.message).toContain("publica una imagen M9");
  });
});
