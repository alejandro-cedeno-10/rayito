import { describe, expect, test } from "vitest";
import { InvalidArgumentError, UnimplementedError } from "../../src/errors.js";
import { type FeatureOptions, planFeatures } from "../../src/feature-options.js";
import { OtlpAuth, TelemetryExport } from "../../src/telemetry-export/domain.js";

describe("feature-options", () => {
  test("with everything undefined the plan is empty", () => {
    expect(planFeatures({})).toEqual({ configureSections: [], telemetry: undefined });
  });

  // `telemetry` (m15-rayd-otlp) is the first real function: it validates
  // for real instead of throwing `UnimplementedError` unconditionally (see
  // the dedicated `telemetry` tests below), so it is not part of this
  // generic "still a stub" table.
  test.each([
    ["mounts", { "/mnt/d": {} }, "mounts", "m15-s3-mounts"],
    ["volumes", { "/mnt/v": {} }, "volumes", "m15-efs-volumes"],
    ["size", "4gb", "size", "m15-sizes-catalog"],
    ["events", {}, "events", "m15-events-webhooks"],
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

  test("a garbage telemetry value is invalid argument, not unimplemented", () => {
    expect(() => planFeatures({ telemetry: {} })).toThrow(InvalidArgumentError);
  });

  test("a real telemetry value with an unknown image variant is accepted", () => {
    const telemetry = new TelemetryExport({ auth: OtlpAuth.bearer("otlp-key") });
    const plan = planFeatures({ telemetry }, undefined);
    expect(plan.telemetry).toBe(telemetry);
  });

  test("a real telemetry value with executionRole on a non-caps image is unimplemented", () => {
    const telemetry = new TelemetryExport({ auth: OtlpAuth.executionRole() });
    expect(() => planFeatures({ telemetry }, "base")).toThrow(UnimplementedError);
  });
});
