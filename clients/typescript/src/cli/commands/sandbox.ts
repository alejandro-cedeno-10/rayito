/**
 * `rayito sandbox list | info | kill | logs` sobre el plano de control del
 * SDK, y `create | exec | metrics` sobre el agente del sandbox. `connect` y
 * `proxy` están en `sandbox-interactive.ts`.
 *
 * Los tres últimos usan el access token del sandbox, que llega sólo por
 * `--token-file` o `RAYITO_ACCESS_TOKEN` (nunca por argv) y nunca se
 * imprime. Códigos de salida: el del comando remoto en `exec` (124 si el
 * agente lo cortó por plazo); 1 si la operación falla; 2 para uso o entorno
 * (sin token, par `K=V` mal formado, fichero de token existente).
 */

import {
  CommandExitError,
  SandboxError,
  SandboxNotFoundError,
  TimeoutError,
} from "../../errors.js";
import { MICROVM_STATES, TERMINAL_STATES } from "../../limits.js";
import { generateAccessToken } from "../../payload.js";
import type { CommandHandle } from "../../sandbox/commands.js";
import { DEFAULT_TIMEOUT_MS } from "../../sandbox/launch.js";
import {
  DEFAULT_EVENT_LIMIT,
  defaultLogGroup,
  expectedStreamName,
  LogsNotFoundError,
  parseSince,
} from "../../sandbox-logs/domain.js";
import { FALLBACK_WARNING, findStreams, readEvents } from "../../sandbox-logs/service.js";
import { stripTrailing } from "../../strings.js";
import { MS_PER_SECOND, NO_SERVER_TIMEOUT_MS } from "../../units.js";
import {
  argument,
  type CommandContext,
  defineCommand,
  defineGroup,
  flag,
  option,
} from "../command.js";
import { AbortedError, type FloatKeys } from "../console.js";
import { infoDocument, launchDocument, listRow, metricsDocument } from "../documents.js";
import { EXIT_FAILURE, EXIT_TIMEOUT } from "../exit-codes.js";
import { UsageError } from "../failures.js";
import { eventTimestamp, pythonDisplay, pythonFloatText } from "../format.js";
import { type InteractiveIo, runTerminal } from "../interactive-terminal.js";
import type { RemoteSandbox } from "../services.js";
import { shellJoin } from "../shell.js";
import {
  InvalidPairError,
  parsePairs,
  resolveToken,
  TOKEN_ENV_VAR,
  TokenFileError,
  writeTokenFile,
} from "../tokens.js";

const LIST_COLUMNS = [
  "sandbox_id",
  "state",
  "template",
  "template_version",
  "started_at",
  "age",
] as const;
/** `cpu_used_pct` es un `float` en Python: lleva siempre su parte decimal. */
const METRICS_FLOAT_KEYS: FloatKeys = new Set(["cpu_used_pct"]);
const METRICS_COLUMNS = [
  "timestamp",
  "cpu_used_pct",
  "cpu_count",
  "mem_used",
  "mem_total",
  "disk_used",
  "disk_total",
] as const;
export const DEFAULT_CREATE_TIMEOUT_SECONDS = DEFAULT_TIMEOUT_MS / MS_PER_SECOND;
export const DEFAULT_METRICS_INTERVAL_SECONDS = 5.0;
const MISSING_TOKEN_MESSAGE = `falta el access token: --token-file o ${TOKEN_ENV_VAR}`;
const DETACH_NEEDS_TOKEN_FILE_MESSAGE = `sin ${TOKEN_ENV_VAR}, --detach necesita --token-file para poder volver a conectarte`;
const COMMAND_TIMEOUT_MESSAGE = "timeout del comando";
const SANDBOX_GONE_MESSAGE = "el sandbox ya no existe";
const STREAM_ONLY_OUTPUT_BYTES = 0;
const NO_INTERACTIVE_TERMINAL_MESSAGE = "esta terminal no admite una sesión interactiva";

export const sandboxGroup = defineGroup({
  path: ["sandbox"],
  help: "MicroVMs: list, info, kill, logs, create, connect, proxy, exec, metrics.",
});

