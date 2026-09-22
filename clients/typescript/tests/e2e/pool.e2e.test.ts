/**
 * M7 Track 3 (`m7-suspended-pool`) a través del SDK TypeScript contra AWS
 * real: un pool de `size: 2`, cinco `take()` con plaza lista, cada uno con
 * su primera celda `1+1`, y el drenado. El número de aceptación es el del
 * e2e Python (design D17); este test prueba la paridad del camino.
 */

import { describe, expect, test } from "vitest";
import { SandboxPool } from "../../src/index.js";
import { e2eEnabled, useE2E, waitUntil } from "./helpers.js";

const TAKES = 5;
const POOL_TIMEOUT_MS = 900_000;
const READY_BUDGET_MS = 90_000;
const CELL_TIMEOUT_MS = 60_000;
const LIVE_STATES = new Set(["PENDING", "RUNNING", "SUSPENDING", "SUSPENDED"]);

describe.skipIf(!e2eEnabled())("m7 pool typescript sdk", () => {
  const context = useE2E();

  test("five takes with a ready slot, then drain", async () => {
    const pool = new SandboxPool(
      {
        size: 2,
        template: context.templateArn,
        timeoutMs: POOL_TIMEOUT_MS,
        idle: { maxIdleSeconds: 120, autoResume: true },
        minRemainingMs: 300_000,
        fillConcurrency: 2,
        sweepIntervalMs: 10_000,
        executionRoleArn: context.settings.executionRoleArn,
        ingress: ["ALL_INGRESS"],
        logging: context.settings.logging,
      },
      { controlPlane: context.controlPlane },
    );
    const used: string[] = [];
    const samples: number[] = [];
    await pool.start();
    try {
      for (let index = 0; index < TAKES; index += 1) {
        await waitUntil(() => pool.stats().ready >= 1, READY_BUDGET_MS, "plaza lista");
        const started = performance.now();
        const sandbox = await pool.take();
        const execution = await sandbox.runCode("1+1", { timeoutMs: CELL_TIMEOUT_MS });
        const elapsed = (performance.now() - started) / 1000;
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
});
