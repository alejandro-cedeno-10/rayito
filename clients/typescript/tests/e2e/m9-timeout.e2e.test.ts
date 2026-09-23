/**
 * M9 `m9-server-timeout` (ADR-011) contra AWS real, espejo nativo en
 * TypeScript de `clients/python/tests/e2e/test_m9_server_timeout.py`
 * (design D12): `setTimeout` que acorta, `connect({ timeoutMs })` AT_LEAST,
 * el modo `pause` con un cliente vivo (suspensión y auto-resume con la regla
 * de 5 min), un stream abierto al vencer el plazo y `setTimeout` más allá del
 * tope. Exige la imagen M9 de `RAYITO_TEMPLATE`; todo sandbox nace con
 * `maxLifetimeMs <= 900 000`, sin execution role (y por tanto con
 * `logging: "disabled"`: CloudWatch exige el rol), y el sweeper de `useE2E`
 * lo termina. Cada test imprime lo que necesitan las filas de
 * AWS_API_NOTES.md §16 (sólo placeholders en ficheros versionados).
 */

import { describe, expect, test } from "vitest";
import {
  type IdlePolicyInput,
  InvalidArgumentError,
  type OnTimeout,
  Sandbox,
  type SandboxInfo,
  SandboxNotFoundError,
  TimeoutError,
} from "../../src/index.js";
import {
  type E2EContext,
  e2eEnabled,
  report,
  seconds,
  useE2E,
  waitUntil,
  withTimeout,
} from "./helpers.js";

const MAX_TEST_LIFETIME_MS = 900_000;
const TERMINATION_SLACK_MS = 45_000;
const SUSPEND_SLACK_MS = 15_000;
const AUTO_RESUME_MIN_MS = 300_000;
const DEADLINE_TOLERANCE_MS = 10_000;
const STATE_POLL_MS = 1000;

interface TimeoutLaunch {
  readonly timeoutMs: number;
  readonly maxLifetimeMs?: number;
  readonly onTimeout?: OnTimeout;
  readonly idle?: IdlePolicyInput | null;
}

/** `create()` con plazo lógico y el guardrail de D12 (`maxLifetimeMs <= 900 000`). */
async function createTimeoutSandbox(context: E2EContext, launch: TimeoutLaunch): Promise<Sandbox> {
  const maxLifetimeMs = launch.maxLifetimeMs ?? MAX_TEST_LIFETIME_MS;
  if (maxLifetimeMs > MAX_TEST_LIFETIME_MS) {
    throw new Error("guardrail: maxLifetimeMs <= 900 000");
  }
  const started = performance.now();
  const sandbox = await Sandbox.create({
    template: context.templateArn,
    timeoutMs: launch.timeoutMs,
    maxLifetimeMs,
    onTimeout: launch.onTimeout ?? "kill",
    idle: launch.idle ?? null,
    ingress: ["ALL_INGRESS"],
    logging: "disabled",
    controlPlane: context.controlPlane,
  });
  context.created.push(sandbox);
  context.bootTimings.set(sandbox.sandboxId, seconds(started));
  return sandbox;
}

async function stateOf(context: E2EContext, sandboxId: string): Promise<SandboxInfo | undefined> {
  try {
    return await context.controlPlane.getMicrovm(sandboxId);
  } catch (error) {
    if (error instanceof SandboxNotFoundError) {
      return undefined;
    }
    throw error;
  }
}

async function waitForState(
  context: E2EContext,
  sandboxId: string,
  wanted: string,
  budgetMs: number,
): Promise<SandboxInfo | undefined> {
  let last: SandboxInfo | undefined;
  await waitUntil(
    async () => {
      last = await stateOf(context, sandboxId);
      return last === undefined ? wanted === "TERMINATED" : last.state === wanted;
    },
    budgetMs,
    `${sandboxId} ${wanted}`,
    STATE_POLL_MS,
  );
  return last;
}

async function remainingMs(sandbox: Sandbox): Promise<number> {
  const health = await sandbox.getHealth();
  return (health.lifecycle?.deadline?.getTime() ?? 0) - Date.now();
}

