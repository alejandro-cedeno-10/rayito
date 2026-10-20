/**
 * Salida de la CLI: líneas con los caracteres de control visibles hacia una
 * terminal, un documento JSON, tablas y la confirmación. Espejo de
 * `rayito.cli._console`. El puerto `Terminal` es lo único que toca
 * `process`: los tests lo sustituyen por buffers.
 */

import { randomUUID } from "node:crypto";

import { JSON_INDENT } from "../units.js";
import { pythonDatetimeText, pythonFloatText } from "./format.js";
import type { InteractiveIo, TerminalInput } from "./interactive-terminal.js";
import { renderTable } from "./table-layout.js";

const C0_END = 0x1f;
const TAB = 0x09;
const LINE_FEED = 0x0a;
const DELETE = 0x7f;
const C1_END = 0x9f;
const HEX_RADIX = 16;
const HEX_WIDTH = 2;
export const MIN_TABLE_WIDTH = 120;
const YES_ANSWERS: ReadonlySet<string> = new Set(["y", "yes"]);
const NO_ANSWERS: ReadonlySet<string> = new Set(["n", "no", ""]);
const INVALID_ANSWER_MESSAGE = "Error: respuesta no válida, escribe y o n";

/** Lo que la CLI usa de un flujo de salida. */
export interface OutputStream {
  write(text: string | Uint8Array): unknown;
  readonly isTTY?: boolean | undefined;
  /** El ancho de la terminal en columnas, si se conoce. */
  readonly columns?: number | undefined;
  /** El alto de la terminal en filas, si se conoce. */
  readonly rows?: number | undefined;
  on?(event: "resize", listener: () => void): unknown;
  off?(event: "resize", listener: () => void): unknown;
}

/** El puerto con la terminal: dos flujos y una línea de entrada. */
export interface Terminal {
  readonly stdout: OutputStream;
  readonly stderr: OutputStream;
  /** Escribe `prompt` en stdout y lee una línea; `undefined` si no hay más entrada. */
  readLine(prompt: string): Promise<string | undefined>;
  /** La entrada cruda, para las terminales interactivas (`connect`, `create`). */
  readonly stdin?: TerminalInput | undefined;
}

/** Claves de un documento que Python guarda como `float`: `0` se escribe `0.0`. */
export type FloatKeys = ReadonlySet<string>;

/**
 * El replacer de `json.dumps(..., default=str)`: un `Date` sale como
 * `str(datetime)` y un número bajo una clave `float` lleva su parte decimal
 * (`pythonFloatText`). `JSON.stringify` no puede escribir `0.0`, así que el
 * número se marca con `marker` y se sustituye por su texto al terminar.
 */
function pythonJsonReplacer(floatKeys: FloatKeys | undefined, marker: string) {
  return function replace(this: Record<string, unknown>, key: string, value: unknown): unknown {
    const original = this[key];
    if (original instanceof Date) {
      return pythonDatetimeText(original);
    }
    if (floatKeys?.has(key) && typeof value === "number") {
      return `${marker}${pythonFloatText(value)}${marker}`;
    }
    return value;
  };
}

function pythonJson(document: unknown, floatKeys?: FloatKeys, indent?: number): string {
  const marker = `${randomUUID()}:`;
  const text = JSON.stringify(document, pythonJsonReplacer(floatKeys, marker), indent);
  return text.replaceAll(`"${marker}`, "").replaceAll(`${marker}"`, "");
}

function isTerminalControl(code: number): boolean {
  if (code === TAB || code === LINE_FEED) {
    return false;
  }
  return code <= C0_END || (code >= DELETE && code <= C1_END);
}

/**
 * `text` con cada carácter de control (C0 salvo tabulador y salto de línea,
 * DEL y C1) escrito como `\xNN` visible y sin efecto: de ahí salen ESC, CSI y
 * OSC, es decir, mover el cursor, borrar líneas o escribir en el portapapeles.
 */
export function visibleControls(text: string): string {
  let visible = "";
  for (const character of text) {
    const code = character.codePointAt(0) ?? 0;
    visible += isTerminalControl(code)
      ? `\\x${code.toString(HEX_RADIX).padStart(HEX_WIDTH, "0")}`
      : character;
  }
  return visible;
}

/** El usuario cortó la orden (entrada agotada en una confirmación): `Aborted.` y salida 1. */
export class AbortedError extends Error {
  constructor() {
    super("Aborted.");
    this.name = "AbortedError";
  }
}

export class CliConsole {
  readonly #terminal: Terminal;

  constructor(terminal: Terminal) {
    this.#terminal = terminal;
  }

  /**
   * Una línea hacia stdout o stderr. Hacia una terminal los caracteres de
   * control salen visibles: un log de CloudWatch o de un build lo puede
   * escribir código no fiable. Redirigido a un fichero o a otra orden el
   * texto sale tal cual.
   */
  echo(message = "", options: { readonly err?: boolean } = {}): void {
    const stream = options.err === true ? this.#terminal.stderr : this.#terminal.stdout;
    const text = stream.isTTY === true ? visibleControls(message) : message;
    stream.write(`${text}\n`);
  }

  /** Un único documento JSON por stdout. */
  emitJson(document: unknown, floatKeys?: FloatKeys): void {
    this.echo(pythonJson(document, floatKeys, JSON_INDENT));
  }

  /** Un documento JSON en una sola línea: los flujos de `--follow`. */
  emitJsonLine(document: unknown, floatKeys?: FloatKeys): void {
    this.echo(pythonJson(document, floatKeys));
  }

  /** Una tabla sin bordes; nunca más estrecha que `MIN_TABLE_WIDTH`, como la de Python. */
  table(columns: readonly string[], rows: Iterable<readonly unknown[]>): void {
    const width = Math.max(this.#terminal.stdout.columns ?? MIN_TABLE_WIDTH, MIN_TABLE_WIDTH);
    this.echo(renderTable(columns, rows, width));
  }

  /** Pregunta `message` con `[y/N]`; sin entrada lanza `AbortedError`. */
  async confirm(message: string): Promise<boolean> {
    for (;;) {
      const answer = await this.#terminal.readLine(`${message} [y/N]: `);
      if (answer === undefined) {
        throw new AbortedError();
      }
      const normalised = answer.trim().toLowerCase();
      if (YES_ANSWERS.has(normalised)) {
        return true;
      }
      if (NO_ANSWERS.has(normalised)) {
        return false;
      }
      this.echo(INVALID_ANSWER_MESSAGE, { err: true });
    }
  }

  /** `true` si la entrada es una terminal: sólo entonces se puede preguntar. */
  get canPrompt(): boolean {
    return this.#terminal.stdin?.isTTY === true;
  }

  /** La terminal local como entrada y salida crudas; `undefined` si la terminal no tiene entrada cruda. */
  get interactive(): InteractiveIo | undefined {
    const input = this.#terminal.stdin;
    return input === undefined ? undefined : { input, output: this.#terminal.stdout };
  }

  /**
   * Texto sin salto de línea (los trozos de un stream). Hacia una terminal
   * los caracteres de control salen visibles, como en `echo`.
   */
  write(text: string): void {
    const stream = this.#terminal.stdout;
    stream.write(stream.isTTY === true ? visibleControls(text) : text);
  }

  /** Escribe tal cual, sin salto ni filtrado: la salida del sandbox en `exec`. */
  writeRaw(stream: "stdout" | "stderr", text: string): void {
    this.#terminal[stream].write(text);
  }
}
