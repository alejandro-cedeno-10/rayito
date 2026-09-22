/**
 * M7 `m7-poly-kernels` a través del SDK TypeScript contra AWS real: un
 * sandbox de la variante `rayito-base-poly` (`RAYITO_TEMPLATE_POLY`), una
 * celda bash con arranque perezoso del kernel, `javascript` como lenguaje
 * conocido que ninguna imagen trae (`ijavascript` necesita compilador en
 * al2023 ARM64, AWS_API_NOTES.md Q57), las opciones excluyentes y
 * `listCodeContexts()` con `default-bash`. Se omite sin la variable.
 */

import { afterAll, describe, expect, test } from "vitest";
import { InvalidArgumentError, type Sandbox } from "../../src/index.js";
import { createTestSandbox, e2eEnabled, timed, useE2E } from "./helpers.js";

const POLY_TEMPLATE_VAR = "RAYITO_TEMPLATE_POLY";
const POLY_IMAGE = "rayito-base-poly";

const polyEnabled = e2eEnabled() && Boolean(process.env[POLY_TEMPLATE_VAR]);

describe.skipIf(!polyEnabled)(`kernels poly (requiere RAYITO_E2E=1 y ${POLY_TEMPLATE_VAR})`, () => {
  const e2e = useE2E(POLY_TEMPLATE_VAR);
  let sandbox: Sandbox | undefined;

  afterAll(async () => {
    if (sandbox !== undefined) {
      await sandbox.kill();
    }
  });

  test("bash cell, javascript unimplemented, exclusive options, listing", async () => {
    sandbox = await createTestSandbox(e2e, { timeoutMs: 900_000 });
    const [first] = await timed("m7 first bash cell (lazy kernel start)", () =>
      (sandbox as Sandbox).runCode("echo hi", { language: "bash" }),
    );
    expect(first.logs.stdout.join("")).toContain("hi");
    expect(first.error).toBeUndefined();
    const [second] = await timed("m7 second bash cell", () =>
      (sandbox as Sandbox).runCode("echo again", { language: "Bash" }),
    );
    expect(second.logs.stdout.join("")).toContain("again");
    const javascript = await sandbox.runCode("1 + 1", { language: "javascript" }).catch((e) => e);
    expect(javascript).toBeInstanceOf(InvalidArgumentError);
    expect((javascript as Error).message).toContain(POLY_IMAGE);
    console.log(`
[m7] javascript on the poly image: ${(javascript as Error).message}`);
    await expect(
      sandbox.runCode("x", { context: "default", language: "bash" }),
    ).rejects.toBeInstanceOf(InvalidArgumentError);
    const listed = await sandbox.listCodeContexts();
    const bash = listed.find((ctx) => ctx.id === "default-bash");
    expect(bash?.language).toBe("bash");
    expect(listed[0]?.id).toBe("default");
    expect((await sandbox.runCode("1 + 1")).text).toBe("2");
  });
});
