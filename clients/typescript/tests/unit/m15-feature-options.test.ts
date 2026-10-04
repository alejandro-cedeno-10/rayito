import { describe, expect, test } from "vitest";
import { InvalidArgumentError, UnimplementedError } from "../../src/errors.js";
import { type FeatureOptions, planFeatures } from "../../src/feature-options.js";
import { S3Mount } from "../../src/s3-mounts/domain.js";
import { S3MountsSection } from "../../src/s3-mounts/section.js";
import { GatewaySectionFactory } from "../../src/secret-gateway/section.js";
import { OtlpAuth, TelemetryExport } from "../../src/telemetry-export/domain.js";
import { gateway } from "./m15-secrets-gateway-fixtures.js";

describe("feature-options", () => {
  test("with everything undefined the plan is empty", () => {
    expect(planFeatures({})).toEqual({ configureSections: [], telemetry: undefined });
  });

  // `telemetry` (m15-rayd-otlp) is the first real function: it validates
  // for real instead of throwing `UnimplementedError` unconditionally (see
  // the dedicated `telemetry` tests below), so it is not part of this
  // generic "still a stub" table.
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

  // `volumes` ya no es un stub genérico (m15-efs-volumes reemplazó su rama
  // con `planVolumes`, que exige un `EfsVolume` real y valida rutas
  // antes de llegar a `UnimplementedError`): ver m15-efs-volumes.test.ts.
  test.each([
    // `events` validates its type and `logging` first: see
    // `m15-events-webhooks-feature-options.test.ts`.
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

  test("gateways builds a GatewaySectionFactory instead of raising", () => {
    const plan = planFeatures({ gateways: { anthropic: gateway() } });
    expect(plan.configureSections).toHaveLength(1);
    expect(plan.configureSections[0]).toBeInstanceOf(GatewaySectionFactory);
  });

  test("gateways with an invalid mapping raises before any other check", () => {
    expect(() => planFeatures({ gateways: {} })).toThrow();
  });
});
