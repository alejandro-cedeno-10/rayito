// Puente de Node del host del agente (ADR-028, D2, D3 y D9; 0.14, D3). Lo ejecuta
// el driver `node-callable` del host como `node node_bridge.mjs <config.json>`:
// importa el agente que escribiste tú, lo ejecuta con la petición que llega por
// stdin (`{"v":1,"prompt","session_id","model","reasoning"}`) y escribe el
// protocolo JSONL v1 de Rayito por stdout (el evento `session` ya lo emitió el
// driver, que además le da el `session_id`). El prompt nunca va en argv.
//
// Un puente, un adaptador por framework (`config.adapter`):
//
//   - `strands` (por defecto): tu `Agent` de `@strands-agents/sdk`. El módulo
//     exporta `agent` o `createAgent(ctx)` (también vale `default`); `ctx.model`
//     es el modelo ya cableado a la pasarela. `AgentPermissions` se aplica a
//     toda llamada de herramienta con un gancho `BeforeToolCallEvent`.
//   - `langgraph`: un grafo compilado de LangGraph.js o de deepagents-js
//     (`graph`, `agent` o `createGraph(ctx)`), que se ejecuta con
//     `graph.stream({ messages }, { configurable: { thread_id } })`. Si el grafo
//     tiene `getInputJsonSchema` (LangGraph.js 1.1 o posterior) y su esquema no
//     es de mensajes, se ejecuta con el objeto `input` de la petición y devuelve
//     su último estado como `output` en `done` (ADR-033 D3).
//   - `vercel-ai`: un agente del AI SDK (`ToolLoopAgent`: `agent`, o
//     `createAgent(ctx)`), con `agent.stream()` o, si no lo tiene,
//     `agent.generate()`.
//   - `callable`: una función `(request) => string | iterable | { text, usage }`
//     (síncrona o asíncrona; un iterable de cadenas sale como `text_delta`).
//
// `entrypoint` es una ruta bajo `app_dir`, con `#nombre` para elegir la
// exportación (`agent.ts#graph`). Node 22.18 o posterior ejecuta `.ts`
// directamente, sin compilar. El modelo llega siempre por la pasarela de
// secretos con un marcador como clave (las variables estándar del proveedor que
// pone el host), nunca con credenciales de AWS. Los adaptadores que no son de
// Strands no pueden interponerse en las herramientas de tu framework: los
// permisos de herramienta sólo los aplica `strands`. Sólo usa la biblioteca
// estándar de Node y el SDK que instala el gestor de paquetes de tu proyecto.

import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import Module from "node:module";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

