#!/usr/bin/env node

/**
 * El ejecutable `rayito-mcp` del paquete npm (`bin`): conecta `process` con
 * `runMcpCli`. Es la única pieza del servidor MCP que toca `process`, y
 * `index` nunca la importa: `sideEffects: false` se mantiene. Con stdio, el
 * fin de `stdin` o SIGINT/SIGTERM paran el servidor y terminan el sandbox.
 */

import { userArguments } from "../process-arguments.js";
import { runMcpCli } from "./cli.js";

const SHUTDOWN_SIGNALS = ["SIGINT", "SIGTERM"] as const;

const shutdown = new AbortController();
for (const signal of SHUTDOWN_SIGNALS) {
  process.once(signal, () => shutdown.abort());
}
const code = await runMcpCli(userArguments(), {
  env: process.env,
  stdout: process.stdout,
  stderr: process.stderr,
  stdin: process.stdin,
  shutdown: shutdown.signal,
});
process.exit(code);
