/**
 * `src/templates/concurrency.ts` (m15-templates): espejo de
 * `test_m15_templates_concurrency.py`.
 */

import { describe, expect, test } from "vitest";
import { BuildError } from "../../src/errors.js";
import { MAX_CONCURRENT_BUILDS, withBuildSlot } from "../../src/templates/concurrency.js";

function pending(): { promise: Promise<void>; resolve: () => void } {
  let resolve!: () => void;
  const promise = new Promise<void>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

describe("templates/concurrency", () => {
  test("up to the limit of slots can run at once; the next one raises build_quota", async () => {
    const gates = Array.from({ length: MAX_CONCURRENT_BUILDS }, () => pending());
    const held = gates.map((gate, index) => withBuildSlot(() => gate.promise.then(() => index)));

    await expect(withBuildSlot(() => Promise.resolve())).rejects.toMatchObject({
      reason: "build_quota",
    });

    gates.forEach((gate) => {
      gate.resolve();
    });
    await Promise.all(held);
  });

  test("a released slot can be reacquired", async () => {
    await withBuildSlot(() => Promise.resolve());
    const gates = Array.from({ length: MAX_CONCURRENT_BUILDS }, () => pending());
    const held = gates.map((gate) => withBuildSlot(() => gate.promise));
    gates.forEach((gate) => {
      gate.resolve();
    });
    await Promise.all(held);
  });

  test("the slot is released even if the build throws", async () => {
    await expect(
      withBuildSlot(() => {
        throw new BuildError("boom");
      }),
    ).rejects.toThrow("boom");
    // if the slot leaked, this would hang forever waiting on a full pool.
    await withBuildSlot(() => Promise.resolve());
  });
});
