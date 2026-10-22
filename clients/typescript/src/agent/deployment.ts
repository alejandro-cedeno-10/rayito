/**
 * `AgentDeployment`: el cliente de un agente servido desde un MicroVM
 * (`AgentProject.deploy()`, ADR-028, D6). Espejo de
 * `rayito._agent._deployment` y `_deployment_async`. Habla
 * `rayito.agent-service/1` por el proxy de AWS con `fetch` y el JWE vigente
 * del sandbox; una respuesta `403` del proxy reacuña el token y reintenta una
 * vez.
 *
 * Construirlo no llama a nadie; el primer `invoke()`, `stream()` o `status()`
 * hace la primera petición. `connect()` reconstruye el cliente desde las
 * etiquetas del sandbox en cualquier proceso.
 */

import {
  AgentError,
  AuthenticationError,
  InvalidArgumentError,
  SandboxError,
  TimeoutError,
  UnimplementedError,
} from "../errors.js";
import {
  AGENT_RUN_SANDBOX_MARGIN_SECONDS,
  AGENT_SERVICE_HEALTH_PATH,
  AGENT_SERVICE_INVOKE_PATH,
  AGENT_SERVICE_MAX_REQUEST_BYTES,
  DEFAULT_AGENT_TIMEOUT_SECONDS,
} from "../limits.js";
import type { HostAccess } from "../models.js";
import type { SandboxConnectOptions } from "../sandbox/sandbox.js";
import {
  AGENT_FAILURE_REASONS,
  type AgentEvent,
  type AgentFailed,
  type AgentResult,
  agentFailed,
  agentFailedToError,
  type Done,
  failureMessage,
  type JsonObject,
  TokenUsage,
  type ToolCall,
} from "./events.js";
import { newProtocolV1State, type ProtocolV1State, parseV1Line } from "./protocol-v1.js";
import { type SseMessage, SseParser } from "./sse.js";

/** Etiquetas del sandbox de un despliegue (`Sandbox.create({ metadata })`):
 * `connect()` las lee para reconstruir el cliente. Van en el `runHookPayload`,
 * así que nunca llevan datos del usuario. */
export const AGENT_DEPLOYMENT_LABEL = "rayito.agent.deployment";
export const AGENT_DEPLOYMENT_PORT_LABEL = "rayito.agent.port";
/** Sólo con una app `raw` (MCP u otro protocolo expuesto sin traducir). */
export const AGENT_DEPLOYMENT_PROTOCOL_LABEL = "rayito.agent.protocol";
export const RAW_PROTOCOL = "raw";
export const JSON_CONTENT_TYPE = "application/json";
export const EVENT_STREAM_CONTENT_TYPE = "text/event-stream";
export const HTTP_OK = 200;
const HTTP_BAD_REQUEST = 400;
const HTTP_UNAUTHORIZED = 401;
const HTTP_FORBIDDEN = 403;
const HTTP_PAYLOAD_TOO_LARGE = 413;
const HTTP_TOO_MANY_REQUESTS = 429;
const HTTP_UNAVAILABLE = 503;
/** `Healthy` / `HealthyBusy` de `GET /ping` (contrato de AgentCore). */
const PING_BUSY = "HealthyBusy";
const PING_HEALTHY = "Healthy";
const MS_PER_SECOND = 1000;
/** Plazo de una petición: el de una ejecución del agente más el margen del
 * sandbox (`AgentLimits.timeoutMs` manda dentro del host). */
export const DEFAULT_DEPLOYMENT_REQUEST_TIMEOUT_MS =
  (DEFAULT_AGENT_TIMEOUT_SECONDS + AGENT_RUN_SANDBOX_MARGIN_SECONDS) * MS_PER_SECOND;
/** Plazo de `GET /ping`. */
export const PING_TIMEOUT_MS = 10_000;
/** Reintentos tras reacuñar el token del proxy (el 403 se reintenta una vez). */
const PROXY_REMINT_ATTEMPTS = 1;
/** Los mensajes de `invoke()`/`stream()` con `prompt` o `input` (ADR-033 D3):
 * los mismos en `rayito._agent._stream_base` y `_deployment_base`, comprobados
 * contra `testdata/agent-deploy/invocation-bodies.json`. Nunca llevan el valor. */
export const PROMPT_OR_INPUT_MESSAGE = "pasa prompt o input, uno de los dos";
export const INPUT_NOT_OBJECT_MESSAGE = "input debe ser un objeto JSON";
export const INPUT_TOO_LARGE_MESSAGE =
  "input no cabe en una petición ({maxBytes} bytes como mucho)";
