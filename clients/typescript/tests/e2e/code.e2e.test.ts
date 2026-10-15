/**
 * Espejo de `test_m4_code.py::test_m4_code` para lo que es código del SDK
 * TypeScript (`sandbox/code.ts`): stderr, `onStdout` con `OutputMessage`, un
 * timeout que interrumpe y conserva el estado, una celda silenciosa larga a
 * través del proxy, contextos aislados con `cwd` y `envs`, `envs` por
 * ejecución, `restartCodeContext`, `removeCodeContext` con sus errores y la
 * muerte del kernel con su recuperación. Los tiempos de readiness, el dueño
 * de los procesos y el PRNG distinto entre VMs son del guest y se prueban una
 * vez, en Python. Un sandbox compartido.
 */

import { beforeAll, describe, expect, test } from "vitest";
import {
  InvalidArgumentError,
  NotFoundError,
  type OutputMessage,
  type Sandbox,
} from "../../src/index.js";
import { createTestSandbox, e2eEnabled, HOME, report, seconds, useE2E } from "./helpers.js";

const TIMEOUT_CELL_MS = 2000;
const TIMEOUT_LATENCY_BUDGET_S = 8;
const SILENT_CELL_SECONDS = 12;
const SILENT_CELL_TIMEOUT_MS = 60_000;
const KERNEL_DEATH_RECOVERY_BUDGET_S = 15;
const KERNEL_DEATH_EXIT_CODE = 3;
const UNKNOWN_CONTEXT_ID = "ctx-000000000000";
const DEFAULT_CONTEXT = "default";
const TIMEOUT_ERROR = "ExecutionTimeout";
const NAME_ERROR = "NameError";

async function contextIds(sbx: Sandbox): Promise<string[]> {
  return (await sbx.listCodeContexts()).map((item) => item.id);
}

describe.skipIf(!e2eEnabled())("runCode through the TypeScript SDK (mirror of M4)", () => {
  const context = useE2E();
  let sbx: Sandbox;

  beforeAll(async () => {
    sbx = await createTestSandbox(context);
  });

  test("stderr and onStdout with OutputMessage", async () => {
    const errored = await sbx.runCode("import sys; print('e', file=sys.stderr)");
    expect(errored.logs.stderr.join("")).toContain("e");
    expect(errored.logs.stdout).toEqual([]);
    const seen: OutputMessage[] = [];
    await sbx.runCode("print('a'); print('b')", { onStdout: (message) => seen.push(message) });
    const joined = seen.map((message) => message.line).join("");
    expect(joined).toContain("a");
    expect(joined).toContain("b");
    for (const message of seen) {
      expect(message.error).toBe(false);
      expect(message.timestamp).toBeGreaterThan(0);
    }
  });

  test("a timeout interrupts the cell and keeps the state, and a silent cell survives the proxy", async () => {
    expect((await sbx.runCode("x = 42")).error).toBeUndefined();
    const started = performance.now();
    const interrupted = await sbx.runCode("import time; time.sleep(10)", {
      timeoutMs: TIMEOUT_CELL_MS,
    });
    const elapsed = seconds(started);
    report(`timeout=${TIMEOUT_CELL_MS} ms sobre time.sleep(10)`, elapsed);
    expect(elapsed).toBeLessThanOrEqual(TIMEOUT_LATENCY_BUDGET_S);
    expect(interrupted.error?.name).toBe(TIMEOUT_ERROR);
    expect(interrupted.error?.value).toContain(String(TIMEOUT_CELL_MS));
    expect((await sbx.runCode("x")).text).toBe("42");
    expect((await sbx.runCode("1+1")).text).toBe("2");

    const silent = await sbx.runCode(`import time; time.sleep(${SILENT_CELL_SECONDS}); 'done'`, {
      timeoutMs: SILENT_CELL_TIMEOUT_MS,
    });
    expect(silent.error).toBeUndefined();
    expect(silent.text).toBe("'done'");
  });

  test("contexts are isolated, take cwd and envs, and executions take their own envs", async () => {
    expect((await sbx.runCode("x = 42")).error).toBeUndefined();
    const isolated = await sbx.createCodeContext();
    expect(isolated.id).not.toBe(DEFAULT_CONTEXT);
    expect(isolated.language).toBe("python");
    expect(isolated.cwd).toBe(HOME);
    expect((await sbx.runCode("x", { context: isolated })).error?.name).toBe(NAME_ERROR);
    await sbx.runCode("y = 7", { context: isolated });
    expect((await sbx.runCode("y", { context: isolated.id })).text).toBe("7");
    const ids = await contextIds(sbx);
    expect(ids[0]).toBe(DEFAULT_CONTEXT);
    expect(ids).toContain(isolated.id);

    const other = await sbx.createCodeContext({ cwd: "/tmp", envs: { M4_ENV: "1" } });
    expect((await sbx.runCode("import os; os.getcwd()", { context: other })).text).toBe("'/tmp'");
    expect((await sbx.runCode("import os; os.environ['M4_ENV']", { context: other })).text).toBe(
      "'1'",
    );
    await sbx.removeCodeContext(isolated);
    await sbx.removeCodeContext(other);

    const cell = "import os; os.environ.get('M4_RUN', 'unset')";
    expect((await sbx.runCode(cell, { envs: { M4_RUN: "yes" } })).text).toBe("'yes'");
    expect((await sbx.runCode(cell)).text).toBe("'unset'");
  });

  test("restartCodeContext clears the scope and removeCodeContext reports its errors", async () => {
    const ctx = await sbx.createCodeContext();
    await sbx.runCode("y = 7", { context: ctx });
    await sbx.restartCodeContext(ctx);
    expect((await sbx.runCode("y", { context: ctx })).error?.name).toBe(NAME_ERROR);
    await sbx.removeCodeContext(ctx);
    expect(await contextIds(sbx)).not.toContain(ctx.id);
    await expect(sbx.runCode("1", { context: ctx })).rejects.toBeInstanceOf(NotFoundError);
    await expect(sbx.removeCodeContext(DEFAULT_CONTEXT)).rejects.toBeInstanceOf(
      InvalidArgumentError,
    );
    await expect(sbx.removeCodeContext(UNKNOWN_CONTEXT_ID)).rejects.toBeInstanceOf(NotFoundError);
  });

  test("KernelDied converges: the next cell runs in a fresh kernel", async () => {
    expect((await sbx.runCode("x = 42")).error).toBeUndefined();
    const died = await sbx.runCode(`import os; os._exit(${KERNEL_DEATH_EXIT_CODE})`);
    expect(died.error?.name).toBe("KernelDied");
    const started = performance.now();
    expect((await sbx.runCode("1+1")).text).toBe("2");
    const elapsed = seconds(started);
    report("primera celda tras KernelDied", elapsed);
    expect(elapsed).toBeLessThanOrEqual(KERNEL_DEATH_RECOVERY_BUDGET_S);
    expect((await sbx.runCode("x")).error?.name).toBe(NAME_ERROR);
  });
});
