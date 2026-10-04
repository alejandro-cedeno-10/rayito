/**
 * `sizing/catalog.ts` (m15-sizes-catalog): cachea `minimumMemoryInMiB` por
 * `(imageArn, imageVersion)` y por instancia. Espejo de
 * `test_m15_sizes_catalog_catalog.py`.
 */

import { describe, expect, test } from "vitest";
import { ConventionCatalog, type ImageVersionReader } from "../../src/sizing/catalog.js";

const ARN = "arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base-4gb";

class FakeImageVersionReader implements ImageVersionReader {
  readonly calls: Array<readonly [string, string]> = [];
  constructor(private readonly minimumMemoryMib = 4096) {}

  async getMicrovmImageVersion(imageArn: string, imageVersion: string): Promise<number> {
    this.calls.push([imageArn, imageVersion]);
    return this.minimumMemoryMib;
  }
}

describe("ConventionCatalog", () => {
  test("first call reads through to the reader", async () => {
    const reader = new FakeImageVersionReader();
    const catalog = new ConventionCatalog();
    await expect(catalog.minimumMemoryMib(reader, ARN, "3")).resolves.toBe(4096);
    expect(reader.calls).toEqual([[ARN, "3"]]);
  });

  test("second call for the same key is served from cache", async () => {
    const reader = new FakeImageVersionReader();
    const catalog = new ConventionCatalog();
    await catalog.minimumMemoryMib(reader, ARN, "3");
    await catalog.minimumMemoryMib(reader, ARN, "3");
    await catalog.minimumMemoryMib(reader, ARN, "3");
    expect(reader.calls).toEqual([[ARN, "3"]]);
  });

  test("concurrent calls for the same key share one in-flight read", async () => {
    const reader = new FakeImageVersionReader();
    const catalog = new ConventionCatalog();
    const [a, b] = await Promise.all([
      catalog.minimumMemoryMib(reader, ARN, "3"),
      catalog.minimumMemoryMib(reader, ARN, "3"),
    ]);
    expect(a).toBe(4096);
    expect(b).toBe(4096);
    expect(reader.calls).toEqual([[ARN, "3"]]);
  });

  test("a different version of the same arn is a different cache key", async () => {
    const reader = new FakeImageVersionReader();
    const catalog = new ConventionCatalog();
    await catalog.minimumMemoryMib(reader, ARN, "3");
    await catalog.minimumMemoryMib(reader, ARN, "4");
    expect(reader.calls).toEqual([
      [ARN, "3"],
      [ARN, "4"],
    ]);
  });

  test("two catalog instances do not share a cache", async () => {
    const reader = new FakeImageVersionReader();
    await new ConventionCatalog().minimumMemoryMib(reader, ARN, "3");
    await expect(new ConventionCatalog().minimumMemoryMib(reader, ARN, "3")).resolves.toBe(4096);
    expect(reader.calls).toEqual([
      [ARN, "3"],
      [ARN, "3"],
    ]);
  });

  test("a failed read is not cached and may be retried", async () => {
    let attempts = 0;
    const reader: ImageVersionReader = {
      async getMicrovmImageVersion() {
        attempts += 1;
        if (attempts === 1) {
          throw new Error("boom");
        }
        return 4096;
      },
    };
    const catalog = new ConventionCatalog();
    await expect(catalog.minimumMemoryMib(reader, ARN, "3")).rejects.toThrow("boom");
    await expect(catalog.minimumMemoryMib(reader, ARN, "3")).resolves.toBe(4096);
    expect(attempts).toBe(2);
  });
});