/** Un `400` a una petición con `input` cuyo cuerpo es el `invalid_request` de
 * un host anterior a la entrada estructurada, que no conoce la clave: sin
 * `INPUT_SUPPORT_FIELD`. */
export const HOST_TOO_OLD_MESSAGE =
  "el agente desplegado no acepta input: vuelve a desplegarlo con esta versión de Rayito";
/** Un `400` a una petición con `input` cuyo cuerpo no es el de ningún host
 * conocido (un proxy, un cuerpo cortado): puede ser un host anterior o no. */
export const HOST_MAYBE_TOO_OLD_MESSAGE =
  "el agente desplegado rechazó la petición con input: si su host es anterior a la " +
  "entrada estructurada, vuelve a desplegarlo con esta versión de Rayito";
/** Un `400` o un `413` del host por un cuerpo que no acepta. */
export const REQUEST_REJECTED_MESSAGE =
  "el agente desplegado rechazó la petición: cuerpo inválido o demasiado grande";
/** El código del cuerpo `{"error": ...}` de un `400` del host. */
const INVALID_REQUEST_ERROR = "invalid_request";
/** Lo que el host con entrada estructurada añade a ese cuerpo (`true`): los
 * hosts anteriores no lo mandan (`INPUT_SUPPORT_FIELD` de `serve.py`). */
export const INPUT_SUPPORT_FIELD = "accepts_input";
const UTF8 = new TextEncoder();

export type DeploymentState = "healthy" | "busy" | "unavailable";

/** El estado de un despliegue según `GET /ping`: `healthy` (listo), `busy`
 * (con el cupo de ejecuciones lleno) o `unavailable` (el host no contesta o
 * aún no está listo). */
export interface DeploymentStatus {
  readonly state: DeploymentState;
  readonly ready: boolean;
  readonly busy: boolean;
  readonly port: number;
  readonly project: string | undefined;
}

export interface AgentInvokeOptions {
  readonly sessionId?: string | undefined;
  readonly model?: string | undefined;
  readonly reasoning?: boolean | undefined;
  readonly signal?: AbortSignal | undefined;
}

/** La petición de un grafo con esquema de entrada propio, en lugar de un
 * prompt: `invoke({ input: { ticket_id: "T-1" } })` (ADR-033 D3). */
export interface AgentInputRequest {
  readonly input: JsonObject;
}

/** Lo que `invoke()` y `stream()` aceptan como primer argumento. */
export type AgentInvocation = string | AgentInputRequest;

/** Lo que va en el cuerpo: exactamente uno de los dos. */
export interface InvocationPayload {
  readonly prompt?: string;
  readonly input?: JsonObject;
}

/** Lo que `AgentDeployment` necesita de un `Sandbox`: lo que `deploy()`
 * creó y lo que `connect()` reabre. */
export interface DeploymentSandbox {
  readonly sandboxId: string;
  readonly metadata: Readonly<Record<string, string>>;
  getHost(port: number): Promise<HostAccess>;
  kill(): Promise<boolean>;
  refreshProxyAccess(): Promise<void>;
  readonly accessToken: string;
  pause(options?: { readonly wait?: boolean | undefined }): Promise<boolean>;
  resume(options?: { readonly wait?: boolean | undefined }): Promise<void>;
  setTimeout(timeoutMs: number): Promise<void>;
}

export interface AgentDeploymentOptions {
  readonly sandbox: DeploymentSandbox;
  readonly port: number;
  readonly hostAccess: HostAccess;
  readonly project?: string | undefined;
  readonly raw?: boolean | undefined;
  readonly requestTimeoutMs?: number | undefined;
  /** El `fetch` a usar; por defecto el global. */
  readonly fetch?: typeof fetch | undefined;
}

export interface DeploymentLabels {
  readonly project: string;
  readonly port: number;
  readonly raw: boolean;
}

/** Las etiquetas con las que `deploy()` marca el sandbox. */
export function labelsFor(
  project: string,
  port: number,
  options: { readonly raw: boolean },
): Record<string, string> {
  const labels: Record<string, string> = {
    [AGENT_DEPLOYMENT_LABEL]: project,
    [AGENT_DEPLOYMENT_PORT_LABEL]: String(port),
  };
  if (options.raw) {
    labels[AGENT_DEPLOYMENT_PROTOCOL_LABEL] = RAW_PROTOCOL;
  }
  return labels;
}

/** Las etiquetas de un despliegue, o `InvalidArgumentError` si el sandbox no
 * lo es. */
