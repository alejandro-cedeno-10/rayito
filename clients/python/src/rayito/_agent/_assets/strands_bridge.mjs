// Puente de Strands Agents para TypeScript (ADR-028, D2, D3 y D9). Lo ejecuta el
// driver `strands-ts` del host como `node strands_bridge.mjs <config.json>`:
// importa tu agente de `@strands-agents/sdk`, lo ejecuta con la petición que
// llega por stdin (`{"v":1,"prompt","session_id","model","reasoning"}`) y
// escribe el protocolo JSONL v1 de Rayito por stdout (el evento `session` ya lo
// emitió el driver, que además le da el `session_id`). El prompt nunca va en argv.
//
// Tu módulo (`entrypoint`, una ruta bajo `app_dir`) exporta `agent` (un `Agent`)
// o `createAgent(ctx)` (también vale `default`), que recibe el modelo ya cableado
// a la pasarela en `ctx.model`, el id de la sesión en `ctx.sessionId` y la carpeta
// de sesiones en `ctx.sessionsDir` (para un `SessionManager` tuyo). Node 22.18 o
// posterior ejecuta `.ts` directamente, sin compilar.
//
// El modelo llega siempre por la pasarela de secretos con un marcador como
// clave, nunca con credenciales de AWS. `AgentPermissions` se aplica a toda
// llamada de herramienta con un gancho `BeforeToolCallEvent`. Sólo usa la
// biblioteca estándar de Node y el SDK que instala `npm ci` de tu lockfile.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

export const PROTOCOL_VERSION = 1;
export const MAX_TOOL_OUTPUT_BYTES = 65536;
export const DENIED_TOOL_MESSAGE = "Permiso denegado por la configuración del agente.";
export const ANTHROPIC_MAX_TOKENS = 8192;
const MODEL_ERROR_NAMES = new Set([
  "ModelError",
  "ContextWindowOverflowError",
  "MaxTokensError",
  "ModelThrottledError",
  "ProviderTokenCountError",
]);
const STOP_REASONS = {
  endTurn: "end_turn",
  toolUse: "tool_use",
  maxTokens: "max_tokens",
  stopSequence: "stop_sequence",
  contentFiltered: "content_filtered",
  guardrailIntervened: "guardrail_intervened",
};
const USAGE_KEYS = {
  inputTokens: "input",
  outputTokens: "output",
  cacheReadInputTokens: "cache_read",
  cacheWriteInputTokens: "cache_write",
};
const SESSION_ID_PATTERN = /^rda_[0-9a-f]{32}$/;
const DETAIL_CODE_PATTERN = /[^a-z0-9_]/g;
const MAX_DETAIL_CODE_LENGTH = 64;

function event(type, fields = {}) {
  return { v: PROTOCOL_VERSION, type, ...fields };
}

function count(value) {
  return Number.isInteger(value) && value > 0 ? value : 0;
}

export function usageOf(raw) {
  const source = raw && typeof raw === "object" ? raw : {};
  const usage = { input: 0, output: 0, reasoning: 0, cache_read: 0, cache_write: 0 };
  for (const [key, name] of Object.entries(USAGE_KEYS)) {
    usage[name] = count(source[key]);
  }
  return usage;
}

export function truncateOutput(text) {
  const encoded = Buffer.from(text, "utf8");
  if (encoded.length <= MAX_TOOL_OUTPUT_BYTES) {
    return [text, false];
  }
  return [encoded.subarray(0, MAX_TOOL_OUTPUT_BYTES).toString("utf8").replace(/�+$/u, ""), true];
}

function toolResultText(content) {
  if (!Array.isArray(content)) {
    return "";
  }
  const parts = [];
  for (const block of content) {
    if (block && typeof block.text === "string") {
      parts.push(block.text);
    } else if (block && "json" in block) {
      parts.push(JSON.stringify(block.json));
    }
  }
  return parts.join("\n");
}

// `agent.stream()` de Strands (TypeScript) a eventos v1. Un paso empieza en
// `modelMessageStartEvent` y termina con el mensaje del asistente; el resultado
// de cada herramienta llega en `afterToolCallEvent` (su `result` es el bloque
// con `status` y `content`, o ese bloque envuelto en `toolResult` cuando viene
// de `JSON.stringify`, como en el vector). Medido en
// `testdata/agent/strands-ts-events.jsonl`.
export class Translator {
  constructor({ reasoning }) {
    this.reasoning = reasoning;
    this.steps = 0;
    this.text = [];
    this.thinking = [];
    this.usage = usageOf(null);
    this.finishReason = null;
  }

