/**
 * El ciclo de una invocación (`runCli`): opciones globales, ayuda, versión,
 * errores de uso (código 2), traducción de fallos y que nada toca AWS antes
 * de que un comando lo pida.
 */

import { describe, expect, it } from "vitest";
import { AbortedError } from "../../../src/cli/console.js";
import { translateFailure, UsageError } from "../../../src/cli/failures.js";
import { versionLine } from "../../../src/cli/run.js";
import {
  AuthenticationError,
  CapacityError,
  QuotaExceededError,
  SandboxError,
  UnimplementedError,
} from "../../../src/errors.js";
import { buildWorld } from "./case-world.js";
import { runWithServices } from "./run-case.js";

const world = buildWorld({
  sandboxes: [
    {
      id: "microvm-a",
      state: "RUNNING",
      image: "rayito-base",
      version: "1.0",
      startedAt: "2026-09-15T14:39:02Z",
    },
  ],
});

function run(args: string[], stdin?: string) {
  return runWithServices({ args, services: world.services, stdin });
}

describe("ayuda y versión", () => {
  it("--version dice la versión y el runtime", async () => {
    const outcome = await run(["--version"]);
    expect(outcome.exitCode).toBe(0);
    expect(outcome.stdout.trim()).toBe(versionLine());
    expect(versionLine()).toMatch(/^rayito \d+\.\d+\.\d+\S* \(node\)$/);
  });

  it("sin argumentos muestra la ayuda y sale con código de uso", async () => {
    const outcome = await run([]);
    expect(outcome.exitCode).toBe(2);
    expect(outcome.stdout).toContain("sandbox");
    expect(outcome.stdout).toContain("stack");
    expect(outcome.stdout).toContain("image");
  });

  it("--help sale con 0", async () => {
    const outcome = await run(["--help"]);
    expect(outcome.exitCode).toBe(0);
    expect(outcome.stdout).toContain("--profile");
  });

  it("un grupo sin subcomando muestra sus comandos con código de uso", async () => {
    const outcome = await run(["sandbox"]);
    expect(outcome.exitCode).toBe(2);
    expect(outcome.stdout).toContain("list");
    expect(outcome.stdout).toContain("kill");
  });

  it("la ayuda de un comando lista sus opciones y argumentos", async () => {
    const outcome = await run(["sandbox", "kill", "--help"]);
    expect(outcome.exitCode).toBe(0);
    expect(outcome.stdout).toContain("--all");
    expect(outcome.stdout).toContain("--yes, -y");
    expect(outcome.stdout).toContain("ID");
  });
});

describe("errores de uso salen con 2", () => {
  it.each([
    [["bogus"], "comando desconocido: bogus"],
    [["sandbox", "bogus"], "comando desconocido: sandbox bogus"],
    [["sandbox", "list", "--bogus"], "opción desconocida: --bogus"],
    [["--bogus", "sandbox", "list"], "opción desconocida: --bogus"],
    [["--profile"], "la opción --profile necesita un valor"],
    [["sandbox", "logs"], "falta el argumento ID"],
    [["sandbox", "info", "a", "b"], "argumentos sobrantes: b"],
    [["sandbox", "logs", "id", "--limit", "x"], "se esperaba un entero"],
    [["sandbox", "exec", "id", "--timeout", "abc", "--", "ls"], "se esperaba un número"],
    [["sandbox", "exec", "id"], "falta el argumento CMD"],
    [["sandbox", "list", "--template"], "la opción --template necesita un valor"],
  ])("rayito %j", async (args, message) => {
    const outcome = await run(args);
    expect(outcome.exitCode).toBe(2);
    expect(outcome.stderr).toContain(message);
  });

  it("acepta la opción con = y las globales antes del comando", async () => {
    const outcome = await run([
      "--json",
      "--region=us-east-1",
      "sandbox",
      "list",
      "--template=rayito-base",
    ]);
    expect(outcome.exitCode).toBe(0);
    expect(JSON.parse(outcome.stdout)).toHaveLength(1);
  });

  it("las opciones globales no valen después del subcomando", async () => {
    const outcome = await run(["sandbox", "list", "--json"]);
    expect(outcome.exitCode).toBe(2);
  });

  it("lo que sigue a -- es posicional aunque empiece por guion", async () => {
    const outcome = await run(["sandbox", "kill", "--", "-weird-id"]);
    expect(outcome.stdout).toContain("-weird-id");
  });
});

