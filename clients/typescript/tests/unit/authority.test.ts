/**
 * Autoridades HTTP, direcciones comodín y loopback con los vectores
 * compartidos con Python (`testdata/mcp/host-vectors.json`).
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { httpAuthority, isLoopback, isWildcard } from "../../src/authority.js";

const VECTORS = JSON.parse(
  readFileSync(join(import.meta.dirname, "../../../../testdata/mcp/host-vectors.json"), "utf8"),
) as {
  authority: Array<{ host: string; port: number; expected: string }>;
  wildcard: Array<{ host: string; expected: boolean }>;
  loopback: Array<{ host: string; expected: boolean }>;
};

describe("autoridad HTTP", () => {
  it.each(VECTORS.authority)("httpAuthority($host, $port) = $expected", (vector) => {
    expect(httpAuthority(vector.host, vector.port)).toBe(vector.expected);
  });

  it.each(VECTORS.wildcard)("isWildcard($host) = $expected", (vector) => {
    expect(isWildcard(vector.host)).toBe(vector.expected);
  });

  it.each(VECTORS.loopback)("isLoopback($host) = $expected", (vector) => {
    expect(isLoopback(vector.host)).toBe(vector.expected);
  });

  it("reconoce el loopback IPv4 mapeado en IPv6", () => {
    expect(isLoopback("::ffff:127.0.0.1")).toBe(true);
    expect(isLoopback("::ffff:192.168.1.5")).toBe(false);
  });
});
