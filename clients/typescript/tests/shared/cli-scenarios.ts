/**
 * Los escenarios de la CLI `rayito` de TypeScript que comparten los e2e
 * contra AWS real (`tests/e2e`) y los e2e locales contra Docker + Floci
 * (`tests/local`): un sandbox de punta a punta (`create --detach`, `info`,
 * `exec`, `metrics`, `list`, `kill`), lo de sólo lectura (`stack`, `image`) y
 * las sesiones interactivas (`create` sin `--detach` y `connect`). Cada
 * entorno aporta su `CliWorld`: de dónde salen los servicios, el entorno y la
 * imagen, y cómo se mata un sandbox que un test dejó vivo.
 *
 * El token del sandbox va a un fichero temporal y nunca a argv ni a la salida.
 */

import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterAll, describe, expect, test } from "vitest";
import { runCli } from "../../src/cli/run.js";
import type { ServicesFactory } from "../../src/cli/services.js";
import { SIZE_NAMES } from "../../src/sizing/sizing.js";
import { FakeInput } from "../unit/cli/interactive-fakes.js";
import { FakeTerminal } from "../unit/cli/terminal.js";

const EXEC_FAILURE_CODE = 7;
const EXEC_TIMEOUT_CODE = 124;
const EXEC_TIMEOUT_SECONDS = 2;
const SLEEP_SECONDS = 20;
const SHELL_EXIT_CODE = 3;
const INPUT_ATTACH_BUDGET_MS = 300_000;
const SESSION_END_BUDGET_MS = 300_000;
const KEYSTROKE_GAP_MS = 100;
const SESSION_TIMEOUT_SECONDS = 900;

/** Lo que cambia entre AWS real y el entorno local. */
export interface CliWorld {
  /** Nombre o ARN de la imagen que se pasa a `sandbox create`. */
  readonly template: string;
  readonly createServices: ServicesFactory;
  readonly env: Readonly<Record<string, string | undefined>>;
  /** Mata un sandbox que un test dejó vivo; no falla si ya no existe. */
  killQuietly(sandboxId: string): Promise<void>;
}

export interface Outcome {
  readonly exitCode: number;
  readonly stdout: string;
  readonly stderr: string;
}

export function startRayito(world: CliWorld, args: string[], input?: FakeInput) {
  const terminal = new FakeTerminal();
  terminal.stdin = input;
  const done = runCli(args, {
    terminal,
    env: { ...world.env },
    now: () => new Date(),
    sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
    signal: new AbortController().signal,
    createServices: world.createServices,
  }).then(
    (exitCode): Outcome => ({ exitCode, stdout: terminal.stdoutText, stderr: terminal.stderrText }),
  );
  return { done, terminal };
}

export function rayito(world: CliWorld, args: string[]): Promise<Outcome> {
  return startRayito(world, args).done;
}

