/**
 * `runMcpCli`: lo que hace `rayito-mcp` desde los argumentos hasta el código
 * de salida, con el entorno, los flujos y la señal de parada inyectados (el
 * ejecutable `main.ts` los toma de `process`). Nada se escribe en `stdout`
 * salvo la ayuda y el protocolo; el log va a `stderr`.
 */

import { EXIT_FAILURE, EXIT_OK, EXIT_USAGE } from "../cli/exit-codes.js";
import { InvalidArgumentError } from "../errors.js";
import type { SandboxEnvironment } from "./lease.js";
import { createStderrLogger, type LineSink } from "./logging.js";
import { HELP_TEXT, McpUsageError, PROGRAM, parseCommandLine, type RunOptions } from "./options.js";
import { loadMcpPeers, MCP_NPX_COMMAND } from "./peers.js";
import { type RunningServer, startMcp } from "./run.js";
import { createMcpApp } from "./server.js";
import { McpSettingsError, parseMcpSettings } from "./settings.js";

export const MISSING_PEER_MESSAGE =
  `${PROGRAM}: faltan los paquetes opcionales del servidor MCP; ` +
  `arráncalo con: ${MCP_NPX_COMMAND}`;

export interface McpCliDeps {
  readonly env: Readonly<Record<string, string | undefined>>;
  readonly stdout: LineSink;
  readonly stderr: LineSink;
  /** Se aborta para parar el servidor (SIGINT, SIGTERM). */
  readonly shutdown: AbortSignal;
  /** Con stdio, su fin para el servidor; el modo HTTP lo ignora. */
  readonly stdin?: NodeJS.EventEmitter | undefined;
  /** Sólo los tests inyectan un plano de control y un transporte falsos. */
  readonly environment?: SandboxEnvironment | undefined;
}

function usageFailure(deps: McpCliDeps, message: string): number {
  deps.stderr.write(`${PROGRAM}: ${message}\n`);
  return EXIT_USAGE;
}

function stopSignal(options: RunOptions, deps: McpCliDeps): AbortSignal {
  if (options.http || deps.stdin === undefined) {
    return deps.shutdown;
  }
  const stop = new AbortController();
  const forward = (): void => stop.abort();
  deps.shutdown.addEventListener("abort", forward, { once: true });
  deps.stdin.once("end", forward);
  return stop.signal;
}

function untilAborted(signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) {
      resolve();
      return;
    }
    signal.addEventListener("abort", () => resolve(), { once: true });
  });
}

export async function runMcpCli(argv: readonly string[], deps: McpCliDeps): Promise<number> {
  let command: ReturnType<typeof parseCommandLine>;
  try {
    command = parseCommandLine(argv);
  } catch (error) {
    if (error instanceof McpUsageError) {
      return usageFailure(deps, error.message);
    }
    throw error;
  }
  if (command.kind === "help") {
    deps.stdout.write(HELP_TEXT);
    return EXIT_OK;
  }
  let settings: ReturnType<typeof parseMcpSettings>;
  try {
    settings = parseMcpSettings(deps.env);
  } catch (error) {
    if (error instanceof McpSettingsError) {
      return usageFailure(deps, error.message);
    }
    throw error;
  }
  let peers: Awaited<ReturnType<typeof loadMcpPeers>>;
  try {
    peers = await loadMcpPeers();
  } catch (error) {
    if (error instanceof InvalidArgumentError) {
      deps.stderr.write(`${MISSING_PEER_MESSAGE}\n`);
      return EXIT_USAGE;
    }
    throw error;
  }
  const logger = createStderrLogger(settings.logLevel, deps.stderr);
  const app = createMcpApp(peers, settings, { logger, ...deps.environment });
  const stop = stopSignal(command.options, deps);
  let running: RunningServer;
  try {
    running = await startMcp(command.options, { peers, app, logger });
  } catch (error) {
    await app.lease.close();
    deps.stderr.write(
      `${PROGRAM}: no se pudo arrancar: ${error instanceof Error ? error.message : String(error)}\n`,
    );
    return EXIT_FAILURE;
  }
  await untilAborted(stop);
  await running.stop();
  return EXIT_OK;
}