export function parseLabels(
  metadata: Readonly<Record<string, string>> | undefined,
): DeploymentLabels {
  const values = metadata ?? {};
  const project = values[AGENT_DEPLOYMENT_LABEL];
  const portText = values[AGENT_DEPLOYMENT_PORT_LABEL];
  if (project === undefined || portText === undefined || !/^\d+$/.test(portText)) {
    throw new InvalidArgumentError("el sandbox no es un despliegue de agente de Rayito");
  }
  return {
    project,
    port: Number(portText),
    raw: values[AGENT_DEPLOYMENT_PROTOCOL_LABEL] === RAW_PROTOCOL,
  };
}

/** Exactamente uno de los dos: un `prompt` no vacío (como siempre) o un
 * `input` que sea un objeto JSON y quepa en `AGENT_SERVICE_MAX_REQUEST_BYTES`.
 * Si no, `InvalidArgumentError` antes de cualquier llamada. */
export function requirePromptOrInput(prompt: unknown, input: unknown): InvocationPayload {
  if ((prompt === undefined) === (input === undefined)) {
    throw new InvalidArgumentError(PROMPT_OR_INPUT_MESSAGE);
  }
  if (input === undefined) {
    return { prompt: requirePrompt(prompt) };
  }
  return { input: requireInputObject(input) };
}

/** `input` si es un objeto JSON (valores codificables) que cabe en una
 * petición. Como en Python (`json.dumps(..., allow_nan=False)`), un número no
 * finito, `undefined`, una función o un símbolo es un error y no un `null` o
 * una clave que `JSON.stringify` callaría; y, como Python rechaza un
 * `datetime`, sólo valen objetos planos (prototipo `Object.prototype` o
 * `null`): un `Date`, un `Map`, una instancia de clase o un objeto con un
 * `toJSON` heredado son un error, no lo que `JSON.stringify` haría de ellos. */
export function requireInputObject(input: unknown): JsonObject {
  if (!isPlainRecord(input) || !isJsonValue(input)) {
    throw new InvalidArgumentError(INPUT_NOT_OBJECT_MESSAGE);
  }
  let encoded: string;
  try {
    encoded = JSON.stringify(input);
  } catch {
    throw new InvalidArgumentError(INPUT_NOT_OBJECT_MESSAGE);
  }
  if (UTF8.encode(encoded).length > AGENT_SERVICE_MAX_REQUEST_BYTES) {
    throw new InvalidArgumentError(
      INPUT_TOO_LARGE_MESSAGE.replace("{maxBytes}", String(AGENT_SERVICE_MAX_REQUEST_BYTES)),
    );
  }
  return input;
}

/** El `prompt` o el `input` del primer argumento de `invoke()`/`stream()`. */
export function invocationOf(request: unknown): InvocationPayload {
  if (typeof request === "string") {
    return { prompt: request };
  }
  if (isRecord(request) && Object.hasOwn(request, "input")) {
    return { input: request.input as JsonObject };
  }
  return {};
}

/** El cuerpo JSON de `POST /invocations`: exactamente uno de `prompt` o
 * `input` (`requirePromptOrInput`) y sólo lo demás que se pidió. Con `input`,
 * el cuerpo entero (no sólo `input`) tiene que caber en
 * `AGENT_SERVICE_MAX_REQUEST_BYTES`, el tope del host; con un prompt no se
 * comprueba aquí, como siempre. */
export function invocationBody(
  prompt: string | undefined,
  options: {
    sessionId?: string | undefined;
    model?: string | undefined;
    reasoning?: boolean | undefined;
    input?: unknown;
  },
): string {
  const checked = requirePromptOrInput(prompt, options.input);
  const payload: Record<string, unknown> =
    checked.input === undefined ? { prompt: checked.prompt } : { input: checked.input };
  if (options.sessionId !== undefined) {
    payload.session_id = options.sessionId;
  }
  if (options.model !== undefined) {
    payload.model = options.model;
  }
  if (options.reasoning === true) {
    payload.reasoning = true;
  }
  const body = JSON.stringify(payload);
  if (checked.input !== undefined && UTF8.encode(body).length > AGENT_SERVICE_MAX_REQUEST_BYTES) {
    throw new InvalidArgumentError(
      INPUT_TOO_LARGE_MESSAGE.replace("{maxBytes}", String(AGENT_SERVICE_MAX_REQUEST_BYTES)),
    );
  }
  return body;
}

