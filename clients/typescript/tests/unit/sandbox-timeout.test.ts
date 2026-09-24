/**
 * El plazo lógico que impone `rayd` (ADR-011) visto desde `Sandbox`:
 * `setTimeout` estático y de instancia, `connect({ timeoutMs })` estático y
 * de instancia, la reapertura de `resume()`, `getInfo().expiresAt`, la
 * puerta de un agente anterior a M9, la puerta `sandbox_timeout` de las
 * unarias y el disparador del modo `pause`, todo contra los fakes.
 */

import { Code, ConnectError } from "@connectrpc/connect";
import { describe, expect, test, vi } from "vitest";
import {
  AuthenticationError,
  InvalidArgumentError,
  LifecycleUnsupportedError,
  SandboxNotFoundError,
  SandboxStateError,
  TimeoutError,
} from "../../src/errors.js";
import { LifecyclePhase, TimeoutMode } from "../../src/gen/rayito/v1/lifecycle_pb.js";
import type { SandboxPool } from "../../src/pool/pool.js";
import { lifecycleFromProto } from "../../src/sandbox/lifecycle.js";
import { S3Prefix } from "../../src/sandbox/persistence.js";
import { Sandbox, type SandboxCreateOptions } from "../../src/sandbox/sandbox.js";
import { FakeControlPlane, IMAGE_ARN, SANDBOX_ID } from "./fake/control-plane.js";
import { managedLifecycle, unmanagedLifecycle } from "./fake/lifecycle.js";
import {
  ACCESS_TOKEN,
  createTestSandbox,
  startRayd,
  type TestSandbox,
  waitUntil,
} from "./helpers.js";

const KILL_LAUNCH: Partial<SandboxCreateOptions> = {
  timeoutMs: 60_000,
  maxLifetimeMs: 900_000,
  onTimeout: "kill",
};
const PAUSE_LAUNCH: Partial<SandboxCreateOptions> = {
  timeoutMs: 60_000,
  maxLifetimeMs: 900_000,
  onTimeout: "pause",
  idle: { maxIdleSeconds: 300, autoResume: true },
};

function managedSandbox(
  create: Partial<SandboxCreateOptions> = KILL_LAUNCH,
  onTimeout: "kill" | "pause" = "kill",
): Promise<TestSandbox> {
  return createTestSandbox({
    create,
    beforeCreate: (rayd) => {
      rayd.health.lifecycle = managedLifecycle({ onTimeout });
    },
  });
}

function deadlineOf(test: TestSandbox): Date {
  return new Date(Number(test.rayd.health.lifecycle?.deadlineUnixMs ?? 0n));
}

