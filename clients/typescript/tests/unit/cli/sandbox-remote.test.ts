/**
 * `rayito sandbox create | exec | metrics` y las partes de `list`/`info`
 * que no entran en los casos compartidos con Python: hablan con el agente
 * del sandbox, así que usan un `RemoteSandbox` falso. Cubren el token (nunca
 * por argv ni impreso), los códigos de salida del comando remoto, el
 * streaming sin filtrar y `--follow`.
 */

import { mkdtempSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import type {
  CliServices,
  RemoteSandbox,
  SandboxCreateRequest,
  SandboxGateway,
  SandboxListQuery,
} from "../../../src/cli/services.js";
import { CommandExitError, SandboxError, TimeoutError } from "../../../src/errors.js";
import type { SandboxInfo, SandboxListItem, SandboxMetrics } from "../../../src/models.js";
import { sandboxInfo } from "../../../src/models.js";
import type { CommandOptions } from "../../../src/sandbox/commands.js";
import { buildWorld } from "./case-world.js";
import { FakePty } from "./interactive-fakes.js";
import { runWithServices } from "./run-case.js";

const TOKEN = "token-secreto-de-prueba";
const SANDBOX_ID = "microvm-a";
const STARTED = new Date("2026-09-15T14:39:02Z");

function info(): SandboxInfo {
  return sandboxInfo({
    sandboxId: SANDBOX_ID,
    state: "RUNNING",
    endpoint: "abc.lambda-microvm.us-east-1.on.aws",
    template: "arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base",
    templateVersion: "1.0",
    startedAt: STARTED,
    maximumDurationSeconds: 3600,
  });
}

const METRICS: SandboxMetrics = {
  cpuUsedPct: 12.5,
  memUsedBytes: 1,
  memTotalBytes: 2,
  diskUsedBytes: 3,
  diskTotalBytes: 4,
  cpuCount: 2,
  timestamp: new Date("2026-09-15T16:02:07Z"),
  memCacheBytes: 0,
};

class FakeRemote implements RemoteSandbox {
  readonly sandboxId = SANDBOX_ID;
  readonly info = info();
  closed = 0;
  killed = 0;
  readonly commandsRun: Array<{ cmd: string; options: CommandOptions }> = [];
  runOutcome: "ok" | Error = "ok";
  stdout = "";
  stderr = "";
  pid = 4242;
  metricsReads = 0;
  metrics: SandboxMetrics = METRICS;
  metricsFailure: Error | undefined;
  readonly pty = new FakePty();
  readonly commands = {
    run: async (cmd: string, options: CommandOptions) => {
      this.commandsRun.push({ cmd, options });
      if (options.background === true) {
        return { pid: this.pid } as never;
      }
      options.onStdout?.(this.stdout);
      options.onStderr?.(this.stderr);
      if (this.runOutcome !== "ok") {
        throw this.runOutcome;
      }
      return { exitCode: 0 } as never;
    },
  };

  async getMetrics(): Promise<SandboxMetrics> {
    this.metricsReads += 1;
    if (this.metricsFailure !== undefined) {
      throw this.metricsFailure;
    }
    return this.metrics;
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
  readonly lists: SandboxListQuery[] = [];
  readonly infos: Array<{ id: string; readMetadata: boolean }> = [];

  async *list(query: SandboxListQuery): AsyncIterable<SandboxListItem> {
    this.lists.push(query);
    yield* [];
  }

  async info(id: string, options: { readonly readMetadata: boolean }): Promise<SandboxInfo> {
    this.infos.push({ id, readMetadata: options.readMetadata });
    return info();
  }

  async kill(): Promise<boolean> {
    return true;
  }

  async connect(id: string, token: string): Promise<RemoteSandbox> {
    this.connects.push({ id, token });
    return this.remote;
  }

  async create(request: SandboxCreateRequest): Promise<RemoteSandbox> {
    this.creates.push(request);
    return this.remote;
  }
}

function setup(): { gateway: FakeGateway; services: CliServices } {
  const gateway = new FakeGateway();
  return { gateway, services: { ...buildWorld({}).services, sandboxes: gateway } };
}

function scratchFile(name: string, content?: string): string {
  const path = join(mkdtempSync(join(tmpdir(), "rayito-cli-")), name);
  if (content !== undefined) {
    writeFileSync(path, content);
  }
  return path;
}

describe("sandbox exec", () => {
  it("ejecuta shlex.join(CMD) con el token del entorno y sale con 0", async () => {
    const { gateway, services } = setup();
    gateway.remote.stdout = "hola\n";
    gateway.remote.stderr = "aviso\n";
    const outcome = await runWithServices({
      args: ["sandbox", "exec", SANDBOX_ID, "--", "echo", "hola mundo"],
      services,
      env: { RAYITO_ACCESS_TOKEN: TOKEN },
    });
    expect(outcome.exitCode).toBe(0);
    expect(outcome.stdout).toBe("hola\n");
    expect(outcome.stderr).toBe("aviso\n");
    expect(gateway.remote.commandsRun[0]?.cmd).toBe("echo 'hola mundo'");
    expect(gateway.connects).toEqual([{ id: SANDBOX_ID, token: TOKEN }]);
    expect(gateway.remote.closed).toBe(1);
  });

  it("--token-file gana a la variable de entorno y el token no se imprime", async () => {
    const { gateway, services } = setup();
    const file = scratchFile("token", `${TOKEN}-archivo\n`);
    const outcome = await runWithServices({
      args: ["sandbox", "exec", SANDBOX_ID, "--token-file", file, "--", "true"],
      services,
      env: { RAYITO_ACCESS_TOKEN: "otro" },
    });
    expect(gateway.connects[0]?.token).toBe(`${TOKEN}-archivo`);
    expect(outcome.stdout + outcome.stderr).not.toContain(TOKEN);
  });

  it("sin token es un error de uso y no conecta", async () => {
    const { gateway, services } = setup();
    const outcome = await runWithServices({
      args: ["sandbox", "exec", SANDBOX_ID, "--", "true"],
      services,
    });
    expect(outcome.exitCode).toBe(2);
    expect(outcome.stderr).toContain("falta el access token: --token-file o RAYITO_ACCESS_TOKEN");
    expect(gateway.connects).toEqual([]);
  });

  it("un fichero de token ilegible es un error de uso que no nombra su contenido", async () => {
    const { services } = setup();
    const outcome = await runWithServices({
      args: ["sandbox", "exec", SANDBOX_ID, "--token-file", "/no/existe", "--", "true"],
      services,
    });
    expect(outcome.exitCode).toBe(2);
    expect(outcome.stderr).toContain("no se pudo leer el fichero de token /no/existe: ENOENT");
  });

  it("sale con el código del comando remoto", async () => {
    const { gateway, services } = setup();
    gateway.remote.runOutcome = new CommandExitError("falló", {
      exitCode: 7,
      stdout: "",
      stderr: "",
    });
    const outcome = await runWithServices({
      args: ["sandbox", "exec", SANDBOX_ID, "--", "false"],
      services,
      env: { RAYITO_ACCESS_TOKEN: TOKEN },
    });
    expect(outcome.exitCode).toBe(7);
    expect(gateway.remote.closed).toBe(1);
  });

  it("un plazo vencido sale con 124 y lo dice por stderr", async () => {
    const { gateway, services } = setup();
    gateway.remote.runOutcome = new TimeoutError("plazo");
    const outcome = await runWithServices({
      args: ["sandbox", "exec", SANDBOX_ID, "--timeout", "2.5", "--", "sleep", "9"],
      services,
      env: { RAYITO_ACCESS_TOKEN: TOKEN },
    });
    expect(outcome.exitCode).toBe(124);
    expect(outcome.stderr).toContain("timeout del comando");
    expect(gateway.remote.commandsRun[0]?.options.timeoutMs).toBe(2500);
  });

  it("--timeout 0 pide un comando sin plazo de servidor y no guarda la salida", async () => {
    const { gateway, services } = setup();
    await runWithServices({
      args: ["sandbox", "exec", SANDBOX_ID, "--", "true"],
      services,
      env: { RAYITO_ACCESS_TOKEN: TOKEN },
    });
    const options = gateway.remote.commandsRun[0]?.options;
    expect(options?.timeoutMs).toBe(0);
    expect(options?.maxOutputBytes).toBe(0);
  });

  it("--background imprime el pid y no espera", async () => {
    const { gateway, services } = setup();
    const outcome = await runWithServices({
      args: ["sandbox", "exec", SANDBOX_ID, "--background", "--", "sleep", "60"],
      services,
      env: { RAYITO_ACCESS_TOKEN: TOKEN },
    });
    expect(outcome.exitCode).toBe(0);
    expect(outcome.stdout).toBe("4242\n");
    expect(gateway.remote.commandsRun[0]?.options.background).toBe(true);
  });

  it("pasa --env, --user y --cwd; un --env mal formado es un error de uso", async () => {
    const { gateway, services } = setup();
    await runWithServices({
      args: [
        "sandbox",
        "exec",
        SANDBOX_ID,
        "-e",
        "A=1",
        "-e",
        "B=x=y",
        "-u",
        "root",
        "-c",
        "/srv",
        "--",
        "env",
      ],
      services,
      env: { RAYITO_ACCESS_TOKEN: TOKEN },
    });
    const options = gateway.remote.commandsRun[0]?.options;
    expect(options?.envs).toEqual({ A: "1", B: "x=y" });
    expect(options?.user).toBe("root");
    expect(options?.cwd).toBe("/srv");
    const bad = await runWithServices({
      args: ["sandbox", "exec", SANDBOX_ID, "--env", "sinvalor", "--", "env"],
      services,
      env: { RAYITO_ACCESS_TOKEN: TOKEN },
    });
    expect(bad.exitCode).toBe(2);
    expect(bad.stderr).not.toContain("sinvalor");
  });

  it("la salida del sandbox no se filtra: llega tal cual, también con controles", async () => {
    const { gateway, services } = setup();
    gateway.remote.stdout = "\u001b[31mrojo\u001b[0m";
    const outcome = await runWithServices({
      args: ["sandbox", "exec", SANDBOX_ID, "--", "ls"],
      services,
      env: { RAYITO_ACCESS_TOKEN: TOKEN },
    });
    expect(outcome.stdout).toBe("\u001b[31mrojo\u001b[0m");
  });
});

describe("sandbox metrics", () => {
  it("imprime una muestra en JSON", async () => {
    const { gateway, services } = setup();
    const outcome = await runWithServices({
      args: ["--json", "sandbox", "metrics", SANDBOX_ID],
      services,
      env: { RAYITO_ACCESS_TOKEN: TOKEN },
    });
    expect(outcome.exitCode).toBe(0);
    expect(JSON.parse(outcome.stdout)).toMatchObject({ cpu_used_pct: 12.5, cpu_count: 2 });
    expect(gateway.remote.metricsReads).toBe(1);
    expect(gateway.remote.closed).toBe(1);
  });

  it("un uso de CPU entero lleva su parte decimal, como el float de Python", async () => {
    const { gateway, services } = setup();
    gateway.remote.metrics = { ...METRICS, cpuUsedPct: 0 };
    const env = { RAYITO_ACCESS_TOKEN: TOKEN };
    const json = await runWithServices({
      args: ["--json", "sandbox", "metrics", SANDBOX_ID],
      services,
      env,
    });
    expect(json.stdout).toContain('"cpu_used_pct": 0.0,');
    const table = await runWithServices({
      args: ["sandbox", "metrics", SANDBOX_ID],
      services,
      env,
    });
    expect(table.stdout.split("\n")[1]).toMatch(/Z\s+0\.0\s+2\s/);
  });

  it("--follow repite hasta que se cancela, una línea JSON por muestra", async () => {
    const { gateway, services } = setup();
    const interrupt = new AbortController();
    let sleeps = 0;
    const outcome = await runWithServices({
      args: ["--json", "sandbox", "metrics", SANDBOX_ID, "--follow", "--interval", "0.5"],
      services,
      env: { RAYITO_ACCESS_TOKEN: TOKEN },
      signal: interrupt.signal,
      sleep: async (ms) => {
        expect(ms).toBe(500);
        sleeps += 1;
        if (sleeps === 2) {
          interrupt.abort();
        }
      },
    });
    const lines = outcome.stdout.trim().split("\n");
    expect(lines).toHaveLength(2);
    expect(sleeps).toBe(2);
    expect(lines.every((line) => JSON.parse(line).cpu_count === 2)).toBe(true);
    expect(gateway.remote.closed).toBe(1);
  });

  it("si el sandbox ya no existe lo dice en vez del error del agente", async () => {
    const { gateway } = setup();
    gateway.remote.metricsFailure = new SandboxError("el agente no responde");
    const outcome = await runWithServices({
      args: ["sandbox", "metrics", SANDBOX_ID],
      services: { ...buildWorld({}).services, sandboxes: gateway },
      env: { RAYITO_ACCESS_TOKEN: TOKEN },
    });
    expect(outcome.exitCode).toBe(1);
    expect(outcome.stderr).toContain("el sandbox ya no existe");
    expect(gateway.remote.closed).toBe(1);
  });

  it("sin token es un error de uso", async () => {
    const { services } = setup();
    const outcome = await runWithServices({ args: ["sandbox", "metrics", SANDBOX_ID], services });
    expect(outcome.exitCode).toBe(2);
  });
});

describe("sandbox create", () => {
  it("--detach crea con el token del entorno, imprime el id y cierra el handle", async () => {
    const { gateway, services } = setup();
    const outcome = await runWithServices({
      args: [
        "sandbox",
        "create",
        "rayito-base",
        "--detach",
        "--metadata",
        "env=ci",
        "-e",
        "A=1",
        "--timeout",
        "600",
      ],
      services,
      env: { RAYITO_ACCESS_TOKEN: TOKEN },
    });
    expect(outcome.exitCode).toBe(0);
    expect(outcome.stdout).toBe(`${SANDBOX_ID}\n`);
    expect(gateway.creates).toEqual([
      {
        template: "rayito-base",
        timeoutMs: 600_000,
        metadata: { env: "ci" },
        envs: { A: "1" },
        accessToken: TOKEN,
      },
    ]);
    expect(gateway.remote.closed).toBe(1);
    expect(outcome.stdout + outcome.stderr).not.toContain(TOKEN);
  });

  it("--json imprime el documento de lanzamiento", async () => {
    const { services } = setup();
    const outcome = await runWithServices({
      args: ["--json", "sandbox", "create", "--detach"],
      services,
      env: { RAYITO_ACCESS_TOKEN: TOKEN },
    });
    expect(JSON.parse(outcome.stdout)).toEqual({
      sandbox_id: SANDBOX_ID,
      endpoint: "abc.lambda-microvm.us-east-1.on.aws",
      template: "rayito-base",
      template_version: "1.0",
      expires_at: "2026-09-15T15:39:02Z",
    });
  });

  it("el plazo por defecto es el del SDK", async () => {
    const { gateway, services } = setup();
    await runWithServices({
      args: ["sandbox", "create", "--detach"],
      services,
      env: { RAYITO_ACCESS_TOKEN: TOKEN },
    });
    expect(gateway.creates[0]?.timeoutMs).toBe(3_600_000);
    expect(gateway.creates[0]?.template).toBeUndefined();
  });

  it("sin token --detach exige --token-file y no crea nada", async () => {
    const { gateway, services } = setup();
    const outcome = await runWithServices({
      args: ["sandbox", "create", "--detach"],
      services,
    });
    expect(outcome.exitCode).toBe(2);
    expect(outcome.stderr).toContain("--detach necesita --token-file");
    expect(gateway.creates).toEqual([]);
  });

  it("--detach --token-file guarda un token nuevo con modo 0600 antes de crear", async () => {
    const { gateway, services } = setup();
    const file = scratchFile("token");
    const outcome = await runWithServices({
      args: ["sandbox", "create", "--detach", "--token-file", file],
      services,
    });
    expect(outcome.exitCode).toBe(0);
    const stored = readFileSync(file, "utf8");
    expect(stored.length).toBeGreaterThan(20);
    expect(gateway.creates[0]?.accessToken).toBe(stored);
    expect(statSync(file).mode & 0o777).toBe(0o600);
    expect(outcome.stdout + outcome.stderr).not.toContain(stored);
  });

  it("un fichero de token que ya existe no se pisa y no crea nada", async () => {
    const { gateway, services } = setup();
    const file = scratchFile("token", "previo");
    const outcome = await runWithServices({
      args: ["sandbox", "create", "--detach", "--token-file", file],
      services,
    });
    expect(outcome.exitCode).toBe(2);
    expect(outcome.stderr).toContain("el fichero de token ya existe");
    expect(readFileSync(file, "utf8")).toBe("previo");
    expect(gateway.creates).toEqual([]);
  });
});

describe("sandbox list e info reenvían los filtros al SDK", () => {
  it("--metadata y --index-table viajan como consulta", async () => {
    const { gateway, services } = setup();
    await runWithServices({
      args: [
        "--json",
        "sandbox",
        "list",
        "--metadata",
        "user=42",
        "--index-table",
        "tabla",
        "--state",
        "suspended",
      ],
      services,
    });
    expect(gateway.lists).toEqual([
      {
        template: undefined,
        templateVersion: undefined,
        states: ["SUSPENDED"],
        metadata: { user: "42" },
        indexTable: "tabla",
      },
    ]);
  });

  it("--all-states pide todos los estados", async () => {
    const { gateway, services } = setup();
    await runWithServices({ args: ["--json", "sandbox", "list", "--all-states"], services });
    expect(gateway.lists[0]?.states).toEqual([
      "PENDING",
      "RUNNING",
      "SUSPENDING",
      "SUSPENDED",
      "TERMINATING",
      "TERMINATED",
    ]);
  });

  it("info sondea Health salvo con --no-metadata", async () => {
    const { gateway, services } = setup();
    await runWithServices({ args: ["--json", "sandbox", "info", SANDBOX_ID], services });
    await runWithServices({
      args: ["--json", "sandbox", "info", SANDBOX_ID, "--no-metadata"],
      services,
    });
    expect(gateway.infos).toEqual([
      { id: SANDBOX_ID, readMetadata: true },
      { id: SANDBOX_ID, readMetadata: false },
    ]);
  });
});
