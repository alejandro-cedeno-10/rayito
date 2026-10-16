/**
 * Las seis herramientas del servidor MCP como funciones sobre el
 * `SandboxLease` (espejo de `rayito.mcp._server`). No importan el SDK de MCP:
 * devuelven `ToolResult`, la forma de `CallToolResult`, y el registro con
 * sus esquemas vive en `server.ts`.
 *
 * Los fallos accionables por el modelo son resultados `isError` con mensaje
 * en español; los exit codes y los errores del kernel son datos. El log lleva
 * nombres de herramienta, tamaños, duraciones, `sandboxId`, exit codes y
 * nombres de error: nunca código, comandos, contenidos ni salidas.
 */

import {
  CommandExitError,
  DiskFullError,
  FileNotFoundError,
  InvalidArgumentError,
  RateLimitError,
  SandboxNotFoundError,
  SandboxStateError,
  TimeoutError,
  UnimplementedError,
} from "../errors.js";
import type { Logger } from "../logger.js";
import type { CommandResult } from "../models.js";
import { Sandbox } from "../sandbox/sandbox.js";
import { MS_PER_SECOND } from "../units.js";
import { describeAwsFailure, isAwsSdkFailure } from "./failures.js";
import {
  MissingTemplateError,
  SandboxCreationError,
  type SandboxEnvironment,
  type SandboxLease,
} from "./lease.js";
import {
  type CommandOutput,
  type ContentBlock,
  characterCount,
  commandOutputFrom,
  directoryListingFrom,
  executionToBlocks,
  fileContentFrom,
  sandboxListFrom,
  writeReceiptFrom,
} from "./results.js";

export const DEFAULT_CODE_TIMEOUT_SECONDS = 300;
export const DEFAULT_COMMAND_TIMEOUT_SECONDS = 60;
export const MAX_TOOL_TIMEOUT_SECONDS = 3600;
export const DEFAULT_LIST_PATH = "/home/user";
export const DEFAULT_LIST_DEPTH = 1;
export const MAX_LIST_DEPTH = 5;

export interface ToolResult {
  [key: string]: unknown;
  readonly content: ContentBlock[];
  readonly structuredContent?: Record<string, unknown>;
  readonly isError?: boolean;
}

export interface RunCodeArgs {
  readonly code: string;
  readonly timeout: number;
}

export interface RunCommandArgs {
  readonly cmd: string;
  readonly timeout: number;
  readonly cwd?: string | undefined;
}

export interface ReadFileArgs {
  readonly path: string;
}

export interface WriteFileArgs {
  readonly path: string;
  readonly content: string;
}

export interface ListFilesArgs {
  readonly path: string;
  readonly depth: number;
}

function isMessageError(error: unknown): boolean {
  return (
    error instanceof FileNotFoundError ||
    error instanceof InvalidArgumentError ||
    error instanceof DiskFullError ||
    error instanceof RateLimitError ||
    error instanceof UnimplementedError
  );
}

function errorResult(message: string): ToolResult {
  return { content: [{ type: "text", text: message }], isError: true };
}

function structuredResult(output: object): ToolResult {
  return {
    content: [{ type: "text", text: JSON.stringify(output) }],
    structuredContent: { ...output },
  };
}

function timeoutMessage(timeoutSeconds: number | undefined): string {
  return timeoutSeconds === undefined
    ? "la operación superó el timeout del agente"
    : `el comando superó el timeout de ${timeoutSeconds} s`;
}

function transitionMessage(error: Error): string {
  return `el sandbox está en transición (${error.message}); reintenta en unos segundos`;
}

/** Un fallo no previsto: sólo el código de AWS y el mensaje redactado, o el nombre del error. */
function unexpectedMessage(error: unknown): string {
  if (!(error instanceof Error)) {
    return "error inesperado del servidor MCP";
  }
  return isAwsSdkFailure(error)
    ? `error de AWS ${describeAwsFailure(error)}`
    : `error inesperado del servidor MCP (${error.name})`;
}

function errorName(error: unknown): string {
  return error instanceof Error ? error.name : typeof error;
}

/** Una línea de log por llamada: nombre, tamaños de argumentos, duración, `sandboxId` y desenlace. */
class CallLog {
  readonly #logger: Logger | undefined;
  readonly #tool: string;
  readonly #startedAt = performance.now();

  constructor(
    logger: Logger | undefined,
    tool: string,
    sizes: Readonly<Record<string, number>> = {},
  ) {
    this.#logger = logger;
    this.#tool = tool;
    logger?.debug?.(`${tool}: inicio`, sizes);
  }

  finish(sandboxId: string | undefined, outcome: Readonly<Record<string, unknown>> = {}): void {
    const elapsedMs = Math.round(performance.now() - this.#startedAt);
    this.#logger?.info?.(`${this.#tool}: fin`, { elapsedMs, sandboxId, ...outcome });
  }
}

export class McpTools {
  readonly #lease: SandboxLease;
  readonly #environment: SandboxEnvironment;

  constructor(lease: SandboxLease, environment: SandboxEnvironment = {}) {
    this.#lease = lease;
    this.#environment = environment;
  }