describe("moving the deadline", () => {
  test("setTimeout sends EXACT, connect({ timeoutMs }) AT_LEAST, getInfo reads the deadline", async () => {
    const managed = await managedSandbox();
    const { sandbox, rayd, plane } = managed;
    plane.setStates(["RUNNING"]);
    await sandbox.setTimeout(150_000);
    const other = await Sandbox.connect(SANDBOX_ID, {
      accessToken: ACCESS_TOKEN,
      controlPlane: plane,
      transport: rayd.transport,
      timeoutMs: 300_000,
    });
    other.close();
    expect(
      rayd.lifecycle.calls.map((call) => ({ timeoutMs: call.timeoutMs, mode: call.mode })),
    ).toEqual([
      { timeoutMs: 150_000, mode: TimeoutMode.EXACT },
      { timeoutMs: 300_000, mode: TimeoutMode.AT_LEAST },
    ]);
    const info = await sandbox.getInfo();
    expect(info.expiresAt).toEqual(deadlineOf(managed));
    expect(info.lifecycle?.extensions).toBe(2);
    expect(info.platformExpiresAt).not.toEqual(info.expiresAt);
  });

  test("getInfo reads Health only under a managed deadline, so polling it never keeps an unmanaged sandbox awake", async () => {
    const unmanaged = await createTestSandbox({
      beforeCreate: (rayd) => {
        rayd.health.lifecycle = unmanagedLifecycle();
      },
    });
    const older = await createTestSandbox();
    const managed = await managedSandbox();
    for (const { plane } of [unmanaged, older, managed]) {
      plane.setStates(["RUNNING"]);
    }
    const probes = [unmanaged, older, managed].map(({ rayd }) => rayd.health.healthCalls.length);
    for (const { sandbox } of [unmanaged, older, managed]) {
      await sandbox.getInfo();
      await sandbox.getInfo();
    }
    expect(unmanaged.rayd.health.healthCalls).toHaveLength(probes[0] ?? 0);
    expect(older.rayd.health.healthCalls).toHaveLength(probes[1] ?? 0);
    expect(managed.rayd.health.healthCalls).toHaveLength((probes[2] ?? 0) + 2);
  });

  test("connect({ timeoutMs }) never shortens a later deadline", async () => {
    const managed = await managedSandbox();
    const { rayd, plane } = managed;
    plane.setStates(["RUNNING"]);
    const before = deadlineOf(managed);
    const other = await Sandbox.connect(SANDBOX_ID, {
      accessToken: ACCESS_TOKEN,
      controlPlane: plane,
      transport: rayd.transport,
      timeoutMs: 10_000,
    });
    try {
      expect(rayd.lifecycle.calls.at(-1)?.mode).toBe(TimeoutMode.AT_LEAST);
      expect(deadlineOf(managed)).toEqual(before);
      expect((await other.getInfo()).expiresAt).toEqual(before);
    } finally {
      other.close();
    }
  });

  test("the instance connect resumes a suspended sandbox, extends it and resolves to itself", async () => {
    const { sandbox, rayd, plane } = await managedSandbox();
    await sandbox.pause({ wait: false });
    plane.setStates(["SUSPENDED", "RUNNING"]);
    const mintsBefore = plane.mints;
    const same = await sandbox.connect({ timeoutMs: 120_000 });
    expect(same).toBe(sandbox);
    expect(plane.callsTo("resumeMicrovm")).toHaveLength(1);
    expect(plane.mints).toBeGreaterThan(mintsBefore);
    expect(Sandbox.coreOf(sandbox).paused).toBe(false);
    expect(rayd.lifecycle.calls.at(-1)).toMatchObject({
      timeoutMs: 120_000,
      mode: TimeoutMode.AT_LEAST,
    });
    plane.setStates(["TERMINATED"]);
    await expect(sandbox.connect()).rejects.toBeInstanceOf(SandboxNotFoundError);
  });

  test("connect without timeoutMs reopens only an expired or grace sandbox, with its own timeout", async () => {
    const { sandbox, rayd, plane } = await managedSandbox();
    plane.setStates(["RUNNING"]);
    const connectOptions = {
      accessToken: ACCESS_TOKEN,
      controlPlane: plane,
      transport: rayd.transport,
    };
    (await Sandbox.connect(SANDBOX_ID, connectOptions)).close();
    expect(rayd.lifecycle.calls).toHaveLength(0);
    rayd.health.lifecycle = managedLifecycle({
      phase: LifecyclePhase.EXPIRED,
      deadlineInMs: -5000,
      timeoutMs: 60_000,
    });
    (await Sandbox.connect(SANDBOX_ID, connectOptions)).close();
    expect(rayd.lifecycle.calls.at(-1)).toMatchObject({
      timeoutMs: 60_000,
      mode: TimeoutMode.AT_LEAST,
    });
    rayd.health.lifecycle = managedLifecycle({
      phase: LifecyclePhase.RESUME_GRACE,
      deadlineInMs: -5000,
      timeoutMs: 45_000,
    });
    await sandbox.resume();
    expect(rayd.lifecycle.calls).toHaveLength(2);
    expect(rayd.lifecycle.calls.at(-1)).toMatchObject({
      timeoutMs: 45_000,
      mode: TimeoutMode.AT_LEAST,
    });
    expect(Sandbox.coreOf(sandbox).lifecycle?.phase).toBe("active");
  });

  test("setTimeout errors: validation, beyond the cap, unmanaged, terminating and an older agent", async () => {
    const managed = await managedSandbox();
    const { sandbox, rayd } = managed;
    await expect(sandbox.setTimeout(999)).rejects.toBeInstanceOf(InvalidArgumentError);
    await expect(sandbox.setTimeout(1.5)).rejects.toBeInstanceOf(InvalidArgumentError);
    expect(rayd.lifecycle.calls).toHaveLength(0);

    const before = deadlineOf(managed);
    const beyond = await sandbox.setTimeout(2_000_000).catch((error: unknown) => error);
    expect(beyond).toBeInstanceOf(InvalidArgumentError);
    expect((beyond as Error).message).toContain("maxLifetimeMs");
    expect((beyond as Error).message).toContain("28800");
    expect((beyond as Error).message).toContain("reincarnate()");
    expect(deadlineOf(managed)).toEqual(before);

    rayd.health.lifecycle = unmanagedLifecycle();
    const unmanaged = await sandbox.setTimeout(60_000).catch((error: unknown) => error);
    expect(unmanaged).toBeInstanceOf(InvalidArgumentError);
    expect(unmanaged).not.toBeInstanceOf(LifecycleUnsupportedError);
    expect((unmanaged as Error).message).toContain("maxLifetimeMs");

    rayd.lifecycle.failNext.push(new ConnectError("sandbox_timeout", Code.FailedPrecondition));
    await expect(sandbox.setTimeout(60_000)).rejects.toBeInstanceOf(TimeoutError);

    rayd.health.lifecycle = undefined;
    await expect(sandbox.setTimeout(60_000)).rejects.toBeInstanceOf(LifecycleUnsupportedError);
  });

  test("Sandbox.connect({ timeoutMs }) on an older agent closes the handle and never terminates", async () => {
    const { rayd, plane } = await createTestSandbox();
    plane.setStates(["RUNNING"]);
    const error = await Sandbox.connect(SANDBOX_ID, {
      accessToken: ACCESS_TOKEN,
      controlPlane: plane,
      transport: rayd.transport,
      timeoutMs: 300_000,
    }).catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(LifecycleUnsupportedError);
    expect(rayd.lifecycle.calls).toHaveLength(0);
    expect(plane.callsTo("terminateMicrovm")).toHaveLength(0);
    const calls = plane.calls.length;
    await expect(
      Sandbox.connect(SANDBOX_ID, {
        accessToken: ACCESS_TOKEN,
        controlPlane: plane,
        timeoutMs: 1.5,
      }),
    ).rejects.toBeInstanceOf(InvalidArgumentError);
    expect(plane.calls).toHaveLength(calls);
  });

  test("the static setTimeout uses the access token and never wakes a suspended sandbox", async () => {
    const rayd = await startRayd();
    try {
      rayd.health.lifecycle = managedLifecycle();
      const plane = new FakeControlPlane({ endpoint: rayd.host, states: ["RUNNING"] });
      const options = { accessToken: ACCESS_TOKEN, controlPlane: plane, transport: rayd.transport };
      await Sandbox.setTimeout(SANDBOX_ID, 90_000, options);
      expect(rayd.lifecycle.calls.map((call) => [call.timeoutMs, call.mode])).toEqual([
        [90_000, TimeoutMode.EXACT],
      ]);
      expect(rayd.lifecycle.calls[0]?.headers["x-access-token"]).toBe(ACCESS_TOKEN);
      expect(plane.callsTo("createAuthToken")).toHaveLength(1);
      expect(rayd.health.healthCalls).toHaveLength(0);

      plane.setStates(["SUSPENDED"]);
      await expect(Sandbox.setTimeout(SANDBOX_ID, 90_000, options)).rejects.toBeInstanceOf(
        SandboxStateError,
      );
      plane.setStates(["TERMINATED"]);
      await expect(Sandbox.setTimeout(SANDBOX_ID, 90_000, options)).rejects.toBeInstanceOf(
        SandboxNotFoundError,
      );
      expect(rayd.lifecycle.calls).toHaveLength(1);
      expect(plane.callsTo("createAuthToken")).toHaveLength(1);

      const calls = plane.calls.length;
      await expect(
        Sandbox.setTimeout(SANDBOX_ID, 90_000, { controlPlane: plane, accessToken: "" }),
      ).rejects.toBeInstanceOf(AuthenticationError);
      await expect(Sandbox.setTimeout(SANDBOX_ID, 500, options)).rejects.toBeInstanceOf(
        InvalidArgumentError,
      );
      expect(plane.calls).toHaveLength(calls);
    } finally {
      await rayd.close();
    }
  });

  test("an expired sandbox answers unaries with FAILED_PRECONDITION, mapped to TimeoutError", async () => {
    const { sandbox, rayd } = await managedSandbox();
    rayd.timeoutGate = true;
    await expect(sandbox.commands.list()).rejects.toBeInstanceOf(TimeoutError);
    await expect(
      sandbox.commands.run("sleep 1", { background: true, timeoutMs: 0 }),
    ).rejects.toBeInstanceOf(TimeoutError);
    await expect(sandbox.getHealth()).resolves.toMatchObject({ agentReady: true });
    await sandbox.setTimeout(30_000);
    expect(rayd.lifecycle.calls).toHaveLength(1);
  });
});