export const tokenFileOption = option(["--token-file"], {
  type: "path",
  help: `Fichero con el access token (gana a ${TOKEN_ENV_VAR}); nunca por argv.`,
});
export const envOption = option(["--env", "-e"], { multiple: true, help: "K=V; repetible." });
export const userOption = option(["--user", "-u"], { help: "Usuario del sandbox." });
export const cwdOption = option(["--cwd", "-c"], { help: "Directorio de trabajo." });

export function pairsOrUsage(
  values: readonly string[],
  optionName: string,
): Record<string, string> {
  try {
    return parsePairs(values, optionName);
  } catch (error) {
    if (error instanceof InvalidPairError) {
      throw new UsageError(error.message);
    }
    throw error;
  }
}

export function tokenOrUsage(context: CommandContext): string {
  let token: string | undefined;
  try {
    token = resolveToken(context.values.string("token_file"), context.env);
  } catch (error) {
    if (error instanceof TokenFileError) {
      throw new UsageError(error.message);
    }
    throw error;
  }
  if (token === undefined) {
    throw new UsageError(MISSING_TOKEN_MESSAGE);
  }
  return token;
}

/** La terminal local como entrada y salida crudas; sin ella no hay sesión interactiva. */
export function interactiveOrUsage(context: CommandContext): InteractiveIo {
  const io = context.console.interactive;
  if (io === undefined) {
    throw new UsageError(NO_INTERACTIVE_TERMINAL_MESSAGE);
  }
  return io;
}

function pythonList(values: readonly string[]): string {
  return `[${values.map((value) => `'${value}'`).join(", ")}]`;
}

function statesOrUsage(values: readonly string[]): readonly string[] | undefined {
  if (values.length === 0) {
    return undefined;
  }
  const states = values.map((value) => value.toUpperCase());
  const unknown = [...new Set(states)].filter((state) => !MICROVM_STATES.includes(state as never));
  if (unknown.length > 0) {
    throw new UsageError(
      `--state: estado desconocido ${pythonList(unknown.sort())}; válidos: ${pythonList(MICROVM_STATES)}`,
    );
  }
  return states;
}

export const listCommand = defineCommand({
  path: ["sandbox", "list"],
  help: "MicroVMs no terminados (sin sondear ningún endpoint salvo --metadata sin --index-table).",
  parameters: [
    option(["--template"], { help: "Nombre o ARN de la imagen." }),
    option(["--template-version"], { help: "Versión de la imagen." }),
    flag(["--all-states"], { help: "Incluye TERMINATING y TERMINATED." }),
    option(["--metadata"], {
      multiple: true,
      metavar: "K=V",
      help:
        "Filtra por metadatos (repetible). Sin --index-table sondea el Health de cada " +
        "sandbox RUNNING (O(n)).",
    }),
    option(["--state"], {
      multiple: true,
      help:
        "Estado a incluir (repetible, p. ej. suspended); con --metadata exige " +
        "--index-table para estados distintos de running.",
    }),
    option(["--index-table"], {
      help:
        "Tabla DynamoDB del índice de metadatos (opcional, infra/metadata-index.yaml): exige " +
        "--metadata; filtra también sandboxes en pausa sin despertarlos. Coste: " +
        "dynamodb:BatchGetItem por página. Apagado por defecto.",
    }),
  ],
  run: async (context) => {
    const { values } = context;
    const metadata = pairsOrUsage(values.strings("metadata"), "--metadata");
    const states = statesOrUsage(values.strings("state"));
    const indexTable = values.string("index_table");
    const allStates = values.flag("all_states");
    if (
      allStates &&
      (values.strings("metadata").length > 0 || states !== undefined || indexTable)
    ) {
      throw new UsageError("--all-states no se combina con --state, --metadata ni --index-table");
    }
    if (indexTable !== undefined && values.strings("metadata").length === 0) {
      throw new UsageError("--index-table sólo filtra junto a --metadata K=V");
    }
    const services = await context.services();
    const rows: Record<string, unknown>[] = [];
    const now = context.now();
    for await (const item of services.sandboxes.list({
      template: values.string("template"),
      templateVersion: values.string("template_version"),
      states: allStates ? MICROVM_STATES : states,
      metadata,
      indexTable,
    })) {
      rows.push(listRow(item, now));
    }
    if (context.globals.json) {
      context.console.emitJson(rows);
      return undefined;
    }
    context.console.table(
      LIST_COLUMNS,
      rows.map((row) => LIST_COLUMNS.map((column) => row[column])),
    );
    return undefined;
  },
});

