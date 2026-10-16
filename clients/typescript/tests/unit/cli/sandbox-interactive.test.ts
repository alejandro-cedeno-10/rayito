/**
 * `rayito sandbox connect | create | proxy` a través de `runCli`, con un
 * `RemoteSandbox` falso (PTY en memoria) y un plano de control falso: el
 * token (nunca por argv ni impreso), el código de salida del shell, que
 * `create` termine el sandbox pase lo que pase y que `connect` no lo
 * termine, y las comprobaciones de `proxy` antes de tocar AWS. Espejo de
 * `test_sandbox_interactive.py` y de los tests de comando de `test_proxy.py`.
 */

import { describe, expect, it } from "vitest";
import type {
  CliServices,
  RemoteSandbox,
  SandboxCreateRequest,
  SandboxGateway,
} from "../../../src/cli/services.js";
import { CommandExitError } from "../../../src/errors.js";
import type { SandboxInfo, SandboxListItem } from "../../../src/models.js";
import { sandboxInfo } from "../../../src/models.js";
import { buildWorld } from "./case-world.js";
import { FakeInput, FakePty } from "./interactive-fakes.js";
import { freePort, MintingPlane, SANDBOX_ID } from "./proxy-harness.js";
import { runWithServices } from "./run-case.js";

const TOKEN = "token-secreto-de-prueba";
const SHELL_FAILURE_CODE = 5;

function info(): SandboxInfo {
  return sandboxInfo({
    sandboxId: SANDBOX_ID,
    state: "RUNNING",
    endpoint: "abc.lambda-microvm.us-east-1.on.aws",
    template: "arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base",
    templateVersion: "1.0",
    startedAt: new Date("2026-09-15T14:39:02Z"),
    maximumDurationSeconds: 3600,
  });
}

class FakeRemote {
  readonly sandboxId = SANDBOX_ID;
  readonly info = info();
  readonly pty = new FakePty();
  closed = 0;
  killed = 0;
  readonly commands = { run: async () => ({ exitCode: 0 }) as never };

  async getMetrics(): Promise<never> {
    throw new Error("no se usa");
  }

  close(): void {
    this.closed += 1;
  }

  async kill(): Promise<boolean> {
    this.killed += 1;
    return true;
  }
}

class FakeGateway implements SandboxGateway {
  readonly remote = new FakeRemote();
  readonly connects: Array<{ id: string; token: string }> = [];
  readonly creates: SandboxCreateRequest[] = [];

  async *list(): AsyncIterable<SandboxListItem> {
    yield* [];
  }

  async info(): Promise<SandboxInfo> {
    return info();
  }

  async kill(): Promise<boolean> {
    return true;
  }

  async connect(id: string, token: string): Promise<RemoteSandbox> {
    this.connects.push({ id, token });
    return this.remote as unknown as RemoteSandbox;
  }

  async create(request: SandboxCreateRequest): Promise<RemoteSandbox> {
    this.creates.push(request);
    return this.remote as unknown as RemoteSandbox;
  }
}

function setup(): { gateway: FakeGateway; services: CliServices } {
  const gateway = new FakeGateway();
  return { gateway, services: { ...buildWorld({}).services, sandboxes: gateway } };
}

