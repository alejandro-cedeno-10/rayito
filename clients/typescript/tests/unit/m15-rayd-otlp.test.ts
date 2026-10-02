/**
 * m15-rayd-otlp: `telemetry-export/{domain,section,propagation}.ts`.
 * No real server or AWS SDK: validation is pure and the bearer is resolved
 * through a `SecretCache` over M13a's fake Secrets Manager (`secrets-fake.ts`);
 * `TraceparentProvider` only needs a minimal module shaped like
 * `@opentelemetry/api`'s `context`/`propagation` exports.
 */

import { inspect } from "node:util";
import { create } from "@bufbuild/protobuf";
import { afterEach, describe, expect, test, vi } from "vitest";
import { InvalidArgumentError, SecretNotFoundError, UnimplementedError } from "../../src/errors.js";
import { ConfigureRequestSchema } from "../../src/gen/rayito/v1/configure_pb.js";
import { AgentFeaturesSchema } from "../../src/gen/rayito/v1/features_pb.js";
import { TelemetryExportNameStyle } from "../../src/gen/rayito/v1/telemetry_export_pb.js";
import * as optional from "../../src/optional.js";
import { SecretCache } from "../../src/secrets/cache.js";
import { SecretStore } from "../../src/secrets/store.js";
import {
  imageMemoryMibFromGuestBytes,
  MAX_INTERVAL_S,
  MIN_INTERVAL_S,
  OtlpAuth,
  planTelemetry,
  TelemetryExport,
} from "../../src/telemetry-export/domain.js";
import {
  callMetadataProvidersFor,
  TraceparentProvider,
} from "../../src/telemetry-export/propagation.js";
import {
  buildSection,
  requireTelemetrySupport,
  resolveBearerToken,
  TelemetryExportSection,
} from "../../src/telemetry-export/section.js";
import { FakeSecretsManager, SENTINEL_NAME, SENTINEL_VALUE } from "./secrets-fake.js";

function cacheOver(api: FakeSecretsManager): SecretCache {
  return new SecretCache({ store: new SecretStore({ client: api, region: "us-east-1" }) });
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
    const auth = OtlpAuth.bearer("otlp-key");
    expect(auth.kind).toBe("bearer");
    expect(auth.secretName).toBe("otlp-key");
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
    const telemetry = new TelemetryExport({ auth: OtlpAuth.bearer("otlp-key") });
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
    // `requiredFlag` names the `AgentFeatures` field (camelCase in TS).
    expect(section.requiredFlag).toBe("telemetryExport");
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
    expect(config?.names).toBe(TelemetryExportNameStyle.E2B);
    expect(config?.imageArn.endsWith("rayito-base-caps")).toBe(true);
    expect(config?.imageVersion).toBe("7");
    expect(config?.imageMemoryMib).toBe(4096);
    expect(config?.auth.case).toBe("executionRole");
  });

  test("fill builds a bearer section with the resolved token, never the secret name", () => {
    const telemetry = new TelemetryExport({ auth: OtlpAuth.bearer("otlp-key") });
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
  test("executionRole never touches Secrets Manager", async () => {
    const api = new FakeSecretsManager();
    expect(await resolveBearerToken(new TelemetryExport(), cacheOver(api))).toBeUndefined();
    expect(api.requests).toEqual([]);
  });

  test("bearer resolves under the rayito/ prefix, like secrets", async () => {
    const api = new FakeSecretsManager();
    api.put("rayito/otlp-key", "sk-test");
    const telemetry = new TelemetryExport({ auth: OtlpAuth.bearer("otlp-key") });
    expect(await resolveBearerToken(telemetry, cacheOver(api))).toBe("sk-test");
    expect(api.requests).toEqual([["GetSecretValue", { SecretId: "rayito/otlp-key" }]]);
  });

  test("bearer reuses the cache within its TTL", async () => {
    const api = new FakeSecretsManager();
    api.put("rayito/otlp-key", "sk-test");
    const cache = cacheOver(api);
    const telemetry = new TelemetryExport({ auth: OtlpAuth.bearer("otlp-key") });
    await resolveBearerToken(telemetry, cache);
    await resolveBearerToken(telemetry, cache);
    expect(api.count("GetSecretValue")).toBe(1);
  });

  test("a missing secret is translated and never named", async () => {
    const telemetry = new TelemetryExport({ auth: OtlpAuth.bearer(SENTINEL_NAME) });
    const failure = resolveBearerToken(telemetry, cacheOver(new FakeSecretsManager()));
    await expect(failure).rejects.toThrow(SecretNotFoundError);
    await failure.catch((error: unknown) => {
      expect(String(error)).not.toContain(SENTINEL_NAME);
    });
  });
});