export const infoCommand = defineCommand({
  path: ["sandbox", "info"],
  help: "`get-microvm` y, sobre un sandbox RUNNING, sus metadatos vía Health.",
  parameters: [
    argument("sandbox_id", { required: true, metavar: "ID", help: "Id del sandbox." }),
    flag(["--no-metadata"], { help: "No sondea Health para leer los metadatos." }),
  ],
  run: async (context) => {
    const services = await context.services();
    const info = await services.sandboxes.info(context.values.string("sandbox_id") ?? "", {
      readMetadata: !context.values.flag("no_metadata"),
    });
    const document = infoDocument(info);
    if (context.globals.json) {
      context.console.emitJson(document);
      return undefined;
    }
    for (const [key, value] of Object.entries(document)) {
      context.console.echo(`${key}: ${pythonDisplay(value)}`);
    }
    return undefined;
  },
});

interface KillOutcome {
  readonly sandboxId: string;
  readonly found: boolean;
}

function reportKills(context: CommandContext, outcomes: readonly KillOutcome[]): void {
  if (context.globals.json) {
    context.console.emitJson(
      outcomes.map(({ sandboxId, found }) => ({
        sandbox_id: sandboxId,
        outcome: found ? "terminated" : "not found",
      })),
    );
    return;
  }
  for (const { sandboxId, found } of outcomes) {
    context.console.echo(`${sandboxId} ${found ? "terminated" : "not found"}`);
  }
}

export const killCommand = defineCommand({
  path: ["sandbox", "kill"],
  help: "`terminate-microvm` por id, o de todos los vivos con --all.",
  parameters: [
    argument("sandbox_ids", { variadic: true, metavar: "ID", help: "Ids a terminar." }),
    flag(["--all"], { name: "all_sandboxes", help: "Termina todos los MicroVMs no terminados." }),
    option(["--template"], { help: "Con --all: sólo esta imagen." }),
    flag(["--yes", "-y"], { help: "Con --all: no pide confirmación." }),
  ],
  run: async (context) => {
    const { values } = context;
    let ids = [...values.strings("sandbox_ids")];
    const all = values.flag("all_sandboxes");
    if (all && ids.length > 0) {
      throw new UsageError("--all: --all y los IDs son excluyentes");
    }
    if (!all && ids.length === 0) {
      throw new UsageError("ID: pasa uno o más IDs, o --all");
    }
    const services = await context.services();
    if (all) {
      ids = [];
      for await (const item of services.sandboxes.list({
        template: values.string("template"),
        templateVersion: undefined,
        states: undefined,
        metadata: {},
        indexTable: undefined,
      })) {
        ids.push(item.sandboxId);
      }
      if (ids.length === 0) {
        reportKills(context, []);
        return undefined;
      }
      for (const id of ids) {
        context.console.echo(id, { err: context.globals.json });
      }
      if (
        !values.flag("yes") &&
        !(await context.console.confirm(`¿Terminar ${ids.length} MicroVMs?`))
      ) {
        throw new AbortedError();
      }
    }
    const outcomes: KillOutcome[] = [];
    for (const sandboxId of ids) {
      outcomes.push({ sandboxId, found: await killOne(services.sandboxes, sandboxId) });
    }
    reportKills(context, outcomes);
    return !all && !outcomes.every(({ found }) => found) ? EXIT_FAILURE : undefined;
  },
});

