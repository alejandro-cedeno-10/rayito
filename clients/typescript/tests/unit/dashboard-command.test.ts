/**
 * `rayito dashboard` de punta a punta con servicios falsos: el mensaje de
 * arranque, el modo (sólo lectura o con acciones), el coste facturado, las
 * cuentas adicionales, el tema y el logo, `--open`, y los errores de uso con
 * código 2. El servidor es el real, en loopback y con un puerto libre; Ctrl-C
 * se simula cancelando la señal de la invocación. Espejo de
 * `cli/test_dashboard_command.py`.
 */

import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { AssumeRoleCommand, AssumeRoleCommandOutput } from "@aws-sdk/client-sts";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { ControlPlane } from "../../src/aws/control-plane.js";
import { runCli } from "../../src/cli/run.js";
import type { CliServices } from "../../src/cli/services.js";
import { DASHBOARD_LOGO_MAX_BYTES, DASHBOARD_THEME_MAX_BYTES } from "../../src/limits.js";
import { OptionalStacks } from "../../src/stacks/service.js";
import { FakeTerminal } from "./cli/terminal.js";
import { FakePlane, listItem } from "./fake/dashboard.js";
import { FakeStackProvisioner } from "./m15-fake-stacks.js";

const REGION = "us-east-1";
const ROLE_ARN = "arn:aws:iam::123456789012:role/rayito-dashboard-read";
const OTHER_ROLE_ARN = "arn:aws:iam::123456789012:role/rayito-dashboard-read-other";
const URL_LINE = 1;
const POLL_MS = 5;
const WAIT_MS = 5000;
const SESSION_PATH = "/api/v1/session";

const SDK_METADATA = "$metadata";

class FakeSts {
  readonly calls: Array<Record<string, unknown>> = [];

  async send(command: AssumeRoleCommand): Promise<AssumeRoleCommandOutput> {
    this.calls.push({ ...command.input });
    return {
      Credentials: {
        AccessKeyId: "AKIAEXAMPLE",
        SecretAccessKey: "secret",
        SessionToken: "token",
        Expiration: new Date("2099-01-01T00:00:00Z"),
      },
      [SDK_METADATA]: {},
    };
  }
}

function services(
  plane = new FakePlane([listItem(1), listItem(2)]),
  sts = new FakeSts(),
): CliServices {
  return {
    region: REGION,
    controlPlane: plane as unknown as ControlPlane,
    sandboxes: undefined as never,
    stacks: new OptionalStacks({ provisioner: new FakeStackProvisioner() }),
    images: undefined as never,
    imageArtifacts: undefined as never,
    logs: undefined as never,
    doctor: undefined as never,
    publishPorts: undefined as never,
    prunePorts: undefined as never,
    templates: undefined as never,
    lifecycleEvents: undefined as never,
    customDomain: undefined as never,
    sts,
  };
}

interface Running {
  readonly terminal: FakeTerminal;
  readonly done: Promise<number>;
  readonly interrupt: AbortController;
  readonly opened: string[];
}

function launch(
  args: readonly string[],
  options: {
    readonly env?: Record<string, string | undefined>;
    readonly services?: CliServices;
  } = {},
): Running {
  const terminal = new FakeTerminal();
  const interrupt = new AbortController();
  const opened: string[] = [];
  const done = runCli(args, {
    terminal,
    env: options.env ?? {},
    now: () => new Date(),
    sleep: async () => undefined,
    signal: interrupt.signal,
    createServices: async () => options.services ?? services(),
    openUrl: async (url) => {
      opened.push(url);
    },
  });
  return { terminal, done, interrupt, opened };
}

async function waitFor(check: () => boolean): Promise<void> {
  const deadline = Date.now() + WAIT_MS;
  while (!check()) {
    if (Date.now() > deadline) {
      throw new Error("el panel no arrancó");
    }
    await new Promise((resolve) => setTimeout(resolve, POLL_MS));
  }
}

async function started(running: Running): Promise<URL> {
  await waitFor(() => running.terminal.stdoutText.split("\n").length > URL_LINE + 1);
  return new URL(running.terminal.stdoutText.split("\n")[URL_LINE] as string);
}

async function stop(running: Running): Promise<number> {
  running.interrupt.abort();
  return running.done;
}

/** Canjea el token de arranque y devuelve un cliente con la cookie. */
async function signIn(url: URL): Promise<(path: string) => Promise<Response>> {
  const redeemed = await fetch(`${url.origin}${url.pathname}${url.search}`, { redirect: "manual" });
  const cookie = (redeemed.headers.getSetCookie()[0] as string).split(";")[0] as string;
  return (path) => fetch(`${url.origin}${path}`, { headers: { Cookie: cookie } });
}

