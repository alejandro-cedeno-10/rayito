/**
 * Un server-stream que termina no deja vivo el timer de su deadline: en
 * `@connectrpc/connect` 2.x abortar el `AbortController` de la llamada no
 * limpia el `setTimeout` de `timeoutMs`, y ese timer (con ref) mantenía vivo
 * el proceso hasta el deadline (315 s tras un `runCode`, 65 s tras un
 * `commands.run`).
 */

import { describe, expect, test } from "vitest";
import { createTestSandbox, sleep } from "./helpers.js";

function activeTimeouts(): number {
  return process.getActiveResourcesInfo().filter((kind) => kind === "Timeout").length;
}

/** Los timers de una unaria o de un stream recién cerrados se sueltan en microtareas: da un respiro. */
async function settledTimeouts(): Promise<number> {
  await sleep(50);
  return activeTimeouts();
}

describe("stream deadline timers", () => {
  test("runCode and commands.run leave no ref'd timer behind once they return", async () => {
    const baseline = await settledTimeouts();
    const { sandbox, rayd } = await createTestSandbox();
    expect((await sandbox.runCode("x = 42")).text).toBeUndefined();
    expect((await sandbox.runCode("x")).text).toBe("42");
    expect((await sandbox.commands.run("echo hola")).stdout).toBe("hola\n");
    sandbox.close();
    await rayd.close();
    expect(await settledTimeouts()).toBeLessThanOrEqual(baseline);
  });

  test("the same holds for pty, watchDir and a killed background command", async () => {
    const baseline = await settledTimeouts();
    const { sandbox, rayd } = await createTestSandbox();
    const pty = await sandbox.pty.create();
    await pty.sendInput("echo hola\n");
    pty.disconnect();
    const watch = await sandbox.files.watchDir("/home/user", { timeoutMs: 60_000 });
    await watch.stop();
    const background = await sandbox.commands.run("sleep 30", {
      background: true,
      timeoutMs: 60_000,
    });
    await background.kill();
    await background.wait().catch(() => undefined);
    sandbox.close();
    await rayd.close();
    expect(await settledTimeouts()).toBeLessThanOrEqual(baseline);
  });
});
