/**
 * Lectura con búfer y escritura con contrapresión sobre un socket de
 * `node:net` o `node:tls` (adaptador de E/S del proxy). Espejo de lo que
 * `asyncio.StreamReader` y `StreamWriter` dan a `rayito.cli._proxy`:
 * `readUntil`, `readExactly` y `read` esperan los bytes, y un socket que se
 * cierra antes de tiempo es un error propio, nunca un cuelgue.
 */

import type { Duplex } from "node:stream";
import { MS_PER_SECOND } from "../units.js";

/** Lo que se reanuda al leer: por encima de esto el socket se pausa hasta que alguien consuma. */
const READ_BUFFER_HIGH_WATER_BYTES = 256 * 1024;

/** El otro lado cerró antes de entregar todo lo que se esperaba. */
export class StreamClosedError extends Error {
  constructor() {
    super("conexión cerrada antes de tiempo");
    this.name = "StreamClosedError";
  }
}

/** No apareció el delimitador dentro del tope de bytes. */
export class StreamLimitError extends Error {
  constructor() {
    super("delimitador fuera del tope de bytes");
    this.name = "StreamLimitError";
  }
}

/** Una espera acotada que venció. */
export class StreamTimeoutError extends Error {
  constructor() {
    super("plazo vencido");
    this.name = "StreamTimeoutError";
  }
}

/** Resuelve con `promise` o rechaza con `StreamTimeoutError` pasados `seconds`. */
export function withTimeoutSeconds<T>(promise: Promise<T>, seconds: number): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => reject(new StreamTimeoutError()), seconds * MS_PER_SECOND);
    promise.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (error: unknown) => {
        clearTimeout(timer);
        reject(error);
      },
    );
  });
}

/** Un error de red con código (`ECONNRESET`, `EPIPE`, un fallo TLS) en vez de un fallo de programación. */
export function isNetworkError(error: unknown): boolean {
  return error instanceof Error && typeof (error as { code?: unknown }).code === "string";
}

export class SocketReader {
  readonly #socket: Duplex;
  #buffer: Buffer = Buffer.alloc(0);
  #finished = false;
  #failure: Error | undefined;
  #wake: (() => void) | undefined;

  constructor(socket: Duplex) {
    this.#socket = socket;
    socket.on("data", (chunk: Buffer) => this.#append(chunk));
    socket.on("end", () => this.#finish());
    socket.on("close", () => this.#finish());
    socket.on("error", (error: Error) => {
      this.#failure = error;
      this.#finish();
    });
  }

  #append(chunk: Buffer): void {
    this.#buffer = Buffer.concat([this.#buffer, chunk]);
    if (this.#buffer.length >= READ_BUFFER_HIGH_WATER_BYTES) {
      this.#socket.pause();
    }
    this.#notify();
  }

  #finish(): void {
    this.#finished = true;
    this.#notify();
  }

  #notify(): void {
    const wake = this.#wake;
    this.#wake = undefined;
    wake?.();
  }

  #waitForData(): Promise<void> {
    return new Promise<void>((resolve) => {
      this.#wake = resolve;
    });
  }

  async #more(): Promise<void> {
    if (this.#failure !== undefined) {
      throw this.#failure;
    }
    if (this.#finished) {
      throw new StreamClosedError();
    }
    await this.#waitForData();
  }

  #take(length: number): Buffer {
    const taken = this.#buffer.subarray(0, length);
    this.#buffer = this.#buffer.subarray(length);
    this.#socket.resume();
    return taken;
  }

  /** Hasta `delimiter` inclusive; `StreamLimitError` si no aparece en `maxBytes`. */
  async readUntil(delimiter: string, maxBytes: number): Promise<Buffer> {
    let searchFrom = 0;
    for (;;) {
      const found = this.#buffer.indexOf(delimiter, searchFrom, "latin1");
      if (found >= 0 && found + delimiter.length <= maxBytes) {
        return this.#take(found + delimiter.length);
      }
      if (found >= 0 || this.#buffer.length >= maxBytes) {
        throw new StreamLimitError();
      }
      searchFrom = Math.max(0, this.#buffer.length - delimiter.length + 1);
      await this.#more();
    }
  }

  /** Exactamente `length` bytes; `StreamClosedError` si el otro lado cierra antes. */
  async readExactly(length: number): Promise<Buffer> {
    while (this.#buffer.length < length) {
      await this.#more();
    }
    return this.#take(length);
  }

  /** Hasta `maxBytes` de lo que haya; vacío cuando el otro lado cerró y no queda nada. */
  async read(maxBytes: number): Promise<Buffer> {
    while (this.#buffer.length === 0) {
      if (this.#failure !== undefined) {
        throw this.#failure;
      }
      if (this.#finished) {
        return Buffer.alloc(0);
      }
      await this.#waitForData();
    }
    return this.#take(Math.min(maxBytes, this.#buffer.length));
  }
}

/** Escribe `data` y espera a que el sistema lo acepte (contrapresión); rechaza si el socket cayó. */
export function writeAll(socket: Duplex, data: string | Uint8Array): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    if (socket.destroyed || !socket.writable) {
      reject(new StreamClosedError());
      return;
    }
    socket.write(data, (error) =>
      error === null || error === undefined ? resolve() : reject(error),
    );
  });
}

/** Cierra el lado de escritura (`FIN`): el otro lado puede seguir enviando. */
export function endWriting(socket: Duplex): void {
  if (!socket.destroyed && socket.writable) {
    socket.end();
  }
}

/** Cierra del todo tras vaciar lo pendiente; nunca lanza ni espera más de lo que tarde en vaciar. */
export function closeSocket(socket: Duplex): Promise<void> {
  return new Promise<void>((resolve) => {
    if (socket.destroyed) {
      resolve();
      return;
    }
    socket.once("close", () => resolve());
    socket.on("error", () => undefined);
    if (socket.writableFinished) {
      socket.destroy();
    } else if (socket.writableEnded) {
      socket.once("finish", () => socket.destroy());
    } else {
      socket.end(() => socket.destroy());
    }
  });
}