/** La excepción de un código HTTP del contrato. `403` es del proxy de AWS
 * (JWE ausente o caducado): `proxyRejected`, que el llamante reintenta una
 * vez tras reacuñar el token; `401` es el token del contenedor, que no existe
 * en un MicroVM. Un `400` a una petición con `input` (`sentInput`) se explica
 * con el cuerpo del host (`badRequestAfterInputMessage`). */
export function httpError(
  status: number,
  options: { readonly sentInput?: boolean | undefined; readonly body?: string | undefined } = {},
): Error {
  if (status === HTTP_BAD_REQUEST && options.sentInput === true) {
    return new InvalidArgumentError(badRequestAfterInputMessage(options.body));
  }
  if (status === HTTP_TOO_MANY_REQUESTS) {
    return new AgentError(failureMessage("busy"), { reason: "busy" });
  }
  if (status === HTTP_FORBIDDEN || status === HTTP_UNAUTHORIZED) {
    return new AuthenticationError("el despliegue rechazó las credenciales de la petición", {
      proxyRejected: status === HTTP_FORBIDDEN,
    });
  }
  if (status === HTTP_UNAVAILABLE) {
    return new TimeoutError("el agente desplegado aún no está listo");
  }
  if (status === HTTP_BAD_REQUEST || status === HTTP_PAYLOAD_TOO_LARGE) {
    return new InvalidArgumentError(REQUEST_REJECTED_MESSAGE);
  }
  return new SandboxError(`el agente desplegado respondió con un estado inesperado (${status})`, {
    statusCode: status,
  });
}

/** El mensaje de un `400` a una petición con `input`, según el cuerpo del
 * host: el `invalid_request` sin `INPUT_SUPPORT_FIELD` es un host anterior
 * (`HOST_TOO_OLD_MESSAGE`); con él, una petición que el host no acepta
 * (`REQUEST_REJECTED_MESSAGE`); cualquier otro cuerpo, o ninguno, no lo dice
 * (`HOST_MAYBE_TOO_OLD_MESSAGE`). Nunca repite el cuerpo. */
export function badRequestAfterInputMessage(body: string | undefined): string {
  let payload: unknown;
  try {
    payload = body ? JSON.parse(body) : undefined;
  } catch {
    payload = undefined;
  }
  if (!isRecord(payload) || payload.error !== INVALID_REQUEST_ERROR) {
    return HOST_MAYBE_TOO_OLD_MESSAGE;
  }
  return payload[INPUT_SUPPORT_FIELD] === true ? REQUEST_REJECTED_MESSAGE : HOST_TOO_OLD_MESSAGE;
}

/** Si `value` se escribe en JSON tal cual: `null`, texto, booleanos,
 * números finitos, listas y objetos planos de esos valores. Un ciclo es
 * `false`: `seen` lleva los contenedores del camino actual. */
function isJsonValue(value: unknown, seen: Set<object> = new Set()): boolean {
  if (value === null || typeof value === "string" || typeof value === "boolean") {
    return true;
  }
  if (typeof value === "number") {
    return Number.isFinite(value);
  }
  if (typeof value !== "object" || seen.has(value)) {
    return false;
  }
  if (!Array.isArray(value) && !isPlainRecord(value)) {
    return false;
  }
  seen.add(value);
  const valid = (Array.isArray(value) ? value : Object.values(value)).every((item) =>
    isJsonValue(item, seen),
  );
  seen.delete(value);
  return valid;
}

/** Un objeto plano: prototipo `Object.prototype` o `null`. Un `toJSON` sólo
 * puede llegar heredado (uno propio es una función, que `isJsonValue`
 * rechaza, o un dato, que `JSON.stringify` escribe tal cual). */
function isPlainRecord(value: unknown): value is Record<string, unknown> {
  if (!isRecord(value)) {
    return false;
  }
  const prototype: unknown = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function countOf(value: unknown): number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : 0;
}

/** La salida estructurada (`output`) de un cuerpo o de un evento `done`, si
 * es un objeto, como campo opcional. */
function outputField(payload: Record<string, unknown>): { readonly output?: JsonObject } {
  return isRecord(payload.output) ? { output: payload.output } : {};
}

function usageOf(raw: unknown): TokenUsage {
  const source = isRecord(raw) ? raw : {};
  return new TokenUsage({
    input: countOf(source.input),
    output: countOf(source.output),
    reasoning: countOf(source.reasoning),
    cacheRead: countOf(source.cache_read),
    cacheWrite: countOf(source.cache_write),
  });
}

