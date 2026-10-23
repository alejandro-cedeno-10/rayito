#!/usr/bin/env node
/**
 * El ejecutable `rayito` del paquete npm (`bin`): conecta la terminal real,
 * Ctrl-C y la sesión de AWS con `runCli`. Es la única pieza de la CLI que
 * toca `process`, y `index` nunca la importa: `sideEffects: false` se
 * mantiene. Ctrl-C cancela la invocación (los bucles como `metrics --follow`
 * paran limpiamente) y, si el comando no acaba en `INTERRUPT_GRACE_MS`, sale
 * con 130. Una salida cerrada por el lector (`| head`) termina en silencio
 * (`closed-pipe.ts`).
 */

import { createInterface } from "node:readline";
import { userArguments } from "../process-arguments.js";
import { createAwsServices } from "./aws-services.js";
import { exitOnClosedPipe } from "./closed-pipe.js";
import type { Terminal } from "./console.js";
import { EXIT_INTERRUPTED } from "./exit-codes.js";
import { openUrl } from "./open-url.js";
import { runCli } from "./run.js";

const SIGINT = "SIGINT";
const INTERRUPT_GRACE_MS = 2000;

function sleepUnlessAborted(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(done, ms);
    signal.addEventListener("abort", done, { once: true });
    function done(): void {
      clearTimeout(timer);
      signal.removeEventListener("abort", done);
      resolve();
    }
  });
}

function realTerminal(): Terminal {
  let lines: AsyncIterator<string> | undefined;
  return {
    stdout: process.stdout,
    stderr: process.stderr,
    stdin: process.stdin,
    async readLine(prompt) {
      process.stdout.write(prompt);
      lines ??= createInterface({ input: process.stdin })[Symbol.asyncIterator]();
      const next = await lines.next();
      return next.done === true ? undefined : next.value;
    },
  };
}

function flush(stream: NodeJS.WriteStream): Promise<void> {
  return new Promise((resolve) => stream.write("", () => resolve()));
}

async function main(): Promise<void> {
  for (const stream of [process.stdout, process.stderr]) {
    exitOnClosedPipe(stream, (code) => process.exit(code));
  }
  const interrupt = new AbortController();
  process.once(SIGINT, () => {
    interrupt.abort();
    setTimeout(() => process.exit(EXIT_INTERRUPTED), INTERRUPT_GRACE_MS).unref();
  });
  const code = await runCli(userArguments(), {
    terminal: realTerminal(),
    env: process.env,
    now: () => new Date(),
    sleep: sleepUnlessAborted,
    signal: interrupt.signal,
    createServices: createAwsServices,
    openUrl,
  });
  await Promise.all([flush(process.stdout), flush(process.stderr)]);
  process.exit(code);
}

await main();
