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

/** El cuerpo JSON de `POST /invocations`: sólo lo que se pidió. */
export function invocationBody(
  prompt: string,
  options: {
    sessionId?: string | undefined;
    model?: string | undefined;
    reasoning?: boolean | undefined;
  },
): string {
  const payload: Record<string, unknown> = { prompt };
  if (options.sessionId !== undefined) {
    payload.session_id = options.sessionId;
  }
  if (options.model !== undefined) {
    payload.model = options.model;
  }
  if (options.reasoning === true) {
    payload.reasoning = true;
  }
  return JSON.stringify(payload);
}

/** La excepción de un código HTTP del contrato. `403` es del proxy de AWS
 * (JWE ausente o caducado): `proxyRejected`, que el llamante reintenta una
 * vez tras reacuñar el token; `401` es el token del contenedor, que no existe
 * en un MicroVM. */
export function httpError(status: number): Error {
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
    return new InvalidArgumentError(
      "el agente desplegado rechazó la petición: cuerpo inválido o demasiado grande",
    );
  }
  return new SandboxError(`el agente desplegado respondió con un estado inesperado (${status})`, {
    statusCode: status,
  });
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function countOf(value: unknown): number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : 0;
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
   * agente falló (`busy` con el cupo lleno). */
  async invoke(prompt: string, options: AgentInvokeOptions = {}): Promise<AgentResult> {
    requireInvocable(this.#raw);
    const body = invocationBody(requirePrompt(prompt), {
      sessionId: options.sessionId,
      model: options.model,
      reasoning: options.reasoning,
    });
    const response = await this.#exchange(
      "POST",
      AGENT_SERVICE_INVOKE_PATH,
      JSON_CONTENT_TYPE,
      body,
      options.signal,
    );
    return resultFromBody(await response.text());
  }

  /** Como `invoke()`, pero con los eventos a medida que llegan:
   * `for await (const event of deployment.stream("hola"))`. */
  stream(prompt: string, options: AgentInvokeOptions = {}): AgentDeploymentStream {
    requireInvocable(this.#raw);
    const body = invocationBody(requirePrompt(prompt), {
      sessionId: options.sessionId,
      model: options.model,
      reasoning: options.reasoning,
    });
    return new AgentDeploymentStream(() =>
      this.#exchange(
        "POST",
        AGENT_SERVICE_INVOKE_PATH,
        EVENT_STREAM_CONTENT_TYPE,
        body,
        options.signal,
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
  ): Promise<Response> {
    const response = await this.#send(method, path, accept, body, {
      timeoutMs: this.#requestTimeoutMs,
      signal,
    });
    if (response.status === HTTP_OK) {
      return response;
    }
    await response.body?.cancel().catch(() => undefined);
    throw httpError(response.status);
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