function failureOf(payload: Record<string, unknown>, sessionId: string | undefined): AgentFailed {
  const reason = payload.reason;
  const detail = payload.detail_code;
  return agentFailed(
    typeof reason === "string" && (AGENT_FAILURE_REASONS as readonly string[]).includes(reason)
      ? reason
      : "protocol_error",
    { detailCode: typeof detail === "string" ? detail : undefined, sessionId },
  );
}

/** El `AgentResult` de la respuesta JSON de `POST /invocations`. Un `failed`
 * es `AgentError` (como `sbx.agent.run()`); un cuerpo fuera del contrato es
 * `SandboxError`. */
export function resultFromBody(body: string): AgentResult {
  let payload: unknown;
  try {
    payload = JSON.parse(body);
  } catch {
    payload = undefined;
  }
  if (!isRecord(payload) || payload.v !== 1) {
    throw new SandboxError("el agente desplegado respondió fuera del contrato del servicio");
  }
  const sessionId = typeof payload.session_id === "string" ? payload.session_id : undefined;
  const usage = usageOf(payload.usage);
  if (isRecord(payload.failed)) {
    throw agentFailedToError(failureOf(payload.failed, sessionId), usage);
  }
  return Object.freeze({
    sessionId: sessionId ?? "",
    text: typeof payload.text === "string" ? payload.text : "",
    steps: countOf(payload.steps),
    usage,
    exitCode: 0,
    toolCalls: Object.freeze([]),
    droppedLines: 0,
    usageReported: payload.usage_reported !== false,
    ...outputField(payload),
  });
}

/** Mensajes SSE a `AgentEvent`. `session`, `done` y `agent_failed` se leen
 * aquí (el servicio puede cortar con motivos que el decodificador v1 del
 * runner no admite, como `timeout`); el resto lo traduce `parseV1Line`. Un
 * mensaje desconocido se ignora y se cuenta. */
export class EventDecoder {
  readonly state: ProtocolV1State = newProtocolV1State();
  sessionId: string | undefined;
  readonly #encoder = new TextEncoder();

