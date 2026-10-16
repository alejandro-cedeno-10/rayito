/**
 * Los `Host` y `Origin` que acepta el listener local: los nombres de loopback
 * con el puerto, normalizados, y nada más. La matriz completa contra el
 * servidor está en `guard-vectors.json`.
 */

import { describe, expect, it } from "vitest";
import { httpAuthority } from "../../src/authority.js";
import { buildAccess, LOOPBACK_AUTHORITY_HOSTS } from "../../src/cli/access.js";

const PORT = 5000;

describe("buildAccess", () => {
  const access = buildAccess({ bind: "127.0.0.1", localPort: PORT });

  it("admits the loopback names with the port, in any case and padding", () => {
    for (const name of LOOPBACK_AUTHORITY_HOSTS) {
      expect(access.admitsHost(httpAuthority(name, PORT))).toBe(true);
    }
    expect(access.admitsHost("LOCALHOST:5000")).toBe(true);
    expect(access.admitsHost(" 127.0.0.1:5000 ")).toBe(true);
    expect(access.admitsHost("[::1]:5000")).toBe(true);
  });

  it.each([
    undefined,
    "",
    "127.0.0.1",
    "localhost",
    "127.0.0.1:5001",
    "evil.example:5000",
    "0.0.0.0:5000",
  ])("refuses the host %s", (host) => {
    expect(access.admitsHost(host)).toBe(false);
  });

  it("admits no Origin or its own, and refuses null and foreign ones", () => {
    expect(access.admitsOrigin(undefined)).toBe(true);
    expect(access.admitsOrigin("http://127.0.0.1:5000")).toBe(true);
    expect(access.admitsOrigin("http://localhost:5000/")).toBe(true);
    for (const origin of [
      "null",
      "http://evil.example",
      "https://127.0.0.1:5000",
      "http://127.0.0.1",
    ]) {
      expect(access.admitsOrigin(origin), origin).toBe(false);
    }
  });

  it("the bare name only equals name:port when the port is the HTTP one", () => {
    const http = buildAccess({ bind: "127.0.0.1", localPort: 80 });
    expect(http.admitsHost("localhost")).toBe(true);
    expect(http.admitsOrigin("http://127.0.0.1")).toBe(true);
    expect(access.admitsHost("localhost")).toBe(false);
  });

  it("brackets an IPv6 literal in the authority", () => {
    expect(httpAuthority("::1", PORT)).toBe("[::1]:5000");
    expect(httpAuthority("localhost", PORT)).toBe("localhost:5000");
  });
});
