/**
 * Adaptadores de AWS de `--target agentcore` para el plano de control
 * (`@aws-sdk/client-bedrock-agentcore-control`) y el de datos
 * (`@aws-sdk/client-bedrock-agentcore`) de AgentCore Runtime (ADR-031, D1 y D8).
 * Los dos son peers opcionales: se importan en la primera llamada, nunca al
 * construir el adaptador. Todo parámetro enviado aparece en `AWS_API_NOTES.md`,
 * «Despliegue en AgentCore Runtime». Los errores se traducen por código según
 * `contract.json.awsErrorMap`; el mensaje crudo de AWS no sale nunca.
 */

import { NodeHttpHandler } from "@smithy/node-http-handler";
import { failureMessage } from "../agent/events.js";
import { requestHandlerOptions } from "../aws/control-plane.js";
import { sanitizeAwsError } from "../aws/sanitize.js";
import { AgentCoreError, AgentError, QuotaExceededError, RateLimitError } from "../errors.js";
import {
  AGENTCORE_NETWORK_MODE,
  AGENTCORE_REQUEST_TIMEOUT_SECONDS,
  AGENTCORE_SERVER_PROTOCOL,
} from "../limits.js";
import { MS_PER_SECOND } from "../units.js";
import {
  ACCESS_DENIED_CODES,
  type AwsSettings,
  awsFailure,
  CONFLICT_CODE,
  codeOf,
  LazyClient,
  loadClient,
  NOT_FOUND_CODE,
} from "./aws-support.js";
import { clientTokenFor } from "./identifiers.js";
import type { ListedRuntime, RuntimeDetails } from "./models.js";
import {
  type InvokeRequest,
  RuntimeConflictError,
  type RuntimeControl,
  type RuntimeInvoker,
  type RuntimeWriteRequest,
  type RuntimeWriteResult,
} from "./ports.js";

const CONTROL_PEER = "@aws-sdk/client-bedrock-agentcore-control";
const DATA_PEER = "@aws-sdk/client-bedrock-agentcore";
const CONTROL_FEATURE = "rayito agent deploy --target agentcore";
const DATA_FEATURE = "rayito agent invoke --target agentcore";
/** `maxResults` de `ListAgentRuntimes`: 1-100 (`AWS_API_NOTES.md`). */
export const LIST_RUNTIMES_PAGE_SIZE = 100;
/** `StopRuntimeSession` es una petición corta: el plazo de una petición síncrona le sobra. */
const STOP_SESSION_TIMEOUT_MS = AGENTCORE_REQUEST_TIMEOUT_SECONDS * MS_PER_SECOND;
export const INVOKE_CONTENT_TYPE = "application/json";
export const RATE_LIMIT_MESSAGE =
  "AWS limitó la tasa de peticiones al runtime de AgentCore: reintenta en unos segundos";
export const QUOTA_MESSAGE =
  "la cuenta alcanzó una cuota de AgentCore (sesiones activas o nuevas por segundo): espera a que terminen o pide más cuota";

type Command = new (input: object) => unknown;

interface ControlModule {
  readonly BedrockAgentCoreControlClient: new (
    config: object,
  ) => { send(command: unknown): Promise<unknown> };
  readonly CreateAgentRuntimeCommand: Command;
  readonly UpdateAgentRuntimeCommand: Command;
  readonly GetAgentRuntimeCommand: Command;
  readonly ListAgentRuntimesCommand: Command;
  readonly DeleteAgentRuntimeCommand: Command;
}

interface DataModule {
  readonly BedrockAgentCoreClient: new (
    config: object,
  ) => { send(command: unknown): Promise<unknown> };
  readonly InvokeAgentRuntimeCommand: Command;
  readonly StopRuntimeSessionCommand: Command;
}