  async runCode(args: RunCodeArgs): Promise<ToolResult> {
    const call = this.#startCall("run_code", { code_chars: characterCount(args.code) });
    return this.#guarded(args.timeout, async () => {
      const sandbox = await this.#lease.acquire();
      const execution = await sandbox.runCode(args.code, {
        timeoutMs: args.timeout * MS_PER_SECOND,
      });
      call.finish(sandbox.sandboxId, {
        results: execution.results.length,
        error: execution.error?.name,
      });
      return { content: executionToBlocks(execution, { sandboxId: sandbox.sandboxId }) };
    });
  }

  async runCommand(args: RunCommandArgs): Promise<ToolResult> {
    const call = this.#startCall("run_command", { cmd_chars: characterCount(args.cmd) });
    return this.#guarded(args.timeout, async () => {
      const sandbox = await this.#lease.acquire();
      const output = await runCommandIn(sandbox, args);
      call.finish(sandbox.sandboxId, { exit_code: output.exit_code });
      return structuredResult(output);
    });
  }

  async readFile(args: ReadFileArgs): Promise<ToolResult> {
    const call = this.#startCall("read_file", { path_chars: characterCount(args.path) });
    return this.#guarded(undefined, async () => {
      const sandbox = await this.#lease.acquire();
      const content = await sandbox.files.read(args.path);
      call.finish(sandbox.sandboxId, { size: characterCount(content) });
      return structuredResult(fileContentFrom(args.path, content));
    });
  }

  async writeFile(args: WriteFileArgs): Promise<ToolResult> {
    const call = this.#startCall("write_file", {
      path_chars: characterCount(args.path),
      content_chars: characterCount(args.content),
    });
    return this.#guarded(undefined, async () => {
      const sandbox = await this.#lease.acquire();
      const entry = await sandbox.files.write(args.path, args.content);
      call.finish(sandbox.sandboxId, { size: entry.size });
      return structuredResult(writeReceiptFrom(entry));
    });
  }

  async listFiles(args: ListFilesArgs): Promise<ToolResult> {
    const call = this.#startCall("list_files", {
      path_chars: characterCount(args.path),
      depth: args.depth,
    });
    return this.#guarded(undefined, async () => {
      const sandbox = await this.#lease.acquire();
      const entries = await sandbox.files.list(args.path, { depth: args.depth });
      call.finish(sandbox.sandboxId, { entries: entries.length });
      return structuredResult(directoryListingFrom(args.path, entries));
    });
  }

  async listSandboxes(): Promise<ToolResult> {
    const call = this.#startCall("list_sandboxes");
    return this.#guarded(undefined, async () => {
      const template = this.#lease.requireTemplate();
      const current = this.#lease.peek();
      const items = [];
      for await (const item of Sandbox.list({
        template,
        controlPlane: this.#environment.controlPlane,
        transport: this.#environment.transport,
      })) {
        items.push(item);
      }
      call.finish(this.#lease.sandboxId, { listed: items.length });
      return structuredResult(sandboxListFrom(items, current?.sandboxId));
    });
  }

  #startCall(tool: string, sizes: Readonly<Record<string, number>> = {}): CallLog {
    return new CallLog(this.#environment.logger, tool, sizes);
  }

  /**
   * Lo que el modelo puede corregir es un resultado `isError` con mensaje en
   * español; sólo `SandboxNotFoundError` (la única señal terminal del SDK)
   * resetea el lease; `SandboxStateError` es transitoria (suspend/resume en
   * curso) y se pide reintentar sin tocar el sandbox.
   */
  async #guarded(
    timeoutSeconds: number | undefined,
    body: () => Promise<ToolResult>,
  ): Promise<ToolResult> {
    try {
      return await body();
    } catch (error) {
      return this.#translate(error, timeoutSeconds);
    }
  }

  async #translate(error: unknown, timeoutSeconds: number | undefined): Promise<ToolResult> {
    const logger = this.#environment.logger;
    if (error instanceof MissingTemplateError || error instanceof SandboxCreationError) {
      return errorResult(error.message);
    }
    if (error instanceof TimeoutError) {
      return errorResult(timeoutMessage(timeoutSeconds));
    }
    if (isMessageError(error)) {
      return errorResult((error as Error).message);
    }
    if (error instanceof SandboxStateError) {
      logger?.info?.("el sandbox está en transición", {
        sandboxId: this.#lease.sandboxId,
        error: errorName(error),
      });
      return errorResult(transitionMessage(error));
    }
    if (error instanceof SandboxNotFoundError) {
      const sandboxId = this.#lease.sandboxId;
      logger?.warn?.("el sandbox ya no existe", { sandboxId, error: errorName(error) });
      await this.#lease.reset();
      return errorResult(
        `el sandbox ${sandboxId} ya no existe (${error.message}); la siguiente llamada crea uno nuevo`,
      );
    }
    logger?.error?.("fallo inesperado", { error: errorName(error) });
    return errorResult(unexpectedMessage(error));
  }
}

async function runCommandIn(sandbox: Sandbox, args: RunCommandArgs): Promise<CommandOutput> {
  try {
    const result: CommandResult = await sandbox.commands.run(args.cmd, {
      timeoutMs: args.timeout * MS_PER_SECOND,
      cwd: args.cwd,
    });
    return commandOutputFrom(result);
  } catch (error) {
    if (error instanceof CommandExitError) {
      return commandOutputFrom(error);
    }
    throw error;
  }
}