  feed(message: SseMessage): AgentEvent[] {
    let payload: unknown;
    try {
      payload = JSON.parse(message.data);
    } catch {
      this.state.ignoredLines += 1;
      return [];
    }
    if (!isRecord(payload)) {
      this.state.ignoredLines += 1;
      return [];
    }
    if (typeof payload.session_id === "string" && payload.session_id !== "") {
      this.sessionId = payload.session_id;
    }
    switch (payload.type) {
      case "session":
        return [];
      case "done":
        return [
          Object.freeze({
            type: "done",
            sessionId: this.sessionId ?? "",
            exitCode: 0,
            usage: this.state.usage,
            usageReported: this.state.usageReported,
            ...outputField(payload),
          }),
        ];
      case "agent_failed":
        return [failureOf(payload, this.sessionId)];
      default:
        return [...parseV1Line(this.#encoder.encode(message.data), this.state)];
    }
  }
}

/** Acumula los eventos de un stream en el `AgentResult` final. */
export class ResultBuilder {
  text = "";
  steps = 0;
  readonly toolCalls: ToolCall[] = [];
  final: Done | AgentFailed | undefined;

  add(event: AgentEvent): void {
    if (event.type === "text") {
      this.text = event.text;
    } else if (event.type === "step_started") {
      this.steps += 1;
    } else if (event.type === "tool_call") {
      this.toolCalls.push(event);
    } else if (event.type === "done" || event.type === "agent_failed") {
      this.final = event;
    }
  }

  /** El resultado, o el `AgentError` si el stream acabó en `AgentFailed` (o
   * sin terminal: `protocol_error`). */
  result(droppedLines = 0): AgentResult {
    const final = this.final;
    if (final?.type === "done") {
      return Object.freeze({
        sessionId: final.sessionId,
        text: this.text,
        steps: this.steps,
        usage: final.usage,
        exitCode: final.exitCode,
        toolCalls: Object.freeze([...this.toolCalls]),
        droppedLines,
        usageReported: final.usageReported !== false,
        ...(final.output === undefined ? {} : { output: final.output }),
      });
    }
    throw agentFailedToError(final ?? agentFailed("protocol_error"));
  }
}

/** El `DeploymentStatus` de la respuesta de `GET /ping`. */
export function statusFromPing(
  status: number,
  body: string,
  options: { readonly port: number; readonly project: string | undefined },
): DeploymentStatus {
  const unavailable: DeploymentStatus = {
    state: "unavailable",
    ready: false,
    busy: false,
    ...options,
  };
  if (status !== HTTP_OK) {
    return unavailable;
  }
  let payload: unknown;
  try {
    payload = JSON.parse(body);
  } catch {
    return unavailable;
  }
  const reported = isRecord(payload) ? payload.status : undefined;
  if (reported !== PING_HEALTHY && reported !== PING_BUSY) {
    return unavailable;
  }
  const busy = reported === PING_BUSY;
  return { state: busy ? "busy" : "healthy", ready: true, busy, ...options };
}

function requireInvocable(raw: boolean): void {
  if (raw) {
    throw new UnimplementedError(
      "AgentDeployment.invoke",
      "una app con protocol: 'raw' sólo se expone: usa url y headers con tu cliente",
      "https://rayito.dev/guias/despliega-tu-agente",
    );
  }
}

export function requirePrompt(prompt: unknown): string {
  if (typeof prompt !== "string" || prompt === "") {
    throw new InvalidArgumentError("el prompt debe ser una cadena no vacía");
  }
  return prompt;
}

/**
 * Iterable asíncrono de `AgentEvent` sobre la respuesta SSE de una
 * invocación: `for await (const event of deployment.stream("hola"))`. La
 * petición se hace al empezar a iterar. El último evento es `done` o
 * `agent_failed`; nunca lanza por un fallo del agente (`result()` sí).
 */
export class AgentDeploymentStream implements AsyncIterable<AgentEvent> {
  readonly #open: () => Promise<Response>;
  readonly #parser = new SseParser();
  readonly #decoder = new EventDecoder();
  readonly #builder = new ResultBuilder();
  #reader: ReadableStreamDefaultReader<Uint8Array> | undefined;

  constructor(open: () => Promise<Response>) {
    this.#open = open;
  }

  get sessionId(): string | undefined {
    return this.#decoder.sessionId;
  }

  get droppedLines(): number {
    return this.#decoder.state.ignoredLines;
  }

  async *[Symbol.asyncIterator](): AsyncGenerator<AgentEvent> {
    const response = await this.#open();
    const reader = response.body?.getReader();
    if (reader === undefined) {
      this.#builder.add(agentFailed("protocol_error"));
      yield agentFailed("protocol_error");
      return;
    }
    this.#reader = reader;
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) {
          return;
        }
        for (const message of this.#parser.feed(value)) {
          for (const event of this.#decoder.feed(message)) {
            this.#builder.add(event);
            yield event;
            if (this.#builder.final !== undefined) {
              return;
            }
          }
        }
      }
    } finally {
      await this.close();
    }
  }

  /** Consume lo que falte y devuelve el `AgentResult`; si el agente falló
   * lanza `AgentError`. */
  async result(): Promise<AgentResult> {
    for await (const event of this) {
      void event;
    }
    return this.#builder.result(this.droppedLines);
  }

  async close(): Promise<void> {
    const reader = this.#reader;
    this.#reader = undefined;
    if (reader !== undefined) {
      await reader.cancel().catch(() => undefined);
    }
  }
}

/**
 * Un agente servido desde un MicroVM (`AgentProject.deploy()`).
 *
 * `url` y `headers` son lo que necesita cualquier cliente HTTP: las cabeceras
 * llevan el JWE del proxy de AWS y se leen frescas en cada acceso (el token
 * vive 60 minutos y el SDK lo renueva). `invoke()` y `stream()` hablan
 * `rayito.agent-service/1`; `status()` lee `GET /ping`; `stop()` termina el
 * sandbox. Un despliegue vive como mucho el `timeout` del sandbox (8 horas
 * como máximo): para más tiempo, vuelve a llamar a `deploy()` y reconecta con
 * `AgentDeployment.connect(sandboxId)`.
 *
 * Coste y activación
 * -------------------
 * Activa: crear el cliente (`connect()` o el valor de `deploy()`) no llama a
 *     nadie; cada `invoke()`, `stream()` o `status()` es una petición HTTP a
 *     tu sandbox.
 * Recursos y llamadas AWS: ninguno nuevo; el sandbox ya lo creó `deploy()`.
 *     Un sandbox suspendido se reanuda con la primera petición (factura
 *     cómputo, como cualquier reanudación) y el modelo lo paga la pasarela
 *     del sandbox, no este cliente.
 * Coste aproximado: el del sandbox mientras vive, más el del modelo (ver
 *     `AgentProject.deploy()`).
 * IAM: `GetMicrovm` y `CreateMicrovmAuthToken` para `connect()` y para el
 *     token del proxy; nada más.
 * Cómo apagarla: `stop()` termina el sandbox y con él el despliegue;
 *     `pause()` deja de facturar cómputo sin perder el servicio.
 * Ejemplo:
 *   const deployment = await AgentDeployment.connect(sandboxId);
 *   (await deployment.invoke("Resume el README.")).text;
 *   await deployment.stop();
 */
