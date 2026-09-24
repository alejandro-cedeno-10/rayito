/**
 * Kernels de la variante `rayito-base-poly` (M7 `m7-poly-kernels` y M9
 * `m9-deno-kernels`) a través del SDK TypeScript contra AWS real. En un
 * sandbox de `RAYITO_TEMPLATE_POLY`: una celda bash con arranque perezoso del
 * kernel, celdas `typescript` y `javascript` (alias `js`) servidas por el
 * kernel Jupyter de Deno (resultado sin escapes ANSI, `console.log`,
 * `Deno.jupyter.html`, errores JS y `envs` de contexto), las opciones
 * excluyentes y `listCodeContexts()` con los contextos por defecto de cada
 * lenguaje. En un sandbox de `RAYITO_TEMPLATE` (`rayito-base`, sin Deno):
 * `javascript` y `typescript` son `InvalidArgumentError` con `Unimplemented`
 * que nombra `rayito-base-poly`. Cada suite se omite sin su variable.
 */

import { Code } from "@connectrpc/connect";
import { afterAll, describe, expect, test } from "vitest";
import { InvalidArgumentError, type Sandbox } from "../../src/index.js";
import { createTestSandbox, e2eEnabled, timed, useE2E } from "./helpers.js";

const POLY_TEMPLATE_VAR = "RAYITO_TEMPLATE_POLY";
const POLY_IMAGE = "rayito-base-poly";
const DENO_LANGUAGES = ["javascript", "typescript"] as const;
const ESCAPE = "\u001b";

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
    sandbox = await createTestSandbox(e2e, { timeoutMs: 900_000 });
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
});

describe.skipIf(!e2eEnabled())("kernels Deno en rayito-base (requiere RAYITO_E2E=1)", () => {
  const e2e = useE2E();

  test("javascript and typescript are UNIMPLEMENTED on rayito-base", async () => {
    const base = await createTestSandbox(e2e);
    for (const language of DENO_LANGUAGES) {
      const executed = await base.runCode("1", { language }).catch((error) => error);
      expect(executed).toBeInstanceOf(InvalidArgumentError);
      expect((executed as InvalidArgumentError).grpcCode).toBe(Code.Unimplemented);
      expect((executed as Error).message).toContain(POLY_IMAGE);
      const created = await base.createCodeContext({ language }).catch((error) => error);
      expect(created).toBeInstanceOf(InvalidArgumentError);
      expect((created as InvalidArgumentError).grpcCode).toBe(Code.Unimplemented);
      expect((created as Error).message).toContain(POLY_IMAGE);
      console.log(`\n[m9] ${language} on rayito-base: ${(executed as Error).message}`);
    }
    expect((await base.listCodeContexts()).map((item) => item.id)).toEqual(["default"]);
  });
});