export const PROTOCOL_VERSION = 1;
export const MAX_TOOL_OUTPUT_BYTES = 65536;
export const DENIED_TOOL_MESSAGE = "Permiso denegado por la configuración del agente.";
export const ANTHROPIC_MAX_TOKENS = 8192;
export const DEFAULT_ADAPTER = "strands";
// El cliente de Bedrock del AWS SDK de JavaScript habla HTTP/2 siempre, y la
// pasarela de secretos del sandbox sólo HTTP/1.1. Con esta variable (el
// endpoint de `bedrock-runtime` que pone el host cuando el modelo va por la
// pasarela) el puente cambia `NodeHttp2Handler` por `NodeHttpHandler` en cada
// copia de `@smithy/node-http-handler` que se cargue, antes de que se cree
// ningún cliente. Es la receta que documenta Strands para pasar por un proxy.
export const GATEWAY_ENDPOINT_ENV = "AWS_ENDPOINT_URL_BEDROCK_RUNTIME";
export const GRAPH_STREAM_MODE = "updates";
export const ENTRYPOINT_SEPARATOR = "#";
export const HISTORY_MAX_MESSAGES = 200;
export const SESSION_FILE_MODE = 0o600;
export const SESSION_DIR_MODE = 0o700;
// Entrada estructurada (ADR-033 D3): el modo de `stream` con el estado completo,
// la clave de los mensajes de un grafo de mensajes, el tope de `output`
// (`AGENT_SERVICE_MAX_OUTPUT_BYTES` de `limits.json`) y los `detail_code`.
export const VALUES_STREAM_MODE = "values";
export const MESSAGES_KEY = "messages";
export const MAX_SERVICE_OUTPUT_BYTES = 1048576;
export const INPUT_REQUIRED = "input_required";
export const INVALID_INPUT = "invalid_input";
export const OUTPUT_NOT_SERIALIZABLE = "output_not_serializable";
const JSON_TYPE_CHECKS = {
  string: (value) => typeof value === "string",
  integer: (value) => Number.isInteger(value),
  number: (value) => typeof value === "number",
  boolean: (value) => typeof value === "boolean",
  object: (value) => isPlainObject(value),
  array: (value) => Array.isArray(value),
  null: (value) => value === null,
};
// Clases de error del modelo o de la pasarela: las de Strands, las del AI SDK
// y las del cliente de Bedrock de AWS (que usan `@langchain/aws` y los
// proveedores de Bedrock). Se reconocen por nombre de clase, nunca por mensaje.
const MODEL_ERROR_NAMES = new Set([
  "ModelError",
  "ContextWindowOverflowError",
  "MaxTokensError",
  "ModelThrottledError",
  "ProviderTokenCountError",
  "APICallError",
  "AI_APICallError",
  "RetryError",
  "AI_RetryError",
  "ThrottlingException",
  "ValidationException",
  "AccessDeniedException",
  "ModelTimeoutException",
  "ModelErrorException",
  "ModelNotReadyException",
  "ServiceUnavailableException",
  "InternalServerException",
]);
const FINISH_REASONS = {
  stop: "end_turn",
  length: "max_tokens",
  "content-filter": "content_filtered",
  "tool-calls": "tool_use",
};
const AI_KINDS = new Set(["ai", "assistant"]);
const TOOL_KINDS = new Set(["tool"]);
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
// Las únicas variables que el host puede pasar con las credenciales del rol de
// AgentCore (`aws_env`), para el cliente de Bedrock de los frameworks que sólo las
// leen del entorno.
const AWS_ENV_NAMES = ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"];
const DETAIL_CODE_PATTERN = /[^a-z0-9_]/g;
const MAX_DETAIL_CODE_LENGTH = 64;

function isSmithyHandlers(exported) {
  return (
    exported !== null &&
    typeof exported === "object" &&
    typeof exported.NodeHttp2Handler === "function" &&
    typeof exported.NodeHttpHandler === "function" &&
    exported.NodeHttp2Handler !== exported.NodeHttpHandler
  );
}

// Cada módulo CommonJS que se cargue a partir de ahora y exporte los dos
// manejadores de `@smithy/node-http-handler` sale con el de HTTP/1.1 en los dos
// nombres. Devuelve la función que deshace el cambio (para las pruebas).
export function forceHttp1ForAwsSdk(loader = Module) {
  const original = loader._load;
  loader._load = function patchedLoad(...args) {
    const exported = original.apply(this, args);
    if (isSmithyHandlers(exported)) {
      exported.NodeHttp2Handler = exported.NodeHttpHandler;
    }
    return exported;
  };
  return () => {
    loader._load = original;
  };
}

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

