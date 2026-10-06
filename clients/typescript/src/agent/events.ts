/**
 * Eventos y resultado de una ejecución del agente de IA (`ai-agent-core`,
 * ADR-025). Espejo de `rayito._agent._events`: `type` es el discriminador
 * que comparten Python, TypeScript y el runner de deepagents. Ningún evento
 * lleva el texto de un error del proveedor: `AgentFailed` sólo lleva un
 * `reason` cerrado y, como mucho, un `detailCode` corto.
 */

import { AgentError, InvalidArgumentError } from "../errors.js";
import { MAX_TOOL_OUTPUT_PREVIEW_BYTES } from "../limits.js";

/** La lista cerrada de `AgentFailed.reason` / `AgentError.reason`. */
export const AGENT_FAILURE_REASONS = [
  "model_error",
  "runtime_error",
  "runtime_missing",
  "runtime_version_mismatch",
  "protocol_error",
  "timeout",
  "max_steps",
  "token_budget",
  "output_limit",
  "aborted",
  "busy",
] as const;
export type AgentFailureReason = (typeof AGENT_FAILURE_REASONS)[number];

/**
 * El mensaje de cada `reason`: tabla fija en español, nunca el texto del
 * proveedor, el prompt ni contenido del sandbox (misma tabla en
 * `rayito._agent._events`, comprobada contra
 * `testdata/agent/domain-vectors.json`).
 */
export const AGENT_FAILURE_MESSAGES: Readonly<Record<AgentFailureReason, string>> = Object.freeze({
  model_error: "el modelo devolvió un error",
  runtime_error: "el runtime del agente terminó con un error",
  runtime_missing: "el runtime del agente no está en la imagen o su servidor residente no responde",
  runtime_version_mismatch: "la versión del runtime de la imagen no es la pedida",
  protocol_error: "el runtime del agente emitió una salida fuera de protocolo",
  timeout: "el agente superó su tiempo máximo",
  max_steps: "el agente superó su número máximo de pasos",
  token_budget: "el agente superó su presupuesto de tokens",
  output_limit: "el agente superó su máximo de bytes de salida",
  aborted: "la ejecución del agente se abortó",
  busy: "ya hay otra ejecución del agente en curso en este sandbox",
});

/** Nombres de los discriminadores, en el orden del protocolo. */
export const AGENT_EVENT_TYPES = [
  "text_delta",
  "text",
  "reasoning",
  "tool_call",
  "step_started",
  "step_finished",
  "agent_failed",
  "done",
] as const;

/** Un `detailCode` es un nombre de clase de error (`APIError`), nunca un
 * mensaje: lo que no case con esto se descarta. */
const DETAIL_CODE_PATTERN = /^[A-Za-z0-9_.-]{1,64}$/;
const UTF8 = new TextEncoder();
const UTF8_LENIENT = new TextDecoder("utf-8", { fatal: false, ignoreBOM: true });

function isFailureReason(reason: unknown): reason is AgentFailureReason {
  return (AGENT_FAILURE_REASONS as readonly unknown[]).includes(reason);
}

/** `detailCode` si es un identificador corto; `undefined` si no. */
export function safeDetailCode(detailCode: unknown): string | undefined {
  return typeof detailCode === "string" && DETAIL_CODE_PATTERN.test(detailCode)
    ? detailCode
    : undefined;
}

/** El mensaje fijo de `reason`, con `detailCode` entre paréntesis si es un
 * identificador seguro. */
export function failureMessage(reason: string, detailCode?: string | null): string {
  if (!isFailureReason(reason)) {
    throw new InvalidArgumentError(
      `reason de fallo del agente desconocido; uno de ${JSON.stringify(AGENT_FAILURE_REASONS)}`,
    );
  }
  const base = AGENT_FAILURE_MESSAGES[reason];
  const code = safeDetailCode(detailCode);
  return code === undefined ? base : `${base} (${code})`;
}

/** Recorta `output` a `MAX_TOOL_OUTPUT_PREVIEW_BYTES` bytes UTF-8 sin
 * partir un carácter; `truncated` dice si se recortó. */
export function truncateToolOutput(output: string): {
  readonly text: string;
  readonly truncated: boolean;
} {
  const encoded = UTF8.encode(output);
  if (encoded.length <= MAX_TOOL_OUTPUT_PREVIEW_BYTES) {
    return { text: output, truncated: false };
  }
  let end = MAX_TOOL_OUTPUT_PREVIEW_BYTES;
  while (end > 0 && ((encoded[end] ?? 0) & 0xc0) === 0x80) {
    end -= 1;
  }
  return { text: UTF8_LENIENT.decode(encoded.subarray(0, end)), truncated: true };
}

export interface TokenUsageOptions {
  readonly input?: number | undefined;
  readonly output?: number | undefined;
  readonly reasoning?: number | undefined;
  readonly cacheRead?: number | undefined;
  readonly cacheWrite?: number | undefined;
}

function nonNegative(value: number, label: string): number {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new InvalidArgumentError(`${label} debe ser un entero >= 0`);
  }
  return value;
}

