/**
 * Helpers de `readyCmd` para `setStartCmd` (E2B `readycmd.py`, puros):
 * compilan a una cadena de shell que `rayd` ejecuta con `/bin/sh -c` y
 * comprueban el código de salida, 0 = listo. Espejo de
 * `rayito._templates._ready_cmds`: cada argumento va entrecomillado con
 * `shellQuote` (el mismo algoritmo que `shlex.quote` de Python), y
 * `testdata/templates/dockerfile-cases.json` fija la salida de ambos.
 */

import type { ReadyPoll } from "./instructions.js";

/** Cadencia de sondeo por defecto de `readyCmd`. */
export const DEFAULT_READY_POLL_INTERVAL_SECONDS = 0.5;
/** Plazo por defecto antes de que `rayd` considere el `readyCmd` fallido. */
export const DEFAULT_READY_TIMEOUT_SECONDS = 60;

export class ReadyCommand {
  readonly cmd: string;
  readonly poll: ReadyPoll;

  constructor(cmd: string, poll: ReadyPoll) {
    this.cmd = cmd;
    this.poll = poll;
  }

  /** Sustituye `poll.timeoutSeconds` por `seconds` (debe ser > 0). */
  timeout(seconds: number): ReadyCommand {
    if (seconds <= 0) {
      throw new RangeError(`timeout debe ser > 0, se dio ${seconds}`);
    }
    return new ReadyCommand(this.cmd, {
      intervalSeconds: this.poll.intervalSeconds,
      timeoutSeconds: seconds,
    });
  }
}

/** Caracteres que `shlex.quote` deja sin comillas (`[\w@%+=:,./-]`, ASCII). */
const SHELL_SAFE_PATTERN = /^[A-Za-z0-9_@%+=:,./-]+$/;

/** `shlex.quote` de Python: `''` para vacío, el valor tal cual si sólo usa
 * caracteres seguros, si no `'...'` con cada `'` como `'"'"'`. */
export function shellQuote(value: string): string {
  if (value === "") {
    return "''";
  }
  if (SHELL_SAFE_PATTERN.test(value)) {
    return value;
  }
  return `'${value.split("'").join(`'"'"'`)}'`;
}

function readyCommand(cmd: string): ReadyCommand {
  return new ReadyCommand(cmd, {
    intervalSeconds: DEFAULT_READY_POLL_INTERVAL_SECONDS,
    timeoutSeconds: DEFAULT_READY_TIMEOUT_SECONDS,
  });
}

/** Listo cuando algo escucha en `port` (TCP, loopback). */
export function waitForPort(port: number): ReadyCommand {
  return readyCommand(`cat < /dev/null > /dev/tcp/127.0.0.1/${port}`);
}

/** Listo cuando `url` responde `status` (curl, sólo cabeceras). */
export function waitForUrl(url: string, status = 200): ReadyCommand {
  return readyCommand(
    `test "$(curl -s -o /dev/null -w '%{http_code}' ${shellQuote(url)})" = '${status}'`,
  );
}

/** Listo cuando hay un proceso cuyo nombre contiene `name` (`pgrep -f`). */
export function waitForProcess(name: string): ReadyCommand {
  return readyCommand(`pgrep -f ${shellQuote(name)} > /dev/null`);
}

/** Listo cuando `path` existe. */
export function waitForFile(path: string): ReadyCommand {
  return readyCommand(`test -e ${shellQuote(path)}`);
}