describe("sandbox connect", () => {
  it("abre la terminal con el token del entorno, sale con 0 y no termina el sandbox", async () => {
    const { gateway, services } = setup();
    gateway.remote.pty.onCreate = (handle) => setTimeout(() => handle.finish(0), 5);
    const outcome = await runWithServices({
      args: ["sandbox", "connect", SANDBOX_ID, "--user", "root", "--cwd", "/work", "-e", "A=1"],
      services,
      env: { RAYITO_ACCESS_TOKEN: TOKEN },
      rawInput: new FakeInput(false),
    });
    expect(outcome.exitCode).toBe(0);
    expect(gateway.connects).toEqual([{ id: SANDBOX_ID, token: TOKEN }]);
    expect(gateway.remote.pty.created[0]).toMatchObject({
      user: "root",
      cwd: "/work",
      envs: { A: "1" },
      timeoutMs: 0,
    });
    expect(gateway.remote.closed).toBe(1);
    expect(gateway.remote.killed).toBe(0);
    expect(outcome.stdout + outcome.stderr).not.toContain(TOKEN);
  });

  it("sale con el código del shell", async () => {
    const { gateway, services } = setup();
    gateway.remote.pty.onCreate = (handle) =>
      setTimeout(
        () =>
          handle.fail(
            new CommandExitError("falló", { exitCode: SHELL_FAILURE_CODE, stdout: "", stderr: "" }),
          ),
        5,
      );
    const outcome = await runWithServices({
      args: ["sandbox", "connect", SANDBOX_ID],
      services,
      env: { RAYITO_ACCESS_TOKEN: TOKEN },
      rawInput: new FakeInput(false),
    });
    expect(outcome.exitCode).toBe(SHELL_FAILURE_CODE);
  });

  it("sin token es un error de uso y no conecta", async () => {
    const { gateway, services } = setup();
    const outcome = await runWithServices({
      args: ["sandbox", "connect", SANDBOX_ID],
      services,
      rawInput: new FakeInput(false),
    });
    expect(outcome.exitCode).toBe(2);
    expect(outcome.stderr).toContain("falta el access token: --token-file o RAYITO_ACCESS_TOKEN");
    expect(gateway.connects).toEqual([]);
  });

  it("un --env mal formado es un error de uso y no conecta", async () => {
    const { gateway, services } = setup();
    const outcome = await runWithServices({
      args: ["sandbox", "connect", SANDBOX_ID, "--env", "sin-igual"],
      services,
      env: { RAYITO_ACCESS_TOKEN: TOKEN },
      rawInput: new FakeInput(false),
    });
    expect(outcome.exitCode).toBe(2);
    expect(gateway.connects).toEqual([]);
  });

  it("sin entrada interactiva es un error de uso y no conecta", async () => {
    const { gateway, services } = setup();
    const outcome = await runWithServices({
      args: ["sandbox", "connect", SANDBOX_ID],
      services,
      env: { RAYITO_ACCESS_TOKEN: TOKEN },
    });
    expect(outcome.exitCode).toBe(2);
    expect(outcome.stderr).toContain("no admite una sesión interactiva");
    expect(gateway.connects).toEqual([]);
  });

  it("cierra el sandbox aunque la terminal falle", async () => {
    const { gateway, services } = setup();
    gateway.remote.pty.onCreate = (handle) => setTimeout(() => handle.fail(new Error("caído")), 5);
    const outcome = await runWithServices({
      args: ["sandbox", "connect", SANDBOX_ID],
      services,
      env: { RAYITO_ACCESS_TOKEN: TOKEN },
      rawInput: new FakeInput(false),
    });
    expect(outcome.exitCode).toBe(1);
    expect(gateway.remote.closed).toBe(1);
  });

  it("Ctrl-C corta la terminal de una tubería", async () => {
    const { gateway, services } = setup();
    const abort = new AbortController();
    gateway.remote.pty.onCreate = () => setTimeout(() => abort.abort(), 5);
    const outcome = await runWithServices({
      args: ["sandbox", "connect", SANDBOX_ID],
      services,
      env: { RAYITO_ACCESS_TOKEN: TOKEN },
      rawInput: new FakeInput(false),
      signal: abort.signal,
    });
    expect(gateway.remote.pty.handle.killed).toBe(1);
    expect(outcome.exitCode).not.toBe(0);
  });
});

describe("sandbox create sin --detach", () => {
  it("crea, abre la terminal y termina el sandbox al salir con el código del shell", async () => {
    const { gateway, services } = setup();
    gateway.remote.pty.onCreate = (handle) =>
      setTimeout(
        () =>
          handle.fail(
            new CommandExitError("falló", { exitCode: SHELL_FAILURE_CODE, stdout: "", stderr: "" }),
          ),
        5,
      );
    const outcome = await runWithServices({
      args: ["sandbox", "create", "--user", "root", "--cwd", "/work"],
      services,
      env: { RAYITO_ACCESS_TOKEN: TOKEN },
      rawInput: new FakeInput(false),
    });
    expect(outcome.exitCode).toBe(SHELL_FAILURE_CODE);
    expect(gateway.creates).toHaveLength(1);
    expect(gateway.creates[0]?.accessToken).toBe(TOKEN);
    expect(gateway.remote.pty.created[0]).toMatchObject({ user: "root", cwd: "/work" });
    expect(gateway.remote.killed).toBe(1);
    expect(gateway.remote.closed).toBe(1);
    expect(outcome.stdout + outcome.stderr).not.toContain(TOKEN);
  });

  it("termina el sandbox aunque la terminal falle", async () => {
    const { gateway, services } = setup();
    gateway.remote.pty.onCreate = (handle) => setTimeout(() => handle.fail(new Error("caído")), 5);
    const outcome = await runWithServices({
      args: ["sandbox", "create"],
      services,
      rawInput: new FakeInput(false),
    });
    expect(outcome.exitCode).toBe(1);
    expect(gateway.remote.killed).toBe(1);
    expect(gateway.remote.closed).toBe(1);
  });

  it("sin entrada interactiva no crea nada", async () => {
    const { gateway, services } = setup();
    const outcome = await runWithServices({ args: ["sandbox", "create"], services });
    expect(outcome.exitCode).toBe(2);
    expect(gateway.creates).toEqual([]);
  });
});