/**
 * Tokens de un paso o de una ejecución. `input` excluye los leídos de caché
 * (`cacheRead`) y los escritos (`cacheWrite`); `total` suma los cinco. El
 * SDK no da un coste: los precios dependen de región y perfil.
 */
export class TokenUsage {
  readonly input: number;
  readonly output: number;
  readonly reasoning: number;
  readonly cacheRead: number;
  readonly cacheWrite: number;

  constructor(options: TokenUsageOptions = {}) {
    this.input = nonNegative(options.input ?? 0, "TokenUsage.input");
    this.output = nonNegative(options.output ?? 0, "TokenUsage.output");
    this.reasoning = nonNegative(options.reasoning ?? 0, "TokenUsage.reasoning");
    this.cacheRead = nonNegative(options.cacheRead ?? 0, "TokenUsage.cacheRead");
    this.cacheWrite = nonNegative(options.cacheWrite ?? 0, "TokenUsage.cacheWrite");
  }

  get total(): number {
    return this.input + this.output + this.reasoning + this.cacheRead + this.cacheWrite;
  }

  plus(other: TokenUsage): TokenUsage {
    return new TokenUsage({
      input: this.input + other.input,
      output: this.output + other.output,
      reasoning: this.reasoning + other.reasoning,
      cacheRead: this.cacheRead + other.cacheRead,
      cacheWrite: this.cacheWrite + other.cacheWrite,
    });
  }
}

/** Un trozo de texto en curso (sólo deepagents). */
export interface TextDelta {
  readonly type: "text_delta";
  readonly text: string;
}

/** Un bloque de texto completo del asistente. */
export interface Text {
  readonly type: "text";
  readonly text: string;
}

/** Razonamiento del modelo; sólo con `reasoning: true`. */
export interface Reasoning {
  readonly type: "reasoning";
  readonly text: string;
}

/** Una herramienta terminada; `output` llega recortado a
 * `MAX_TOOL_OUTPUT_PREVIEW_BYTES` (`outputTruncated`). */
export interface ToolCall {
  readonly type: "tool_call";
  readonly callId: string;
  readonly name: string;
  readonly status: "completed" | "error";
  readonly input: Readonly<Record<string, unknown>> | undefined;
  readonly output: string | undefined;
  readonly outputTruncated: boolean;
}

/** Empieza el paso `index` (desde 1). */
export interface StepStarted {
  readonly type: "step_started";
  readonly index: number;
}

/** Termina el paso `index` con sus tokens. */
export interface StepFinished {
  readonly type: "step_finished";
  readonly index: number;
  readonly usage: TokenUsage;
  readonly finishReason: string | undefined;
}

/** Último evento de una ejecución fallida. */
export interface AgentFailed {
  readonly type: "agent_failed";
  readonly reason: AgentFailureReason;
  readonly exitCode: number | undefined;
  readonly detailCode: string | undefined;
  readonly sessionId: string | undefined;
}

/** Último evento de una ejecución correcta, con los tokens acumulados. */
export interface Done {
  readonly type: "done";
  readonly sessionId: string;
  readonly exitCode: number;
  readonly usage: TokenUsage;
}

export type AgentEvent =
  | TextDelta
  | Text
  | Reasoning
  | ToolCall
  | StepStarted
  | StepFinished
  | AgentFailed
  | Done;

export interface AgentFailedOptions {
  readonly exitCode?: number | undefined;
  readonly detailCode?: string | null | undefined;
  readonly sessionId?: string | undefined;
}

/** Construye un `AgentFailed` validando `reason` y descartando un
 * `detailCode` que no sea un identificador corto. */
export function agentFailed(reason: string, options: AgentFailedOptions = {}): AgentFailed {
  if (!isFailureReason(reason)) {
    throw new InvalidArgumentError(
      `AgentFailed.reason debe ser uno de ${JSON.stringify(AGENT_FAILURE_REASONS)}`,
    );
  }
  return Object.freeze({
    type: "agent_failed",
    reason,
    exitCode: options.exitCode,
    detailCode: safeDetailCode(options.detailCode),
    sessionId: options.sessionId,
  });
}

/** El `AgentError` que `sbx.agent.run()` lanza por un `AgentFailed`. */
export function agentFailedToError(event: AgentFailed, usage?: TokenUsage): AgentError {
  return new AgentError(failureMessage(event.reason, event.detailCode), {
    reason: event.reason,
    sessionId: event.sessionId,
    usage,
    exitCode: event.exitCode,
    detailCode: event.detailCode,
  });
}

/**
 * El resultado de `sbx.agent.run()`. `text` es el último `Text` del
 * asistente; `droppedLines` cuenta las líneas del runtime descartadas.
 */
export interface AgentResult {
  readonly sessionId: string;
  readonly text: string;
  readonly steps: number;
  readonly usage: TokenUsage;
  readonly exitCode: number;
  readonly toolCalls: readonly ToolCall[];
  readonly droppedLines: number;
}
