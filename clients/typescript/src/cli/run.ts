/**
 * Ejecuta una invocación de la CLI de principio a fin y devuelve el código
 * de salida. No toca `process`: la terminal, el entorno, el reloj y la sesión
 * de AWS llegan por `CliRuntime`, así que los tests corren sin AWS ni
 * procesos.
 */

import type { Logger } from "../logger.js";
import { VERSION } from "../version.js";
import { SdkAgentProjectTools } from "./agent-project-adapter.js";
import { SdkAgentCoreTools } from "./agentcore-adapter.js";
import type { CommandContext } from "./command.js";
import { CliConsole, type Terminal } from "./console.js";
import { EXIT_OK, EXIT_USAGE } from "./exit-codes.js";
import { translateFailure, UsageError } from "./failures.js";
import { helpFor } from "./help.js";
import type { AgentCoreTools, AgentProjectTools } from "./ops-ports.js";
import { type CommandTree, parseInvocation } from "./parse.js";
import { COMMAND_TREE } from "./registry.js";
import type { CliServices, ServicesFactory } from "./services.js";

export const PROGRAM = "rayito";
export const RUNTIME_NAME = "node";
const USAGE_HINT = "Prueba `rayito --help` o `rayito <comando> --help`.";

export interface CliRuntime {
  readonly terminal: Terminal;
  readonly env: Readonly<Record<string, string | undefined>>;
  readonly now: () => Date;
  /** Duerme `ms` o hasta que `signal` se cancele. */
  readonly sleep: (ms: number, signal: AbortSignal) => Promise<void>;
  readonly signal: AbortSignal;
  readonly createServices: ServicesFactory;
  /** Abre una URL en el navegador; sin él, `rayito dashboard --open` no hace nada. */
  readonly openUrl?: ((url: string) => Promise<void>) | undefined;
  /** Sin él, `rayito agent` usa el SDK; los tests lo sustituyen por dobles. */
  readonly agentProjects?: AgentProjectTools | undefined;
  /** Sin él, `rayito agent ... --target agentcore` usa el SDK; los tests lo sustituyen por dobles. */
  readonly agentcore?: AgentCoreTools | undefined;
}

/** El logger de `--verbose`: INFO y más, a stderr, sólo con ids, estados y códigos. */
function stderrLogger(console: CliConsole): Logger {
  const write =
    (level: string) =>
    (message: string, fields?: Readonly<Record<string, unknown>>): void => {
      const detail = fields === undefined ? "" : ` ${JSON.stringify(fields)}`;
      console.echo(`${level}:rayito:${message}${detail}`, { err: true });
    };
  return { info: write("INFO"), warn: write("WARNING"), error: write("ERROR") };
}

/** El logger sin `--verbose`: sólo avisos y errores, a stderr, con el mensaje
 * y los valores de sus campos (`receta de la flota: recipe_renewal_ignored`),
 * como el manejador de último recurso de `logging` en la CLI de Python. */
export function warningsLogger(console: CliConsole): Logger {
  const write = (message: string, fields?: Readonly<Record<string, unknown>>): void => {
    const values = fields === undefined ? [] : Object.values(fields).map(String);
    console.echo(values.length === 0 ? message : `${message}: ${values.join(" ")}`, { err: true });
  };
  return { warn: write, error: write };
}

export function versionLine(): string {
  return `${PROGRAM} ${VERSION} (${RUNTIME_NAME})`;
}

export async function runCli(
  argv: readonly string[],
  runtime: CliRuntime,
  tree: CommandTree = COMMAND_TREE,
): Promise<number> {
  const console = new CliConsole(runtime.terminal);
  let verbose = false;
  try {
    const invocation = parseInvocation(argv, tree, runtime.env);
    if (invocation.kind === "version") {
      console.echo(versionLine());
      return EXIT_OK;
    }
    if (invocation.kind === "help") {
      console.echo(helpFor(invocation.path, tree));
      return invocation.bare ? EXIT_USAGE : EXIT_OK;
    }
    const { command, values, globals } = invocation;
    verbose = globals.verbose;
    let services: Promise<CliServices> | undefined;
    const logger = globals.verbose ? stderrLogger(console) : warningsLogger(console);
    const servicesOf = (): Promise<CliServices> => {
      services ??= runtime.createServices({
        profile: globals.profile,
        region: globals.region,
        env: runtime.env,
        logger,
      });
      return services;
    };
    const context: CommandContext = {
      values,
      globals,
      console,
      env: runtime.env,
      now: runtime.now,
      sleep: (ms) => runtime.sleep(ms, runtime.signal),
      signal: runtime.signal,
      openUrl: runtime.openUrl ?? (async () => undefined),
      services: servicesOf,
      agentProjects:
        runtime.agentProjects ?? new SdkAgentProjectTools(servicesOf, undefined, logger),
      agentcore: runtime.agentcore ?? new SdkAgentCoreTools(servicesOf),
    };
    return (await command.run(context)) ?? EXIT_OK;
  } catch (error) {
    const failure = translateFailure(error, verbose);
    console.echo(failure.line, { err: true });
    if (error instanceof UsageError) {
      console.echo(USAGE_HINT, { err: true });
    }
    return failure.code;
  }
}
