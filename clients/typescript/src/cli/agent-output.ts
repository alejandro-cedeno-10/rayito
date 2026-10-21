/**
 * La entrada y la salida de `rayito agent invoke`: el prompt o el objeto de
 * `--input` / `--input-file`, el resultado de una invocación y los eventos de
 * un stream, en texto o en `--json`. Los comparten `agent invoke` de MicroVMs
 * y de AgentCore. No llama a AWS.
 */

import { readFileSync } from "node:fs";
import type { AgentInvocation } from "../agent/deployment.js";

import {
  type AgentEvent,
  type AgentResult,
  failureMessage,
  type JsonObject,
} from "../agent/events.js";
import type { CommandContext } from "./command.js";
import { failure } from "./commands/template.js";
import { UsageError } from "./failures.js";

const USAGE_UNKNOWN_LABEL = "desconocido";
/** `--input-file -` lee el objeto de la entrada estándar (descriptor 0). */
export const STDIN_PATH = "-";
const STDIN_DESCRIPTOR = 0;
export const INVOKE_INPUT_REQUIRED = "pasa PROMPT, --input o --input-file, sólo uno de los tres";
export const INVOKE_INPUT_UNREADABLE = "no se pudo leer el fichero de --input-file";
export const INVOKE_INPUT_INVALID = "la entrada no es un objeto JSON";

/** Lee un fichero de texto (o la entrada estándar con su descriptor). */
export type InputFileReader = (path: string | number) => string;

function defaultReader(path: string | number): string {
  return readFileSync(path, "utf8");
}

function readInputFile(path: string, read: InputFileReader): string {
  try {
    return read(path === STDIN_PATH ? STDIN_DESCRIPTOR : path);
  } catch {
    throw new UsageError(INVOKE_INPUT_UNREADABLE);
  }
}

/** Lo que `agent invoke` manda: el `PROMPT` o el objeto de `--input` o de
 * `--input-file` (`-` lee stdin). Exactamente uno de los tres; si no, o si la
 * entrada no es un objeto JSON, error de uso antes de llamar a nadie. El error
 * nunca lleva el contenido. */
export function invokeRequestOf(
  context: CommandContext,
  read: InputFileReader = defaultReader,
): AgentInvocation {
  const { values } = context;
  const prompt = values.string("prompt");
  const inputJson = values.string("input_json");
  const inputFile = values.string("input_file");
  if ([prompt, inputJson, inputFile].filter((value) => value !== undefined).length !== 1) {
    throw new UsageError(INVOKE_INPUT_REQUIRED);
  }
  if (prompt !== undefined) {
    return prompt;
  }
  const text = inputFile === undefined ? (inputJson as string) : readInputFile(inputFile, read);
  let data: unknown;
  try {
    data = JSON.parse(text);
  } catch {
    throw new UsageError(INVOKE_INPUT_INVALID);
  }
  if (typeof data !== "object" || data === null || Array.isArray(data)) {
    throw new UsageError(INVOKE_INPUT_INVALID);
  }
  return { input: data as JsonObject };
}
/** Sangría del JSON de `output` en texto. */
const OUTPUT_INDENT = 2;

/** La salida estructurada como JSON legible. */
export function outputText(output: JsonObject): string {
  return JSON.stringify(output, null, OUTPUT_INDENT);
}

/** El resultado en `--json`; `output` (la salida de un grafo invocado con
 * `--input`) va tal cual y sólo si la hay. */
export function resultDocument(result: AgentResult): Record<string, unknown> {
  const { usage } = result;
  return {
    sessionId: result.sessionId,
    text: result.text,
    steps: result.steps,
    usage: {
      input: usage.input,
      output: usage.output,
      reasoning: usage.reasoning,
      cacheRead: usage.cacheRead,
      cacheWrite: usage.cacheWrite,
      total: usage.total,
    },
    usageReported: result.usageReported !== false,
    exitCode: result.exitCode,
    ...(result.output === undefined ? {} : { output: result.output }),
  };
}

export function printResult(context: CommandContext, result: AgentResult): void {
  if (context.globals.json) {
    context.console.emitJson(resultDocument(result));
    return;
  }
  if (result.output === undefined) {
    context.console.echo(result.text);
  } else {
    if (result.text !== "") {
      context.console.echo(result.text, { err: true });
    }
    context.console.echo(outputText(result.output));
  }
  context.console.echo(`session_id=${result.sessionId}`, { err: true });
  const tokens = result.usageReported === false ? USAGE_UNKNOWN_LABEL : result.usage.total;
  context.console.echo(`steps=${result.steps} tokens=${tokens}`, { err: true });
}

/**
 * Escribe los eventos de un stream: en `--json`, un objeto por línea; en
 * texto, los `text_delta` tal cual (el `text` entero sólo si no hubo deltas)
 * y las herramientas en stderr. Un `agent_failed` termina la orden con 1.
 */
export class EventPrinter {
  readonly #context: CommandContext;
  #sawDelta = false;
  #wroteText = false;
  failed: AgentEvent | undefined;

  constructor(context: CommandContext) {
    this.#context = context;
  }

  readonly print = (event: AgentEvent): void => {
    const { console, globals } = this.#context;
    if (globals.json) {
      console.emitJsonLine(event);
    } else if (event.type === "text_delta") {
      this.#sawDelta = true;
      this.#write(event.text);
    } else if (event.type === "text" && !this.#sawDelta) {
      this.#write(event.text);
    } else if (event.type === "tool_call") {
      console.echo(`tool=${event.name} status=${event.status}`, { err: true });
    } else if (event.type === "done" && event.output !== undefined) {
      this.#endText();
      console.echo(outputText(event.output));
    }
    if (event.type === "agent_failed") {
      this.failed = event;
    }
  };

  finish(): number | undefined {
    this.#endText();
    if (this.failed?.type === "agent_failed") {
      return failure(this.#context, failureMessage(this.failed.reason, this.failed.detailCode));
    }
    return undefined;
  }

  #endText(): void {
    if (this.#wroteText) {
      this.#wroteText = false;
      this.#context.console.writeRaw("stdout", "\n");
    }
  }

  #write(text: string): void {
    this.#wroteText = true;
    this.#context.console.write(text);
  }
}
