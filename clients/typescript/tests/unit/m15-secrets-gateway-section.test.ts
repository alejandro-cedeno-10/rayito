/**
 * `secret-gateway/section.ts`: `GatewaySection.fill` resuelve cada cabecera
 * por `SecretCache.get` (un acierto no llama a AWS) y construye el
 * `SecretGatewayConfig`; `GatewayHandle` expone el resultado y su
 * `refresh()`. Espejo de `test_m15_secrets_gateway_section.py`.
 */

import { create } from "@bufbuild/protobuf";
import { describe, expect, test } from "vitest";
import { ConfigureRequestSchema } from "../../src/gen/rayito/v1/configure_pb.js";
import {
  SecretGatewayRouteStatusSchema,
  SecretGatewayStatusSchema,
} from "../../src/gen/rayito/v1/secret_gateway_pb.js";
import { GatewayStatus } from "../../src/secret-gateway/domain.js";
import {
  EMPTY_GATEWAYS,
  GatewayHandle,
  GatewaySection,
  GatewaySectionFactory,
  gatewayStatusesFromProto,
} from "../../src/secret-gateway/section.js";
import { SecretCache } from "../../src/secrets/cache.js";
import { SecretStore } from "../../src/secrets/store.js";
import { gateway } from "./m15-secrets-gateway-fixtures.js";
import { FakeSecretsManager, SENTINEL_VALUE } from "./secrets-fake.js";

function cacheOver(api: FakeSecretsManager): SecretCache {
  return new SecretCache({ store: new SecretStore({ client: api, region: "us-east-1" }) });
}

function rig() {
  const api = new FakeSecretsManager();
  api.put("rayito/anthropic", SENTINEL_VALUE);
  return { api, cache: cacheOver(api) };
}

describe("GatewaySection", () => {
  test("fill resolves headers and builds the wire message", async () => {
    const { cache } = rig();
    const section = new GatewaySection({ anthropic: gateway({ ratePerMinute: 600 }) }, cache);
    const request = create(ConfigureRequestSchema, {});
    await section.fill(request);
    const route = request.secretGateway?.routes[0];
    expect(route?.name).toBe("anthropic");
    expect(route?.upstream).toBe("https://api.anthropic.com");
    expect(route?.headers["x-api-key"]).toBe(SENTINEL_VALUE);
    expect(route?.allow[0]?.method).toBe("POST");
    expect(route?.allow[0]?.path).toBe("/v1/messages");
    expect(route?.ratePerMinute).toBe(600);
  });

  test("a cached header value makes no second AWS call", async () => {
    const { api, cache } = rig();
    const section = new GatewaySection({ a: gateway() }, cache);
    await section.fill(create(ConfigureRequestSchema, {}));
    const after = api.count("GetSecretValue");
    await section.fill(create(ConfigureRequestSchema, {}));
    expect(api.count("GetSecretValue")).toBe(after);
  });

  test("section/requiredFlag are fixed", () => {
    const { cache } = rig();
    const section = new GatewaySection({ a: gateway() }, cache);
    expect(section.section).toBe("secret_gateway");
    expect(section.requiredFlag).toBe("secretGateway");
  });
});

describe("GatewaySectionFactory", () => {
  test("build binds the resolved cache", () => {
    const { cache } = rig();
    const factory = new GatewaySectionFactory({ a: gateway() });
    const section = factory.build(cache);
    expect(section).toBeInstanceOf(GatewaySection);
  });
});

describe("gatewayStatusesFromProto", () => {
  test("maps routes and an empty error class to undefined", () => {
    const status = create(SecretGatewayStatusSchema, {
      routes: [
        create(SecretGatewayRouteStatusSchema, { name: "a", port: 4321, lastErrorClass: "" }),
        create(SecretGatewayRouteStatusSchema, {
          name: "b",
          port: 0,
          lastErrorClass: "rate_limited",
        }),
      ],
    });
    const result = gatewayStatusesFromProto(status);
    expect(result.a?.port).toBe(4321);
    expect(result.a?.lastErrorClass).toBeUndefined();
    expect(result.b?.lastErrorClass).toBe("rate_limited");
  });
});

describe("GatewayHandle", () => {
  test("the empty handle has size 0 and refresh is a no-op", async () => {
    expect(EMPTY_GATEWAYS.size).toBe(0);
    await EMPTY_GATEWAYS.refresh();
  });

  test("refresh calls the refresher and replaces the statuses", async () => {
    let calls = 0;
    const handle = new GatewayHandle({ a: new GatewayStatus(1) }, async () => {
      calls += 1;
      return { a: new GatewayStatus(999) };
    });
    expect(handle.get("a")?.port).toBe(1);
    await handle.refresh();
    expect(handle.get("a")?.port).toBe(999);
    expect(calls).toBe(1);
  });
});
