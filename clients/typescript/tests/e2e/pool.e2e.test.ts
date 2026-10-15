/**
 * M7 Track 3 (`m7-suspended-pool`) a través del SDK TypeScript contra AWS
 * real: un pool de `size: 2`, cinco `take()` con plaza lista, cada uno con
 * su primera celda `1+1`, y el drenado. El número de aceptación es el del
 * e2e Python (design D17); este test prueba la paridad del camino.
 */

import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, test } from "vitest";
import type { LambdaMicrovmsControlPlane } from "../../src/index.js";
import {
  JsonFilePoolBackend,
  type PoolConfig,
  SandboxNotFoundError,
  SandboxPool,
} from "../../src/index.js";
import { e2eEnabled, seconds, useE2E, waitUntil } from "./helpers.js";

const TAKES = 5;
const POOL_MAX_IDLE_SECONDS = 120;
const POOL_TIMEOUT_MS = 900_000;
const READY_BUDGET_MS = 90_000;
const CELL_TIMEOUT_MS = 60_000;
const POOL_MIN_REMAINING_MS = 300_000;
const RECYCLE_TIMEOUT_MS = 720_000;
const RECYCLE_MIN_REMAINING_MS = 600_000;
const RECYCLE_BUDGET_MS = 240_000;
const REPLACEMENT_BUDGET_MS = 60_000;
const TERMINAL_BUDGET_MS = 60_000;
const LIVE_STATES = new Set(["PENDING", "RUNNING", "SUSPENDING", "SUSPENDED"]);