describe("buildSection", () => {
  test("an executionRole section carries no bearer token", async () => {
    const section = await buildSection(new TelemetryExport(), {
      imageArn: "arn:aws:lambda:us-east-1:123456789012:microvm-image/rayito-base-caps",
      imageVersion: "1",
      imageMemoryMib: 2048,
      secretCache: cacheOver(new FakeSecretsManager()),
    });
    const request = create(ConfigureRequestSchema, {});
    section.fill(request);
    expect(request.telemetryExport?.auth.case).toBe("executionRole");
    expect(section.hasBearerToken).toBe(false);
  });

  test("a bearer section carries the resolved token into fill()", async () => {
    const api = new FakeSecretsManager();
    api.put("rayito/otlp-key", "sk-test");
    const section = await buildSection(new TelemetryExport({ auth: OtlpAuth.bearer("otlp-key") }), {
      imageArn: "arn:aws:lambda:us-east-1:123456789012:microvm-image/rayito-base",
      imageVersion: "1",
      imageMemoryMib: 2048,
      secretCache: cacheOver(api),
    });
    const request = create(ConfigureRequestSchema, {});
    section.fill(request);
    expect(
      request.telemetryExport?.auth.case === "bearer" && request.telemetryExport.auth.value.token,
    ).toBe("sk-test");
  });

  test("the token never shows in inspect, console output or JSON", () => {
    const section = new TelemetryExportSection(
      new TelemetryExport({ auth: OtlpAuth.bearer("otlp-key") }),
      "arn:aws:lambda:us-east-1:123456789012:microvm-image/rayito-base",
      "1",
      2048,
      SENTINEL_VALUE,
    );
    expect(inspect(section, { depth: 10, showHidden: true })).not.toContain(SENTINEL_VALUE);
    expect(JSON.stringify(section)).not.toContain(SENTINEL_VALUE);
    expect(section.hasBearerToken).toBe(true);
  });
});

describe("requireTelemetrySupport", () => {
  test("a 0.6 agent without the exporter names the real cause and the image", () => {
    const features = create(AgentFeaturesSchema, {
      configure: true,
      telemetryExport: false,
    });
    expect(() => requireTelemetrySupport(features)).toThrow(UnimplementedError);
    try {
      requireTelemetrySupport(features);
    } catch (error) {
      expect(String((error as Error).message)).toContain("AWS_REGION");
      expect(String((error as Error).message)).toContain("rayd 0.6.0");
      expect(String((error as Error).message)).not.toContain("pendiente de medición");
    }
  });

  test("an agent with the exporter passes the gate", () => {
    const features = create(AgentFeaturesSchema, {
      configure: true,
      telemetryExport: true,
    });
    expect(() => requireTelemetrySupport(features)).not.toThrow();
  });
});

