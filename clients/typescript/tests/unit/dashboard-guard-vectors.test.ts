/**
 * La matriz de `testdata/dashboard/guard-vectors.json` contra
 * `LocalApp.respond` (sin sockets): sesión, `Host`, `Origin`,
 * `Sec-Fetch-Site`, CSRF, tipo de contenido, métodos y rutas. El mismo
 * fichero lo ejecuta Python (`test_dashboard_guard_vectors.py`), así las dos
 * cáscaras locales dan la misma respuesta a cada petición.
 */

import { describe, expect, it } from "vitest";
import { buildAccess } from "../../src/cli/access.js";
import { dashboardStaticDirectory } from "../../src/cli/dashboard-assets.js";
import { LOOPBACK, LocalApp, SessionGuard, startUrl } from "../../src/cli/dashboard-server.js";
import { loadStaticFiles } from "../../src/dashboard/api/static.js";
import { loadDashboardVectors, Rig, type World } from "./fake/dashboard-world.js";

interface GuardCase {
  readonly id: string;
  readonly world: World;
  readonly session: "signed" | "anonymous";
  readonly redeemFirst?: boolean;
  readonly request: {
    readonly method: string;
    readonly target: string;
    readonly headers: Record<string, string | null>;
    readonly body?: string;
  };
  readonly expect: {
    readonly status: number;
    readonly errorCode?: string;
    readonly headers?: Record<string, string | null>;
    readonly setCookieHas?: string[];
    readonly setCookieLacks?: string[];
    readonly bodyHas?: string[];
    readonly bodyText?: string;
    readonly bodyContains?: string;
    readonly contentTypePrefix?: string;
    readonly writes?: string[];
    readonly noPlaneCalls?: boolean;
    readonly costQueries?: number;
  };
}

const CASES = loadDashboardVectors<{ cases: GuardCase[] }>("guard-vectors.json").cases;
const PORT = 43210;
const SIGNED_STATIC = loadStaticFiles(dashboardStaticDirectory());
const DECODER = new TextDecoder();
const ENCODER = new TextEncoder();

class Scenario {
  readonly rig: Rig;
  readonly guard: SessionGuard;
  readonly app: LocalApp;
  readonly values: Record<string, string>;
  readonly cookie: string;

  constructor(testCase: GuardCase) {
    this.rig = new Rig(testCase.world);
    this.guard = new SessionGuard(PORT);
    this.app = new LocalApp(
      this.rig.api,
      buildAccess({ bind: LOOPBACK, localPort: PORT }),
      this.guard,
      SIGNED_STATIC,
    );
    const start = new URL(startUrl(PORT, this.guard.startToken));
    this.values = {
      port: String(PORT),
      host: `${LOOPBACK}:${PORT}`,
      cookieName: this.guard.cookieName,
      csrf: this.guard.csrfToken,
      startTarget: `${start.pathname}${start.search}`,
    };
    this.cookie = "";
    if (testCase.session === "signed") {
      this.cookie = `${this.guard.cookieName}=${this.guard.redeem(this.guard.startToken)}`;
    } else if (testCase.redeemFirst === true) {
      this.guard.redeem(this.guard.startToken);
    }
  }

  fill(text: string): string {
    return Object.entries(this.values).reduce(
      (current, [name, value]) => current.replaceAll(`{${name}}`, value),
      text,
    );
  }

  headers(declared: Record<string, string | null>): Record<string, string> {
    const headers: Record<string, string> = { host: this.values.host as string };
    if (this.cookie !== "") {
      headers.cookie = this.cookie;
    }
    for (const [name, value] of Object.entries(declared)) {
      if (value === null) {
        delete headers[name.toLowerCase()];
      } else {
        headers[name.toLowerCase()] = this.fill(value);
      }
    }
    return headers;
  }
}

describe("guard vectors (local shell)", () => {
  for (const testCase of CASES) {
    it(testCase.id, async () => {
      const scenario = new Scenario(testCase);
      const spec = testCase.request;
      const reply = await scenario.app.respond(
        spec.method,
        scenario.fill(spec.target),
        scenario.headers(spec.headers),
        spec.body === undefined ? undefined : ENCODER.encode(spec.body),
      );
      const expected = testCase.expect;
      expect(reply.status).toBe(expected.status);
      const text = DECODER.decode(reply.body);
      if (expected.errorCode !== undefined) {
        expect(JSON.parse(text).error.code).toBe(expected.errorCode);
      }
      for (const [name, value] of Object.entries(expected.headers ?? {})) {
        if (value === null) {
          expect(reply.headers).not.toHaveProperty(name);
        } else {
          expect(reply.headers[name]).toBe(value);
        }
      }
      const attributes = new Set(
        (reply.headers["Set-Cookie"] ?? "").split(";").map((part) => part.trim()),
      );
      for (const attribute of expected.setCookieHas ?? []) {
        expect(attributes.has(attribute), attribute).toBe(true);
      }
      for (const attribute of expected.setCookieLacks ?? []) {
        expect(attributes.has(attribute), attribute).toBe(false);
      }
      for (const key of expected.bodyHas ?? []) {
        expect(JSON.parse(text)).toHaveProperty(key);
      }
      if (expected.bodyText !== undefined) {
        expect(text).toBe(expected.bodyText);
      }
      if (expected.bodyContains !== undefined) {
        expect(text).toContain(expected.bodyContains);
      }
      if (expected.contentTypePrefix !== undefined) {
        expect(reply.contentType.startsWith(expected.contentTypePrefix)).toBe(true);
      }
      if (expected.writes !== undefined) {
        expect(scenario.rig.writes()).toEqual(expected.writes);
      }
      if (expected.noPlaneCalls === true) {
        expect(scenario.rig.plane.calls).toEqual([]);
      }
      if (expected.costQueries !== undefined) {
        expect(scenario.rig.costSource.queries).toHaveLength(expected.costQueries);
      }
      await scenario.rig.api.close();
    });
  }
});
