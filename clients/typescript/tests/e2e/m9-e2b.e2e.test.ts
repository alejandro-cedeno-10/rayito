/**
 * Aceptación de `m9-e2b-v2-surface` en TypeScript contra AWS real
 * (`openspec/changes/m9-e2b-v2-surface/design.md` D23, tarea 11.5).
 *
 * `beforeAll` corre `pnpm build`; después cada programa de
 * `tests/e2e/e2b-corpus/*.mjs` se ejecuta con `node` como lo haría un usuario
 * de E2B: su única línea de Rayito es `import ... from "rayito/e2b"`, que la
 * auto-referencia del paquete resuelve a `dist/e2b.mjs`. El entorno lleva
 * `RAYITO_TEMPLATE`, un `RAYITO_ACCESS_TOKEN` recién generado (para
 * `Sandbox.connect(id)`), `AWS_REGION` y `AWS_PROFILE`; el token nunca se
 * imprime y la salida se muestra sin él y sin URLs.
 *
 * Guardrails: cada programa tiene un presupuesto de pared y, al acabar (bien,
 * mal o por timeout), se terminan los MicroVMs de la imagen que no estaban
 * vivos antes de lanzarlo; el sweeper de `useE2E` hace el pre-flight. Los
 * programas usan los valores por defecto de E2B (plazo lógico de 300 s que
 * impone `rayd`). El tope de plataforma por defecto del shim (`maxLifetimeMs`,
 * 3 600 000) se baja a 900 s (como `TEST_SANDBOX_TIMEOUT_SECONDS` en Python)
 * sin tocar el corpus: cada programa corre con
 * `node --import tests/e2e/e2b-corpus-cap.mjs`, que envuelve
 * `Sandbox.createFor` del shim, y `e2b-corpus-cap-check.mjs` comprueba antes,
 * sin crear nada, que ese tope llega al `create` nativo por todas las rutas.
 */

