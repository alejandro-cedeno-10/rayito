/**
 * Seguridad del servidor local de `rayito dashboard` sobre sockets reales en
 * loopback: sólo escucha en `127.0.0.1`; sin sesión no responde nada; el token
 * de arranque se canjea una sola vez por una cookie `HttpOnly`; los topes de
 * petición y el desdoblamiento de HTTP/1.1; la SPA que sirve; y que ni el
 * token ni las peticiones se registran. La matriz de `Host`, `Origin`,
 * `Sec-Fetch-Site` y CSRF la cubre `dashboard-guard-vectors.test.ts`.
 */

import { readdirSync, readFileSync } from "node:fs";
import { createConnection } from "node:net";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { dashboardStaticDirectory } from "../../src/cli/dashboard-assets.js";
import {
  CSRF_HEADER,
  LOOPBACK,
  SECURITY_HEADERS,
  SessionGuard,
} from "../../src/cli/dashboard-server.js";
import {
  DASHBOARD_REQUEST_MAX_BODY_BYTES,
  DASHBOARD_START_TOKEN_TTL_SECONDS,
} from "../../src/limits.js";
import { FakeClock } from "./fake/dashboard.js";
import { Rig } from "./fake/dashboard-world.js";
import { LocalClient } from "./local-client.js";

const JSON_HEADERS = { "Content-Type": "application/json" };
const KILL = "/api/v1/sandboxes/microvm-00000000-0000-0000-0000-000000000001/kill";
const open: LocalClient[] = [];
const MS_PER_SECOND = 1000;

async function start(
  world = { allowActions: true, sandboxes: [{ n: 1 }, { n: 2 }] },
  options: { signIn?: boolean } = {},
): Promise<{ client: LocalClient; rig: Rig }> {
  const rig = new Rig(world);
  const client = await LocalClient.start(rig.api, options);
  open.push(client);
  return { client, rig };
}

afterEach(async () => {
  await Promise.all(open.splice(0).map((client) => client.close()));
  vi.restoreAllMocks();
});

describe("listener", () => {
  it("only listens on loopback", async () => {
    const { client } = await start();
    expect(client.server.address).toBe(LOOPBACK);
    expect(client.port).toBeGreaterThan(0);
    const response = await client.raw("GET", "/api/v1/meta");
    expect(response.status).toBe(200);
    await expect(
      new Promise((resolve, reject) => {
        const socket = createConnection({ host: LOOPBACK, port: client.port }, () => {
          socket.destroy();
          resolve(true);
        });
        socket.on("error", reject);
      }),
    ).resolves.toBe(true);
  });

  it("refuses to reuse a busy port", async () => {
    const { client } = await start();
    const { LocalDashboard } = await import("../../src/cli/dashboard-server.js");
    await expect(
      LocalDashboard.start(new Rig().api, {
        port: client.port,
        staticDirectory: dashboardStaticDirectory(),
      }),
    ).rejects.toMatchObject({ code: "EADDRINUSE" });
  });
});