  feed(item) {
    if (item.type === "modelStreamUpdateEvent" && item.event) {
      return this.#modelEvent(item.event);
    }
    if (item.type === "modelMessageEvent") {
      return this.#closeStep();
    }
    if (item.type === "afterToolCallEvent") {
      return this.#toolCall(item);
    }
    return [];
  }

  #modelEvent(inner) {
    if (inner.type === "modelMessageStartEvent") {
      this.steps += 1;
      this.text = [];
      this.thinking = [];
      this.usage = usageOf(null);
      this.finishReason = null;
      return [event("step_started", { index: this.steps })];
    }
    if (inner.type === "modelContentBlockDeltaEvent" && inner.delta) {
      return this.#delta(inner.delta);
    }
    if (inner.type === "modelMessageStopEvent" && typeof inner.stopReason === "string") {
      this.finishReason = STOP_REASONS[inner.stopReason] ?? inner.stopReason;
    }
    if (inner.type === "modelMetadataEvent") {
      this.usage = usageOf(inner.usage);
    }
    return [];
  }

  #delta(delta) {
    if (delta.type === "textDelta" && typeof delta.text === "string") {
      this.text.push(delta.text);
      return delta.text ? [event("text_delta", { text: delta.text })] : [];
    }
    if (delta.type === "reasoningContentDelta" && typeof delta.text === "string") {
      this.thinking.push(delta.text);
    }
    return [];
  }

  #closeStep() {
    const events = [];
    const text = this.text.join("");
    if (text) {
      events.push(event("text", { text }));
    }
    const thinking = this.thinking.join("");
    if (this.reasoning && thinking) {
      events.push(event("reasoning", { text: thinking }));
    }
    events.push(
      event("step_finished", {
        index: this.steps,
        usage: this.usage,
        finish_reason: this.finishReason,
      }),
    );
    return events;
  }

  #toolCall(item) {
    const result = item.result?.toolResult ?? item.result;
    if (!result || typeof result.status !== "string") {
      return [];
    }
    const [output, truncated] = truncateOutput(toolResultText(result.content));
    const input = item.toolUse?.input;
    return [
      event("tool_call", {
        call_id: String(result.toolUseId ?? ""),
        name: String(item.toolUse?.name ?? ""),
        status: result.status === "success" ? "completed" : "error",
        input: input && typeof input === "object" && !Array.isArray(input) ? input : null,
        output,
        output_truncated: truncated,
      }),
    ];
  }
}

// `allow` o `deny` para una llamada: una herramienta sin regla usa `default`; con
// patrones gana el último que casa con el comando (`command`).
export function decide(rules, tool, args) {
  const fallback = rules.default ?? "allow";
  const rule = rules.tools?.[tool];
  if (rule === undefined) {
    return fallback;
  }
  if (typeof rule === "string") {
    return rule;
  }
  const command = String(args?.command ?? "");
  let decision = fallback;
  for (const [pattern, action] of Object.entries(rule)) {
    if (globMatches(pattern, command)) {
      decision = action;
    }
  }
  return decision;
}

function globMatches(pattern, text) {
  const source = pattern
    .split("*")
    .map((part) => part.replace(/[.+?^${}()|[\]\\]/g, "\\$&"))
    .join(".*");
  return new RegExp(`^${source}$`, "s").test(text);
}

export function classify(error) {
  const detail = String(error?.constructor?.name ?? "error")
    .toLowerCase()
    .replace(DETAIL_CODE_PATTERN, "_")
    .slice(0, MAX_DETAIL_CODE_LENGTH);
  for (let current = error, depth = 0; current && depth < 6; depth += 1) {
    if (MODEL_ERROR_NAMES.has(current.constructor?.name)) {
      return ["model_error", detail];
    }
    current = current.cause;
  }
  return ["runtime_error", detail];
}

const MODEL_MODULES = {
  bedrock: ["@strands-agents/sdk/models/bedrock", "BedrockModel"],
  anthropic: ["@strands-agents/sdk/models/anthropic", "AnthropicModel"],
  openai: ["@strands-agents/sdk/models/openai", "OpenAIModel"],
  "openai-compatible": ["@strands-agents/sdk/models/openai", "OpenAIModel"],
  google: ["@strands-agents/sdk/models/google", "GoogleModel"],
};

