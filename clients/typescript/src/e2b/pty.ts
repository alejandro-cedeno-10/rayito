/**
 * `sandbox.pty` de E2B JS 2.51 por composición sobre el `Pty` nativo:
 * `create({ cols, rows, onData, ... })` en vez de `size: { cols, rows }`, y
 * `connect(pid, { onData, ... })`. Devuelve el `PtyHandle` nativo.
 */

import { errorMessage } from "../errors.js";
import type { RequestOptions } from "../sandbox/commands.js";
import type { Pty as NativePty, PtyHandle } from "../sandbox/pty.js";
import { settleAsyncCallback } from "./compat.js";

export type PtyOutputCallback = (data: Uint8Array) => void | Promise<void>;

export interface PtyCreateOpts {
  readonly cols: number;
  readonly rows: number;
  readonly onData: PtyOutputCallback;
  readonly user?: string | undefined;
  readonly cwd?: string | undefined;
  readonly envs?: Readonly<Record<string, string>> | undefined;
  /** Timeout del servidor en ms (60 000 por defecto; `0` = sin límite). */
  readonly timeoutMs?: number | undefined;
  readonly requestTimeoutMs?: number | undefined;
  readonly signal?: AbortSignal | undefined;
}

export interface PtyConnectOpts {
  readonly onData: PtyOutputCallback;
  readonly timeoutMs?: number | undefined;
  readonly requestTimeoutMs?: number | undefined;
  readonly signal?: AbortSignal | undefined;
}

export interface PtySize {
  readonly cols: number;
  readonly rows: number;
}

export class Pty {
  readonly native: NativePty;

  constructor(native: NativePty) {
    this.native = native;
  }

  create(opts: PtyCreateOpts): Promise<PtyHandle> {
    return this.native.create({
      size: { cols: opts.cols, rows: opts.rows },
      onData: this.#onData(opts.onData),
      user: opts.user,
      cwd: opts.cwd,
      envs: opts.envs,
      timeoutMs: opts.timeoutMs,
      requestTimeoutMs: opts.requestTimeoutMs,
      signal: opts.signal,
    });
  }

  /**
   * Se re-engancha a una PTY viva con `fromSeq: 0`, que entrega sólo la
   * salida nueva (no reenvía lo ya emitido), como `pty.connect` de Python.
   */
  connect(pid: number, opts: PtyConnectOpts): Promise<PtyHandle> {
    return this.native.connect(pid, {
      fromSeq: 0,
      onData: this.#onData(opts.onData),
      timeoutMs: opts.timeoutMs,
      requestTimeoutMs: opts.requestTimeoutMs,
      signal: opts.signal,
    });
  }

  sendInput(pid: number, data: string | Uint8Array, opts: RequestOptions = {}): Promise<void> {
    return this.native.sendInput(pid, data, opts);
  }

  resize(pid: number, size: PtySize, opts: RequestOptions = {}): Promise<void> {
    return this.native.resize(pid, size, opts);
  }

  kill(pid: number, opts: RequestOptions = {}): Promise<boolean> {
    return this.native.kill(pid, opts);
  }

  /** Un `onData` asíncrono que rechaza se registra como aviso y la PTY sigue. */
  #onData(onData: PtyOutputCallback | undefined): ((data: Uint8Array) => void) | undefined {
    if (onData === undefined) {
      return undefined;
    }
    return settleAsyncCallback(onData, (error) =>
      this.native.core.logger?.warn?.("onData rechazó su promesa; la PTY sigue", {
        reason: errorMessage(error),
      }),
    );
  }
}