describe("start token and session", () => {
  it("swaps the start token for a strict HttpOnly cookie", async () => {
    const { client } = await start(undefined, { signIn: false });
    const response = await client.raw("GET", client.startTarget, { sign: false });
    expect(response.status).toBe(303);
    expect(response.headers.location).toBe("/");
    expect(response.headers["referrer-policy"]).toBe("no-referrer");
    const attributes = (response.headers["set-cookie"] ?? "").split("; ");
    expect(attributes).toEqual(expect.arrayContaining(["HttpOnly", "SameSite=Strict", "Path=/"]));
    expect(attributes).not.toContain("Secure");
  });

  it("works once", async () => {
    const { client } = await start();
    const response = await client.raw("GET", client.startTarget, { sign: false });
    expect(response.status).toBe(401);
    expect(JSON.parse(response.body).error.code).toBe("unauthorized");
  });

  it("a wrong token does not burn the real one", () => {
    const guard = new SessionGuard(1234);
    expect(guard.redeem("not-the-token")).toBeUndefined();
    expect(guard.redeem(guard.startToken)).toBeDefined();
    expect(() => guard.startToken).toThrow();
  });

  it("an expired token is refused, and it is burnt", () => {
    const clock = new FakeClock();
    const guard = new SessionGuard(1234, { clock: () => clock.now * MS_PER_SECOND });
    const token = guard.startToken;
    clock.advance(DASHBOARD_START_TOKEN_TTL_SECONDS + 1);
    expect(guard.redeem(token)).toBeUndefined();
    expect(guard.hasSession(`${guard.cookieName}=anything`)).toBe(false);
    expect(guard.redeem(token)).toBeUndefined();
  });

  it("the cookie name carries the port and the tokens differ between runs", () => {
    const first = new SessionGuard(4000);
    const second = new SessionGuard(4000);
    expect(first.cookieName).toBe("rayito_dashboard_4000");
    expect(first.startToken).not.toBe(second.startToken);
    expect(first.csrfToken).not.toBe(second.csrfToken);
  });

  it("the last cookie of the name is the one compared", () => {
    const guard = new SessionGuard(4000);
    const session = guard.redeem(guard.startToken) as string;
    expect(guard.hasSession(`${guard.cookieName}=${session}; other=1`)).toBe(true);
    expect(guard.hasSession(`a=1; ${guard.cookieName}=${session}`)).toBe(true);
    expect(guard.hasSession(`${guard.cookieName}=forged; ${guard.cookieName}=${session}`)).toBe(
      true,
    );
    expect(guard.hasSession(`${guard.cookieName}=${session}; ${guard.cookieName}=forged`)).toBe(
      false,
    );
    expect(guard.hasSession(`${guard.cookieName}x=${session}`)).toBe(false);
  });

  it("a session receives the csrf token", async () => {
    const { client } = await start();
    const response = await client.raw("GET", "/api/v1/session");
    expect(JSON.parse(response.body).csrfToken).toBe(client.csrf);
  });
});

describe("requests", () => {
  it("a valid POST goes through and runs the action", async () => {
    const { client, rig } = await start();
    const response = await client.call(
      "POST",
      "/sandboxes/microvm-00000000-0000-0000-0000-000000000001/kill",
      {
        confirm: true,
      },
    );
    expect(response.status).toBe(202);
    await rig.waitForJob((response.body.job as { jobId: string }).jobId);
    expect(rig.writes()).toEqual(["terminateMicrovm"]);
  });

  it("is read-only by default over HTTP", async () => {
    const { client, rig } = await start({ allowActions: false, sandboxes: [{ n: 1 }] });
    const response = await client.raw("POST", KILL, {
      headers: { ...JSON_HEADERS, [CSRF_HEADER]: client.csrf },
      body: '{"confirm": true}',
    });
    expect(response.status).toBe(403);
    expect(JSON.parse(response.body).error.code).toBe("actions_disabled");
    expect(rig.plane.calls).toEqual([]);
  });

  it("an oversized body is refused without reading it", async () => {
    const { client } = await start();
    const response = await client.raw("POST", "/api/v1/actions", {
      headers: {
        ...JSON_HEADERS,
        [CSRF_HEADER]: client.csrf,
        "Content-Length": String(DASHBOARD_REQUEST_MAX_BODY_BYTES + 1),
      },
    });
    expect(response.status).toBe(413);
    expect(JSON.parse(response.body).error.code).toBe("payload_too_large");
  });

  it("a POST without a numeric Content-Length is refused", async () => {
    const { client } = await start();
    const response = await client.raw("POST", "/api/v1/actions", {
      headers: { ...JSON_HEADERS, [CSRF_HEADER]: client.csrf, "Content-Length": "abc" },
    });
    expect([400, 413]).toContain(response.status);
  });

  it.each(["PUT", "DELETE", "PATCH"])("%s is not an allowed method", async (method) => {
    const { client } = await start();
    const response = await client.raw(method, "/api/v1/meta");
    expect(response.status).toBe(405);
    expect(JSON.parse(response.body).error.code).toBe("method_not_allowed");
  });

  it("a body on a method without one closes the connection so it is not a second request", async () => {
    const { client } = await start();
    const smuggled = `GET /api/v1/meta HTTP/1.1\r\nHost: ${client.host}\r\nCookie: ${client.cookie}\r\n\r\n`;
    const request =
      `PUT /api/v1/meta HTTP/1.1\r\nHost: ${client.host}\r\nCookie: ${client.cookie}\r\n` +
      `Content-Length: ${smuggled.length}\r\n\r\n${smuggled}`;
    const received = await new Promise<string>((resolve, reject) => {
      const socket = createConnection({ host: LOOPBACK, port: client.port }, () => {
        socket.write(request);
      });
      let text = "";
      socket.on("data", (chunk) => {
        text += chunk.toString("latin1");
      });
      socket.on("end", () => resolve(text));
      socket.on("error", reject);
    });
    expect(received.split("HTTP/1.1 ").length - 1).toBe(1);
    expect(received.split("\r\n")[0]).toContain("405");
  });
});