async function killOne(
  gateway: { kill(sandboxId: string): Promise<boolean> },
  sandboxId: string,
): Promise<boolean> {
  try {
    return await gateway.kill(sandboxId);
  } catch (error) {
    if (error instanceof SandboxNotFoundError) {
      return false;
    }
    throw error;
  }
}

export const logsCommand = defineCommand({
  path: ["sandbox", "logs"],
  help: "Eventos del stream de CloudWatch del sandbox (sólo con executionRoleArn).",
  parameters: [
    argument("sandbox_id", { required: true, metavar: "ID", help: "Id del sandbox." }),
    option(["--log-group"], { help: "Por defecto /rayito/<nombre de la imagen>." }),
    option(["--limit"], { type: "int", default: DEFAULT_EVENT_LIMIT, help: "Máximo de eventos." }),
    option(["--since"], { help: "ISO-8601 o relativo: 30m, 2h, 1d." }),
  ],
  run: async (context) => {
    const { values } = context;
    const services = await context.services();
    const info = await services.sandboxes.info(values.string("sandbox_id") ?? "", {
      readMetadata: false,
    });
    const group = values.string("log_group") ?? defaultLogGroup(info.template);
    const since = values.string("since");
    const startTimeMs = since === undefined ? undefined : sinceOrUsage(since, context.now());
    let streams: string[];
    try {
      streams = await findStreams(services.logs, group, info);
    } catch (error) {
      if (error instanceof LogsNotFoundError) {
        context.console.echo(`rayito: ${error.message}`, { err: true });
        return EXIT_FAILURE;
      }
      throw error;
    }
    const expected = expectedStreamName(info);
    if (streams.length !== 1 || streams[0] !== expected) {
      context.console.echo(
        FALLBACK_WARNING.replace("{expected}", expected).replace("{streams}", streams.join(", ")),
        { err: true },
      );
    }
    const events = await collectEvents(
      context,
      group,
      streams,
      startTimeMs,
      values.number("limit"),
    );
    if (context.globals.json) {
      context.console.emitJson(events);
      return undefined;
    }
    for (const event of events) {
      context.console.echo(`${event.timestamp} ${event.message}`);
    }
    return undefined;
  },
});

function sinceOrUsage(since: string, now: Date): number {
  try {
    return parseSince(since, now);
  } catch (error) {
    throw new UsageError(`--since: ${(error as Error).message}`);
  }
}

async function collectEvents(
  context: CommandContext,
  group: string,
  streams: readonly string[],
  startTimeMs: number | undefined,
  limit: number,
): Promise<Array<{ timestamp: string; message: string; stream: string }>> {
  const services = await context.services();
  const events: Array<{ timestamp: string; message: string; stream: string }> = [];
  for (const stream of streams) {
    const remaining = limit - events.length;
    if (remaining <= 0) {
      break;
    }
    for (const event of await readEvents(services.logs, {
      group,
      stream,
      startTimeMs,
      limit: remaining,
    })) {
      events.push({
        timestamp: eventTimestamp(event.timestamp),
        message: stripTrailing(event.message, "\n"),
        stream,
      });
    }
  }
  return events;
}

function launchToken(
  context: CommandContext,
  options: { readonly detach: boolean; readonly tokenFile: string | undefined },
): string {
  const existing = context.env[TOKEN_ENV_VAR] || undefined;
  if (options.detach && existing === undefined && options.tokenFile === undefined) {
    throw new UsageError(DETACH_NEEDS_TOKEN_FILE_MESSAGE);
  }
  const token = existing ?? generateAccessToken();
  if (options.detach && options.tokenFile !== undefined) {
    try {
      writeTokenFile(options.tokenFile, token);
    } catch (error) {
      if (error instanceof TokenFileError) {
        throw new UsageError(error.message);
      }
      throw error;
    }
  }
  return token;
}