interface RuntimeSummaryOutput {
  readonly agentRuntimeId?: string;
  readonly agentRuntimeArn?: string;
  readonly agentRuntimeName?: string;
  readonly agentRuntimeVersion?: string;
  readonly status?: string;
  readonly description?: string;
}

interface RuntimeDetailsOutput extends RuntimeSummaryOutput {
  readonly roleArn?: string;
  readonly networkConfiguration?: { readonly networkMode?: string };
  readonly protocolConfiguration?: { readonly serverProtocol?: string };
  readonly lifecycleConfiguration?: {
    readonly idleRuntimeSessionTimeout?: number;
    readonly maxLifetime?: number;
  };
  readonly environmentVariables?: Record<string, string>;
  readonly agentRuntimeArtifact?: {
    readonly containerConfiguration?: { readonly containerUri?: string };
  };
  readonly metadataConfiguration?: { readonly requireMMDSV2?: boolean };
}

interface ListOutput {
  readonly agentRuntimes?: readonly RuntimeSummaryOutput[];
  readonly nextToken?: string;
}

function listedOf(item: RuntimeSummaryOutput): ListedRuntime {
  return {
    runtimeId: item.agentRuntimeId ?? "",
    runtimeArn: item.agentRuntimeArn ?? "",
    runtimeName: item.agentRuntimeName ?? "",
    runtimeVersion: item.agentRuntimeVersion ?? "",
    status: item.status ?? "",
    description: item.description ?? "",
  };
}

function detailsOf(output: RuntimeDetailsOutput): RuntimeDetails {
  return {
    ...listedOf(output),
    roleArn: output.roleArn ?? "",
    networkMode: output.networkConfiguration?.networkMode ?? "",
    serverProtocol: output.protocolConfiguration?.serverProtocol ?? "",
    environmentVariables: output.environmentVariables ?? {},
    idleRuntimeSessionTimeout: output.lifecycleConfiguration?.idleRuntimeSessionTimeout ?? 0,
    maxLifetime: output.lifecycleConfiguration?.maxLifetime ?? 0,
    containerUri: output.agentRuntimeArtifact?.containerConfiguration?.containerUri ?? "",
    requireMMDSV2: output.metadataConfiguration?.requireMMDSV2 === true,
  };
}

function writeInput(request: RuntimeWriteRequest): Record<string, unknown> {
  return {
    agentRuntimeArtifact: { containerConfiguration: { containerUri: request.containerUri } },
    roleArn: request.roleArn,
    networkConfiguration: { networkMode: AGENTCORE_NETWORK_MODE },
    protocolConfiguration: { serverProtocol: AGENTCORE_SERVER_PROTOCOL },
    lifecycleConfiguration: {
      idleRuntimeSessionTimeout: request.idleRuntimeSessionTimeout,
      maxLifetime: request.maxLifetime,
    },
    environmentVariables: request.environmentVariables,
    description: request.description,
  };
}

function writeResultOf(
  output: RuntimeSummaryOutput,
  runtimeId: string | undefined,
): RuntimeWriteResult {
  return {
    runtimeId: output.agentRuntimeId ?? runtimeId ?? "",
    runtimeArn: output.agentRuntimeArn ?? "",
    runtimeVersion: output.agentRuntimeVersion ?? "",
    status: output.status ?? "",
  };
}

/** `RuntimeControl` sobre `@aws-sdk/client-bedrock-agentcore-control`. */
export class SdkRuntimeControl implements RuntimeControl {
  readonly #settings: AwsSettings;
  readonly #client: LazyClient<ControlModule>;

