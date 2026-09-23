/**
 * Los accesores de D18: `currentProxyToken(port)` lee el `TokenStore` sin
 * acuñar nada y `isRunning({ requestTimeoutMs })` acota el `Health`.
 */

import { describe, expect, test } from "vitest";
import { deadlineFromHeaders } from "./fake/common.js";
import { JWE } from "./fake/control-plane.js";
import { createTestSandbox } from "./helpers.js";

describe("Sandbox accessors", () => {
  test("currentProxyToken returns the JWE held for rayd's port, synchronously and without minting", async () => {
    const { sandbox, plane } = await createTestSandbox();
    const mints = plane.mints;
    expect(sandbox.currentProxyToken()).toBe(`${JWE}.${mints}`);
    expect(sandbox.currentProxyToken(8080)).toBe(`${JWE}.${mints}`);
    expect(sandbox.currentProxyToken(3000)).toBeUndefined();
    expect(plane.mints).toBe(mints);
  });

  test("isRunning bounds the Health probe with requestTimeoutMs", async () => {
    const { sandbox, rayd } = await createTestSandbox({ create: { requestTimeoutMs: 30_000 } });
    expect(await sandbox.isRunning({ requestTimeoutMs: 400 })).toBe(true);
    const bounded = deadlineFromHeaders(rayd.health.healthCalls.at(-1) ?? {});
    expect(bounded).toBeDefined();
    expect(bounded as number).toBeLessThanOrEqual(400);
    expect(await sandbox.isRunning()).toBe(true);
    const capped = deadlineFromHeaders(rayd.health.healthCalls.at(-1) ?? {});
    expect(capped as number).toBeGreaterThan(400);
    expect(capped as number).toBeLessThanOrEqual(5000);
  });
});