// El modelo de Strands del proveedor de `config`, apuntado a la pasarela con el
// marcador como clave. Bedrock lleva la clave de portador en
// `AWS_BEARER_TOKEN_BEDROCK` (la fija el entorno del host).
export async function buildModel(config, modelId, importModule = (name) => import(name)) {
  const entry = MODEL_MODULES[config.provider];
  if (!entry) {
    throw new Error("unknown_provider");
  }
  const module = await importModule(entry[0]);
  const Model = module[entry[1]];
  const gateway = config.base_url !== null && config.base_url !== undefined;
  const key = config.credential_placeholder;
  if (config.provider === "bedrock") {
    const options = { modelId, region: config.region };
    if (gateway) {
      options.clientConfig = { endpoint: config.base_url };
    }
    return new Model(options);
  }
  const options = { modelId };
  if (config.provider === "anthropic") {
    options.maxTokens = ANTHROPIC_MAX_TOKENS;
    if (gateway) {
      Object.assign(options, { apiKey: key, clientConfig: { baseURL: config.base_url } });
    }
  } else if (config.provider === "google") {
    if (gateway) {
      Object.assign(options, { apiKey: key, clientConfig: { httpOptions: { baseUrl: config.base_url } } });
    }
  } else {
    options.api = "chat";
    if (gateway) {
      Object.assign(options, { apiKey: key, clientConfig: { baseURL: config.base_url } });
    }
  }
  return new Model(options);
}

export function registerPermissions(agent, sdk, rules) {
  agent.addHook(sdk.BeforeToolCallEvent, (hookEvent) => {
    const tool = String(hookEvent.toolUse?.name ?? "");
    if (decide(rules, tool, hookEvent.toolUse?.input) !== "allow") {
      hookEvent.cancel = DENIED_TOOL_MESSAGE;
    }
  });
}

export async function loadAgent(config, model, sessionId, importModule = (name) => import(name)) {
  const path = resolve(String(config.app_dir), String(config.entrypoint));
  const module = await importModule(pathToFileURL(path).href);
  const target = module.agent ?? module.createAgent ?? module.default;
  if (target && typeof target.stream === "function") {
    return target;
  }
  if (typeof target !== "function") {
    throw new Error("invalid_entrypoint");
  }
  return target({
    model,
    instructions: config.instructions ?? null,
    workdir: config.workdir,
    stateDir: config.state_dir,
    sessionId,
    sessionsDir: config.sessions_dir,
  });
}

function validRequest(request) {
  return (
    request &&
    request.v === PROTOCOL_VERSION &&
    typeof request.prompt === "string" &&
    typeof request.session_id === "string" &&
    SESSION_ID_PATTERN.test(request.session_id)
  );
}

export async function run({ config, request, emit, importModule = (name) => import(name) }) {
  const sdk = await importModule("@strands-agents/sdk");
  const model = await buildModel(config, request.model ?? config.model, importModule);
  const agent = await loadAgent(config, model, request.session_id, importModule);
  registerPermissions(agent, sdk, config.permissions ?? {});
  const translator = new Translator({ reasoning: request.reasoning === true });
  for await (const item of agent.stream(request.prompt)) {
    for (const translated of translator.feed(item)) {
      emit(translated);
    }
  }
}

function protocolEmitter() {
  const write = process.stdout.write.bind(process.stdout);
  process.stdout.write = process.stderr.write.bind(process.stderr);
  return (item) => write(`${JSON.stringify(item)}\n`);
}

function readStdin() {
  try {
    return readFileSync(0, "utf8");
  } catch {
    return "";
  }
}

export async function main(argv) {
  const emit = protocolEmitter();
  let config;
  let request;
  try {
    config = JSON.parse(readFileSync(argv[2], "utf8"));
    request = JSON.parse(readStdin());
  } catch {
    emit(event("agent_failed", { reason: "protocol_error", detail_code: "invalid_input" }));
    return;
  }
  if (!validRequest(request)) {
    emit(event("agent_failed", { reason: "protocol_error", detail_code: "invalid_request" }));
    return;
  }
  try {
    await run({ config, request, emit });
  } catch (error) {
    const [reason, detail] = classify(error);
    emit(event("agent_failed", { reason, detail_code: detail, session_id: request.session_id }));
    return;
  }
  emit(event("done", { session_id: request.session_id }));
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  await main(process.argv);
}