function pause(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitUntilListening(input: FakeInput): Promise<void> {
  const deadline = Date.now() + INPUT_ATTACH_BUDGET_MS;
  while (input.listenerCount("data") === 0) {
    if (Date.now() > deadline) {
      throw new Error("la terminal no abrió la entrada a tiempo");
    }
    await pause(KEYSTROKE_GAP_MS);
  }
}

export async function typeLines(input: FakeInput, lines: readonly string[]): Promise<void> {
  await waitUntilListening(input);
  for (const line of lines) {
    input.emit("data", Buffer.from(`${line}\n`));
    await pause(KEYSTROKE_GAP_MS);
  }
}

function endsWithin(session: Promise<Outcome>): Promise<Outcome> {
  return Promise.race([
    session,
    pause(SESSION_END_BUDGET_MS).then(() => {
      throw new Error("la sesión no terminó a tiempo");
    }),
  ]);
}

/** Un directorio temporal con el fichero del token, y el sandbox que el bloque deja vivo. */
function sandboxScope(world: CliWorld) {
  const scratch = mkdtempSync(join(tmpdir(), "rayito-cli-scenarios-"));
  const state = { sandboxId: undefined as string | undefined };
  afterAll(async () => {
    rmSync(scratch, { recursive: true, force: true });
    if (state.sandboxId !== undefined) {
      await world.killQuietly(state.sandboxId);
    }
  });
  return { tokenFile: join(scratch, "token"), state };
}

/** `stack list|status` e `image list|sizes`: lecturas que no crean nada. */
export function defineCliReadScenarios(world: CliWorld): void {
  describe("lecturas de la CLI", () => {
    test("stack list y status no necesitan crear nada", async () => {
      const listed = await rayito(world, ["--json", "stack", "list"]);
      expect(listed.exitCode).toBe(0);
      expect(JSON.parse(listed.stdout).map((row: { name: string }) => row.name)).toContain(
        "metadata-index",
      );
      const status = await rayito(world, ["--json", "stack", "status", "metadata-index"]);
      expect(status.exitCode).toBe(0);
      const document = JSON.parse(status.stdout);
      expect(document === null || typeof document.state === "string").toBe(true);
    });

    test("image list y sizes leen el catálogo de la cuenta", async () => {
      const images = await rayito(world, ["--json", "image", "list"]);
      expect(images.exitCode).toBe(0);
      expect(Array.isArray(JSON.parse(images.stdout))).toBe(true);
      const sizes = await rayito(world, ["--json", "image", "sizes"]);
      expect(sizes.exitCode).toBe(0);
      expect(JSON.parse(sizes.stdout)).toHaveLength(SIZE_NAMES.length);
    });
  });
}

/** Un sandbox de punta a punta desde la CLI: `create --detach` hasta `kill`. */
export function defineCliSandboxScenarios(world: CliWorld, runLabel: string): void {
  describe("sandbox de punta a punta desde la CLI", () => {
    const { tokenFile, state } = sandboxScope(world);

    test("create --detach, info, exec, metrics, list y kill", async () => {
      const created = await rayito(world, [
        "--json",
        "sandbox",
        "create",
        world.template,
        "--detach",
        "--token-file",
        tokenFile,
        "--timeout",
        String(SESSION_TIMEOUT_SECONDS),
        "--metadata",
        `run=${runLabel}`,
      ]);
      expect(created.exitCode).toBe(0);
      state.sandboxId = JSON.parse(created.stdout).sandbox_id as string;
      const sandboxId = state.sandboxId;
      expect(sandboxId).toMatch(/^microvm-/);

      const info = await rayito(world, ["--json", "sandbox", "info", sandboxId]);
      expect(info.exitCode).toBe(0);
      expect(JSON.parse(info.stdout)).toMatchObject({
        sandbox_id: sandboxId,
        state: "RUNNING",
        metadata: { run: runLabel },
      });

      const exec = (...rest: string[]) =>
        rayito(world, ["sandbox", "exec", sandboxId, "--token-file", tokenFile, ...rest]);
      expect(await exec("--", "echo", "hola mundo")).toMatchObject({
        exitCode: 0,
        stdout: "hola mundo\n",
      });
      expect((await exec("--", "sh", "-c", `exit ${EXEC_FAILURE_CODE}`)).exitCode).toBe(
        EXEC_FAILURE_CODE,
      );
      const timedOut = await exec(
        "--timeout",
        String(EXEC_TIMEOUT_SECONDS),
        "--",
        "sleep",
        String(SLEEP_SECONDS),
      );
      expect(timedOut.exitCode).toBe(EXEC_TIMEOUT_CODE);

      const metrics = await rayito(world, [
        "--json",
        "sandbox",
        "metrics",
        sandboxId,
        "--token-file",
        tokenFile,
      ]);
      expect(metrics.exitCode).toBe(0);
      expect(JSON.parse(metrics.stdout).cpu_count).toBeGreaterThan(0);

      const listed = await rayito(world, [
        "--json",
        "sandbox",
        "list",
        "--metadata",
        `run=${runLabel}`,
      ]);
      expect(listed.exitCode).toBe(0);
      expect(
        JSON.parse(listed.stdout).map((row: { sandbox_id: string }) => row.sandbox_id),
      ).toEqual([sandboxId]);

      const killed = await rayito(world, ["--json", "sandbox", "kill", sandboxId]);
      expect(killed.exitCode).toBe(0);
      expect(JSON.parse(killed.stdout)).toEqual([{ sandbox_id: sandboxId, outcome: "terminated" }]);
    });
  });
}

/** Extras de la sesión `connect` de un entorno: líneas que teclear antes del `echo` y qué hacer después. */
export interface ConnectExtras {
  readonly setupLines?: readonly string[];
  readonly afterConnect?: (sandboxId: string) => Promise<void>;
}

/** Las terminales de `create` sin `--detach` y de `connect`, con la entrada por tubería. */
export function defineCliInteractiveScenarios(
  world: CliWorld,
  runLabel: string,
  extras: ConnectExtras = {},
): void {
  describe("sesiones interactivas de la CLI", () => {
    const { tokenFile, state } = sandboxScope(world);

    test("create sin --detach abre una terminal real y termina el sandbox al salir", async () => {
      const input = new FakeInput(false);
      const session = startRayito(
        world,
        [
          "sandbox",
          "create",
          world.template,
          "--timeout",
          String(SESSION_TIMEOUT_SECONDS),
          "--metadata",
          `run=${runLabel}`,
        ],
        input,
      );
      await typeLines(input, ["echo hola-pty", `exit ${SHELL_EXIT_CODE}`]);
      const outcome = await endsWithin(session.done);
      expect(outcome.exitCode).toBe(SHELL_EXIT_CODE);
      expect(outcome.stdout).toMatch(/[\r\n]hola-pty\r\n/);

      const left = await rayito(world, [
        "--json",
        "sandbox",
        "list",
        "--metadata",
        `run=${runLabel}`,
      ]);
      expect(left.exitCode).toBe(0);
      expect(JSON.parse(left.stdout)).toEqual([]);
    });

    test("connect a un sandbox existente", async () => {
      const created = await rayito(world, [
        "--json",
        "sandbox",
        "create",
        world.template,
        "--detach",
        "--token-file",
        tokenFile,
        "--timeout",
        String(SESSION_TIMEOUT_SECONDS),
        "--metadata",
        `run=${runLabel}`,
      ]);
      expect(created.exitCode).toBe(0);
      state.sandboxId = JSON.parse(created.stdout).sandbox_id as string;

      const input = new FakeInput(false);
      const session = startRayito(
        world,
        ["sandbox", "connect", state.sandboxId, "--token-file", tokenFile],
        input,
      );
      await typeLines(input, [...(extras.setupLines ?? []), "echo $((6*7))", "exit 0"]);
      const connected = await endsWithin(session.done);
      expect(connected.exitCode).toBe(0);
      expect(connected.stdout).toMatch(/[\r\n]42\r\n/);
      await extras.afterConnect?.(state.sandboxId);
    });
  });
}
