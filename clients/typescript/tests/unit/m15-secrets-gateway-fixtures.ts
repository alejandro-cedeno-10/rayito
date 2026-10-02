/**
 * Fixture compartido por los tests de `secret-gateway/*.ts`: un
 * `SecretGateway` bien formado, con overrides, sin repetir la allowlist y
 * las cabeceras en cada test.
 */

import { SecretGateway, type SecretGatewayOptions } from "../../src/secret-gateway/domain.js";

export function gateway(overrides: Partial<SecretGatewayOptions> = {}): SecretGateway {
  return new SecretGateway({
    upstream: "https://api.anthropic.com",
    headers: { "x-api-key": "anthropic" },
    allow: [["POST", "/v1/messages"]],
    ...overrides,
  });
}