import { execFileSync, spawn } from "node:child_process";
import { readdirSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { beforeAll, describe, expect, test } from "vitest";
import { SandboxError } from "../../src/index.js";
import { generateAccessToken } from "../../src/payload.js";
import { type E2EContext, e2eEnabled, useE2E } from "./helpers.js";

const PACKAGE_DIR = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const CORPUS_DIR = join(PACKAGE_DIR, "tests", "e2e", "e2b-corpus");
/** Precarga que acota `maxLifetimeMs` a 900 000 en todo `create` de `rayito/e2b`. */
const CAP_PRELOAD = pathToFileURL(join(PACKAGE_DIR, "tests", "e2e", "e2b-corpus-cap.mjs")).href;
const CAP_CHECK = join(PACKAGE_DIR, "tests", "e2e", "e2b-corpus-cap-check.mjs");
const CAP_CHECK_OK = "corpus-cap ok";
const PROGRAMS = [
  "create-info-list",
  "get-host",
  "watch-dir",
  "pty",
  "git",
  "pause",
  "abort-run-code",
  "bound-client",
] as const;
const DOCS_PREFIX = "// Sigue https://docs.e2b.dev/";
const ALLOWED_IMPORT = /^(?:rayito\/e2b|node:[a-z/_]+)$/;
const BUILD_TIMEOUT_MS = 300_000;
const PROGRAM_BUDGET_MS = 600_000;
const OUTPUT_TAIL_LINES = 40;
const URL_PATTERN = /\b(?:https?|grpcs?):\/\/\S+/g;

interface ProgramRun {
  readonly code: number | null;
  readonly stdout: string;
  readonly stderr: string;
  readonly seconds: number;
}

function report(label: string, value: string): void {
  console.log(`\n[m9-e2b-ts] ${label}: ${value}`);
}

function scrub(text: string, token: string): string {
  return text.split(token).join("<token>").replace(URL_PATTERN, "<url>");
}

function tail(text: string): string {
  return text.split("\n").slice(-OUTPUT_TAIL_LINES).join("\n");
}

function runNode(program: string, env: NodeJS.ProcessEnv): Promise<ProgramRun> {
  const started = performance.now();
  return new Promise((resolvePromise, reject) => {
    const child = spawn(process.execPath, ["--import", CAP_PRELOAD, program], {
      env,
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.setEncoding("utf8").on("data", (chunk: string) => {
      stdout += chunk;
    });
    child.stderr.setEncoding("utf8").on("data", (chunk: string) => {
      stderr += chunk;
    });
    const timer = setTimeout(() => child.kill("SIGKILL"), PROGRAM_BUDGET_MS);
    child.on("error", (error) => {
      clearTimeout(timer);
      reject(error);
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      resolvePromise({ code, stdout, stderr, seconds: (performance.now() - started) / 1000 });
    });
  });
}

async function liveIds(context: E2EContext): Promise<string[]> {
  const ids: string[] = [];
  for await (const item of context.controlPlane.listMicrovms({ imageArn: context.templateArn })) {
    ids.push(item.sandboxId);
  }
  return ids;
}

/** Termina (idempotente) los MicroVMs de la imagen que no estaban vivos antes del programa. */
async function sweepNew(context: E2EContext, before: ReadonlySet<string>): Promise<number> {
  const leftovers = (await liveIds(context)).filter((id) => !before.has(id));
  for (const sandboxId of leftovers) {
    try {
      await context.controlPlane.terminateMicrovm(sandboxId);
    } catch (error) {
      if (!(error instanceof SandboxError)) {
        throw error;
      }
    }
  }
  return leftovers.length;
}

function importedModules(source: string): string[] {
  return [...source.matchAll(/^import\s[^;]*?from\s+"([^"]+)";/gms)].map((match) => match[1] ?? "");
}

describe.skipIf(!e2eEnabled())("m9 e2b corpus (node)", () => {
  const context = useE2E();
  const token = generateAccessToken();
  const seconds = new Map<string, number>();

  beforeAll(() => {
    execFileSync("pnpm", ["build"], {
      cwd: PACKAGE_DIR,
      stdio: "inherit",
      timeout: BUILD_TIMEOUT_MS,
      shell: process.platform === "win32",
    });
  }, BUILD_TIMEOUT_MS);

  test("the corpus is plain E2B code", () => {
    const found = readdirSync(CORPUS_DIR)
      .filter((name) => name.endsWith(".mjs"))
      .map((name) => name.slice(0, -".mjs".length))
      .sort();
    expect(found).toEqual([...PROGRAMS].sort());
    for (const program of PROGRAMS) {
      const source = readFileSync(join(CORPUS_DIR, `${program}.mjs`), "utf8");
      expect(source.split("\n")[0]?.startsWith(DOCS_PREFIX), program).toBe(true);
      const modules = importedModules(source);
      expect(modules, program).toContain("rayito/e2b");
      expect(
        modules.filter((name) => !ALLOWED_IMPORT.test(name)),
        program,
      ).toEqual([]);
    }
  });

  test("every corpus create is capped at 900 s", async () => {
    const run = await runNode(CAP_CHECK, { ...process.env });
    expect(run.code, tail(run.stderr)).toBe(0);
    expect(run.stdout.trim().startsWith(CAP_CHECK_OK), tail(run.stdout)).toBe(true);
    report("cap", run.stdout.trim());
  });

  test.each(PROGRAMS)("corpus program %s", async (program) => {
    const env: NodeJS.ProcessEnv = {
      ...process.env,
      RAYITO_TEMPLATE: context.settings.template,
      RAYITO_ACCESS_TOKEN: token,
    };
    if (context.settings.region !== undefined) {
      env.AWS_REGION = context.settings.region;
    }
    const before = new Set(await liveIds(context));
    let run: ProgramRun;
    try {
      run = await runNode(join(CORPUS_DIR, `${program}.mjs`), env);
    } finally {
      const swept = await sweepNew(context, before);
      report(`${program} sweep`, `${swept} MicroVM(s)`);
    }
    const stdout = scrub(run.stdout, token);
    const stderr = scrub(run.stderr, token);
    report(program, `exit ${run.code} en ${run.seconds.toFixed(2)} s`);
    const leaked = run.stdout.includes(token) || run.stderr.includes(token);
    expect(leaked, "el token salió del SDK").toBe(false);
    expect(run.code, tail(stderr)).toBe(0);
    expect(stdout.trim().endsWith(`${program} ok`), tail(stdout)).toBe(true);
    seconds.set(program, run.seconds);
  });

  test("corpus summary", () => {
    for (const program of PROGRAMS) {
      const elapsed = seconds.get(program);
      report(program, elapsed === undefined ? "falló o no corrió" : `${elapsed.toFixed(2)} s`);
    }
    console.log(`\ncorpus_ok=${seconds.size}/${PROGRAMS.length}`);
    expect([...seconds.keys()].sort()).toEqual([...PROGRAMS].sort());
  });
});
