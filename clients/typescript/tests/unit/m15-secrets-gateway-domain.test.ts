/**
 * `secret-gateway/domain.ts`: validación pura de `SecretGateway`,
 * `validateGateways` y `GatewayStatus.url`. Espejo de
 * `test_m15_secrets_gateway_domain.py`.
 */

import { describe, expect, test } from "vitest";
import headerNames from "../../../../testdata/secret-gateway/header-names.json" with {
  type: "json",
};
import requestPaths from "../../../../testdata/secret-gateway/request-paths.json" with {
  type: "json",
};
import { InvalidArgumentError } from "../../src/errors.js";
import {
  GatewayStatus,
  isSafeRequestPath,
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

  test("an upstream with userinfo is rejected", () => {
    expect(() => gateway({ upstream: "https://user:pass@api.anthropic.com" })).toThrow(
      InvalidArgumentError,
    );
  });

  // `testdata/secret-gateway/header-names.json`: the same vectors `rayd-core`
  // and the Python SDK read, so the three layers agree on every name.
  test.each(headerNames.valid)("shared valid header name is accepted: %j", (name) => {
    expect(() => gateway({ headers: { [name]: "s" } })).not.toThrow();
  });

  test.each(headerNames.invalid)("shared invalid header name is rejected: %j", (name) => {
    expect(() => gateway({ headers: { [name]: "s" } })).toThrow(InvalidArgumentError);
  });

  test.each(headerNames.duplicates)("shared duplicate pair is rejected: %s / %s", (a, b) => {
    expect(() => gateway({ headers: { [a]: "a", [b]: "b" } })).toThrow(InvalidArgumentError);
  });

  // `testdata/secret-gateway/request-paths.json`: the request paths `rayd`
  // refuses before its allowlist. As an `allow` path, an unsafe one could
  // never match a request, so the SDK refuses it before any RPC, like `rayd`
  // refuses it at `Configure` (`invalid_allow_path`).
  test.each(requestPaths.unsafe)(
    "shared unsafe request path is rejected as an allow path: %j",
    (path) => {
      expect(isSafeRequestPath(path)).toBe(false);
      expect(() => gateway({ allow: [["GET", path]] })).toThrow(InvalidArgumentError);
    },
  );

  test.each(requestPaths.safe)(
    "shared safe request path is accepted as an allow path: %j",
    (path) => {
      expect(isSafeRequestPath(path)).toBe(true);
      expect(() => gateway({ allow: [["GET", path]] })).not.toThrow();
    },
  );

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
