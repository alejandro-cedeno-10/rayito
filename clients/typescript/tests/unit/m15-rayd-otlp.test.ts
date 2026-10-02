/**
 * m15-rayd-otlp: `telemetry-export/domain.ts` and `telemetry-export/section.ts`.
 * No real server or AWS SDK: validation is pure and `resolveBearerToken`
 * only needs a minimal client shaped like `SecretsManagerClient`.
 */

import { create } from "@bufbuild/protobuf";
import { afterEach, describe, expect, test, vi } from "vitest";
import { InvalidArgumentError, SecretError, UnimplementedError } from "../../src/errors.js";
import { ConfigureRequestSchema } from "../../src/gen/rayito/v1/configure_pb.js";
import { NameStyle } from "../../src/gen/rayito/v1/telemetry_export_pb.js";
import * as optional from "../../src/optional.js";
import {
  MAX_INTERVAL_S,
  MIN_INTERVAL_S,
  OtlpAuth,
  planTelemetry,
  TelemetryExport,
} from "../../src/telemetry-export/domain.js";
import {
  buildSection,
  resolveBearerToken,
  TelemetryExportSection,
} from "../../src/telemetry-export/section.js";

class RecordedCommand {
  constructor(readonly input: { SecretId: string }) {}
}

/** Mirrors `secrets-adapter.test.ts`'s `fakeModule`: the real peer is never
 * imported, `loadOptionalPeer` is spied on instead. */
