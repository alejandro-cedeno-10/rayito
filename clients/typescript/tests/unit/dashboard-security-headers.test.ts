/**
 * Las cabeceras de seguridad de `testdata/dashboard/security-headers.json`,
 * exactas: las que construye `headers.ts` y las que el servidor local manda en
 * cada respuesta (la SPA, la API, el tema y el logo). Python ejecuta el mismo
 * fichero (`test_dashboard_security_headers.py`).
 */

import { afterEach, describe, expect, it } from "vitest";
import { dashboardStaticDirectory } from "../../src/cli/dashboard-assets.js";
import { LocalDashboard } from "../../src/cli/dashboard-server.js";
import {
  contentSecurityPolicy,
  LOGO_HEADERS,
  securityHeaders,
} from "../../src/dashboard/api/headers.js";
import { parseLogo } from "../../src/dashboard/logo.js";
import { Appearance } from "../../src/dashboard/theme.js";
import { loadDashboardVectors, Rig } from "./fake/dashboard-world.js";
import { LocalClient } from "./local-client.js";

const VECTORS = loadDashboardVectors<{
  headers: Record<string, string>;
  logoHeaders: Record<string, string>;
  withConnectSrc: { connectSrc: string[]; contentSecurityPolicy: string };
}>("security-headers.json");
const LOGO_SVG = new TextEncoder().encode('<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>');
const open: LocalClient[] = [];

afterEach(async () => {
  await Promise.all(open.splice(0).map((client) => client.close()));
});

describe("security headers", () => {
  it("the builders give the shared headers", () => {
    expect(securityHeaders()).toEqual(VECTORS.headers);
    expect(LOGO_HEADERS).toEqual(VECTORS.logoHeaders);
    expect(contentSecurityPolicy(VECTORS.withConnectSrc.connectSrc)).toBe(
      VECTORS.withConnectSrc.contentSecurityPolicy,
    );
  });

  it("every local response carries the shared headers", async () => {
    const rig = new Rig({ sandboxes: [{ n: 1 }] });
    const client = await LocalClient.start(rig.api, {
      appearance: new Appearance({ logo: parseLogo(LOGO_SVG) }),
    });
    open.push(client);
    for (const target of [
      "/",
      "/js/app.js",
      "/theme.css",
      "/api/v1/meta",
      "/api/v1/nope",
      "/nope",
    ]) {
      const response = await client.raw("GET", target);
      for (const [name, value] of Object.entries(VECTORS.headers)) {
        expect(response.headers[name.toLowerCase()], `${target} ${name}`).toBe(value);
      }
    }
    const logo = await client.raw("GET", "/theme/logo");
    for (const [name, value] of Object.entries(VECTORS.logoHeaders)) {
      expect(logo.headers[name.toLowerCase()]).toBe(value);
    }
    const unauthorized = await client.raw("GET", "/api/v1/meta", { sign: false });
    expect(unauthorized.headers["content-security-policy"]).toBe(
      VECTORS.headers["Content-Security-Policy"],
    );
  });
});

describe("the SPA directory", () => {
  it("is found", () => {
    expect(dashboardStaticDirectory()).toContain("_static");
    expect(LocalDashboard).toBeDefined();
  });
});
