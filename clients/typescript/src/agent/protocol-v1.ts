/**
 * Decodificador del protocolo JSONL v1 de Rayito (ADR-025, ADR-028). Espejo
 * de `rayito._agent._protocol_v1`: el formato en que el host del agente y el
 * runner de deepagents escriben los eventos de una ejecución (`session`,
 * `text_delta`, `text`, `reasoning`, `tool_call`, `step_started`,
 * `step_finished`, `agent_failed`, `done`, más las líneas `rayito.busy` y
 * `rayito.runtime_missing` del script de ejecución). Puro: una línea de bytes
 * entra y salen eventos del dominio.
 *
 * Lo comparten todos los adaptadores que hablan v1 (deepagents, Strands,
 * Hermes, `CustomRuntime`); los vectores `testdata/agent/rayito-protocol-v1*`
 * lo prueban una vez.
 */

import { AGENT_PROTOCOL_VERSION } from "../limits.js";
import {
  type AgentEvent,
  type AgentFailed,
  agentFailed,
  type Done,
  TokenUsage,
  truncateToolOutput,
} from "./events.js";

/** Fallos que un runner o un host pueden declarar; el resto sólo los decide el SDK. */
export const RUNNER_FAILURE_REASONS = ["model_error", "runtime_error", "protocol_error"] as const;
/** Líneas del script de ejecución (no del runner) que se convierten en un fallo. */
export const SCRIPT_FAILURE_TYPES = ["rayito.busy", "rayito.runtime_missing"] as const;
/** El identificador de sesión que emite el runner: `rda_` y 32 hex. */
export const SESSION_ID_PATTERN = /^rda_[0-9a-f]{32}$/;
const SCRIPT_PREFIX = "rayito.";
const UTF8_DECODER = new TextDecoder();

/** Lo que el decodificador acumula de una ejecución. */
export interface ProtocolV1State {
  sessionId: string | undefined;
  steps: number;
  usage: TokenUsage;
  done: boolean;
  failed: AgentFailed | undefined;
  ignoredLines: number;
}

/** El estado inicial de una ejecución. */
export function newProtocolV1State(): ProtocolV1State {
  return {
    sessionId: undefined,
    steps: 0,
    usage: new TokenUsage(),
    done: false,
    failed: undefined,
    ignoredLines: 0,
  };
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function count(value: unknown): number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : 0;
}

function usageOf(raw: unknown): TokenUsage {
  if (!isPlainObject(raw)) {
    return new TokenUsage();
  }
  return new TokenUsage({
    input: count(raw.input),
    output: count(raw.output),
    reasoning: count(raw.reasoning),
    cacheRead: count(raw.cache_read),
    cacheWrite: count(raw.cache_write),
  });
}

/** Una línea del protocolo v1 a cero o más eventos; lo ilegible, de otra
 * versión o de tipo desconocido se ignora y se cuenta. */
export function parseV1Line(line: Uint8Array, state: ProtocolV1State): readonly AgentEvent[] {
  let payload: unknown;
  try {
    payload = JSON.parse(UTF8_DECODER.decode(line));
  } catch {
    state.ignoredLines += 1;
    return [];
  }
  if (!isPlainObject(payload) || payload.v !== AGENT_PROTOCOL_VERSION) {
    state.ignoredLines += 1;
    return [];
  }
  const events = mapPayload(payload, state);
  if (events === undefined) {
    state.ignoredLines += 1;
    return [];
  }
  return events;
}

/** `Done` sólo con salida 0, el `done` del emisor y una sesión. */
export function finishV1(state: ProtocolV1State, exitCode: number): Done | AgentFailed {
  if (state.failed !== undefined) {
    return state.failed;
  }
  if (exitCode !== 0) {
    return agentFailed("runtime_error", { exitCode, sessionId: state.sessionId });
  }
  if (!state.done || state.sessionId === undefined) {
    return agentFailed("protocol_error", { exitCode, sessionId: state.sessionId });
  }
  return Object.freeze({
    type: "done",
    sessionId: state.sessionId,
    exitCode,
    usage: state.usage,
  });
}

function mapPayload(
  payload: Record<string, unknown>,
  state: ProtocolV1State,
): AgentEvent[] | undefined {
  const kind = payload.type;
  switch (kind) {
    case "rayito.busy":
    case "rayito.runtime_missing":
      state.failed = agentFailed(kind.slice(SCRIPT_PREFIX.length));
      return [state.failed];
    case "session": {
      const sessionId = payload.session_id;
      if (typeof sessionId !== "string" || !SESSION_ID_PATTERN.test(sessionId)) {
        return undefined;
      }
      state.sessionId = sessionId;
      return [];
    }
    case "step_started":
      state.steps += 1;
      return [Object.freeze({ type: "step_started", index: state.steps })];
    case "step_finished":
      return [stepFinished(payload, state)];
    case "text_delta":
    case "text":
    case "reasoning": {
      const text = payload.text;
      return typeof text === "string" ? [Object.freeze({ type: kind, text })] : undefined;
    }
    case "tool_call":
      return toolCallOf(payload);
    case "agent_failed":
      return [failure(payload, state)];
    case "done":
      state.done = true;
      return [];
    default:
      return undefined;
  }
}

function stepFinished(payload: Record<string, unknown>, state: ProtocolV1State): AgentEvent {
  const usage = usageOf(payload.usage);
  state.usage = state.usage.plus(usage);
  return Object.freeze({
    type: "step_finished",
    index: state.steps,
    usage,
    finishReason: typeof payload.finish_reason === "string" ? payload.finish_reason : undefined,
  });
}

function failure(payload: Record<string, unknown>, state: ProtocolV1State): AgentFailed {
  const reason = (RUNNER_FAILURE_REASONS as readonly unknown[]).includes(payload.reason)
    ? (payload.reason as string)
    : "protocol_error";
  state.failed = agentFailed(reason, {
    detailCode: typeof payload.detail_code === "string" ? payload.detail_code : undefined,
    sessionId: state.sessionId,
  });
  return state.failed;
}

function toolCallOf(payload: Record<string, unknown>): AgentEvent[] | undefined {
  const status = payload.status;
  if (status !== "completed" && status !== "error") {
    return undefined;
  }
  let output: string | undefined;
  let truncated = false;
  if (typeof payload.output === "string") {
    const cut = truncateToolOutput(payload.output);
    output = cut.text;
    truncated = cut.truncated;
  }
  return [
    Object.freeze({
      type: "tool_call",
      callId: String(payload.call_id ?? ""),
      name: String(payload.name ?? ""),
      status,
      input: isPlainObject(payload.input) ? { ...payload.input } : undefined,
      output,
      outputTruncated: truncated || payload.output_truncated === true,
    }),
  ];
}