async function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const probe = createServer();
    probe.listen(0, "127.0.0.1", () => {
      const port = (probe.address() as { port: number }).port;
      probe.close(() => resolve(port));
    });
    probe.on("error", reject);
  });
}

let directory = "";

beforeEach(() => {
  directory = mkdtempSync(join(tmpdir(), "rayito-dashboard-"));
});

afterEach(() => {
  rmSync(directory, { recursive: true, force: true });
});

describe("rayito dashboard", () => {
  it("prints the start URL once, serves read-only and closes with Ctrl-C", async () => {
    const running = launch(["dashboard"]);
    const url = await started(running);
    expect(running.terminal.stdoutText).toContain(
      `Panel de Rayito (${REGION}, sólo lectura). Ábrelo en tu navegador:`,
    );
    expect(url.hostname).toBe("127.0.0.1");
    expect(url.searchParams.get("token")).not.toBeNull();
    const get = await signIn(url);
    const meta = (await (await get("/api/v1/meta")).json()) as Record<string, unknown>;
    expect(meta).toMatchObject({ allowActions: false, deployment: "local", region: REGION });
    const sandboxes = (await (await get("/api/v1/sandboxes")).json()) as { sandboxes: unknown[] };
    expect(sandboxes.sandboxes).toHaveLength(2);
    expect(await stop(running)).toBe(0);
    expect(running.terminal.stdoutText).toContain(
      "Ctrl+C para cerrar. El enlace sirve una sola vez.",
    );
    expect(running.terminal.stdoutText).toContain("Panel cerrado.");
    expect(running.terminal.stderrText).toBe("");
  });

  it("makes no AWS call until the SPA asks for a view", async () => {
    const plane = new FakePlane([listItem(1)]);
    const running = launch(["dashboard", "--allow-actions", "--cost-explorer"], {
      services: services(plane),
    });
    const get = await signIn(await started(running));
    await get("/api/v1/meta");
    await get("/theme.css");
    expect(plane.calls).toEqual([]);
    await stop(running);
  });

  it("stops listening when it closes", async () => {
    const running = launch(["dashboard"]);
    const url = await started(running);
    await stop(running);
    await expect(fetch(`${url.origin}/`)).rejects.toThrow();
  });

  it("announces the actions, the paid cost and the account count", async () => {
    const running = launch([
      "dashboard",
      "--allow-actions",
      "--cost-explorer",
      "--account",
      `uno=${ROLE_ARN}`,
      "--account",
      `dos=${OTHER_ROLE_ARN}`,
    ]);
    await started(running);
    expect(running.terminal.stdoutText).toContain(
      `Panel de Rayito (${REGION}, con acciones (kill, suspend, resume), coste facturado activado ` +
        "(0.01 USD por consulta), más 2 cuentas de sólo lectura). Ábrelo en tu navegador:",
    );
    await stop(running);
  });

  it("reads the accounts from the environment, separated by spaces", async () => {
    const sts = new FakeSts();
    const running = launch(["dashboard", "--external-id", "ext-9"], {
      env: { RAYITO_DASHBOARD_ACCOUNTS: `uno=${ROLE_ARN}  dos=${OTHER_ROLE_ARN}` },
      services: services(new FakePlane([listItem(1)]), sts),
    });
    const url = await started(running);
    expect(running.terminal.stdoutText).toContain("más 2 cuentas de sólo lectura");
    const get = await signIn(url);
    const accounts = (await (await get("/api/v1/accounts")).json()) as unknown;
    expect(accounts).toEqual({ accounts: [{ name: "uno" }, { name: "dos" }] });
    expect(sts.calls).toEqual([]);
    const refused = await get("/api/v1/sandboxes?account=nope");
    expect(refused.status).toBe(400);
    await stop(running);
  });

  it("opens the browser only with --open", async () => {
    const plain = launch(["dashboard"]);
    await started(plain);
    await stop(plain);
    expect(plain.opened).toEqual([]);
    const withOpen = launch(["dashboard", "--open"]);
    const url = await started(withOpen);
    await waitFor(() => withOpen.opened.length > 0);
    expect(withOpen.opened).toEqual([url.toString()]);
    await stop(withOpen);
  });

  it("serves a user theme and logo", async () => {
    const theme = join(directory, "tema.json");
    const logo = join(directory, "logo.svg");
    writeFileSync(theme, '{"version": 1, "name": "demo", "dark": {"bg": "#101010"}}');
    writeFileSync(logo, '<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>');
    const running = launch(["dashboard", "--theme", theme, "--logo", logo]);
    const get = await signIn(await started(running));
    expect(await (await get("/theme.css")).text()).toContain("--bg: #101010;");
    expect((await get("/theme/logo")).headers.get("content-type")).toBe("image/svg+xml");
    expect(((await (await get("/api/v1/meta")).json()) as { theme: unknown }).theme).toEqual({
      name: "demo",
      logo: true,
    });
    await stop(running);
  });

  it("reads the theme and logo from the environment", async () => {
    const theme = join(directory, "tema.css");
    writeFileSync(theme, ":root { --radius: 2px; }");
    const running = launch(["dashboard"], { env: { RAYITO_DASHBOARD_THEME: theme } });
    const get = await signIn(await started(running));
    expect(await (await get("/theme.css")).text()).toContain("--radius: 2px;");
    await stop(running);
  });

  it("uses the port it is given", async () => {
    const port = await freePort();
    const running = launch(["dashboard", "--port", String(port)]);
    const url = await started(running);
    expect(url.port).toBe(String(port));
    await stop(running);
  });
});