  constructor(settings: AwsSettings) {
    this.#settings = settings;
    this.#client = new LazyClient(() =>
      loadClient<ControlModule>(
        CONTROL_PEER,
        CONTROL_FEATURE,
        (sdk) => sdk.BedrockAgentCoreControlClient,
        settings,
      ),
    );
  }

  async #call<T>(
    operation: string,
    build: (sdk: ControlModule) => unknown,
    onCode?: (code: string | undefined) => unknown,
  ): Promise<T> {
    const { sdk, send } = await this.#client.get();
    try {
      return await send<T>(build(sdk));
    } catch (error) {
      const handled = onCode?.(codeOf(error));
      throw handled ?? awsFailure(operation, error);
    }
  }

  async listRuntimes(): Promise<ListedRuntime[]> {
    const runtimes: ListedRuntime[] = [];
    let nextToken: string | undefined;
    do {
      const page: ListOutput = await this.#call<ListOutput>(
        "ListAgentRuntimes",
        (sdk) =>
          new sdk.ListAgentRuntimesCommand({
            maxResults: LIST_RUNTIMES_PAGE_SIZE,
            ...(nextToken === undefined ? {} : { nextToken }),
          }),
      );
      runtimes.push(...(page.agentRuntimes ?? []).map(listedOf));
      nextToken = page.nextToken;
    } while (nextToken !== undefined);
    return runtimes;
  }

  async getRuntime(runtimeId: string): Promise<RuntimeDetails | undefined> {
    const { sdk, send } = await this.#client.get();
    try {
      return detailsOf(
        await send<RuntimeDetailsOutput>(
          new sdk.GetAgentRuntimeCommand({ agentRuntimeId: runtimeId }),
        ),
      );
    } catch (error) {
      if (codeOf(error) === NOT_FOUND_CODE) {
        return undefined;
      }
      throw awsFailure("GetAgentRuntime", error);
    }
  }

  async createRuntime(request: RuntimeWriteRequest): Promise<RuntimeWriteResult> {
    const output = await this.#call<RuntimeSummaryOutput>(
      "CreateAgentRuntime",
      (sdk) => {
        const input = {
          agentRuntimeName: request.runtimeName,
          ...writeInput(request),
          ...(request.tags === undefined ? {} : { tags: request.tags }),
        };
        return new sdk.CreateAgentRuntimeCommand({ ...input, clientToken: clientTokenFor(input) });
      },
      (code) => (code === CONFLICT_CODE ? new RuntimeConflictError() : undefined),
    );
    return writeResultOf(output, undefined);
  }

  async updateRuntime(request: RuntimeWriteRequest): Promise<RuntimeWriteResult> {
    const output = await this.#call<RuntimeSummaryOutput>(
      "UpdateAgentRuntime",
      (sdk) => {
        const input = {
          agentRuntimeId: request.runtimeId,
          ...writeInput(request),
          ...(request.requireMMDSV2 === true
            ? { metadataConfiguration: { requireMMDSV2: true } }
            : {}),
        };
        return new sdk.UpdateAgentRuntimeCommand({
          ...input,
          clientToken: clientTokenFor(input, request.baseVersion ?? ""),
        });
      },
      (code) =>
        code === NOT_FOUND_CODE
          ? new AgentCoreError("runtime_not_found", {
              name: request.runtimeId ?? "",
              region: this.#settings.region,
            })
          : undefined,
    );
    return writeResultOf(output, request.runtimeId);
  }

  async deleteRuntime(runtimeId: string): Promise<void> {
    const { sdk, send } = await this.#client.get();
    try {
      await send(new sdk.DeleteAgentRuntimeCommand({ agentRuntimeId: runtimeId }));
    } catch (error) {
      if (codeOf(error) !== NOT_FOUND_CODE) {
        throw awsFailure("DeleteAgentRuntime", error);
      }
    }
  }
}

interface InvokeOutput {
  readonly response?: unknown;
}

interface ByteStream {
  [Symbol.asyncIterator]?: () => AsyncIterator<Uint8Array | string>;
  transformToByteArray?: () => Promise<Uint8Array>;
}

const TEXT_ENCODER = new TextEncoder();

