/**
 * El puente de terminal de `rayito sandbox create|connect`: una PTY del
 * sandbox conectada a la terminal local. Espejo de `rayito.cli._terminal`.
 *
 * Dos modos de entrada, según `input`:
 *
 * - terminal (TTY): modo raw (`setRawMode`, restaurado al salir), el cambio
 *   de tamaño se reenvía y Ctrl-C viaja como `\x03`, nunca como SIGINT local;
 * - cualquier otra cosa (tuberías): los bytes tal cual y `\x04` al EOF.
 *
 * La entrada se entrega a la PTY en orden, de una en una. Los bytes de la
 * terminal nunca se loguean.
 */

import { CommandExitError, TimeoutError } from "../errors.js";
import type { PtySize } from "../models.js";
import { NO_SERVER_TIMEOUT_MS } from "../units.js";
import { EXIT_TIMEOUT } from "./exit-codes.js";
import type { RemotePtyHandle, RemoteSandbox } from "./services.js";

export const DEFAULT_SIZE: PtySize = { cols: 80, rows: 24 };
/** Fin de transmisión de una terminal: lo que Ctrl-D escribe, y lo que cierra el shell al acabar una tubería. */
export const EOF_BYTE = Uint8Array.of(0x04);

/** La entrada de la terminal local (la forma de `process.stdin`). */
export interface TerminalInput {
  readonly isTTY?: boolean | undefined;
  readonly setRawMode?: ((mode: boolean) => unknown) | undefined;
  on(event: "data", listener: (chunk: Uint8Array) => void): unknown;
  on(event: "end", listener: () => void): unknown;
  off(event: "data", listener: (chunk: Uint8Array) => void): unknown;
  off(event: "end", listener: () => void): unknown;
  pause(): unknown;
  resume(): unknown;
}

/** La salida de la terminal local (la forma de `process.stdout`). */
export interface TerminalOutput {
  write(data: string | Uint8Array): unknown;
  readonly isTTY?: boolean | undefined;
  readonly columns?: number | undefined;
  readonly rows?: number | undefined;
  on?(event: "resize", listener: () => void): unknown;
  off?(event: "resize", listener: () => void): unknown;
}

export interface InteractiveIo {
  readonly input: TerminalInput;
  readonly output: TerminalOutput;
}

export interface TerminalOptions {
  readonly user?: string | undefined;
  readonly cwd?: string | undefined;
  readonly envs?: Readonly<Record<string, string>> | undefined;
  /** Ctrl-C en una entrada que no es una terminal: corta la PTY en vez de dejarla viva. */
  readonly signal?: AbortSignal | undefined;
}

function isRawTerminal(input: TerminalInput): boolean {
  return input.isTTY === true && typeof input.setRawMode === "function";
}

/** Una dimensión que la terminal no da, o da como 0 (una PTY sin tamaño fijado), es la de 80x24. */
function dimensionOr(value: number | undefined, fallback: number): number {
  return value !== undefined && value > 0 ? value : fallback;
}

/** El tamaño de la terminal local, o 80x24 si no es una terminal o no lo dice. */
export function terminalSize(io: InteractiveIo): PtySize {
  if (!isRawTerminal(io.input)) {
    return DEFAULT_SIZE;
  }
  return {
    cols: dimensionOr(io.output.columns, DEFAULT_SIZE.cols),
    rows: dimensionOr(io.output.rows, DEFAULT_SIZE.rows),
  };
}

/**
 * Los envíos a la PTY de uno en uno y en orden. El primer fallo (la PTY ya
 * terminó, el sandbox cayó) corta el reenvío sin más: el resultado lo da
 * `wait()`, y un lector que sigue colgado de un envío nunca retrasa la salida.
 */
class InputQueue {
  readonly #handle: RemotePtyHandle;
  #tail: Promise<void> = Promise.resolve();
  #stopped = false;

  constructor(handle: RemotePtyHandle) {
    this.#handle = handle;
  }

  send(data: Uint8Array): void {
    this.#tail = this.#tail.then(() => this.#deliver(data));
  }

  async #deliver(data: Uint8Array): Promise<void> {
    if (this.#stopped) {
      return;
    }
    try {
      await this.#handle.sendInput(data);
    } catch {
      this.#stopped = true;
    }
  }
}

/** Engancha la entrada local a la PTY y devuelve cómo deshacer lo que haya cambiado en la terminal. */
function startInput(io: InteractiveIo, handle: RemotePtyHandle, queue: InputQueue): () => void {
  const onData = (chunk: Uint8Array): void => queue.send(chunk);
  const onEnd = (): void => queue.send(EOF_BYTE);
  const raw = isRawTerminal(io.input);
  const onResize = (): void => {
    void handle.resize(terminalSize(io)).catch(() => undefined);
  };
  if (raw) {
    io.input.setRawMode?.(true);
    io.output.on?.("resize", onResize);
  } else {
    io.input.on("end", onEnd);
  }
  io.input.on("data", onData);
  io.input.resume();
  return () => {
    io.input.off("data", onData);
    io.input.off("end", onEnd);
    io.output.off?.("resize", onResize);
    if (raw) {
      io.input.setRawMode?.(false);
    }
    io.input.pause();
  };
}

async function exitCodeOf(handle: RemotePtyHandle): Promise<number> {
  try {
    return (await handle.wait()).exitCode;
  } catch (error) {
    if (error instanceof CommandExitError) {
      return error.exitCode;
    }
    if (error instanceof TimeoutError) {
      return EXIT_TIMEOUT;
    }
    throw error;
  }
}

/**
 * Abre la PTY (sin plazo de servidor), reenvía la entrada y devuelve el
 * código de salida del shell: el `exitCode` de un `CommandExitError` y 124 si
 * el agente la cierra por plazo.
 */
export async function runTerminal(
  sandbox: RemoteSandbox,
  io: InteractiveIo,
  options: TerminalOptions = {},
): Promise<number> {
  const handle = await sandbox.pty.create({
    size: terminalSize(io),
    user: options.user,
    cwd: options.cwd,
    envs: options.envs,
    onData: (chunk) => io.output.write(chunk),
    timeoutMs: NO_SERVER_TIMEOUT_MS,
  });
  const queue = new InputQueue(handle);
  const restore = startInput(io, handle, queue);
  const killOnAbort = (): void => {
    void handle.kill().catch(() => undefined);
  };
  options.signal?.addEventListener("abort", killOnAbort, { once: true });
  if (options.signal?.aborted === true) {
    killOnAbort();
  }
  try {
    return await exitCodeOf(handle);
  } finally {
    options.signal?.removeEventListener("abort", killOnAbort);
    restore();
  }
}
