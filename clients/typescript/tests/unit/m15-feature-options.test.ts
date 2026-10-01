import { describe, expect, test } from "vitest";
import { UnimplementedError } from "../../src/errors.js";
import { type FeatureOptions, planFeatures } from "../../src/feature-options.js";

describe("feature-options", () => {
  test("with everything undefined the plan is empty", () => {
    expect(planFeatures({})).toEqual({ configureSections: [] });
  });

  test("size no longer raises and produces no configure section", () => {
    // m15-sizes-catalog: ya no es un stub; la resolución real se prueba en
    // sizing.test.ts y m15-sizes-catalog-create.test.ts.
    expect(planFeatures({ size: "4gb" })).toEqual({ configureSections: [] });
  });

  test.each([
    ["mounts", { "/mnt/d": {} }, "mounts", "m15-s3-mounts"],
    ["volumes", { "/mnt/v": {} }, "volumes", "m15-efs-volumes"],
    ["events", {}, "events", "m15-events-webhooks"],
    ["telemetry", {}, "telemetry", "m15-rayd-otlp"],
    ["gateways", { anthropic: {} }, "gateways", "m15-secrets-gateway"],
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
});
