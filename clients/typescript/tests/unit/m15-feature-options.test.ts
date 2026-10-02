import { describe, expect, test } from "vitest";
import { UnimplementedError } from "../../src/errors.js";
import { type FeatureOptions, planFeatures } from "../../src/feature-options.js";
import { S3Mount } from "../../src/s3-mounts/domain.js";
import { S3MountsSection } from "../../src/s3-mounts/section.js";
import { GatewaySectionFactory } from "../../src/secret-gateway/section.js";
import { gateway } from "./m15-secrets-gateway-fixtures.js";

describe("feature-options", () => {
  test("with everything undefined the plan is empty", () => {
    expect(planFeatures({})).toEqual({ configureSections: [] });
  });

  test("size no longer raises and produces no configure section", () => {
    // m15-sizes-catalog: ya no es un stub; la resolución real se prueba en
    // sizing.test.ts y m15-sizes-catalog-create.test.ts.
    expect(planFeatures({ size: "4gb" })).toEqual({ configureSections: [] });
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
    // `events` validates its type and `logging` first: see
    // `m15-events-webhooks-feature-options.test.ts`.
    ["telemetry", {}, "telemetry", "m15-rayd-otlp"],
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

  test("gateways builds a GatewaySectionFactory instead of raising", () => {
    const plan = planFeatures({ gateways: { anthropic: gateway() } });
    expect(plan.configureSections).toHaveLength(1);
    expect(plan.configureSections[0]).toBeInstanceOf(GatewaySectionFactory);
  });

  test("gateways with an invalid mapping raises before any other check", () => {
    expect(() => planFeatures({ gateways: {} })).toThrow();
  });
});