describe("nada toca AWS antes de que un comando lo pida", () => {
  it("la ayuda, la versión y stack list no construyen la sesión", async () => {
    let built = 0;
    const { runCli } = await import("../../../src/cli/run.js");
    const { FakeTerminal } = await import("./terminal.js");
    for (const args of [
      ["--help"],
      ["--version"],
      ["stack", "list"],
      ["sandbox", "list", "--bogus"],
    ]) {
      await runCli(args, {
        terminal: new FakeTerminal(),
        env: {},
        now: () => new Date(),
        sleep: async () => undefined,
        signal: new AbortController().signal,
        createServices: async () => {
          built += 1;
          return world.services;
        },
      });
    }
    expect(built).toBe(0);
  });

  it("un comando que usa AWS recibe el perfil, la región y el logger de --verbose", async () => {
    const { runCli } = await import("../../../src/cli/run.js");
    const { FakeTerminal } = await import("./terminal.js");
    const received: Array<{ profile?: string; region?: string; verbose: boolean }> = [];
    await runCli(["--profile", "dev", "--region", "eu-west-1", "--verbose", "sandbox", "list"], {
      terminal: new FakeTerminal(),
      env: {},
      now: () => new Date(),
      sleep: async () => undefined,
      signal: new AbortController().signal,
      createServices: async (settings) => {
        received.push({
          ...(settings.profile === undefined ? {} : { profile: settings.profile }),
          ...(settings.region === undefined ? {} : { region: settings.region }),
          verbose: settings.logger?.info !== undefined,
        });
        return world.services;
      },
    });
    expect(received).toEqual([{ profile: "dev", region: "eu-west-1", verbose: true }]);
  });
});

describe("translateFailure", () => {
  it("el uso y el entorno son 2", () => {
    expect(translateFailure(new UsageError("sin región"))).toEqual({
      line: "rayito: sin región",
      code: 2,
    });
  });

  it("las credenciales ausentes del SDK son un error de entorno", () => {
    const error = new Error("no hay");
    error.name = "CredentialsProviderError";
    const failure = translateFailure(error);
    expect(failure.code).toBe(2);
    expect(failure.line).toContain("sin credenciales de AWS");
  });

  it.each([
    new SandboxError("el sandbox falló"),
    new UnimplementedError("esto", "aún no"),
    new AuthenticationError("sin permiso"),
    new QuotaExceededError("cuota"),
    new CapacityError("sin capacidad"),
  ])("los errores del SDK salen con 1 y su mensaje: %s", (error) => {
    const failure = translateFailure(error);
    expect(failure.code).toBe(1);
    expect(failure.line).toBe(`rayito: ${error.message}`);
  });

  it("un error crudo de AWS sale como AWS error <código> y sin secretos", () => {
    const keyId = ["AKIA", "ABCDEFGHIJKLMNOP"].join("");
    const error = new Error(`Credential=${keyId}/20260915/us-east-1`);
    error.name = "AccessDeniedException";
    Object.defineProperty(error, "$metadata", { value: {} });
    const failure = translateFailure(error);
    expect(failure.code).toBe(1);
    expect(failure.line).toMatch(/^rayito: AWS error AccessDeniedException: /);
    expect(failure.line).not.toContain(keyId);
  });

  it("abortar una confirmación es Aborted. con 1", () => {
    expect(translateFailure(new AbortedError())).toEqual({ line: "Aborted.", code: 1 });
  });

  it("un error ajeno no enseña su mensaje, y con --verbose su pila", () => {
    const error = new TypeError("detalle interno");
    expect(translateFailure(error).line).toBe("rayito: error inesperado (TypeError)");
    expect(translateFailure(error, true).line).toContain("TypeError: detalle interno");
  });
});