async function usage(
  args: readonly string[],
  env: Record<string, string> = {},
): Promise<{ code: number; err: string }> {
  const running = launch(args, { env });
  const code = await running.done;
  expect(running.terminal.stdoutText).toBe("");
  return { code, err: running.terminal.stderrText };
}

describe("usage errors", () => {
  it.each(["70000", "-5"])("rejects the port %s", async (port) => {
    const outcome = await usage(["dashboard", `--port=${port}`]);
    expect(outcome.code).toBe(2);
    expect(outcome.err).toContain("--port: debe estar entre 1 y 65535");
  });

  it("rejects a busy port without echoing anything but the number", async () => {
    const first = launch(["dashboard"]);
    const url = await started(first);
    const outcome = await usage(["dashboard", "--port", url.port]);
    expect(outcome.code).toBe(2);
    expect(outcome.err).toContain(
      `no se pudo abrir el puerto ${url.port} en 127.0.0.1 (¿ocupado?)`,
    );
    await stop(first);
  });

  it("rejects a theme with another extension", async () => {
    const theme = join(directory, "tema.txt");
    writeFileSync(theme, "{}");
    const outcome = await usage(["dashboard", "--theme", theme]);
    expect(outcome.code).toBe(2);
    expect(outcome.err).toContain("--theme: la extensión debe ser .json o .css");
  });

  it("rejects an unreadable theme or logo without naming the path", async () => {
    const missing = join(directory, "falta.json");
    const theme = await usage(["dashboard", "--theme", missing]);
    expect(theme.err).toContain("--theme: no se pudo leer el fichero");
    expect(theme.err).not.toContain(directory);
    const logo = await usage(["dashboard", "--logo", join(directory, "falta.svg")]);
    expect(logo.err).toContain("--logo: no se pudo leer el fichero");
  });

  it("rejects a theme that is not UTF-8 text, or too big, or invalid", async () => {
    const binary = join(directory, "binario.json");
    writeFileSync(binary, Buffer.from([0xff, 0xfe, 0xfd]));
    expect((await usage(["dashboard", "--theme", binary])).err).toContain(
      "--theme: el fichero no es texto UTF-8",
    );
    const big = join(directory, "grande.json");
    writeFileSync(big, " ".repeat(DASHBOARD_THEME_MAX_BYTES + 1));
    expect((await usage(["dashboard", "--theme", big])).err).toContain(
      `tema: pesa más de ${DASHBOARD_THEME_MAX_BYTES} bytes`,
    );
    const invalid = join(directory, "malo.json");
    writeFileSync(invalid, '{"version": 1, "dark": {"bg": "url(https://example.com/x)"}}');
    const outcome = await usage(["dashboard", "--theme", invalid]);
    expect(outcome.code).toBe(2);
    expect(outcome.err).toContain("tema: valor no válido para bg (se espera color)");
    expect(outcome.err).not.toContain("example.com");
  });

  it("rejects an invalid or oversized logo", async () => {
    const html = join(directory, "logo.svg");
    writeFileSync(html, "<html/>");
    expect((await usage(["dashboard", "--logo", html])).err).toContain(
      "logo: no es un PNG ni un SVG válido",
    );
    const big = join(directory, "grande.png");
    writeFileSync(big, Buffer.alloc(DASHBOARD_LOGO_MAX_BYTES + 1, 1));
    expect((await usage(["dashboard", "--logo", big])).err).toContain(
      `logo: pesa más de ${DASHBOARD_LOGO_MAX_BYTES} bytes`,
    );
  });

  it("rejects a malformed account without echoing the value", async () => {
    const outcome = await usage(["dashboard", "--account", "staging=123456789012"]);
    expect(outcome.code).toBe(2);
    expect(outcome.err).toContain("--account: el valor tras «=» debe ser el ARN de un rol");
    expect(outcome.err).not.toContain("123456789012");
  });

  it("session endpoint stays behind the session", async () => {
    const running = launch(["dashboard"]);
    const url = await started(running);
    expect((await fetch(`${url.origin}${SESSION_PATH}`)).status).toBe(401);
    await stop(running);
  });
});
