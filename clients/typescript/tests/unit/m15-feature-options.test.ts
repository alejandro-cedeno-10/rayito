import { describe, expect, test } from "vitest";
import { UnimplementedError } from "../../src/errors.js";
import { type FeatureOptions, planFeatures } from "../../src/feature-options.js";
import { GatewaySectionFactory } from "../../src/secret-gateway/section.js";
import { gateway } from "./m15-secrets-gateway-fixtures.js";

describe("feature-options", () => {
  test("with everything undefined the plan is empty", () => {
    expect(planFeatures({})).toEqual({ configureSections: [] });
  });

  test.each([
    ["mounts", { "/mnt/d": {} }, "mounts", "m15-s3-mounts"],
    ["volumes", { "/mnt/v": {} }, "volumes", "m15-efs-volumes"],
    ["size", "4gb", "size", "m15-sizes-catalog"],
    ["events", {}, "events", "m15-events-webhooks"],
    ["telemetry", {}, "telemetry", "m15-rayd-otlp"],
    ["domain", {}, "domain", "m15-custom-domain"],
  ] as const)(
    "option %s raises UnimplementedError naming its own change",
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

  test("gateways builds a GatewaySectionFactory instead of raising", () => {
    const plan = planFeatures({ gateways: { anthropic: gateway() } });
    expect(plan.configureSections).toHaveLength(1);
    expect(plan.configureSections[0]).toBeInstanceOf(GatewaySectionFactory);
  });

  test("gateways with an invalid mapping raises before any other check", () => {
    expect(() => planFeatures({ gateways: {} })).toThrow();
  });
});
