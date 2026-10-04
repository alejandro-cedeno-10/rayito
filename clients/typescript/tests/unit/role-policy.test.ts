import { describe, expect, it } from "vitest";
import { UnimplementedError } from "../../src/errors.js";
import {
  CAPS_VARIANTS,
  EFS_CAPS_VARIANT,
  requireCapsFor,
  resolveImageVariant,
} from "../../src/role-policy.js";

describe("role-policy (espejo de test_m15_role_policy.py)", () => {
  it.each([
    ["rayito-base-caps", "base-caps"],
    ["rayito-base-caps-4gb", "base-caps"],
    ["rayito-base-caps-efs", "base-caps-efs"],
    ["rayito-base-caps-efs-4gb", "base-caps-efs"],
    ["rayito-base", "base"],
    [undefined, undefined],
    ["arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base-caps", undefined],
    ["mi-imagen-propia", undefined],
  ])("resolveImageVariant(%s) -> %s", (template, expected) => {
    expect(resolveImageVariant(template)).toBe(expected);
  });

  it("acepta la caps y la caps con amazon-efs-utils", () => {
    expect(CAPS_VARIANTS.has(EFS_CAPS_VARIANT)).toBe(true);
    expect(() => requireCapsFor("volumes", "base-caps")).not.toThrow();
    expect(() =>
      requireCapsFor("volumes", resolveImageVariant("rayito-base-caps-efs-4gb")),
    ).not.toThrow();
    expect(() => requireCapsFor("volumes", undefined)).not.toThrow();
  });

  it("rechaza una variante conocida que no es caps", () => {
    expect(() => requireCapsFor("volumes", "base")).toThrow(UnimplementedError);
  });
});