describe.skipIf(!e2eEnabled())("M9 server-enforced timeout (TypeScript)", () => {
  const context = useE2E();

  test("setTimeout shortens the deadline and rayd ends the VM on its own", async () => {
    const sandbox = await createTimeoutSandbox(context, { timeoutMs: 60_000 });
    const shortenedAt = Date.now();
    await sandbox.setTimeout(10_000);
    const lifecycle = (await sandbox.getHealth()).lifecycle;
    expect(lifecycle?.phase).toBe("active");
    expect(lifecycle?.extensions).toBe(1);
    const info = await waitForState(
      context,
      sandbox.sandboxId,
      "TERMINATED",
      10_000 + TERMINATION_SLACK_MS,
    );
    const overrunMs = (info?.terminatedAt?.getTime() ?? Date.now()) - (shortenedAt + 10_000);
    report(`${sandbox.sandboxId}: terminatedAt − deadline (overrun_s)`, overrunMs / 1000);
    console.log(`\n[m9] stateReason=${info?.stateReason ?? "n/a"} timedOut=${info?.timedOut}`);
  });

  test("connect({ timeoutMs }) extends to at least now + timeoutMs and never shortens", async () => {
    const sandbox = await createTimeoutSandbox(context, { timeoutMs: 600_000 });
    const connectOptions = {
      accessToken: sandbox.accessToken,
      controlPlane: context.controlPlane,
      timeoutMs: 300_000,
    };
    const kept = await Sandbox.connect(sandbox.sandboxId, connectOptions);
    try {
      expect(Math.abs((await remainingMs(kept)) - 600_000)).toBeLessThan(DEADLINE_TOLERANCE_MS);
    } finally {
      kept.close();
    }
    await sandbox.setTimeout(30_000);
    const extended = await Sandbox.connect(sandbox.sandboxId, connectOptions);
    try {
      expect(Math.abs((await remainingMs(extended)) - 300_000)).toBeLessThan(DEADLINE_TOLERANCE_MS);
      const info = await extended.getInfo();
      expect(Math.abs(info.expiresAt.getTime() - (Date.now() + 300_000))).toBeLessThan(
        DEADLINE_TOLERANCE_MS,
      );
    } finally {
      extended.close();
    }
  });

  test("onTimeout pause with a live client suspends at the deadline and auto-resumes", async () => {
    const sandbox = await createTimeoutSandbox(context, {
      timeoutMs: 60_000,
      onTimeout: "pause",
      idle: { maxIdleSeconds: 300, autoResume: true },
    });
    await sandbox.runCode("x = 42");
    const deadline = (await sandbox.getHealth()).lifecycle?.deadline?.getTime() ?? Date.now();
    await waitForState(
      context,
      sandbox.sandboxId,
      "SUSPENDED",
      Math.max(0, deadline - Date.now()) + SUSPEND_SLACK_MS,
    );
    report(
      `${sandbox.sandboxId}: deadline -> SUSPENDED (pause_suspend_s)`,
      (Date.now() - deadline) / 1000,
    );
    const resumedAt = Date.now();
    const execution = await withTimeout(
      sandbox.runCode("x"),
      120_000,
      "runCode tras el auto-resume",
    );
    report(
      `${sandbox.sandboxId}: auto-resume -> runCode (auto_resume_s)`,
      (Date.now() - resumedAt) / 1000,
    );
    expect(execution.text).toBe("42");
    const lifecycle = (await sandbox.getHealth()).lifecycle;
    expect(lifecycle?.phase).toBe("active");
    expect(
      Math.abs((lifecycle?.deadline?.getTime() ?? 0) - (resumedAt + AUTO_RESUME_MIN_MS)),
    ).toBeLessThan(DEADLINE_TOLERANCE_MS * 3);
  });

  test("a stream open at the deadline rejects with TimeoutError", async () => {
    const sandbox = await createTimeoutSandbox(context, { timeoutMs: 45_000 });
    const handle = await sandbox.commands.run("sleep 600", { background: true, timeoutMs: 0 });
    const outcome = await withTimeout(
      handle.wait().catch((error: unknown) => error),
      45_000 + TERMINATION_SLACK_MS,
      "el final sandbox_timeout",
    );
    expect(outcome).toBeInstanceOf(TimeoutError);
    await waitForState(context, sandbox.sandboxId, "TERMINATED", TERMINATION_SLACK_MS);
  });

  test("setTimeout beyond maxLifetimeMs is InvalidArgumentError and keeps the deadline", async () => {
    const sandbox = await createTimeoutSandbox(context, { timeoutMs: 120_000 });
    const before = (await sandbox.getHealth()).lifecycle?.deadline?.getTime();
    const error = await sandbox.setTimeout(2_000_000).catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(InvalidArgumentError);
    expect((error as Error).message).toContain("maxLifetimeMs");
    expect((error as Error).message).toContain("28800");
    expect((await sandbox.getHealth()).lifecycle?.deadline?.getTime()).toBe(before);
  });
});
