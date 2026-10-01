import { describe, expect, test } from "vitest";
import { InvalidArgumentError } from "../../src/errors.js";
import { MAX_MOUNTS, validateMountPaths } from "../../src/mount-path.js";

describe("mount-path", () => {
  test("valid paths are returned as given", () => {
    const paths = ["/mnt/data", "/home/user/shared"];
    expect(validateMountPaths(paths)).toBe(paths);
  });

  test("relative paths are rejected", () => {
    expect(() => validateMountPaths(["mnt/data"])).toThrow(InvalidArgumentError);
  });

  test.each(["/mnt/../etc", "/mnt//data", "/mnt/data/", "/mnt/./data"])(
    "non-canonical path %s is rejected",
    (path) => {
      expect(() => validateMountPaths([path])).toThrow(/canónica/);
    },
  );

  test.each(["/etc/passwd", "/root/data", "/mnt", "/home/user"])(
    "path outside the allowed roots %s is rejected",
    (path) => {
      expect(() => validateMountPaths([path])).toThrow(InvalidArgumentError);
    },
  );

  test("overlapping paths are rejected", () => {
    expect(() => validateMountPaths(["/mnt/data", "/mnt/data/sub"])).toThrow(/solapar/);
  });

  test("more than the maximum is rejected", () => {
    const tooMany = Array.from({ length: MAX_MOUNTS + 1 }, (_, i) => `/mnt/d${i}`);
    expect(() => validateMountPaths(tooMany)).toThrow(String(MAX_MOUNTS));
  });

  test("exactly the maximum is accepted", () => {
    const paths = Array.from({ length: MAX_MOUNTS }, (_, i) => `/mnt/d${i}`);
    expect(validateMountPaths(paths)).toEqual(paths);
  });

  test("no paths is fine", () => {
    expect(validateMountPaths([])).toEqual([]);
  });
});