function fakeSecretsManagerModule(response: { SecretString?: string } | Error) {
  const sent: RecordedCommand[] = [];
  return {
    sent,
    module: {
      SecretsManagerClient: class {
        async send(recorded: RecordedCommand): Promise<unknown> {
          sent.push(recorded);
          if (response instanceof Error) {
            throw response;
          }
          return response;
        }
      },
      GetSecretValueCommand: RecordedCommand,
    },
  };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("OtlpAuth", () => {
  test("executionRole has no secret name", () => {
    const auth = OtlpAuth.executionRole();
    expect(auth.kind).toBe("executionRole");
    expect(auth.secretName).toBeUndefined();
  });

  test("bearer keeps the secret name", () => {
    const auth = OtlpAuth.bearer("rayito/otlp-key");
    expect(auth.kind).toBe("bearer");
    expect(auth.secretName).toBe("rayito/otlp-key");
  });

  test("bearer with a blank secret name is invalid", () => {
    expect(() => OtlpAuth.bearer("   ")).toThrow(InvalidArgumentError);
  });
});

describe("TelemetryExport", () => {
  test("defaults are 60s, rayito service and executionRole", () => {
    const telemetry = new TelemetryExport();
    expect(telemetry.intervalS).toBe(60);
    expect(telemetry.serviceName).toBe("rayito");
    expect(telemetry.names).toBe("rayito");
    expect(telemetry.auth.kind).toBe("executionRole");
  });

  test.each([MIN_INTERVAL_S, MAX_INTERVAL_S, 60, 300])("interval %i is accepted", (intervalS) => {
    expect(() => new TelemetryExport({ intervalS })).not.toThrow();
  });

  test.each([0, MIN_INTERVAL_S - 1, MAX_INTERVAL_S + 1, 301, -1])(
    "interval %i is rejected",
    (intervalS) => {
      expect(() => new TelemetryExport({ intervalS })).toThrow(InvalidArgumentError);
    },
  );

  test("a blank service name is invalid", () => {
    expect(() => new TelemetryExport({ serviceName: "   " })).toThrow(InvalidArgumentError);
  });

  test("an unknown name style is invalid", () => {
    // biome-ignore lint/suspicious/noExplicitAny: deliberately wrong input
    expect(() => new TelemetryExport({ names: "e2b-but-misspelled" as any })).toThrow(
      InvalidArgumentError,
    );
  });
});

describe("planTelemetry", () => {
  test("rejects a non-TelemetryExport value", () => {
    expect(() => planTelemetry({}, undefined)).toThrow(InvalidArgumentError);
  });

  test("defers the caps check for an unknown image variant", () => {
    const telemetry = new TelemetryExport({ auth: OtlpAuth.executionRole() });
    expect(planTelemetry(telemetry, undefined)).toBe(telemetry);
  });

  test("passes bearer auth on any image variant", () => {
    const telemetry = new TelemetryExport({ auth: OtlpAuth.bearer("rayito/otlp-key") });
    expect(planTelemetry(telemetry, "base")).toBe(telemetry);
  });

  test("requires caps for executionRole on a known non-caps variant", () => {
    const telemetry = new TelemetryExport({ auth: OtlpAuth.executionRole() });
    expect(() => planTelemetry(telemetry, "base")).toThrow(UnimplementedError);
  });

  test("accepts executionRole on the caps variant", () => {
    const telemetry = new TelemetryExport({ auth: OtlpAuth.executionRole() });
    expect(planTelemetry(telemetry, "base-caps")).toBe(telemetry);
  });
});

describe("TelemetryExportSection", () => {
  test("names itself telemetry_export", () => {
    const section = new TelemetryExportSection(
      new TelemetryExport(),
      "arn:aws:lambda:us-east-1:123456789012:microvm-image/rayito-base",
      "3",
      2048,
      undefined,
    );
    expect(section.section).toBe("telemetry_export");
    expect(section.requiredFlag).toBe("telemetry_export");
  });

  test("fill builds an executionRole section with image facts", () => {
    const telemetry = new TelemetryExport({ intervalS: 90, serviceName: "agente", names: "e2b" });
    const section = new TelemetryExportSection(
      telemetry,
      "arn:aws:lambda:us-east-1:123456789012:microvm-image/rayito-base-caps",
      "7",
      4096,
      undefined,
    );
    const request = create(ConfigureRequestSchema, {});
    section.fill(request);
    const config = request.telemetryExport;
    expect(config?.intervalS).toBe(90);
    expect(config?.serviceName).toBe("agente");
    expect(config?.names).toBe(NameStyle.E2B);
    expect(config?.imageArn.endsWith("rayito-base-caps")).toBe(true);
    expect(config?.imageVersion).toBe("7");
    expect(config?.imageMemoryMib).toBe(4096);
    expect(config?.auth.case).toBe("executionRole");
  });

  test("fill builds a bearer section with the resolved token, never the secret name", () => {
    const telemetry = new TelemetryExport({ auth: OtlpAuth.bearer("rayito/otlp-key") });
    const section = new TelemetryExportSection(
      telemetry,
      "arn:aws:lambda:us-east-1:123456789012:microvm-image/rayito-base",
      "1",
      2048,
      "sk-resolved",
    );
    const request = create(ConfigureRequestSchema, {});
    section.fill(request);
    const config = request.telemetryExport;
    expect(config?.auth.case).toBe("bearer");
    expect(config?.auth.case === "bearer" && config.auth.value.token).toBe("sk-resolved");
  });
});

describe("resolveBearerToken", () => {
  test("returns the secret string and sends GetSecretValue with the secret id", async () => {
    const fake = fakeSecretsManagerModule({ SecretString: "sk-test" });
    vi.spyOn(optional, "loadOptionalPeer").mockResolvedValue(fake.module);
    const value = await resolveBearerToken("rayito/otlp-key", "us-east-1", undefined);
    expect(value).toBe("sk-test");
    expect(fake.sent).toEqual([new RecordedCommand({ SecretId: "rayito/otlp-key" })]);
  });

  test("rejects an empty SecretString", async () => {
    vi.spyOn(optional, "loadOptionalPeer").mockResolvedValue(
      fakeSecretsManagerModule({ SecretString: "" }).module,
    );
    await expect(resolveBearerToken("rayito/otlp-key", "us-east-1", undefined)).rejects.toThrow(
      SecretError,
    );
  });

  test("wraps a client error without repeating the secret name", async () => {
    vi.spyOn(optional, "loadOptionalPeer").mockResolvedValue(
      fakeSecretsManagerModule(new Error("boom")).module,
    );
    const secretName = "rayito/super-secret-name";
    await expect(resolveBearerToken(secretName, "us-east-1", undefined)).rejects.toThrow(
      SecretError,
    );
    try {
      await resolveBearerToken(secretName, "us-east-1", undefined);
      expect.unreachable();
    } catch (error) {
      expect(String(error)).not.toContain(secretName);
    }
  });
});

describe("buildSection", () => {
  test("never resolves a bearer token when auth is executionRole", async () => {
    const loader = vi.spyOn(optional, "loadOptionalPeer");
    const telemetry = new TelemetryExport({ auth: OtlpAuth.executionRole() });
    const section = await buildSection(telemetry, {
      imageArn: "arn:aws:lambda:us-east-1:123456789012:microvm-image/rayito-base-caps",
      imageVersion: "1",
      imageMemoryMib: 2048,
      region: "us-east-1",
      credentials: undefined,
    });
    const request = create(ConfigureRequestSchema, {});
    section.fill(request);
    expect(request.telemetryExport?.auth.case).toBe("executionRole");
    expect(loader).not.toHaveBeenCalled();
  });

  test("resolves the bearer token when auth is bearer", async () => {
    vi.spyOn(optional, "loadOptionalPeer").mockResolvedValue(
      fakeSecretsManagerModule({ SecretString: "sk-test" }).module,
    );
    const telemetry = new TelemetryExport({ auth: OtlpAuth.bearer("rayito/otlp-key") });
    const section = await buildSection(telemetry, {
      imageArn: "arn:aws:lambda:us-east-1:123456789012:microvm-image/rayito-base",
      imageVersion: "1",
      imageMemoryMib: 2048,
      region: "us-east-1",
      credentials: undefined,
    });
    const request = create(ConfigureRequestSchema, {});
    section.fill(request);
    expect(
      request.telemetryExport?.auth.case === "bearer" && request.telemetryExport.auth.value.token,
    ).toBe("sk-test");
  });
});
