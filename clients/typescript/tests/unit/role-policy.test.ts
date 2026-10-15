import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { UnimplementedError } from "../../src/errors.js";
import { OFFICIAL_IMAGE_VARIANTS } from "../../src/limits.js";
import {
  CAPS_VARIANTS,
  EFS_CAPS_VARIANT,
  requireCapsFor,
  requireEfsFor,
  resolveImageVariant,
} from "../../src/role-policy.js";

interface Vectors {
  readonly resolve: readonly { template: string | null; variant: string | null }[];
  readonly requireCaps: readonly { variant: string | null; accepted: boolean }[];
  readonly requireEfs: readonly { variant: string | null; accepted: boolean }[];
}

/** Los mismos vectores que `test_m15_role_policy.py`. */
const VECTORS = JSON.parse(
  readFileSync(
    join(import.meta.dirname, "..", "..", "..", "..", "testdata", "image-variants", "vectors.json"),
    "utf8",
  ),
) as Vectors;

const orUndefined = (value: string | null): string | undefined => value ?? undefined;

describe("role-policy (espejo de test_m15_role_policy.py)", () => {
  it.each(VECTORS.resolve)("resolveImageVariant($template) -> $variant", (vector) => {
    expect(resolveImageVariant(orUndefined(vector.template))).toBe(orUndefined(vector.variant));
  });

  it("el catálogo contiene las variantes caps", () => {
    for (const variant of CAPS_VARIANTS) {
      expect(OFFICIAL_IMAGE_VARIANTS).toContain(variant);
    }
  });

  it.each(VECTORS.requireCaps)("requireCapsFor($variant) aceptada=$accepted", (vector) => {
    const call = () => requireCapsFor("mounts", orUndefined(vector.variant));
    if (vector.accepted) {
      expect(call).not.toThrow();
    } else {
      expect(call).toThrow(UnimplementedError);
      expect(call).toThrow(/base-caps/);
    }
  });

  it.each(VECTORS.requireEfs)("requireEfsFor($variant) aceptada=$accepted", (vector) => {
    const call = () => requireEfsFor("volumes", orUndefined(vector.variant));
    if (vector.accepted) {
      expect(call).not.toThrow();
    } else {
      expect(call).toThrow(UnimplementedError);
      expect(call).toThrow(new RegExp(EFS_CAPS_VARIANT));
    }
  });

  it("el nombre de la imagen efs resuelve a una variante caps aceptada", () => {
    expect(() =>
      requireCapsFor("mounts", resolveImageVariant("rayito-base-caps-efs-4gb")),
    ).not.toThrow();
  });
});