describe("callMetadataProvidersFor", () => {
  test("no tracerProvider installs nothing and imports nothing", async () => {
    const loader = vi.spyOn(optional, "loadOptionalPeer");
    expect(await callMetadataProvidersFor(undefined)).toEqual([]);
    expect(loader).not.toHaveBeenCalled();
  });

  test("a tracerProvider installs one TraceparentProvider", async () => {
    vi.spyOn(optional, "loadOptionalPeer").mockResolvedValue(
      fakeOpenTelemetryModule({ traceparent: "00-a-b-01" }).module,
    );
    const providers = await callMetadataProvidersFor({ getTracer: () => ({}) as never });
    expect(providers).toHaveLength(1);
    expect(providers[0]).toBeInstanceOf(TraceparentProvider);
  });
});

describe("imageMemoryMibFromGuestBytes", () => {
  test("undefined (Health never read yet) is 0 MiB", () => {
    expect(imageMemoryMibFromGuestBytes(undefined)).toBe(0);
  });

  test("divides the guest's 4x view down to the image's declared memory (Q88)", () => {
    // 8 GiB of guest-visible memory -> a 2 GiB image (GUEST_MEMORY_MULTIPLIER = 4).
    const guestBytes = 8 * 1024 * 1024 * 1024;
    expect(imageMemoryMibFromGuestBytes(guestBytes)).toBe(2048);
  });

  test("floors rather than rounds, like the Python SDK's floor division", () => {
    const almostFiveMib = 5 * 1024 * 1024 * 4 - 1;
    expect(imageMemoryMibFromGuestBytes(almostFiveMib)).toBe(4);
  });
});

/** Mirrors `fakeSecretsManagerModule`: the real `@opentelemetry/api` peer is
 * never imported, `loadOptionalPeer` is spied on with a minimal fake shaped
 * like its `context`/`propagation` exports. */
function fakeOpenTelemetryModule(carrierToInject: Record<string, string>) {
  const activeCalls: unknown[] = [];
  return {
    activeCalls,
    module: {
      context: {
        active: () => {
          const token = Symbol("active-context");
          activeCalls.push(token);
          return token;
        },
      },
      propagation: {
        inject: (_activeContext: unknown, carrier: Record<string, string>) => {
          Object.assign(carrier, carrierToInject);
        },
      },
    },
  };
}

describe("TraceparentProvider", () => {
  test("create() surfaces a clear error when @opentelemetry/api is missing", async () => {
    vi.spyOn(optional, "loadOptionalPeer").mockRejectedValue(
      new InvalidArgumentError("tracerProvider necesita el paquete opcional '@opentelemetry/api'"),
    );
    await expect(TraceparentProvider.create()).rejects.toThrow(InvalidArgumentError);
  });

  test("metadata() returns whatever the active context's propagator injected", async () => {
    const fake = fakeOpenTelemetryModule({ traceparent: "00-a-b-01" });
    vi.spyOn(optional, "loadOptionalPeer").mockResolvedValue(fake.module);
    const provider = await TraceparentProvider.create();
    expect(provider.metadata()).toEqual([["traceparent", "00-a-b-01"]]);
    expect(fake.activeCalls).toHaveLength(1);
  });

  test("metadata() never carries baggage, even when the propagator injects one", async () => {
    const fake = fakeOpenTelemetryModule({
      traceparent: "00-a-b-01",
      baggage: "secret=value",
    });
    vi.spyOn(optional, "loadOptionalPeer").mockResolvedValue(fake.module);
    const provider = await TraceparentProvider.create();
    const metadata = Object.fromEntries(provider.metadata());
    expect(metadata.traceparent).toBe("00-a-b-01");
    expect(metadata.baggage).toBeUndefined();
  });

  test("metadata() is computed fresh on every call, not fixed at create()", async () => {
    const fake = fakeOpenTelemetryModule({ traceparent: "00-a-b-01" });
    vi.spyOn(optional, "loadOptionalPeer").mockResolvedValue(fake.module);
    const provider = await TraceparentProvider.create();
    provider.metadata();
    provider.metadata();
    expect(fake.activeCalls).toHaveLength(2);
  });
});