describe.skipIf(!e2eEnabled())("m7 pool typescript sdk", () => {
  const context = useE2E();

  function poolConfig(overrides: Partial<PoolConfig> = {}): PoolConfig {
    return {
      size: 2,
      template: context.templateArn,
      timeoutMs: POOL_TIMEOUT_MS,
      idle: { maxIdleSeconds: POOL_MAX_IDLE_SECONDS, autoResume: true },
      minRemainingMs: POOL_MIN_REMAINING_MS,
      fillConcurrency: 2,
      sweepIntervalMs: 10_000,
      executionRoleArn: context.settings.executionRoleArn,
      ingress: ["ALL_INGRESS"],
      logging: context.settings.logging,
      ...overrides,
    };
  }

  async function liveIds(): Promise<Set<string>> {
    const ids = new Set<string>();
    for await (const item of context.controlPlane.listMicrovms({ imageArn: context.templateArn })) {
      if (LIVE_STATES.has(item.state)) {
        ids.add(item.sandboxId);
      }
    }
    return ids;
  }

  async function isTerminal(
    plane: LambdaMicrovmsControlPlane,
    sandboxId: string,
  ): Promise<boolean> {
    try {
      return ["TERMINATING", "TERMINATED"].includes((await plane.getMicrovm(sandboxId)).state);
    } catch (error) {
      if (error instanceof SandboxNotFoundError) {
        return true;
      }
      throw error;
    }
  }

  test("five takes with a ready slot, then drain", async () => {
    const pool = new SandboxPool(poolConfig(), { controlPlane: context.controlPlane });
    const used: string[] = [];
    const samples: number[] = [];
    await pool.start();
    try {
      for (let index = 0; index < TAKES; index += 1) {
        await waitUntil(() => pool.stats().ready >= 1, READY_BUDGET_MS, "plaza lista");
        const started = performance.now();
        const sandbox = await pool.take();
        const execution = await sandbox.runCode("1+1", { timeoutMs: CELL_TIMEOUT_MS });
        const elapsed = seconds(started);
        samples.push(elapsed);
        used.push(sandbox.sandboxId);
        console.log(
          `\n[m7-pool-ts] take ${index + 1}: ${elapsed.toFixed(3)} s (${sandbox.sandboxId})`,
        );
        expect(execution.text).toBe("2");
        await sandbox.kill();
      }
      const stats = pool.stats();
      console.log(`\n[m7-pool-ts] stats: ${JSON.stringify(stats)}`);
      expect(stats.hits).toBe(TAKES);
      expect(stats.misses).toBe(0);
      used.push(...stats.slots.map((slot) => slot.sandboxId));
    } finally {
      await pool.close({ drain: true });
    }
    await waitUntil(
      async () => {
        for await (const item of context.controlPlane.listMicrovms({
          imageArn: context.templateArn,
        })) {
          if (LIVE_STATES.has(item.state) && used.includes(item.sandboxId)) {
            return false;
          }
        }
        return true;
      },
      60_000,
      "ningún VM vivo del pool",
    );
    console.log(
      `\n[m7-pool-ts] take -> primera celda: ${samples.map((s) => s.toFixed(3)).join(", ")} s`,
    );
  });
  test("recycles a slot before the wall", async () => {
    const pool = new SandboxPool(
      poolConfig({
        size: 1,
        timeoutMs: RECYCLE_TIMEOUT_MS,
        minRemainingMs: RECYCLE_MIN_REMAINING_MS,
        fillConcurrency: 1,
      }),
      { controlPlane: context.controlPlane },
    );
    await pool.start();
    let first = "";
    let replacement = "";
    try {
      await waitUntil(() => pool.stats().ready === 1, READY_BUDGET_MS, "plaza lista");
      first = pool.stats().slots[0]?.sandboxId ?? "";
      const recycledAfter = await waitUntil(
        () => pool.stats().recycled === 1,
        RECYCLE_BUDGET_MS,
        "reciclado",
      );
      console.log(`\n[m7-pool-ts] reciclado tras ${recycledAfter.toFixed(1)} s desde ready`);
      await waitUntil(
        () => pool.stats().slots.some((slot) => slot.state === "ready" && slot.sandboxId !== first),
        REPLACEMENT_BUDGET_MS,
        "plaza de reemplazo",
      );
      replacement = pool.stats().slots.find((slot) => slot.state === "ready")?.sandboxId ?? "";
      await waitUntil(
        () => isTerminal(context.controlPlane, first),
        TERMINAL_BUDGET_MS,
        "terminal",
      );
    } finally {
      await pool.close({ drain: true });
    }
    expect(replacement).not.toBe("");
    expect(replacement).not.toBe(first);
  });

  test("a JSON backend recovers the parked slot without launching", async () => {
    const directory = await mkdtemp(join(tmpdir(), "rayito-pool-"));
    const path = join(directory, "pool.json");
    const config = poolConfig({ size: 1, fillConcurrency: 1 });
    const first = new SandboxPool(config, {
      backend: new JsonFilePoolBackend(path),
      controlPlane: context.controlPlane,
    });
    let parked = "";
    try {
      await first.start();
      await waitUntil(() => first.stats().ready === 1, READY_BUDGET_MS, "plaza lista");
      parked = first.stats().slots[0]?.sandboxId ?? "";
      await first.close({ drain: false });

      const second = new SandboxPool(config, {
        backend: new JsonFilePoolBackend(path),
        controlPlane: context.controlPlane,
      });
      await second.start();
      try {
        const stats = second.stats();
        expect(stats.ready).toBe(1);
        expect(stats.launched).toBe(0);
        expect(stats.slots[0]?.sandboxId).toBe(parked);
        const started = performance.now();
        const sandbox = await second.take();
        expect((await sandbox.runCode("1+1", { timeoutMs: CELL_TIMEOUT_MS })).text).toBe("2");
        console.log(
          `\n[m7-pool-ts] take() recuperado -> primera celda: ${seconds(started).toFixed(3)} s`,
        );
        expect(sandbox.sandboxId).toBe(parked);
        await sandbox.kill();
      } finally {
        await second.close({ drain: true });
      }
      await waitUntil(
        async () => !(await liveIds()).has(parked),
        TERMINAL_BUDGET_MS,
        "la plaza recuperada terminada",
      );
    } finally {
      await rm(directory, { recursive: true, force: true });
    }
  });
});