export class AgentDeployment {
  readonly port: number;
  readonly #sandbox: DeploymentSandbox;
  readonly #hostAccess: HostAccess;
  readonly #project: string | undefined;
  readonly #raw: boolean;
  readonly #requestTimeoutMs: number;
  readonly #fetch: typeof fetch;

  constructor(options: AgentDeploymentOptions) {
    this.#sandbox = options.sandbox;
    this.port = options.port;
    this.#hostAccess = options.hostAccess;
    this.#project = options.project;
    this.#raw = options.raw ?? false;
    this.#requestTimeoutMs = options.requestTimeoutMs ?? DEFAULT_DEPLOYMENT_REQUEST_TIMEOUT_MS;
    this.#fetch = options.fetch ?? ((input, init) => globalThis.fetch(input, init));
  }

  /** Reconstruye el cliente de un despliegue desde las etiquetas de su
   * sandbox, en cualquier proceso. `options` son los de `Sandbox.connect`
   * (`region`, credenciales...). */
  static async connect(
    sandboxId: string,
    options: SandboxConnectOptions = {},
  ): Promise<AgentDeployment> {
    const { Sandbox } = await import("../sandbox/sandbox.js");
    const sandbox = await Sandbox.connect(sandboxId, options);
    const labels = parseLabels(sandbox.metadata);
    return new AgentDeployment({
      sandbox,
      port: labels.port,
      hostAccess: await sandbox.getHost(labels.port),
      project: labels.project,
      raw: labels.raw,
    });
  }

  get sandboxId(): string {
    return this.#sandbox.sandboxId;
  }

  /** El access token del sandbox del despliegue. Con el que pasaste a
   * `deploy({ accessToken })` o, si no pasaste ninguno, el que se generó:
   * guárdalo (nunca lo imprimas ni lo versiones) para reconectarte desde otro
   * proceso con `AgentDeployment.connect(sandboxId, { accessToken })` o con
   * `rayito agent invoke --token-file`. */
  get accessToken(): string {
    return this.#sandbox.accessToken;
  }

  get url(): string {
    return this.#hostAccess.url;
  }

  get headers(): Record<string, string> {
    return this.#hostAccess.headers;
  }

  /** Una invocación sin streaming: el `AgentResult`, o `AgentError` si el
   * agente falló (`busy` con el cupo lleno). Con `{ input }` en lugar del
   * prompt se invoca un grafo con esquema de entrada propio y `result.output`
   * trae su salida. */
  invoke(prompt: string, options?: AgentInvokeOptions): Promise<AgentResult>;
  invoke(request: AgentInputRequest, options?: AgentInvokeOptions): Promise<AgentResult>;
  invoke(request: AgentInvocation, options?: AgentInvokeOptions): Promise<AgentResult>;
  async invoke(request: AgentInvocation, options: AgentInvokeOptions = {}): Promise<AgentResult> {
    requireInvocable(this.#raw);
    const { prompt, input } = invocationOf(request);
    const body = invocationBody(prompt, {
      sessionId: options.sessionId,
      model: options.model,
      reasoning: options.reasoning,
      input,
    });
    const response = await this.#exchange(
      "POST",
      AGENT_SERVICE_INVOKE_PATH,
      JSON_CONTENT_TYPE,
      body,
      options.signal,
      input !== undefined,
    );
    return resultFromBody(await response.text());
  }

  /** Como `invoke()`, pero con los eventos a medida que llegan:
   * `for await (const event of deployment.stream("hola"))`. */
  stream(prompt: string, options?: AgentInvokeOptions): AgentDeploymentStream;
  stream(request: AgentInputRequest, options?: AgentInvokeOptions): AgentDeploymentStream;
  stream(request: AgentInvocation, options?: AgentInvokeOptions): AgentDeploymentStream;
  stream(request: AgentInvocation, options: AgentInvokeOptions = {}): AgentDeploymentStream {
    requireInvocable(this.#raw);
    const { prompt, input } = invocationOf(request);
    const body = invocationBody(prompt, {
      sessionId: options.sessionId,
      model: options.model,
      reasoning: options.reasoning,
      input,
    });
    return new AgentDeploymentStream(() =>
      this.#exchange(
        "POST",
        AGENT_SERVICE_INVOKE_PATH,
        EVENT_STREAM_CONTENT_TYPE,
        body,
        options.signal,
        input !== undefined,
      ),
    );
  }

