/**
 * `sizing/sizing.ts` (m15-sizes-catalog): dominio puro, espejo de
 * `test_m15_sizes_catalog_domain.py`.
 */

import { describe, expect, test, vi } from "vitest";
import { InvalidArgumentError } from "../../src/errors.js";
import { SUPPORTED_MEMORY_MIB } from "../../src/limits.js";
import {
  applySizeSuffix,
  BASELINE_MEMORY_MIB,
  baselineCpuFor,
  isBaselineSize,
  isRoundedUp,
  MAX_SUPPORTED_MEMORY_MIB,
  resolveSize,
  SIZE_NAMES,
  warnIfRounded,
} from "../../src/sizing/sizing.js";

describe("sizing", () => {
  test("SIZE_NAMES aligns one to one with SUPPORTED_MEMORY_MIB", () => {
    expect(SIZE_NAMES.length).toBe(SUPPORTED_MEMORY_MIB.length);
  });

  test.each(SIZE_NAMES.map((name, index) => [name, SUPPORTED_MEMORY_MIB[index]] as const))(
    "resolveSize by exact name %s",
    (name, mib) => {
      const resolved = resolveSize(name);
      expect(resolved).toEqual({ name, memoryMib: mib, requestedMib: mib });
      expect(isRoundedUp(resolved)).toBe(false);
    },
  );

  test("resolveSize rounds a size request up", () => {
    const resolved = resolveSize({ memoryMib: 3000 });
    expect(resolved.memoryMib).toBe(4096);
    expect(resolved.name).toBe("4gb");
    expect(resolved.requestedMib).toBe(3000);
    expect(isRoundedUp(resolved)).toBe(true);
  });

  test("resolveSize exact size request does not round", () => {
    const resolved = resolveSize({ memoryMib: 2048 });
    expect(resolved).toEqual({ name: "2gb", memoryMib: 2048, requestedMib: 2048 });
    expect(isRoundedUp(resolved)).toBe(false);
  });

  test("resolveSize rejects an unknown name", () => {
    expect(() => resolveSize("huge" as never)).toThrow(InvalidArgumentError);
  });

  test.each([0, -1])("resolveSize rejects a non-positive request (%d)", (memoryMib) => {
    expect(() => resolveSize({ memoryMib })).toThrow(InvalidArgumentError);
  });

  test("resolveSize above the catalog is invalid argument before any AWS call", () => {
    expect(() => resolveSize({ memoryMib: 16384 })).toThrow(
      new RegExp(String(MAX_SUPPORTED_MEMORY_MIB)),
    );
  });

  test("isBaselineSize is true only for the unsuffixed memory", () => {
    expect(isBaselineSize(resolveSize("2gb"))).toBe(true);
    expect(isBaselineSize(resolveSize("4gb"))).toBe(false);
    expect(BASELINE_MEMORY_MIB).toBe(2048);
  });

  test("applySizeSuffix keeps the baseline name unchanged", () => {
    expect(applySizeSuffix("rayito-base", resolveSize("2gb"))).toBe("rayito-base");
  });

  test("applySizeSuffix appends the size name", () => {
    expect(applySizeSuffix("rayito-base", resolveSize("4gb"))).toBe("rayito-base-4gb");
    expect(applySizeSuffix("mi-imagen", resolveSize("512mb"))).toBe("mi-imagen-512mb");
  });

  test("applySizeSuffix rejects an ARN", () => {
    const arn = "arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base";
    expect(() => applySizeSuffix(arn, resolveSize("4gb"))).toThrow(/ARN/);
  });

  test("baselineCpuFor matches the Q88 measurement at 2048 MiB", () => {
    expect(baselineCpuFor(2048)).toBe(4);
  });

  test.each([
    [512, 1],
    [1024, 2],
    [2048, 4],
    [4096, 8],
    [8192, 16],
  ])(
    "baselineCpuFor matches the Q88 measurement for every catalog size (%d -> %d)",
    (mib, expected) => {
      expect(baselineCpuFor(mib)).toBe(expected);
    },
  );

  test("baselineCpuFor never goes below one", () => {
    expect(baselineCpuFor(1)).toBe(1);
  });

  test("warnIfRounded warns only when rounded", () => {
    const spy = vi.spyOn(process, "emitWarning").mockImplementation(() => {});
    warnIfRounded(resolveSize("4gb"));
    expect(spy).not.toHaveBeenCalled();
    warnIfRounded(resolveSize({ memoryMib: 3000 }));
    expect(spy).toHaveBeenCalledTimes(1);
    const [message, options] = spy.mock.calls[0] as [string, { type: string }];
    expect(options.type).toBe("RayitoCompatWarning");
    expect(message).toContain("3000");
    expect(message).toContain("4096");
    spy.mockRestore();
  });
});
