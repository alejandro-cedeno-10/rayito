import { describe, expect, test } from "vitest";
import { UnimplementedError } from "../../src/errors.js";
import { type FeatureOptions, planFeatures } from "../../src/feature-options.js";
import { S3Mount } from "../../src/s3-mounts/domain.js";
import { S3MountsSection } from "../../src/s3-mounts/section.js";

describe("feature-options", () => {
  test("with everything undefined the plan is empty", () => {
    expect(planFeatures({})).toEqual({ configureSections: [] });
  });

  test("mounts builds a real section instead of raising", () => {
    const mounts = new Map([["/mnt/data", new S3Mount({ bucket: "team-data" })]]);
    const plan = planFeatures({ mounts }, "base-caps");
    expect(plan.configureSections).toHaveLength(1);
    expect(plan.configureSections[0]).toBeInstanceOf(S3MountsSection);
  });

  test("mounts on a non-caps image variant raises before run-microvm", () => {
    const mounts = new Map([["/mnt/data", new S3Mount({ bucket: "team-data" })]]);
    expect(() => planFeatures({ mounts }, "base")).toThrow(UnimplementedError);
  });

  test.each([
    ["volumes", { "/mnt/v": {} }, "volumes", "m15-efs-volumes"],
    ["size", "4gb", "size", "m15-sizes-catalog"],
    ["events", {}, "events", "m15-events-webhooks"],
    ["telemetry", {}, "telemetry", "m15-rayd-otlp"],
    ["gateways", { anthropic: {} }, "gateways", "m15-secrets-gateway"],
    ["domain", {}, "domain", "m15-custom-domain"],
  ] as const)(
    "remaining stub option %s raises UnimplementedError naming its own change",
    (field, value, optionName, slug) => {
      const options = { [field]: value } as FeatureOptions;
      try {
        planFeatures(options);
        throw new Error("expected planFeatures to throw");
      } catch (error) {
        expect(error).toBeInstanceOf(UnimplementedError);
        const unimplemented = error as UnimplementedError;
        expect(unimplemented.feature).toBe(optionName);
        expect(unimplemented.reason).toContain(slug);
      }
    },
  );
});