  /** `GET /ping`: `healthy`, `busy` o `unavailable`. */
  async status(): Promise<DeploymentStatus> {
    const target = { port: this.port, project: this.#project };
    let response: Response;
    try {
      response = await this.#send("GET", AGENT_SERVICE_HEALTH_PATH, JSON_CONTENT_TYPE, undefined, {
        timeoutMs: PING_TIMEOUT_MS,
      });
    } catch (error) {
      if (error instanceof SandboxError) {
        return { state: "unavailable", ready: false, busy: false, ...target };
      }
      throw error;
    }
    return statusFromPing(response.status, await response.text(), target);
  }

  /** Suspende el sandbox del despliegue (`Sandbox.pause()`): mientras duerme
   * no factura cómputo y el servicio conserva su estado. `false` si ya estaba
   * suspendido. La siguiente petición (o `resume()`) lo reanuda; con `idle`
   * (activo por defecto) también se suspende solo. */
  async pause(options: { readonly wait?: boolean | undefined } = {}): Promise<boolean> {
    return this.#sandbox.pause(options);
  }

  /** Reanuda el sandbox suspendido (`Sandbox.resume()`) y, con `wait`, espera
   * a que esté listo. No hace falta antes de `invoke()`: una petición a un
   * sandbox suspendido ya lo reanuda. */
  async resume(options: { readonly wait?: boolean | undefined } = {}): Promise<void> {
    await this.#sandbox.resume(options);
  }

  /** Fija la vida restante del sandbox del despliegue (`Sandbox.setTimeout`):
   * `rayd` lo termina solo al vencer. Una flota lo usa para que un VM sobrante
   * desaparezca sin servidor propio. */
  async setTimeout(timeoutMs: number): Promise<void> {
    await this.#sandbox.setTimeout(timeoutMs);
  }

  /** Termina el sandbox del despliegue. Idempotente. */
  async stop(): Promise<void> {
    await this.#sandbox.kill();
  }

  async #exchange(
    method: string,
    path: string,
    accept: string,
    body: string | undefined,
    signal: AbortSignal | undefined,
    sentInput = false,
  ): Promise<Response> {
    const response = await this.#send(method, path, accept, body, {
      timeoutMs: this.#requestTimeoutMs,
      signal,
    });
    if (response.status === HTTP_OK) {
      return response;
    }
    if (response.status === HTTP_BAD_REQUEST && sentInput) {
      const errorBody = await response.text().catch(() => undefined);
      throw httpError(response.status, { sentInput, body: errorBody });
    }
    await response.body?.cancel().catch(() => undefined);
    throw httpError(response.status, { sentInput });
  }

  /** Una petición con las cabeceras vigentes; un `403` del proxy reacuña el
   * token y reintenta una vez, y el segundo es `AuthenticationError`. */
  async #send(
    method: string,
    path: string,
    accept: string,
    body: string | undefined,
    options: { readonly timeoutMs: number; readonly signal?: AbortSignal | undefined },
  ): Promise<Response> {
    for (let attempt = 0; attempt <= PROXY_REMINT_ATTEMPTS; attempt += 1) {
      const response = await this.#request(method, path, accept, body, options);
      if (response.status !== HTTP_FORBIDDEN) {
        return response;
      }
      await response.body?.cancel().catch(() => undefined);
      if (attempt < PROXY_REMINT_ATTEMPTS) {
        await this.#sandbox.refreshProxyAccess();
      }
    }
    throw new AuthenticationError("el proxy de AWS rechazó el token del despliegue", {
      proxyRejected: true,
    });
  }

  async #request(
    method: string,
    path: string,
    accept: string,
    body: string | undefined,
    options: { readonly timeoutMs: number; readonly signal?: AbortSignal | undefined },
  ): Promise<Response> {
    const timeout = AbortSignal.timeout(options.timeoutMs);
    const signal =
      options.signal === undefined ? timeout : AbortSignal.any([timeout, options.signal]);
    try {
      return await this.#fetch(`${this.url}${path}`, {
        method,
        headers: { ...this.headers, "Content-Type": JSON_CONTENT_TYPE, Accept: accept },
        ...(body === undefined ? {} : { body }),
        signal,
      });
    } catch {
      throw new SandboxError("no se pudo contactar con el agente desplegado");
    }
  }
}
