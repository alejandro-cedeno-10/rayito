import { describe, expect, test } from "vitest";
import { UnimplementedError } from "../../src/errors.js";
import { type FeatureOptions, planFeatures } from "../../src/feature-options.js";

describe("feature-options", () => {
  test("with everything undefined the plan is empty", () => {
    expect(planFeatures({})).toEqual({ configureSections: [] });
  });

  // `volumes` ya no es un stub genérico (m15-efs-volumes reemplazó su rama
  // con `requireVolumeSupport`, que exige un `EfsVolume` real y valida rutas
  // antes de llegar a `UnimplementedError`): ver m15-efs-volumes.test.ts.
  test.each([
    ["mounts", { "/mnt/d": {} }, "mounts", "m15-s3-mounts"],
    ["size", "4gb", "size", "m15-sizes-catalog"],
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
