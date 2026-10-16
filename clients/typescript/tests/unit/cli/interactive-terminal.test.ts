/**
 * El puente de terminal de `rayito sandbox connect|create`: modo raw y
 * restauración, tamaño inicial y reenvío del cambio de tamaño, Ctrl-C como
 * byte, EOF de una tubería, entrada en orden, códigos de salida y corte por
 * Ctrl-C. Espejo de `clients/python/tests/unit/cli/test_sandbox_interactive.py`.
 */

import { describe, expect, it } from "vitest";
import { EXIT_TIMEOUT } from "../../../src/cli/exit-codes.js";
import { DEFAULT_SIZE, runTerminal, terminalSize } from "../../../src/cli/interactive-terminal.js";
import type { RemoteSandbox } from "../../../src/cli/services.js";
import { CommandExitError, TimeoutError } from "../../../src/errors.js";
import { FakeInput, FakeOutput, FakePty, fakeIo, sandboxWithPty } from "./interactive-fakes.js";

const ETX = "\x03";
const SHELL_FAILURE_CODE = 7;
const SLOW_SEND_MS = 15;

function sandbox(pty: FakePty): RemoteSandbox {
  return sandboxWithPty(pty) as RemoteSandbox;
}

const tick = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 5));

describe("entrada que no es una terminal (tubería)", () => {
  it("reenvía los bytes tal cual, manda EOF al acabar y devuelve el código del shell", async () => {
    const pty = new FakePty();
    const input = new FakeInput(false);
    const io = fakeIo(input);
    const running = runTerminal(sandbox(pty), io);
    await tick();
    input.emit("data", Buffer.from("ls\n"));
    input.emit("data", Buffer.from("exit\n"));
    input.emit("end");
    await tick();
    pty.created[0]?.onData(Buffer.from("salida\n"));
    pty.handle.finish(0);

    expect(await running).toBe(0);
    expect(pty.handle.inputText).toBe("ls\nexit\n\x04");
    expect(io.output.text).toBe("salida\n");
    expect(input.rawModes).toEqual([]);
    expect(input.paused).toBe(true);
  });

  it("abre la PTY con 80x24, sin plazo de servidor y con usuario, directorio y entorno", async () => {
    const pty = new FakePty();
    const running = runTerminal(sandbox(pty), fakeIo(new FakeInput(false)), {
      user: "root",
      cwd: "/work",
      envs: { A: "1" },
    });
    await tick();
    pty.handle.finish(0);
    await running;
    expect(pty.created[0]).toMatchObject({
      size: DEFAULT_SIZE,
      user: "root",
      cwd: "/work",
      envs: { A: "1" },
      timeoutMs: 0,
    });
  });

  it("entrega la entrada en orden aunque cada envío tarde", async () => {
    const pty = new FakePty();
    pty.handle.sendDelayMs = SLOW_SEND_MS;
    const input = new FakeInput(false);
    const running = runTerminal(sandbox(pty), fakeIo(input));
    await tick();
    for (const piece of ["a", "b", "c", "d"]) {
      input.emit("data", Buffer.from(piece));
    }
    await new Promise((resolve) => setTimeout(resolve, SLOW_SEND_MS * 6));
    pty.handle.finish(0);
    await running;
    expect(pty.handle.inputText).toBe("abcd");
  });

  it("si la PTY ya terminó, deja de reenviar sin romper la salida", async () => {
    const pty = new FakePty();
    const input = new FakeInput(false);
    const running = runTerminal(sandbox(pty), fakeIo(input));
    await tick();
    pty.handle.sendFailure = new Error("PTY terminada");
    input.emit("data", Buffer.from("tarde"));
    input.emit("data", Buffer.from("más tarde"));
    await tick();
    pty.handle.finish(3);
    expect(await running).toBe(3);
  });
});

