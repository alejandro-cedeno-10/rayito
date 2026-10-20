/**
 * `AgentCoreFleet`, `AgentCoreFleetStream` y `connectWith` (ADR-031, D7): el
 * cliente de un agente desplegado en Amazon Bedrock AgentCore Runtime, con la
 * misma API de `invoke()` y `stream()` que `AgentFleet`.
 */

import { Readable } from "node:stream";
import { NO_REGION_MESSAGE } from "../agent/aws-resolve.js";
import {
  AgentDeploymentStream,
  type AgentInvokeOptions,
  EVENT_STREAM_CONTENT_TYPE,
  invocationBody,
  JSON_CONTENT_TYPE,
  requirePrompt,
  resultFromBody,
} from "../agent/deployment.js";
import type { AgentEvent, AgentResult } from "../agent/events.js";
import type { AwsClientSettings } from "../aws/control-plane.js";
import { InvalidArgumentError } from "../errors.js";
import { AGENTCORE_REQUEST_TIMEOUT_SECONDS, AGENTCORE_STREAM_MAX_SECONDS } from "../limits.js";
import { MS_PER_SECOND } from "../units.js";
import { sdkAgentCorePorts } from "./aws-ports.js";
import { agentcoreSessionId } from "./identifiers.js";
import { AgentCoreDeleter, findAgent } from "./lifecycle.js";
import type { AgentCoreDeleteResult, AgentCoreStatus } from "./models.js";
import { runtimeLogGroupFor, runtimeNameFor, stackNameFor, validateAgentName } from "./names.js";
import type { AgentCorePorts } from "./ports.js";

export interface AgentCoreFleetSettings {
  readonly name: string;
  readonly region: string;
  readonly runtimeId: string;
  readonly runtimeArn: string;
  readonly runtimeVersion: string;
  readonly imageUri: string;
  readonly ports: AgentCorePorts;
}

export interface AgentCoreConnectOptions {
  readonly region?: string | undefined;
  readonly credentials?: AwsClientSettings["credentials"];
}

const REQUEST_TIMEOUT_MS = AGENTCORE_REQUEST_TIMEOUT_SECONDS * MS_PER_SECOND;
const STREAM_TIMEOUT_MS = AGENTCORE_STREAM_MAX_SECONDS * MS_PER_SECOND;
const UTF8_DECODER_OPTIONS = { stream: true } as const;

async function textOf(chunks: AsyncIterable<Uint8Array>): Promise<string> {
  const decoder = new TextDecoder();
  let text = "";
  for await (const chunk of chunks) {
    text += decoder.decode(chunk, UTF8_DECODER_OPTIONS);
  }
  return text + decoder.decode();
}

function responseOf(chunks: AsyncIterable<Uint8Array>): Response {
  return new Response(
    Readable.toWeb(Readable.from(chunks)) as unknown as ConstructorParameters<typeof Response>[0],
  );
}

function withSession(result: AgentResult, sessionId: string): AgentResult {
  return Object.freeze({ ...result, sessionId });
}

/**
 * Los eventos de una invocación en streaming, con la misma forma que
 * `AgentFleetStream`: `for await (const event of stream)`, `sessionId` (el id
 * que usó la llamada), `result()` y `close()`. El último evento es `done` o
 * `agent_failed`; `result()` lanza `AgentError` si el agente falló.
 */
export class AgentCoreFleetStream implements AsyncIterable<AgentEvent> {
  readonly #inner: AgentDeploymentStream;
  readonly #sessionId: string;

  constructor(inner: AgentDeploymentStream, sessionId: string) {
    this.#inner = inner;
    this.#sessionId = sessionId;
  }

  get sessionId(): string {
    return this.#sessionId;
  }

  get droppedLines(): number {
    return this.#inner.droppedLines;
  }

  [Symbol.asyncIterator](): AsyncGenerator<AgentEvent> {
    return this.#inner[Symbol.asyncIterator]();
  }

  async result(): Promise<AgentResult> {
    return withSession(await this.#inner.result(), this.#sessionId);
  }

  close(): Promise<void> {
    return this.#inner.close();
  }
}

/**
 * Un agente desplegado en Amazon Bedrock AgentCore Runtime
 * (ADR-031, D7). Tiene la misma API de `invoke()` y `stream()` y la misma
 * semántica de sesiones que `AgentFleet`: el resultado lleva el id de sesión
 * que hay que pasar para seguir la conversación, y los mismos decodificadores
 * leen la respuesta del host. La llamada es `InvokeAgentRuntime` firmada con
 * SigV4 con las credenciales de quien llama; el id de sesión viaja sólo en la
 * cabecera.
 *
 * Construirlo no llama a AWS: `connect()` lee el runtime por nombre y cada
 * `invoke()`, `stream()`, `status()`, `stopSession()` o `delete()` es una
 * llamada a tu cuenta.
 *
 * Coste y activación
 * -------------------
 * Activa: crearlo con `deployAgent(path, { target: "agentcore" })` o
 *     reconectarlo con `AgentCoreFleet.connect(nombre)`; cada `invoke()` o
 *     `stream()` abre (o continúa) una sesión de AgentCore, que corre en su
 *     propia microVM.
 * Recursos y llamadas AWS: `InvokeAgentRuntime`, `StopRuntimeSession`,
 *     `ListAgentRuntimes` y `GetAgentRuntime`; `delete()` borra el runtime, la
 *     pila `rayito-agentcore-<nombre>` y el grupo de logs del runtime.
 * Coste aproximado: AgentCore cobra por uso real, sin sesiones no hay cómputo:
 *     $0,0895 por vCPU-hora y $0,00945 por GB-hora (plataforma V1, us-east-1,
 *     consultado 2026-10-19 en https://aws.amazon.com/bedrock/agentcore/pricing/);
 *     el modelo lo cobra su proveedor.
 * IAM: `bedrock-agentcore:InvokeAgentRuntime` y `StopRuntimeSession` sobre el
 *     runtime, `bedrock-agentcore:ListAgentRuntimes` y `GetAgentRuntime`, y para
 *     `delete()` los de `AgentCoreDeleter`.
 * Cómo apagarla: `stopSession()` termina la microVM de una sesión;
 *     `delete()` quita el agente y deja de costar su imagen; sin sesiones no
 *     hay coste de cómputo.
 * Ejemplo:
 *   const fleet = await AgentCoreFleet.connect("mi-agente", { region: "us-east-1" });
 *   (await fleet.invoke("Resume el README.")).text;
 *   await fleet.stopSession(sessionId);
 */
export class AgentCoreFleet {
  readonly name: string;
  readonly region: string;
  readonly runtimeName: string;
  readonly runtimeId: string;
  readonly runtimeArn: string;
  readonly runtimeVersion: string;
  readonly imageUri: string;
  readonly #ports: AgentCorePorts;