describe("launching with a lifecycle", () => {
  test("an older agent terminates the VM and raises LifecycleUnsupportedError unless kept", async () => {
    for (const keepOnFailure of [false, true]) {
      let plane: FakeControlPlane | undefined;
      const error = await createTestSandbox({
        create: { ...KILL_LAUNCH, keepOnFailure },
        beforeCreate: (_rayd, fake) => {
          plane = fake;
        },
      }).catch((caught: unknown) => caught);
      expect(error).toBeInstanceOf(LifecycleUnsupportedError);
      expect((error as Error).message).toContain("rayito-base-2gb");
      expect((error as Error).message).toContain("publica una imagen M9");
      expect(plane?.callsTo("terminateMicrovm")).toHaveLength(keepOnFailure ? 0 : 1);
    }
  });

  test("a launch without maxLifetimeMs or onTimeout still works on an older agent", async () => {
    const { sandbox, plane } = await createTestSandbox({ create: { timeoutMs: 600_000 } });
    const launch = plane.launches[0]?.toApi();
    expect(launch?.maximumDurationInSeconds).toBe(600);
    expect(launch?.runHookPayload).not.toContain("lifecycle");
    expect(Sandbox.coreOf(sandbox).lifecycle).toBeUndefined();
    expect(plane.callsTo("terminateMicrovm")).toHaveLength(0);
  });

  test("onTimeout pause without idle fails before any AWS call", async () => {
    const plane = new FakeControlPlane({ endpoint: "127.0.0.1" });
    await expect(
      Sandbox.create({
        template: IMAGE_ARN,
        accessToken: ACCESS_TOKEN,
        controlPlane: plane,
        timeoutMs: 60_000,
        onTimeout: "pause",
        idle: null,
      }),
    ).rejects.toBeInstanceOf(InvalidArgumentError);
    expect(plane.callsTo("runMicrovm")).toHaveLength(0);
  });

  test("create({ pool }) refuses maxLifetimeMs and onTimeout", async () => {
    const take = vi.fn();
    const pool = { take } as unknown as SandboxPool;
    await expect(Sandbox.create({ pool, maxLifetimeMs: 900_000 })).rejects.toThrow(/maxLifetimeMs/);
    await expect(Sandbox.create({ pool, onTimeout: "kill" })).rejects.toThrow(/onTimeout/);
    expect(take).not.toHaveBeenCalled();
  });

  test("reincarnate relaunches with the same maxLifetimeMs and onTimeout", async () => {
    const successorId = "microvm-00000000-0000-0000-0000-000000000002";
    const { sandbox, plane } = await createTestSandbox({
      create: {
        ...KILL_LAUNCH,
        timeoutMs: 300_000,
        executionRoleArn: "arn:aws:iam::123456789012:role/rayito-execution",
        persist: new S3Prefix({ bucket: "amzn-s3-demo-bucket" }),
      },
      beforeCreate: (rayd) => {
        rayd.health.lifecycle = managedLifecycle();
      },
    });
    plane.sandboxIds.push(successorId);
    const successor = await sandbox.reincarnate();
    try {
      const [first, second] = plane.launches.map((launch) => launch.toApi());
      expect(second?.maximumDurationInSeconds).toBe(900);
      expect(JSON.parse(second?.runHookPayload ?? "{}").lifecycle).toEqual(
        JSON.parse(first?.runHookPayload ?? "{}").lifecycle,
      );
      expect(JSON.parse(second?.runHookPayload ?? "{}").lifecycle).toEqual({
        auto_resume: false,
        cap_s: 900,
        on_timeout: "kill",
        timeout_s: 300,
      });
    } finally {
      successor.close();
    }
  });
});

