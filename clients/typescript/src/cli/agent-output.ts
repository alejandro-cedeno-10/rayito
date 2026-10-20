/**
 * La salida de `rayito agent invoke`: el resultado de una invocación y los
 * eventos de un stream, en texto o en `--json`. Los comparten `agent invoke`
 * de MicroVMs y de AgentCore. Sólo da forma al texto: no llama a AWS.
 */

import { type AgentEvent, type AgentResult, failureMessage } from "../agent/events.js";
import type { CommandContext } from "./command.js";
import { failure } from "./commands/template.js";

const USAGE_UNKNOWN_LABEL = "desconocido";

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
  };
}

export function printResult(context: CommandContext, result: AgentResult): void {
  if (context.globals.json) {
    context.console.emitJson(resultDocument(result));
    return;
  }
  context.console.echo(result.text);
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
    }
    if (event.type === "agent_failed") {
      this.failed = event;
    }
  };

  finish(): number | undefined {
    if (this.#wroteText) {
      this.#context.console.writeRaw("stdout", "\n");
    }
    if (this.failed?.type === "agent_failed") {
      return failure(this.#context, failureMessage(this.failed.reason, this.failed.detailCode));
    }
    return undefined;
  }

  #write(text: string): void {
    this.#wroteText = true;
    this.#context.console.write(text);
  }
}