export const createCommand = defineCommand({
  path: ["sandbox", "create"],
  help:
    "Crea un sandbox. Sin --detach abre una terminal y lo termina al salir (el código de " +
    "salida es el del shell); con --detach sale sin terminal e imprime el sandbox_id.",
  parameters: [
    argument("template", {
      metavar: "TEMPLATE",
      help: "Nombre o ARN; por defecto RAYITO_TEMPLATE.",
    }),
    option(["--timeout"], {
      type: "int",
      default: DEFAULT_CREATE_TIMEOUT_SECONDS,
      help: "Vida máxima del sandbox en segundos.",
    }),
    option(["--metadata"], { multiple: true, help: "K=V no secreto; repetible." }),
    envOption,
    flag(["--detach", "-d"], { help: "Crea y sale sin terminal; imprime el sandbox_id." }),
    tokenFileOption,
    userOption,
    cwdOption,
  ],
  run: async (context) => {
    const { values } = context;
    const detach = values.flag("detach");
    const interactive = detach ? undefined : interactiveOrUsage(context);
    const metadata = pairsOrUsage(values.strings("metadata"), "--metadata");
    const envs = pairsOrUsage(values.strings("env"), "--env");
    const token = launchToken(context, { detach, tokenFile: values.string("token_file") });
    const services = await context.services();
    const sandbox = await services.sandboxes.create({
      template: values.string("template"),
      timeoutMs: values.number("timeout") * MS_PER_SECOND,
      metadata,
      envs,
      accessToken: token,
    });
    if (interactive !== undefined) {
      return terminateAfterTerminal(context, sandbox, interactive);
    }
    try {
      if (context.globals.json) {
        context.console.emitJson(launchDocument(sandbox.info));
      } else {
        context.console.echo(sandbox.sandboxId);
      }
    } finally {
      sandbox.close();
    }
    return undefined;
  },
});

/** La terminal del sandbox recién creado, que se termina al salir pase lo que pase. */
async function terminateAfterTerminal(
  context: CommandContext,
  sandbox: RemoteSandbox,
  interactive: InteractiveIo,
): Promise<number | undefined> {
  try {
    const code = await runTerminal(sandbox, interactive, {
      user: context.values.string("user"),
      cwd: context.values.string("cwd"),
      signal: context.signal,
    });
    return code === 0 ? undefined : code;
  } finally {
    try {
      await sandbox.kill();
    } finally {
      sandbox.close();
    }
  }
}

function isCommandHandle(outcome: unknown): outcome is CommandHandle {
  return typeof (outcome as { pid?: unknown } | null)?.pid === "number";
}

async function runRemoteCommand(
  context: CommandContext,
  sandbox: RemoteSandbox,
  command: string,
): Promise<number> {
  const { values } = context;
  const timeoutSeconds = values.number("timeout");
  const envs = pairsOrUsage(values.strings("env"), "--env");
  const common = {
    ...(Object.keys(envs).length === 0 ? {} : { envs }),
    user: values.string("user"),
    cwd: values.string("cwd"),
    timeoutMs: timeoutSeconds === 0 ? NO_SERVER_TIMEOUT_MS : timeoutSeconds * MS_PER_SECOND,
  };
  if (values.flag("background")) {
    const handle = await sandbox.commands.run(command, { ...common, background: true });
    context.console.echo(String(isCommandHandle(handle) ? handle.pid : ""));
    return 0;
  }
  try {
    await sandbox.commands.run(command, {
      ...common,
      onStdout: (text) => context.console.writeRaw("stdout", text),
      onStderr: (text) => context.console.writeRaw("stderr", text),
      maxOutputBytes: STREAM_ONLY_OUTPUT_BYTES,
    });
  } catch (error) {
    if (error instanceof CommandExitError) {
      return error.exitCode;
    }
    if (error instanceof TimeoutError) {
      context.console.echo(COMMAND_TIMEOUT_MESSAGE, { err: true });
      return EXIT_TIMEOUT;
    }
    throw error;
  }
  return 0;
}