describe("the pause trigger", () => {
  test("kill and unmanaged never arm it; an active pause deadline does, and close cancels it", async () => {
    const killed = await managedSandbox();
    expect(Sandbox.coreOf(killed.sandbox).deadlineTriggerArmed).toBe(false);
    killed.rayd.health.lifecycle = unmanagedLifecycle();
    await killed.sandbox.getHealth();
    expect(Sandbox.coreOf(killed.sandbox).deadlineTriggerArmed).toBe(false);

    const paused = await managedSandbox(PAUSE_LAUNCH, "pause");
    const core = Sandbox.coreOf(paused.sandbox);
    expect(core.deadlineTriggerArmed).toBe(true);
    expect(paused.plane.callsTo("suspendMicrovm")).toHaveLength(0);
    paused.sandbox.close();
    expect(core.deadlineTriggerArmed).toBe(false);
  });

  test("it suspends only when rayd reports expired and the VM is RUNNING, never marking a pause", async () => {
    const { sandbox, rayd, plane } = await managedSandbox(PAUSE_LAUNCH, "pause");
    const core = Sandbox.coreOf(sandbox);
    plane.setStates(["RUNNING"]);
    rayd.health.lifecycle = managedLifecycle({
      phase: LifecyclePhase.EXPIRED,
      deadlineInMs: -2000,
      onTimeout: "pause",
    });
    await sandbox.getHealth();
    await waitUntil(() => plane.callsTo("suspendMicrovm").length === 1);
    expect(core.paused).toBe(false);
    expect(core.deadlineTriggerArmed).toBe(false);

    plane.setStates(["SUSPENDED"]);
    const probes = rayd.health.healthCalls.length;
    const reads = plane.callsTo("getMicrovm").length;
    core.recordLifecycle(lifecycleFromProto(rayd.health.lifecycle));
    await waitUntil(() => plane.callsTo("getMicrovm").length === reads + 1);
    await waitUntil(() => !core.deadlineTriggerArmed);
    expect(rayd.health.healthCalls).toHaveLength(probes);
    expect(plane.callsTo("suspendMicrovm")).toHaveLength(1);
  });

  test("an active Health with a later deadline re-arms instead of suspending", async () => {
    const { sandbox, rayd, plane, logger } = await managedSandbox(PAUSE_LAUNCH, "pause");
    const core = Sandbox.coreOf(sandbox);
    plane.setStates(["RUNNING"]);
    const probes = rayd.health.healthCalls.length;
    core.recordLifecycle(
      lifecycleFromProto(
        managedLifecycle({
          phase: LifecyclePhase.EXPIRED,
          deadlineInMs: -1000,
          onTimeout: "pause",
        }),
      ),
    );
    await waitUntil(() => rayd.health.healthCalls.length === probes + 1);
    await waitUntil(() => core.deadlineTriggerArmed);
    expect(core.lifecycle?.phase).toBe("active");
    expect(plane.callsTo("suspendMicrovm")).toHaveLength(0);
    expect(logger.dump()).not.toContain(ACCESS_TOKEN);
  });

  test("a brief deadline pause, once resumed, reopens with the auto-resume rule", async () => {
    const { sandbox, rayd, plane, logger } = await managedSandbox(PAUSE_LAUNCH, "pause");
    const core = Sandbox.coreOf(sandbox);
    const expired = () =>
      managedLifecycle({
        phase: LifecyclePhase.EXPIRED,
        deadlineInMs: -2000,
        onTimeout: "pause",
        autoResume: true,
      });
    const suspendForDeadline = async () => {
      const suspends = plane.callsTo("suspendMicrovm").length;
      plane.setStates(["RUNNING"]);
      rayd.health.lifecycle = expired();
      core.recordLifecycle(lifecycleFromProto(rayd.health.lifecycle));
      await waitUntil(() => plane.callsTo("suspendMicrovm").length === suspends + 1);
      await waitUntil(() => !core.deadlineTriggerArmed);
    };
    rayd.timeoutGate = "phase";
    rayd.health.lifecycle = expired();
    await expect(sandbox.commands.list()).rejects.toBeInstanceOf(TimeoutError);

    await suspendForDeadline();
    await expect(sandbox.commands.list()).rejects.toBeInstanceOf(TimeoutError);
    expect(rayd.lifecycle.calls).toHaveLength(0);

    await suspendForDeadline();
    rayd.resume();
    await expect(sandbox.commands.list()).resolves.toEqual([]);
    expect(rayd.lifecycle.calls.map(({ mode, timeoutMs }) => [mode, timeoutMs])).toEqual([
      [TimeoutMode.EXACT, 300_000],
    ]);
    expect(core.lifecycle?.phase).toBe("active");
    const reopenedDeadline = Number(rayd.health.lifecycle?.deadlineUnixMs ?? 0n);
    expect(Math.abs(reopenedDeadline - Date.now() - 300_000)).toBeLessThan(5000);

    await suspendForDeadline();
    rayd.resume();
    const result = await sandbox.commands.run("echo hola");
    expect(result.stdout).toBe("hola\n");
    expect(rayd.lifecycle.calls).toHaveLength(2);

    rayd.health.lifecycle = expired();
    rayd.resume();
    await expect(sandbox.commands.list()).rejects.toBeInstanceOf(TimeoutError);
    expect(rayd.lifecycle.calls).toHaveLength(2);
    expect(logger.dump()).not.toContain(ACCESS_TOKEN);
  });

  async function pausedByDeadline() {
    const managed = await managedSandbox(PAUSE_LAUNCH, "pause");
    const { rayd, plane } = managed;
    const core = Sandbox.coreOf(managed.sandbox);
    rayd.timeoutGate = "phase";
    plane.setStates(["RUNNING"]);
    rayd.health.lifecycle = managedLifecycle({
      phase: LifecyclePhase.EXPIRED,
      deadlineInMs: -2000,
      onTimeout: "pause",
      autoResume: true,
    });
    core.recordLifecycle(lifecycleFromProto(rayd.health.lifecycle));
    await waitUntil(() => plane.callsTo("suspendMicrovm").length === 1);
    await waitUntil(() => !core.deadlineTriggerArmed);
    return managed;
  }

  test("a sandbox_timeout before the freeze does not burn the reopen", async () => {
    const { sandbox, rayd } = await pausedByDeadline();
    await expect(sandbox.commands.list()).rejects.toBeInstanceOf(TimeoutError);
    expect(rayd.lifecycle.calls).toHaveLength(0);
    rayd.resume();
    await expect(sandbox.commands.list()).resolves.toEqual([]);
    expect(rayd.lifecycle.calls.map(({ mode, timeoutMs }) => [mode, timeoutMs])).toEqual([
      [TimeoutMode.EXACT, 300_000],
    ]);
  });

  test("concurrent callers share one reopen and one SetTimeout", async () => {
    const { sandbox, rayd } = await pausedByDeadline();
    rayd.resume();
    const results = await Promise.all([
      sandbox.commands.list(),
      sandbox.commands.list(),
      sandbox.files.makeDir("/home/user/concurrent"),
    ]);
    expect(results).toEqual([[], [], true]);
    expect(rayd.lifecycle.calls).toHaveLength(1);
  });

  test("a transient Unavailable on the reopen SetTimeout is retried through the reconnect", async () => {
    const { sandbox, rayd } = await pausedByDeadline();
    rayd.resume();
    rayd.lifecycle.failNext.push(new ConnectError("upstream connect error", Code.Unavailable));
    await expect(sandbox.commands.list()).resolves.toEqual([]);
    expect(rayd.lifecycle.calls).toHaveLength(2);
    expect(Sandbox.coreOf(sandbox).lifecycle?.phase).toBe("active");
  });

  test("a failing trigger is logged without secrets and swallowed", async () => {
    const { sandbox, plane, logger } = await managedSandbox(PAUSE_LAUNCH, "pause");
    const core = Sandbox.coreOf(sandbox);
    plane.getMicrovmError = new SandboxStateError("get-microvm falló");
    core.recordLifecycle(
      lifecycleFromProto(
        managedLifecycle({
          phase: LifecyclePhase.EXPIRED,
          deadlineInMs: -1000,
          onTimeout: "pause",
        }),
      ),
    );
    await waitUntil(() => logger.at("warn").length > 0);
    expect(logger.at("warn")[0]?.fields).toMatchObject({
      sandboxId: SANDBOX_ID,
      reason: "SandboxStateError",
    });
    expect(logger.dump()).not.toContain(ACCESS_TOKEN);
    expect(plane.callsTo("suspendMicrovm")).toHaveLength(0);
  });
});