function proxyServices(plane: MintingPlane): { services: CliServices } {
  return { services: { ...buildWorld({}).services, controlPlane: plane } };
}

describe("sandbox proxy", () => {
  it("--bind fuera de loopback sin --allow-remote es un error de uso", async () => {
    const plane = new MintingPlane("RUNNING");
    const outcome = await runWithServices({
      args: ["sandbox", "proxy", SANDBOX_ID, "--port", "8080", "--bind", "0.0.0.0"],
      services: proxyServices(plane).services,
    });
    expect(outcome.exitCode).toBe(2);
    expect(outcome.stderr).toContain("allow-remote");
    expect(plane.lookups).toBe(0);
  });

  it("--bind comodín sin --allowed-host es un error de uso", async () => {
    const plane = new MintingPlane("RUNNING");
    const outcome = await runWithServices({
      args: [
        "sandbox",
        "proxy",
        SANDBOX_ID,
        "--port",
        "8080",
        "--bind",
        "0.0.0.0",
        "--allow-remote",
      ],
      services: proxyServices(plane).services,
    });
    expect(outcome.exitCode).toBe(2);
    expect(outcome.stderr).toContain("--allowed-host");
    expect(plane.lookups).toBe(0);
  });

  it("rechaza el puerto de los hooks sin tocar el plano de control", async () => {
    const plane = new MintingPlane("RUNNING");
    const outcome = await runWithServices({
      args: ["sandbox", "proxy", SANDBOX_ID, "--port", "9000"],
      services: proxyServices(plane).services,
    });
    expect(outcome.exitCode).toBe(1);
    expect(outcome.stderr).toContain("lifecycle hooks");
    expect(plane.lookups).toBe(0);
  });

  it("rechaza un --local-port fuera de rango sin tocar el plano de control", async () => {
    const plane = new MintingPlane("RUNNING");
    const outcome = await runWithServices({
      args: ["sandbox", "proxy", SANDBOX_ID, "--port", "8080", "--local-port", "65536"],
      services: proxyServices(plane).services,
    });
    expect(outcome.exitCode).toBe(1);
    expect(outcome.stderr).toContain("--local-port fuera de rango");
    expect(plane.lookups).toBe(0);
  });

  it("--port es obligatorio", async () => {
    const plane = new MintingPlane("RUNNING");
    const outcome = await runWithServices({
      args: ["sandbox", "proxy", SANDBOX_ID],
      services: proxyServices(plane).services,
    });
    expect(outcome.exitCode).toBe(2);
    expect(plane.lookups).toBe(0);
  });

  it("anuncia la URL local y la de <id>.localhost, y acuña un solo JWE de un puerto", async () => {
    const plane = new MintingPlane("RUNNING");
    const port = await freePort();
    const abort = new AbortController();
    abort.abort();
    const outcome = await runWithServices({
      args: ["sandbox", "proxy", SANDBOX_ID, "--port", "8080", "--local-port", String(port)],
      services: proxyServices(plane).services,
      signal: abort.signal,
    });
    expect(outcome.exitCode).toBe(0);
    expect(outcome.stdout).toContain(`http://127.0.0.1:${port} → ${SANDBOX_ID}:8080`);
    expect(outcome.stdout).toContain(`http://${SANDBOX_ID}.localhost:${port}`);
    expect(plane.minted).toHaveLength(1);
    expect(outcome.stdout + outcome.stderr).not.toContain("jwe-1");
  });

  it("un --bind remoto con --allow-remote avisa en stderr", async () => {
    const plane = new MintingPlane("RUNNING");
    const port = await freePort();
    const abort = new AbortController();
    abort.abort();
    const outcome = await runWithServices({
      args: [
        "sandbox",
        "proxy",
        SANDBOX_ID,
        "--port",
        "8080",
        "--local-port",
        String(port),
        "--bind",
        "0.0.0.0",
        "--allow-remote",
        "--allowed-host",
        "devbox.example.com",
      ],
      services: proxyServices(plane).services,
      signal: abort.signal,
    });
    expect(outcome.exitCode).toBe(0);
    expect(outcome.stderr).toContain("aviso: --bind 0.0.0.0 no es loopback");
  });

  it("un sandbox terminado sale con 1 y no acuña nada", async () => {
    const plane = new MintingPlane("TERMINATED");
    const port = await freePort();
    const outcome = await runWithServices({
      args: ["sandbox", "proxy", SANDBOX_ID, "--port", "8080", "--local-port", String(port)],
      services: proxyServices(plane).services,
    });
    expect(outcome.exitCode).toBe(1);
    expect(outcome.stderr).toContain("no se puede exponer con el proxy");
    expect(plane.minted).toHaveLength(0);
  });
});
