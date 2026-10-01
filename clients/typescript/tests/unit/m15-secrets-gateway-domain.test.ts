/**
 * `secret-gateway/domain.ts`: validación pura de `SecretGateway`,
 * `validateGateways` y `GatewayStatus.url`. Espejo de
 * `test_m15_secrets_gateway_domain.py`.
 */

import { describe, expect, test } from "vitest";
import { InvalidArgumentError } from "../../src/errors.js";
import {
  GatewayStatus,
  MAX_ALLOW_RULES_PER_ROUTE,
  MAX_HEADERS_PER_ROUTE,
  MAX_RATE_PER_MINUTE,
  MAX_ROUTES_PER_GATEWAY,
  type SecretGateway,
  validateGateways,
  validateRouteName,
} from "../../src/secret-gateway/domain.js";
import { gateway } from "./m15-secrets-gateway-fixtures.js";

describe("SecretGateway", () => {
  test("a well-formed gateway is accepted", () => {
    const gw = gateway({ ratePerMinute: 600 });
    expect(gw.upstream).toBe("https://api.anthropic.com");
    expect(gw.ratePerMinute).toBe(600);
  });

  test.each([
    "http://api.anthropic.com",
    "https://api.anthropic.com/v1",
    "https://api.anthropic.com?x=1",
    "https://",
    "not-a-url",
  ])("upstream %s is rejected", (upstream) => {
    expect(() => gateway({ upstream })).toThrow(InvalidArgumentError);
  });

  test("headers must be non-empty and bounded", () => {
    expect(() => gateway({ headers: {} })).toThrow(InvalidArgumentError);
    const tooMany = Object.fromEntries(
      Array.from({ length: MAX_HEADERS_PER_ROUTE + 1 }, (_, i) => [`h${i}`, "s"]),
    );
    expect(() => gateway({ headers: tooMany })).toThrow(InvalidArgumentError);
  });

  test("allow must be non-empty, bounded and well-formed", () => {
    expect(() => gateway({ allow: [] })).toThrow(InvalidArgumentError);
    expect(() =>
      gateway({
        allow: Array.from({ length: MAX_ALLOW_RULES_PER_ROUTE + 1 }, () => ["POST", "/x"] as const),
      }),
    ).toThrow(InvalidArgumentError);
    expect(() => gateway({ allow: [["get", "/x"]] })).toThrow(InvalidArgumentError);
    expect(() => gateway({ allow: [["GET", "x"]] })).toThrow(InvalidArgumentError);
  });

  test("ratePerMinute 0 means the default, and out-of-range is rejected", () => {
    expect(gateway({ ratePerMinute: 0 }).ratePerMinute).toBe(0);
    expect(() => gateway({ ratePerMinute: MAX_RATE_PER_MINUTE + 1 })).toThrow(InvalidArgumentError);
    expect(() => gateway({ ratePerMinute: -1 })).toThrow(InvalidArgumentError);
  });
});

describe("validateRouteName", () => {
  test("accepts lowercase kebab and rejects the rest", () => {
    expect(validateRouteName("anthropic-prod")).toBe("anthropic-prod");
    for (const bad of ["", "Anthropic", "a_b", "a b", "a".repeat(65)]) {
      expect(() => validateRouteName(bad)).toThrow(InvalidArgumentError);
    }
  });
});

describe("validateGateways", () => {
  test("rejects empty, too many, and non-SecretGateway values", () => {
    expect(() => validateGateways({})).toThrow(InvalidArgumentError);
    const tooMany = Object.fromEntries(
      Array.from({ length: MAX_ROUTES_PER_GATEWAY + 1 }, (_, i) => [`g${i}`, gateway()]),
    );
    expect(() => validateGateways(tooMany)).toThrow(InvalidArgumentError);
    expect(() => validateGateways({ a: {} as SecretGateway })).toThrow(InvalidArgumentError);
  });

  test("accepts a well-formed mapping", () => {
    const mapping = { anthropic: gateway() };
    expect(validateGateways(mapping)).toBe(mapping);
  });
});

describe("GatewayStatus", () => {
  test("url is loopback and lastErrorClass defaults to undefined", () => {
    const status = new GatewayStatus(54321);
    expect(status.url).toBe("http://127.0.0.1:54321");
    expect(status.lastErrorClass).toBeUndefined();
  });
});