/** El cuerpo de `InvokeAgentRuntime` (un `SdkStream` de Node) como bloques de bytes. */
export async function* bytesOf(body: unknown): AsyncGenerator<Uint8Array> {
  const stream = body as ByteStream | undefined;
  if (stream?.[Symbol.asyncIterator] !== undefined) {
    for await (const chunk of stream as AsyncIterable<Uint8Array | string>) {
      yield typeof chunk === "string" ? TEXT_ENCODER.encode(chunk) : chunk;
    }
  } else if (stream?.transformToByteArray !== undefined) {
    yield await stream.transformToByteArray();
  }
}

function invokeFailure(error: unknown, request: InvokeRequest, region: string): unknown {
  const code = codeOf(error);
  const cause = sanitizeAwsError(error, { includeMessage: false });
  if (code === NOT_FOUND_CODE) {
    return new AgentCoreError("runtime_not_found", { name: request.agentName, region }, { cause });
  }
  if (code !== undefined && ACCESS_DENIED_CODES.has(code)) {
    return awsFailure("InvokeAgentRuntime", error);
  }
  if (code === "ThrottlingException") {
    return new RateLimitError(RATE_LIMIT_MESSAGE, { awsCode: code, cause });
  }
  if (code === "ServiceQuotaExceededException") {
    return new QuotaExceededError(QUOTA_MESSAGE);
  }
  if (code === "RuntimeClientError") {
    return new AgentError(failureMessage("runtime_error"), { reason: "runtime_error", cause });
  }
  return awsFailure("InvokeAgentRuntime", error);
}

/**
 * `RuntimeInvoker` sobre `@aws-sdk/client-bedrock-agentcore`, firmado con SigV4
 * por el SDK. Un cliente por plazo de lectura: 15 min para `invoke` y 60 min
 * para `stream` (cuotas de AgentCore, consultadas 2026-10-19).
 */
export class SdkRuntimeInvoker implements RuntimeInvoker {
  readonly #settings: AwsSettings;
  readonly #clients = new Map<number, LazyClient<DataModule>>();

  constructor(settings: AwsSettings) {
    this.#settings = settings;
  }

  #client(requestTimeoutMs: number): LazyClient<DataModule> {
    let client = this.#clients.get(requestTimeoutMs);
    if (client === undefined) {
      client = new LazyClient(() =>
        loadClient<DataModule>(
          DATA_PEER,
          DATA_FEATURE,
          (sdk) => sdk.BedrockAgentCoreClient,
          this.#settings,
          {
            requestHandler: new NodeHttpHandler({
              ...requestHandlerOptions(this.#settings.proxy),
              requestTimeout: requestTimeoutMs,
            }),
          },
        ),
      );
      this.#clients.set(requestTimeoutMs, client);
    }
    return client;
  }

  async invoke(request: InvokeRequest): Promise<AsyncIterable<Uint8Array>> {
    const { sdk, send } = await this.#client(request.requestTimeoutMs).get();
    try {
      const output = await send<InvokeOutput>(
        new sdk.InvokeAgentRuntimeCommand({
          agentRuntimeArn: request.runtimeArn,
          runtimeSessionId: request.sessionId,
          contentType: INVOKE_CONTENT_TYPE,
          accept: request.accept,
          payload: request.payload,
        }),
        request.signal === undefined ? undefined : { abortSignal: request.signal },
      );
      return bytesOf(output.response);
    } catch (error) {
      throw invokeFailure(error, request, this.#settings.region);
    }
  }

  async stopSession(runtimeArn: string, sessionId: string): Promise<boolean> {
    const { sdk, send } = await this.#client(STOP_SESSION_TIMEOUT_MS).get();
    try {
      await send(
        new sdk.StopRuntimeSessionCommand({
          agentRuntimeArn: runtimeArn,
          runtimeSessionId: sessionId,
        }),
      );
      return true;
    } catch (error) {
      if (codeOf(error) === NOT_FOUND_CODE) {
        return false;
      }
      throw awsFailure("StopRuntimeSession", error);
    }
  }
}