// El uso con los nombres de Rayito (`input`, `output`, `reasoning`,
// `cache_read`, `cache_write`) tal como lo devuelve una función tuya.
export function rayitoUsage(raw) {
  const source = raw && typeof raw === "object" ? raw : {};
  const usage = usageOf(null);
  for (const name of Object.keys(usage)) {
    usage[name] = count(source[name]);
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

// Un fallo propio del puente (una exportación que no existe, un adaptador
// desconocido): lleva su `detail_code`, que nunca es texto de nadie.
export class BridgeError extends Error {
  constructor(code) {
    super(code);
    this.code = code;
  }
}

// Un fallo de la entrada o la salida estructurada de un grafo
// (`protocol_error` con `input_required`, `invalid_input` u
// `output_not_serializable`), nunca con el valor.
export class InputError extends Error {
  constructor(code) {
    super(code);
    this.code = code;
  }
}

export function sanitizeDetailCode(value) {
  return String(value).toLowerCase().replace(DETAIL_CODE_PATTERN, "_").slice(0, MAX_DETAIL_CODE_LENGTH);
}

export function classify(error) {
  if (error instanceof BridgeError) {
    return ["runtime_error", sanitizeDetailCode(error.code)];
  }
  if (error instanceof InputError) {
    return ["protocol_error", sanitizeDetailCode(error.code)];
  }
  const detail = sanitizeDetailCode(error?.constructor?.name ?? "error");
  for (let current = error, depth = 0; current && depth < 6; depth += 1) {
    if (MODEL_ERROR_NAMES.has(current.constructor?.name) || MODEL_ERROR_NAMES.has(current.name)) {
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

export function parseEntrypoint(value) {
  const text = String(value ?? "");
  const at = text.indexOf(ENTRYPOINT_SEPARATOR);
  return at < 0
    ? { path: text, exportName: null }
    : { path: text.slice(0, at), exportName: text.slice(at + 1) };
}

// La exportación de `config.entrypoint` (`ruta` o `ruta#nombre`) bajo
// `app_dir`: la que se nombra o, si no, la primera de `preferred` que exista
// y, al final, `default`.
export async function loadTarget(config, preferred, importModule = (name) => import(name)) {
  const { path, exportName } = parseEntrypoint(config.entrypoint);
  const module = await importModule(pathToFileURL(resolve(String(config.app_dir), path)).href);
  if (exportName) {
    if (module[exportName] === undefined) {
      throw new BridgeError("entrypoint_not_found");
    }
    return module[exportName];
  }
  for (const name of preferred) {
    if (module[name] !== undefined) {
      return module[name];
    }
  }
  return module.default;
}

export function factoryContext(config, request, extra = {}) {
  return {
    instructions: config.instructions ?? null,
    workdir: config.workdir,
    stateDir: config.state_dir,
    sessionId: request.session_id,
    sessionsDir: config.sessions_dir,
    ...extra,
  };
}

export async function loadAgent(config, model, sessionId, importModule = (name) => import(name)) {
  const target = await loadTarget(config, ["agent", "createAgent"], importModule);
  if (target && typeof target.stream === "function") {
    return target;
  }
  if (typeof target !== "function") {
    throw new BridgeError("invalid_entrypoint");
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
    (request.input === undefined || isPlainObject(request.input)) &&
    typeof request.session_id === "string" &&
    SESSION_ID_PATTERN.test(request.session_id)
  );
}

async function runStrands({ config, request, emit, importModule }) {
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

// ---- historial de sesión de los marcos sin estado --------------------------

// Un marco que no guarda la conversación por su cuenta (el AI SDK, un grafo sin
// `checkpointer`) la guarda aquí, un fichero por sesión bajo `sessions_dir`:
// sólo el texto de cada turno, que ambos marcos aceptan como `{ role, content }`.
// El puente es un proceso por petición, así que sin esto no habría memoria.
export class HistoryStore {
  constructor(directory, sessionId) {
    this.path = directory ? resolve(String(directory), `${sessionId}.json`) : null;
  }

  load() {
    if (!this.path) {
      return [];
    }
    try {
      const messages = JSON.parse(readFileSync(this.path, "utf8"));
      return Array.isArray(messages) ? messages : [];
    } catch {
      return [];
    }
  }

  save(history, prompt, answer) {
    if (!this.path) {
      return;
    }
    const turns = [...history, { role: "user", content: prompt }];
    if (answer) {
      turns.push({ role: "assistant", content: answer });
    }
    mkdirSync(resolve(this.path, ".."), { recursive: true, mode: SESSION_DIR_MODE });
    writeFileSync(this.path, JSON.stringify(turns.slice(-HISTORY_MAX_MESSAGES)), {
      mode: SESSION_FILE_MODE,
    });
  }
}

function stringifyOutput(value) {
  if (typeof value === "string") {
    return value;
  }
  try {
    return JSON.stringify(value) ?? "";
  } catch {
    return "";
  }
}

function plainInput(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : null;
}

function toolCallEvent({ callId, name, status, input, output }) {
  const [preview, truncated] = truncateOutput(output);
  return event("tool_call", {
    call_id: String(callId ?? ""),
    name: String(name ?? ""),
    status,
    input: plainInput(input),
    output: preview,
    output_truncated: truncated,
  });
}

// ---- adaptador `callable` ---------------------------------------------------

async function collectCallable(result, emit) {
  const value = await result;
  if (typeof value === "string") {
    return [value, null];
  }
  if (value && typeof value === "object" && typeof value.text === "string") {
    return [value.text, value.usage ?? null];
  }
  const source = value?.[Symbol.asyncIterator] ?? value?.[Symbol.iterator];
  if (typeof source !== "function") {
    throw new BridgeError("invalid_response");
  }
  const parts = [];
  for await (const chunk of value) {
    if (typeof chunk !== "string") {
      throw new BridgeError("invalid_response");
    }
    if (chunk) {
      parts.push(chunk);
      emit(event("text_delta", { text: chunk }));
    }
  }
  return [parts.join(""), null];
}

export async function runCallable({ config, request, emit, importModule }) {
  const target = await loadTarget(config, ["handler", "run"], importModule);
  if (typeof target !== "function") {
    throw new BridgeError("invalid_entrypoint");
  }
  emit(event("step_started", { index: 1 }));
  const [text, usage] = await collectCallable(
    target({
      prompt: request.prompt,
      session_id: request.session_id,
      model: request.model ?? null,
      reasoning: request.reasoning === true,
    }),
    emit,
  );
  emit(event("text", { text }));
  emit(
    event("step_finished", {
      index: 1,
      usage: usage ? rayitoUsage(usage) : null,
      finish_reason: "end_turn",
    }),
  );
}

// ---- adaptador `langgraph` (LangGraph.js y deepagents-js) ------------------

export function messageKind(message) {
  const raw =
    message?.type ??
    message?.role ??
    (typeof message?._getType === "function" ? message._getType() : null) ??
    (typeof message?.getType === "function" ? message.getType() : "");
  return String(raw).toLowerCase();
}

export function contentText(content) {
  if (typeof content === "string") {
    return content;
  }
  if (!Array.isArray(content)) {
    return "";
  }
  return content
    .map((block) => (typeof block === "string" ? block : block?.type === "text" ? block.text : ""))
    .filter((text) => typeof text === "string")
    .join("");
}

export function graphUsage(raw) {
  const source = raw && typeof raw === "object" ? raw : {};
  const details = source.input_token_details ?? {};
  return {
    input: count(source.input_tokens),
    output: count(source.output_tokens),
    reasoning: count(source.output_token_details?.reasoning),
    cache_read: count(details.cache_read),
    cache_write: count(details.cache_creation),
  };
}

// `graph.stream(..., { streamMode: "updates" })` a eventos v1: cada mensaje del
// asistente es un paso (texto y uso) y cada mensaje de herramienta, una llamada
// resuelta con los argumentos que el asistente pidió. Los grafos de LangGraph.js y
// de deepagents-js comparten esta forma (`{ nodo: { messages } }`).
export class GraphTranslator {
  constructor() {
    this.steps = 0;
    this.pending = new Map();
    this.answer = "";
  }

  feed(chunk) {
    const events = [];
    for (const update of Object.values(chunk && typeof chunk === "object" ? chunk : {})) {
      const messages = update?.messages;
      for (const message of Array.isArray(messages) ? messages : messages ? [messages] : []) {
        events.push(...this.#message(message));
      }
    }
    return events;
  }

  #message(message) {
    const kind = messageKind(message);
    if (AI_KINDS.has(kind)) {
      return this.#assistant(message);
    }
    if (TOOL_KINDS.has(kind)) {
      return [this.#tool(message)];
    }
    return [];
  }

  #assistant(message) {
    this.steps += 1;
    const calls = Array.isArray(message.tool_calls) ? message.tool_calls : [];
    for (const call of calls) {
      this.pending.set(String(call.id ?? ""), { name: call.name, input: call.args });
    }
    const events = [event("step_started", { index: this.steps })];
    const text = contentText(message.content);
    if (text) {
      events.push(event("text", { text }));
    }
    if (calls.length === 0) {
      this.answer = text;
    }
    events.push(
      event("step_finished", {
        index: this.steps,
        usage: graphUsage(message.usage_metadata),
        finish_reason: calls.length > 0 ? "tool_use" : "end_turn",
      }),
    );
    return events;
  }

  #tool(message) {
    const known = this.pending.get(String(message.tool_call_id ?? "")) ?? {};
    return toolCallEvent({
      callId: message.tool_call_id,
      name: message.name ?? known.name,
      status: message.status === "error" ? "error" : "completed",
      input: known.input,
      output: contentText(message.content) || stringifyOutput(message.content),
    });
  }
}

async function loadGraph(config, request, importModule) {
  const target = await loadTarget(config, ["graph", "agent", "createGraph", "createAgent"], importModule);
  if (target && typeof target.stream === "function") {
    return target;
  }
  if (typeof target !== "function") {
    throw new BridgeError("invalid_entrypoint");
  }
  return target(factoryContext(config, request, { modelId: request.model ?? config.model }));
}

export async function runLanggraph({ config, request, emit, importModule }) {
  const graph = await loadGraph(config, request, importModule);
  const schema = await structuredInputSchema(graph);
  if (schema !== undefined) {
    return runStructuredGraph({ graph, schema, request, emit });
  }
  const store = new HistoryStore(graph.checkpointer ? null : config.sessions_dir, request.session_id);
  const history = store.load();
  const input =
    request.input === undefined
      ? { messages: [...history, { role: "user", content: request.prompt }] }
      : messagesInput(request.input, history);
  const options = { configurable: { thread_id: request.session_id } };
  const translator = new GraphTranslator();
  for await (const chunk of await graph.stream(input, { ...options, streamMode: GRAPH_STREAM_MODE })) {
    for (const translated of translator.feed(chunk)) {
      emit(translated);
    }
  }
  if (request.input === undefined) {
    store.save(history, request.prompt, translator.answer);
  }
  return undefined;
}

function isPlainObject(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

// El esquema de entrada de un grafo que no es de mensajes, o `undefined`: sin
// `getInputJsonSchema`, si falla, si no tiene `properties` o si tiene
// `messages`, el grafo se ejecuta como siempre.
export async function structuredInputSchema(graph) {
  if (typeof graph?.getInputJsonSchema !== "function") {
    return undefined;
  }
  let schema;
  try {
    schema = await graph.getInputJsonSchema();
  } catch {
    return undefined;
  }
  const properties = schema?.properties;
  return isPlainObject(properties) && !(MESSAGES_KEY in properties) ? schema : undefined;
}

// Un `input` para un grafo de mensajes: sus claves tal cual, con sus
// `messages` detrás del historial de la sesión.
function messagesInput(input, history) {
  const own = Array.isArray(input[MESSAGES_KEY]) ? input[MESSAGES_KEY] : [];
  return { ...input, [MESSAGES_KEY]: [...history, ...own] };
}

function matchesSchema(value, spec) {
  if (!isPlainObject(spec)) {
    return true;
  }
  if (spec.type !== undefined) {
    const checks = (Array.isArray(spec.type) ? spec.type : [spec.type])
      .map((name) => JSON_TYPE_CHECKS[name])
      .filter((check) => check !== undefined);
    return checks.length === 0 || checks.some((check) => check(value));
  }
  const branches = spec.anyOf ?? spec.oneOf;
  return Array.isArray(branches) ? branches.some((branch) => matchesSchema(value, branch)) : true;
}

// Lo básico del esquema, como el driver de Python: un objeto, las claves de
// `required`, ninguna de más con `additionalProperties: false` y el tipo de
// cada clave de primer nivel.
export function validateInput(schema, data) {
  const known = isPlainObject(schema.properties) ? schema.properties : {};
  const required = Array.isArray(schema.required) ? schema.required : [];
  const valid =
    isPlainObject(data) &&
    required.every((key) => key in data) &&
    (schema.additionalProperties !== false || Object.keys(data).every((key) => key in known)) &&
    Object.entries(data).every(([key, value]) => matchesSchema(value, known[key]));
  if (!valid) {
    throw new InputError(INVALID_INPUT);
  }
}

async function outputKeys(graph) {
  if (typeof graph.getOutputJsonSchema !== "function") {
    return undefined;
  }
  try {
    const properties = (await graph.getOutputJsonSchema())?.properties;
    return isPlainObject(properties) ? new Set(Object.keys(properties)) : undefined;
  } catch {
    return undefined;
  }
}

function jsonable(value) {
  if (typeof value?.toDict === "function") {
    return value.toDict();
  }
  if (Array.isArray(value)) {
    return value.map(jsonable);
  }
  if (isPlainObject(value)) {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, jsonable(item)]));
  }
  return value;
}

// El último estado filtrado al esquema de salida y en JSON (los mensajes, con
// `toDict()`); lo que no se codifica o no cabe es `output_not_serializable`.
export async function structuredOutput(graph, values) {
  const keys = await outputKeys(graph);
  const state = isPlainObject(values) ? values : {};
  const selected = Object.fromEntries(
    Object.entries(state).filter(([key]) => keys === undefined || keys.has(key)),
  );
  let encoded;
  try {
    encoded = JSON.stringify(jsonable(selected));
  } catch {
    throw new InputError(OUTPUT_NOT_SERIALIZABLE);
  }
  if (encoded === undefined || Buffer.byteLength(encoded, "utf8") > MAX_SERVICE_OUTPUT_BYTES) {
    throw new InputError(OUTPUT_NOT_SERIALIZABLE);
  }
  return JSON.parse(encoded);
}

// Un grafo con esquema de entrada propio: exige `input`, lo comprueba y lo
// pasa con `thread_id` igual a la sesión; devuelve la salida estructurada.
async function runStructuredGraph({ graph, schema, request, emit }) {
  if (request.input === undefined) {
    throw new InputError(INPUT_REQUIRED);
  }
  validateInput(schema, request.input);
  const options = {
    configurable: { thread_id: request.session_id },
    streamMode: [GRAPH_STREAM_MODE, VALUES_STREAM_MODE],
  };
  const translator = new GraphTranslator();
  let last;
  for await (const [mode, chunk] of await graph.stream(request.input, options)) {
    if (mode === VALUES_STREAM_MODE) {
      last = chunk;
    } else {
      for (const translated of translator.feed(chunk)) {
        emit(translated);
      }
    }
  }
  return structuredOutput(graph, last);
}

// ---- adaptador `vercel-ai` (AI SDK) ----------------------------------------

export function aiUsage(raw) {
  const source = raw && typeof raw === "object" ? raw : {};
  return {
    input: count(source.inputTokens),
    output: count(source.outputTokens),
    reasoning: count(source.reasoningTokens ?? source.outputTokenDetails?.reasoningTokens),
    cache_read: count(source.cachedInputTokens ?? source.inputTokenDetails?.cacheReadTokens),
    cache_write: count(source.inputTokenDetails?.cacheWriteTokens),
  };
}

function finishReason(value) {
  return FINISH_REASONS[value] ?? (typeof value === "string" ? value : null);
}

// `result.fullStream` del AI SDK a eventos v1. Un paso va de `start-step` a
// `finish-step`; el texto llega en `text-delta` y se cierra como un `text` al
// terminar el paso; las herramientas, en `tool-call` y `tool-result`/`tool-error`.
export class AiStreamTranslator {
  constructor({ reasoning }) {
    this.reasoning = reasoning;
    this.steps = 0;
    this.text = [];
    this.thinking = [];
    this.pending = new Map();
    this.answer = "";
    this.failure = null;
  }

  feed(part) {
    switch (part?.type) {
      case "start-step":
        return this.#start();
      case "text-delta":
        return this.#delta(part);
      case "reasoning-delta":
        this.thinking.push(String(part.text ?? part.delta ?? ""));
        return [];
      case "tool-call":
        this.pending.set(String(part.toolCallId), { name: part.toolName, input: part.input });
        return [];
      case "tool-result":
        return [this.#tool(part, "completed", stringifyOutput(part.output))];
      case "tool-error":
        return [this.#tool(part, "error", String(part.error?.message ?? ""))];
      case "finish-step":
        return this.#finish(part);
      case "error":
        this.failure = part.error ?? new Error("stream_error");
        return [];
      default:
        return [];
    }
  }

  #start() {
    this.steps += 1;
    this.text = [];
    this.thinking = [];
    return [event("step_started", { index: this.steps })];
  }

  #delta(part) {
    const text = String(part.text ?? part.delta ?? part.textDelta ?? "");
    this.text.push(text);
    return text ? [event("text_delta", { text })] : [];
  }

  #tool(part, status, output) {
    const known = this.pending.get(String(part.toolCallId)) ?? {};
    return toolCallEvent({
      callId: part.toolCallId,
      name: part.toolName ?? known.name,
      status,
      input: part.input ?? known.input,
      output,
    });
  }

  #finish(part) {
    const events = [];
    const text = this.text.join("");
    if (text) {
      events.push(event("text", { text }));
    }
    const thinking = this.thinking.join("");
    if (this.reasoning && thinking) {
      events.push(event("reasoning", { text: thinking }));
    }
    if (part.finishReason !== "tool-calls") {
      this.answer = text;
    }
    events.push(
      event("step_finished", {
        index: this.steps,
        usage: aiUsage(part.usage),
        finish_reason: finishReason(part.finishReason),
      }),
    );
    return events;
  }
}

// El resultado de `agent.generate()` (sin streaming) como la misma secuencia de
// partes que `fullStream`: un paso por cada elemento de `result.steps`, o uno
// solo con `result.text` si el agente no los informa.
export function* generatedParts(result) {
  const steps = Array.isArray(result?.steps) && result.steps.length > 0 ? result.steps : [result];
  for (const step of steps) {
    yield { type: "start-step" };
    for (const call of step?.toolCalls ?? []) {
      yield { type: "tool-call", ...call };
    }
    for (const output of step?.toolResults ?? []) {
      yield { type: "tool-result", ...output };
    }
    if (typeof step?.text === "string" && step.text) {
      yield { type: "text-delta", text: step.text };
    }
    yield { type: "finish-step", usage: step?.usage, finishReason: step?.finishReason };
  }
}

async function loadAiAgent(config, request, importModule) {
  const target = await loadTarget(config, ["agent", "createAgent"], importModule);
  if (target && (typeof target.stream === "function" || typeof target.generate === "function")) {
    return target;
  }
  if (typeof target !== "function") {
    throw new BridgeError("invalid_entrypoint");
  }
  return target(factoryContext(config, request, { modelId: request.model ?? config.model }));
}

async function* aiParts(agent, messages) {
  if (typeof agent.stream === "function") {
    yield* (await agent.stream({ messages })).fullStream;
    return;
  }
  yield* generatedParts(await agent.generate({ messages }));
}

export async function runVercelAi({ config, request, emit, importModule }) {
  const agent = await loadAiAgent(config, request, importModule);
  const store = new HistoryStore(config.sessions_dir, request.session_id);
  const history = store.load();
  const messages = [...history, { role: "user", content: request.prompt }];
  const translator = new AiStreamTranslator({ reasoning: request.reasoning === true });
  for await (const part of aiParts(agent, messages)) {
    for (const translated of translator.feed(part)) {
      emit(translated);
    }
    if (translator.failure) {
      throw translator.failure;
    }
  }
  store.save(history, request.prompt, translator.answer);
}

// ---- despacho ---------------------------------------------------------------

export const ADAPTERS = {
  strands: runStrands,
  langgraph: runLanggraph,
  "vercel-ai": runVercelAi,
  callable: runCallable,
};

export async function run({ config, request, emit, importModule = (name) => import(name) }) {
  const adapter = ADAPTERS[config.adapter ?? DEFAULT_ADAPTER];
  if (!adapter) {
    throw new BridgeError("unknown_adapter");
  }
  return adapter({ config, request, emit, importModule });
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

// Ejecuta `action` con las credenciales de `awsEnv` en `process.env` y deja el
// entorno como estaba al terminar, con éxito o no. Sin `awsEnv` no toca nada.
export async function withAwsEnv(awsEnv, action) {
  const source = awsEnv && typeof awsEnv === "object" ? awsEnv : {};
  const names = AWS_ENV_NAMES.filter((name) => typeof source[name] === "string" && source[name]);
  const previous = new Map(names.map((name) => [name, process.env[name]]));
  for (const name of names) {
    process.env[name] = source[name];
  }
  try {
    return await action();
  } finally {
    for (const [name, value] of previous) {
      if (value === undefined) {
        delete process.env[name];
      } else {
        process.env[name] = value;
      }
    }
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
  if (process.env[GATEWAY_ENDPOINT_ENV]) {
    forceHttp1ForAwsSdk();
  }
  let output;
  try {
    output = await withAwsEnv(request.aws_env, () => run({ config, request, emit }));
  } catch (error) {
    const [reason, detail] = classify(error);
    emit(event("agent_failed", { reason, detail_code: detail, session_id: request.session_id }));
    return;
  }
  emit(event("done", { session_id: request.session_id, ...(isPlainObject(output) ? { output } : {}) }));
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  await main(process.argv);
}