export const execCommand = defineCommand({
  path: ["sandbox", "exec"],
  help: "Ejecuta `shlex.join(CMD)` en el sandbox con la salida en streaming; sale con el código remoto (124 si venció el timeout).",
  parameters: [
    argument("sandbox_id", { required: true, metavar: "ID", help: "Id del sandbox." }),
    argument("command", {
      required: true,
      variadic: true,
      metavar: "CMD",
      help: "Comando y argumentos.",
    }),
    flag(["--background", "-b"], { help: "No espera: imprime el pid." }),
    cwdOption,
    userOption,
    envOption,
    option(["--timeout"], {
      type: "float",
      default: 0,
      help: "Segundos; 0 es sin plazo de servidor.",
    }),
    tokenFileOption,
  ],
  run: async (context) => {
    pairsOrUsage(context.values.strings("env"), "--env");
    const token = tokenOrUsage(context);
    const services = await context.services();
    const sandbox = await services.sandboxes.connect(
      context.values.string("sandbox_id") ?? "",
      token,
    );
    try {
      const code = await runRemoteCommand(
        context,
        sandbox,
        shellJoin(context.values.strings("command")),
      );
      return code === 0 ? undefined : code;
    } finally {
      sandbox.close();
    }
  },
});

async function sandboxIsGone(context: CommandContext, sandboxId: string): Promise<boolean> {
  const services = await context.services();
  try {
    const info = await services.controlPlane.getMicrovm(sandboxId);
    return TERMINAL_STATES.has(info.state);
  } catch (error) {
    if (error instanceof SandboxNotFoundError) {
      return true;
    }
    throw error;
  }
}

async function readMetrics(context: CommandContext, sandbox: RemoteSandbox) {
  try {
    return await sandbox.getMetrics();
  } catch (error) {
    if (error instanceof SandboxError && (await sandboxIsGone(context, sandbox.sandboxId))) {
      throw new SandboxError(SANDBOX_GONE_MESSAGE);
    }
    throw error;
  }
}

function metricsCell(column: string, value: unknown): unknown {
  return METRICS_FLOAT_KEYS.has(column) && typeof value === "number"
    ? pythonFloatText(value)
    : value;
}

function showMetrics(
  context: CommandContext,
  document: Record<string, unknown>,
  follow: boolean,
): void {
  if (context.globals.json && follow) {
    context.console.emitJsonLine(document, METRICS_FLOAT_KEYS);
    return;
  }
  if (context.globals.json) {
    context.console.emitJson(document, METRICS_FLOAT_KEYS);
    return;
  }
  const columns = [...METRICS_COLUMNS, ...("mem_cache" in document ? ["mem_cache"] : [])];
  context.console.table(columns, [columns.map((column) => metricsCell(column, document[column]))]);
}

export const metricsCommand = defineCommand({
  path: ["sandbox", "metrics"],
  help: "Una muestra de CPU, memoria y disco (`HealthService.Metrics`); conectar despierta un sandbox suspendido.",
  parameters: [
    argument("sandbox_id", { required: true, metavar: "ID", help: "Id del sandbox." }),
    flag(["--follow", "-f"], { help: "Repite hasta Ctrl-C o hasta que muera." }),
    option(["--interval"], {
      type: "float",
      default: DEFAULT_METRICS_INTERVAL_SECONDS,
      help: "Segundos entre muestras con --follow.",
    }),
    tokenFileOption,
  ],
  run: async (context) => {
    const token = tokenOrUsage(context);
    const services = await context.services();
    const sandbox = await services.sandboxes.connect(
      context.values.string("sandbox_id") ?? "",
      token,
    );
    try {
      await printMetrics(context, sandbox);
    } finally {
      sandbox.close();
    }
    return undefined;
  },
});

async function printMetrics(context: CommandContext, sandbox: RemoteSandbox): Promise<void> {
  const follow = context.values.flag("follow");
  const intervalMs = context.values.number("interval") * MS_PER_SECOND;
  for (;;) {
    showMetrics(context, metricsDocument(await readMetrics(context, sandbox)), follow);
    if (!follow || context.signal.aborted) {
      return;
    }
    await context.sleep(intervalMs);
    if (context.signal.aborted) {
      return;
    }
  }
}
