/**
 * Kernels de la variante `rayito-base-poly` (M7 `m7-poly-kernels` y M9
 * `m9-deno-kernels`) a través del SDK TypeScript contra AWS real. En un
 * sandbox de `RAYITO_TEMPLATE_POLY`: una celda bash con arranque perezoso del
 * kernel, celdas `typescript` y `javascript` (alias `js`) servidas por el
 * kernel Jupyter de Deno (resultado sin escapes ANSI, `console.log`,
 * `Deno.jupyter.html`, errores JS y `envs` de contexto), las opciones
 * excluyentes y `listCodeContexts()` con los contextos por defecto de cada
 * lenguaje. En un sandbox de `RAYITO_TEMPLATE` (`rayito-base`, sin Deno):
 * `bash`, `javascript` y `typescript` son `UnimplementedError` con un mensaje
 * que nombra `rayito-base-poly`. Un timeout en bash devuelve `ExecutionTimeout`
 * y el contexto sigue vivo, y el shim de E2B ejecuta `js` y `ts` sobre poly.
 * Cada suite se omite sin su variable.
 */

import { afterAll, describe, expect, test } from "vitest";
import { Sandbox as ShimSandbox } from "../../src/e2b/index.js";
import {
  CommandExitError,
  InvalidArgumentError,
  type Sandbox,
  UnimplementedError,
} from "../../src/index.js";
import {
  createTestSandbox,
  e2eEnabled,
  TEST_SANDBOX_TIMEOUT_MS,
  timed,
  useE2E,
} from "./helpers.js";

const POLY_TEMPLATE_VAR = "RAYITO_TEMPLATE_POLY";
const POLY_IMAGE = "rayito-base-poly";
const POLY_LANGUAGES = ["bash", "javascript", "typescript"] as const;
const BASH_TIMEOUT_MS = 2000;
const BASH_PROBE_TIMEOUT_MS = 30_000;
const BASH_KERNEL_MODULE = "bash_kernel";
const ESCAPE = "\u001b";
const PAUSE_MAX_IDLE_SECONDS = 300;
const ENDLESS_LOOP_TIMEOUT_MS = 2_000;
const AWAITED_DELAY_MS = 200;

const polyEnabled = e2eEnabled() && Boolean(process.env[POLY_TEMPLATE_VAR]);

