/**
 * `loadOptionalPeer`: sólo se resuelve un peer opcional cuando ya se activó
 * una función (ADR-014).
 */

import { describe, expect, test } from "vitest";
import { InvalidArgumentError } from "../../src/errors.js";
import { loadOptionalPeer } from "../../src/optional.js";

const MISSING_SPECIFIER = "rayito-test-definitely-not-installed";

describe("loadOptionalPeer", () => {
  test("a specifier that does not exist is rejected with InvalidArgumentError naming the package", async () => {
    await expect(loadOptionalPeer(MISSING_SPECIFIER, "las trazas OTel")).rejects.toSatisfy(
      (error: unknown) => {
        expect(error).toBeInstanceOf(InvalidArgumentError);
        const message = (error as Error).message;
        expect(message).toContain(MISSING_SPECIFIER);
        expect(message).toContain("las trazas OTel");
        expect(message).toContain(`npm install ${MISSING_SPECIFIER}`);
        return true;
      },
    );
  });

  test("the InvalidArgumentError chains the original resolution failure as its cause", async () => {
    await expect(loadOptionalPeer(MISSING_SPECIFIER, "una función")).rejects.toSatisfy(
      (error: unknown) => {
        expect((error as { cause?: unknown }).cause).toBeInstanceOf(Error);
        return true;
      },
    );
  });

  test("an existing specifier resolves", async () => {
    const module = await loadOptionalPeer<typeof import("node:util")>("node:util", "una función");
    expect(typeof module.inspect).toBe("function");
  });
});