  constructor(settings: AgentCoreFleetSettings) {
    this.name = settings.name;
    this.region = settings.region;
    this.runtimeName = runtimeNameFor(settings.name);
    this.runtimeId = settings.runtimeId;
    this.runtimeArn = settings.runtimeArn;
    this.runtimeVersion = settings.runtimeVersion;
    this.imageUri = settings.imageUri;
    this.#ports = settings.ports;
  }

  /** El grupo de logs que AgentCore crea para el runtime. */
  get logGroup(): string {
    return runtimeLogGroupFor(this.runtimeId);
  }

  /** Reconecta con el agente `name` desplegado en `region`; `runtime_not_found` si no hay uno de Rayito con ese nombre. */
  static async connect(
    name: string,
    options: AgentCoreConnectOptions = {},
  ): Promise<AgentCoreFleet> {
    const region = options.region ?? process.env.AWS_REGION ?? process.env.AWS_DEFAULT_REGION;
    if (!region) {
      throw new InvalidArgumentError(NO_REGION_MESSAGE);
    }
    const ports = sdkAgentCorePorts({
      region,
      ...(options.credentials === undefined ? {} : { credentials: options.credentials }),
    });
    return connectWith(ports, name, region);
  }

  async invoke(prompt: string, options: AgentInvokeOptions = {}): Promise<AgentResult> {
    const sessionId = agentcoreSessionId(options.sessionId);
    const chunks = await this.#open(prompt, options, sessionId, {
      accept: JSON_CONTENT_TYPE,
      requestTimeoutMs: REQUEST_TIMEOUT_MS,
    });
    return withSession(resultFromBody(await textOf(chunks)), sessionId);
  }

  async stream(prompt: string, options: AgentInvokeOptions = {}): Promise<AgentCoreFleetStream> {
    const sessionId = agentcoreSessionId(options.sessionId);
    const chunks = await this.#open(prompt, options, sessionId, {
      accept: EVENT_STREAM_CONTENT_TYPE,
      requestTimeoutMs: STREAM_TIMEOUT_MS,
    });
    const inner = new AgentDeploymentStream(async () => responseOf(chunks));
    return new AgentCoreFleetStream(inner, sessionId);
  }

  async status(): Promise<AgentCoreStatus> {
    const details = await findAgent(this.#ports, {
      name: this.name,
      region: this.region,
      runtimeName: this.runtimeName,
    });
    return {
      name: this.name,
      region: this.region,
      runtimeName: this.runtimeName,
      runtimeId: details.runtimeId,
      runtimeArn: details.runtimeArn,
      runtimeVersion: details.runtimeVersion,
      status: details.status,
      imageUri: details.containerUri,
      logGroup: runtimeLogGroupFor(details.runtimeId),
    };
  }

  /** Termina la microVM de una sesión (`StopRuntimeSession`); `false` si la sesión ya no existía. */
  stopSession(sessionId: string): Promise<boolean> {
    return this.#ports.invoker.stopSession(this.runtimeArn, agentcoreSessionId(sessionId));
  }

  /** Borra el runtime, la pila del agente y el grupo de logs del runtime; sólo lo que creó Rayito. */
  delete(): Promise<AgentCoreDeleteResult> {
    return new AgentCoreDeleter(this.#ports).delete({
      name: this.name,
      region: this.region,
      runtimeName: this.runtimeName,
      stackName: stackNameFor(this.name),
    });
  }

  #open(
    prompt: string,
    options: AgentInvokeOptions,
    sessionId: string,
    transport: { readonly accept: string; readonly requestTimeoutMs: number },
  ): Promise<AsyncIterable<Uint8Array>> {
    const body = invocationBody(requirePrompt(prompt), {
      model: options.model,
      reasoning: options.reasoning,
    });
    return this.#ports.invoker.invoke({
      agentName: this.name,
      runtimeArn: this.runtimeArn,
      sessionId,
      accept: transport.accept,
      payload: new TextEncoder().encode(body),
      requestTimeoutMs: transport.requestTimeoutMs,
      signal: options.signal,
    });
  }
}

/** `AgentCoreFleet.connect()` con las dependencias de `ports`: lo que los tests sustituyen. */
export async function connectWith(
  ports: AgentCorePorts,
  name: string,
  region: string,
): Promise<AgentCoreFleet> {
  validateAgentName(name);
  const details = await findAgent(ports, { name, region, runtimeName: runtimeNameFor(name) });
  return new AgentCoreFleet({
    name,
    region,
    runtimeId: details.runtimeId,
    runtimeArn: details.runtimeArn,
    runtimeVersion: details.runtimeVersion,
    imageUri: details.containerUri,
    ports,
  });
}