describe("entrada que es una terminal", () => {
  it("activa el modo raw durante la sesión y lo restaura al salir", async () => {
    const pty = new FakePty();
    const input = new FakeInput(true);
    const running = runTerminal(sandbox(pty), fakeIo(input));
    await tick();
    expect(input.rawModes).toEqual([true]);
    expect(input.paused).toBe(false);
    pty.handle.finish(0);
    await running;
    expect(input.rawModes).toEqual([true, false]);
    expect(input.paused).toBe(true);
    expect(input.listenerCount("data")).toBe(0);
  });

  it("restaura la terminal aunque la sesión falle", async () => {
    const pty = new FakePty();
    const input = new FakeInput(true);
    const running = runTerminal(sandbox(pty), fakeIo(input));
    await tick();
    pty.handle.fail(new Error("sandbox caído"));
    await expect(running).rejects.toThrow("sandbox caído");
    expect(input.rawModes).toEqual([true, false]);
  });

  it("manda Ctrl-C como el byte 0x03, nunca como una señal local", async () => {
    const pty = new FakePty();
    const input = new FakeInput(true);
    const running = runTerminal(sandbox(pty), fakeIo(input));
    await tick();
    input.emit("data", Buffer.from(ETX));
    await tick();
    pty.handle.finish(130);
    await running;
    expect(pty.handle.inputText).toBe(ETX);
  });

  it("toma el tamaño inicial de la terminal y reenvía los cambios", async () => {
    const pty = new FakePty();
    const input = new FakeInput(true);
    const output = new FakeOutput();
    output.columns = 132;
    output.rows = 43;
    const running = runTerminal(sandbox(pty), fakeIo(input, output));
    await tick();
    expect(pty.created[0]?.size).toEqual({ cols: 132, rows: 43 });
    output.columns = 100;
    output.rows = 30;
    output.emit("resize");
    await tick();
    expect(pty.handle.resizes).toEqual([{ cols: 100, rows: 30 }]);
    pty.handle.finish(0);
    await running;
    expect(output.listenerCount("resize")).toBe(0);
  });

  it("una terminal que no dice su tamaño usa 80x24", () => {
    expect(terminalSize(fakeIo(new FakeInput(true)))).toEqual(DEFAULT_SIZE);
  });

  it("una terminal que da 0x0 (una PTY sin tamaño fijado) usa 80x24 y no una de 1x1", () => {
    const output = new FakeOutput();
    output.columns = 0;
    output.rows = 0;
    expect(terminalSize(fakeIo(new FakeInput(true), output))).toEqual(DEFAULT_SIZE);
  });

  it("completa sólo la dimensión que falta", () => {
    const output = new FakeOutput();
    output.columns = 132;
    output.rows = 0;
    expect(terminalSize(fakeIo(new FakeInput(true), output))).toEqual({
      cols: 132,
      rows: DEFAULT_SIZE.rows,
    });
  });
});

describe("código de salida", () => {
  it("es el del shell cuando acaba con error", async () => {
    const pty = new FakePty();
    const running = runTerminal(sandbox(pty), fakeIo(new FakeInput(false)));
    await tick();
    pty.handle.fail(
      new CommandExitError("falló", { exitCode: SHELL_FAILURE_CODE, stdout: "", stderr: "" }),
    );
    expect(await running).toBe(SHELL_FAILURE_CODE);
  });

  it("es 124 si el agente cierra la PTY por plazo", async () => {
    const pty = new FakePty();
    const running = runTerminal(sandbox(pty), fakeIo(new FakeInput(false)));
    await tick();
    pty.handle.fail(new TimeoutError("plazo"));
    expect(await running).toBe(EXIT_TIMEOUT);
  });

  it("Ctrl-C en una tubería corta la PTY", async () => {
    const pty = new FakePty();
    const abort = new AbortController();
    const running = runTerminal(sandbox(pty), fakeIo(new FakeInput(false)), {
      signal: abort.signal,
    });
    await tick();
    abort.abort();
    await running;
    expect(pty.handle.killed).toBe(1);
  });

  it("una señal ya cancelada corta la PTY nada más abrirla", async () => {
    const pty = new FakePty();
    const abort = new AbortController();
    abort.abort();
    await runTerminal(sandbox(pty), fakeIo(new FakeInput(false)), { signal: abort.signal });
    expect(pty.handle.killed).toBe(1);
  });
});