describe.skipIf(!polyEnabled)(`kernels poly (requiere RAYITO_E2E=1 y ${POLY_TEMPLATE_VAR})`, () => {
  const e2e = useE2E(POLY_TEMPLATE_VAR);
  let sandbox: Sandbox | undefined;

  afterAll(async () => {
    if (sandbox !== undefined) {
      await sandbox.kill();
    }
  });

  test("bash, javascript and typescript cells, exclusive options, listing", async () => {
    sandbox = await createTestSandbox(e2e, { timeoutMs: TEST_SANDBOX_TIMEOUT_MS });
    const poly = sandbox;
    const [first] = await timed("m7 first bash cell (lazy kernel start)", () =>
      poly.runCode("echo hi", { language: "bash" }),
    );
    expect(first.logs.stdout.join("")).toContain("hi");
    expect(first.error).toBeUndefined();
    const [second] = await timed("m7 second bash cell", () =>
      poly.runCode("echo again", { language: "Bash" }),
    );
    expect(second.logs.stdout.join("")).toContain("again");

    const [typescript] = await timed("m9 first typescript cell (lazy Deno start)", () =>
      poly.runCode("const x: number = 40 + 2; x", { language: "typescript" }),
    );
    expect(typescript.error).toBeUndefined();
    expect(typescript.text).toBe("42");
    expect(typescript.text).not.toContain(ESCAPE);
    const [typescriptAgain] = await timed("m9 second typescript cell", () =>
      poly.runCode("x + 1", { language: "ts" }),
    );
    expect(typescriptAgain.text).toBe("43");
    const [javascript] = await timed("m9 first javascript cell (lazy Deno start)", () =>
      poly.runCode("let y = 40 + 2; y", { language: "js" }),
    );
    expect(javascript.error).toBeUndefined();
    expect(javascript.text).toBe("42");
    expect(javascript.text).not.toContain(ESCAPE);

    const logged = await poly.runCode("console.log('out')", { language: "typescript" });
    expect(logged.logs.stdout.join("")).toContain("out");
    const html = await poly.runCode("Deno.jupyter.html`<b>hi</b>`", { language: "typescript" });
    expect(html.results[0]?.html).toBe("<b>hi</b>");
    const thrown = await poly.runCode("throw new Error('boom')", { language: "typescript" });
    expect(thrown.error?.name).toBe("Error");
    expect(thrown.error?.value).toBe("boom");

    const ctx = await poly.createCodeContext({ language: "typescript", envs: { K: "1" } });
    expect(ctx.language).toBe("typescript");
    expect((await poly.runCode("Deno.env.get('K') === '1'", { context: ctx })).text).toBe("true");

    await expect(
      poly.runCode("x", { context: "default", language: "bash" }),
    ).rejects.toBeInstanceOf(InvalidArgumentError);
    await expect(poly.runCode("x", { context: ctx, language: "ts" })).rejects.toBeInstanceOf(
      InvalidArgumentError,
    );
    const listed = await poly.listCodeContexts();
    expect(listed[0]?.id).toBe("default");
    expect(listed.find((item) => item.id === "default-bash")?.language).toBe("bash");
    expect(listed.find((item) => item.id === "default-typescript")?.language).toBe("typescript");
    expect(listed.find((item) => item.id === "default-javascript")?.language).toBe("javascript");
    expect(listed.find((item) => item.id === ctx.id)?.language).toBe("typescript");
    expect((await poly.runCode("1 + 1")).text).toBe("2");
  });

  test("deno outputs: stderr, html, thrown errors and top-level await", async () => {
    const poly = await createTestSandbox(e2e, { timeoutMs: TEST_SANDBOX_TIMEOUT_MS });
    const errored = await poly.runCode("console.error('err')", { language: "typescript" });
    expect(errored.logs.stderr.join("")).toContain("err");
    const awaited = await poly.runCode(
      `await new Promise((r) => setTimeout(() => r(7), ${AWAITED_DELAY_MS}))`,
      { language: "typescript" },
    );
    expect(awaited.error).toBeUndefined();
    expect(awaited.text).toBe("7");
    expect(awaited.text).not.toContain(ESCAPE);
  });

  test("typescript context: envs, timeout of an endless loop and restart", async () => {
    const poly = await createTestSandbox(e2e, { timeoutMs: TEST_SANDBOX_TIMEOUT_MS });
    const ctx = await poly.createCodeContext({ language: "typescript", envs: { K: "1" } });
    expect((await poly.runCode("Deno.env.get('K') === '1'", { context: ctx })).text).toBe("true");
    expect((await poly.runCode("let z = 5", { context: ctx })).error).toBeUndefined();
    const [timedOut] = await timed(
      `typescript endless loop with timeout ${ENDLESS_LOOP_TIMEOUT_MS} ms returned after`,
      () => poly.runCode("while (true) {}", { context: ctx, timeoutMs: ENDLESS_LOOP_TIMEOUT_MS }),
    );
    expect(timedOut.error?.name).toBe("ExecutionTimeout");
    expect((await poly.runCode("1 + 1", { context: ctx })).text).toBe("2");
    expect((await poly.runCode("let w = 9", { context: ctx })).error).toBeUndefined();
    await poly.restartCodeContext(ctx);
    const cleared = await poly.runCode("w", { context: ctx });
    expect(cleared.error?.name).toBe("ReferenceError");
    expect((await poly.listCodeContexts()).map((item) => item.id)).toContain(ctx.id);
    await poly.removeCodeContext(ctx);
  });

  test("bash timeout gives ExecutionTimeout and the context comes back", async () => {
    const poly = await createTestSandbox(e2e, { timeoutMs: TEST_SANDBOX_TIMEOUT_MS });
    const [timedOut] = await timed(`bash timeout ${BASH_TIMEOUT_MS} ms returned after`, () =>
      poly.runCode("sleep 30", { language: "bash", timeoutMs: BASH_TIMEOUT_MS }),
    );
    expect(timedOut.error?.name).toBe("ExecutionTimeout");
    const back = await poly.runCode("echo back", { language: "bash" });
    expect(back.logs.stdout.join("")).toContain("back");
  });

  test("the E2B shim runs js and ts on poly", async () => {
    const poly = await createTestSandbox(e2e, { timeoutMs: TEST_SANDBOX_TIMEOUT_MS });
    const shim = await ShimSandbox.connect(poly.sandboxId, {
      accessToken: poly.accessToken,
      controlPlane: e2e.controlPlane,
    });
    try {
      expect((await shim.runCode("1 + 1", { language: "js" })).text).toBe("2");
      expect((await shim.runCode("const n: number = 3; n", { language: "ts" })).text).toBe("3");
    } finally {
      shim.native.close();
    }
  });

  test("javascript state survives pause and resume", async () => {
    const poly = await createTestSandbox(e2e, {
      timeoutMs: TEST_SANDBOX_TIMEOUT_MS,
      idle: { maxIdleSeconds: PAUSE_MAX_IDLE_SECONDS, autoResume: true },
    });
    expect(
      (await poly.runCode("globalThis.kept = 42", { language: "javascript" })).error,
    ).toBeUndefined();
    const generation = (await poly.getHealth()).resumeGeneration;
    const [paused] = await timed("pause() -> SUSPENDED", () => poly.pause());
    expect(paused).toBe(true);
    await timed("resume() -> kernel_ready", () => poly.resume());
    expect((await poly.runCode("kept", { language: "javascript" })).text).toBe("42");
    const health = await poly.getHealth();
    expect(health.kernelStateLost).toBe(false);
    expect(health.resumeGeneration).toBeGreaterThan(generation);
  });
});

describe.skipIf(!e2eEnabled())("kernels Deno en rayito-base (requiere RAYITO_E2E=1)", () => {
  const e2e = useE2E();

  test("bash, javascript and typescript are UNIMPLEMENTED on rayito-base", async () => {
    const base = await createTestSandbox(e2e);
    for (const language of POLY_LANGUAGES) {
      const executed = await base.runCode("1", { language }).catch((error) => error);
      expect(executed).toBeInstanceOf(UnimplementedError);
      expect(executed).not.toBeInstanceOf(InvalidArgumentError);
      expect((executed as Error).message).toContain(POLY_IMAGE);
      const created = await base.createCodeContext({ language }).catch((error) => error);
      expect(created).toBeInstanceOf(UnimplementedError);
      expect(created).not.toBeInstanceOf(InvalidArgumentError);
      expect((created as Error).message).toContain(POLY_IMAGE);
      console.log(`\n[m9] ${language} on rayito-base: ${(executed as Error).message}`);
    }
    expect((await base.listCodeContexts()).map((item) => item.id)).toEqual(["default"]);
    const probe = await base.commands
      .run(`python3 -c 'import ${BASH_KERNEL_MODULE}'`, { timeoutMs: BASH_PROBE_TIMEOUT_MS })
      .catch((error: unknown) => error);
    expect(probe).toBeInstanceOf(CommandExitError);
  });
});