describe("static files", () => {
  it("serves the SPA with the shared headers", async () => {
    const { client } = await start();
    const response = await client.raw("GET", "/");
    expect(response.status).toBe(200);
    expect(response.body).toContain("<rayito-app>");
    for (const [name, value] of Object.entries(SECURITY_HEADERS)) {
      expect(response.headers[name.toLowerCase()]).toBe(value);
    }
    expect(response.headers["content-security-policy"]).not.toContain("unsafe");
  });

  it("every static file has its content type and unknown paths are 404", async () => {
    const { client } = await start();
    const files = listFiles(dashboardStaticDirectory());
    expect(files).toEqual(
      expect.arrayContaining(["/index.html", "/js/app.js", "/css/app.css", "/css/tokens.css"]),
    );
    for (const path of files) {
      const response = await client.raw("GET", path);
      expect(response.status, path).toBe(200);
    }
    for (const path of [
      "/nope.js",
      "/../../etc/passwd",
      "/js/",
      "/%2e%2e/secret",
      "/_static/index.html",
    ]) {
      expect((await client.raw("GET", path)).status, path).toBe(404);
    }
  });

  it("theme.css is always served and empty without a user theme", async () => {
    const { client } = await start();
    const response = await client.raw("GET", "/theme.css");
    expect(response.status).toBe(200);
    expect(response.body).toBe("");
    expect(response.headers["content-type"]).toContain("text/css");
    expect((await client.raw("GET", "/theme/logo")).status).toBe(404);
    expect((await client.call("GET", "/meta")).body.theme).toEqual({ name: "rayito", logo: false });
  });
});

function listFiles(directory: string, prefix = ""): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) =>
    entry.isDirectory()
      ? listFiles(join(directory, entry.name), `${prefix}${entry.name}/`)
      : [`/${prefix}${entry.name}`],
  );
}

describe("nothing is logged or printed", () => {
  it("writes nothing to stdout or stderr and the logger stays silent", async () => {
    const out = vi.spyOn(process.stdout, "write").mockImplementation(() => true);
    const err = vi.spyOn(process.stderr, "write").mockImplementation(() => true);
    const { client } = await start();
    const token = client.server.guard.csrfToken;
    await client.call("GET", "/meta");
    await client.call("POST", "/sandboxes/microvm-00000000-0000-0000-0000-000000000001/kill", {
      confirm: true,
    });
    await client.raw("GET", "/?token=not-valid", { sign: false });
    const written = [...out.mock.calls, ...err.mock.calls].map((call) => String(call[0])).join("");
    expect(written).toBe("");
    expect(written).not.toContain(token);
  });

  it("the SPA files in the package are the canonical ones", () => {
    const html = readFileSync(join(dashboardStaticDirectory(), "index.html"), "utf8");
    expect(html.indexOf("/css/tokens.css")).toBeLessThan(html.indexOf("/theme.css"));
  });
});
