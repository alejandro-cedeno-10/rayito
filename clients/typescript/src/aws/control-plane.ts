/**
 * Plano de control: puerto `ControlPlane` y adaptador sobre
 * `@aws-sdk/client-lambda-microvms`.
 *
 * Todo parámetro enviado a AWS aparece literalmente en `AWS_API_NOTES.md` §2,
 * §3, §5 y §6. Los errores se mapean por **nombre** de excepción del SDK,
 * nunca por status HTTP (`ServiceQuotaExceededException` llega con 402).
 */

import {
  CreateMicrovmAuthTokenCommand,
  GetMicrovmCommand,
  LambdaMicrovmsClient,
  type LambdaMicrovmsClientConfig,
  ListMicrovmsCommand,
  ResumeMicrovmCommand,
  RunMicrovmCommand,
  SuspendMicrovmCommand,
  TerminateMicrovmCommand,
} from "@aws-sdk/client-lambda-microvms";
import { GetCallerIdentityCommand, STSClient } from "@aws-sdk/client-sts";
import { NodeHttpHandler } from "@smithy/node-http-handler";
import { abortReasonOr, raceAbort } from "../abort.js";
import {
  AuthenticationError,
  CapacityError,
  InvalidArgumentError,
  QuotaExceededError,
  RateLimitError,
  SandboxError,
  SandboxNotFoundError,
  SandboxStateError,
} from "../errors.js";
import { defineHidden } from "../hidden.js";
import { API_TPS, LIST_MAX_RESULTS, TERMINAL_STATES, TOKEN_TTL_MINUTES } from "../limits.js";
import {
  type IdlePolicy,
  type MicrovmListPage,
  type SandboxInfo,
  type SandboxListItem,
  sandboxInfo,
  sandboxListItem,
} from "../models.js";
import { ProxyTunnelAgent, validateProxyUrl } from "../transport/proxy-tunnel.js";
import { validateIntegration, validateRetries } from "../validation.js";
import { VERSION } from "../version.js";

export const AUTH_TOKEN_RESPONSE_KEY = "X-aws-proxy-auth";
const IMAGE_NAME_PATTERN = /^[a-zA-Z0-9_-]{1,64}$/;

/**
 * Un `PortSpecification` de `create-microvm-auth-token`: puerto o rango.
 * `allPorts` no se modela a propósito: expondría el puerto de hooks (ADR-006).
 */
export class PortSpec {
  readonly start: number;
  readonly end: number;

  private constructor(start: number, end: number) {
    this.start = start;
    this.end = end;
    Object.freeze(this);
  }

  static single(port: number): PortSpec {
    return new PortSpec(port, port);
  }

  static range(start: number, end: number): PortSpec {
    if (start > end) {
      throw new InvalidArgumentError(`rango de puertos invertido: ${start}-${end}`);
    }
    return new PortSpec(start, end);
  }

  covers(port: number): boolean {
    return this.start <= port && port <= this.end;
  }

  equals(other: PortSpec): boolean {
    return this.start === other.start && this.end === other.end;
  }

  toApi(): { port: number } | { range: { startPort: number; endPort: number } } {
    if (this.start === this.end) {
      return { port: this.start };
    }
    return { range: { startPort: this.start, endPort: this.end } };
  }
}

export function samePortSet(a: readonly PortSpec[], b: readonly PortSpec[]): boolean {
  return a.length === b.length && a.every((spec, index) => spec.equals(b[index] as PortSpec));
}

export type LoggingConfig =
  | { disabled: Record<string, never> }
  | { cloudWatch: { logGroup: string } };

export interface LaunchRequestFields {
  readonly imageArn: string;
  readonly maximumDurationSeconds: number;
  readonly runHookPayload: string;
  readonly clientToken: string;
  readonly logging: LoggingConfig;
  readonly imageVersion?: string | undefined;
  readonly executionRoleArn?: string | undefined;
  readonly idle?: IdlePolicy | undefined;
  readonly ingressConnectors?: readonly string[] | undefined;
  readonly egressConnectors?: readonly string[] | undefined;
}

export interface RunMicrovmApiInput {
  imageIdentifier: string;
  maximumDurationInSeconds: number;
  runHookPayload: string;
  clientToken: string;
  logging: LoggingConfig;
  imageVersion?: string;
  executionRoleArn?: string;
  idlePolicy?: IdlePolicyApi;
  ingressNetworkConnectors?: string[];
  egressNetworkConnectors?: string[];
}

export interface IdlePolicyApi {
  maxIdleDurationSeconds: number;
  suspendedDurationSeconds: number;
  autoResumeEnabled: boolean;
}

/** `RunMicrovmRequest` ya validado. `idle` trae los tres campos resueltos. */
export class LaunchRequest {
  readonly imageArn: string;
  readonly maximumDurationSeconds: number;
  declare readonly runHookPayload: string;
  readonly clientToken: string;
  readonly logging: LoggingConfig;
  readonly imageVersion: string | undefined;
  readonly executionRoleArn: string | undefined;
  readonly idle: IdlePolicy | undefined;
  readonly ingressConnectors: readonly string[];
  readonly egressConnectors: readonly string[];

  constructor(fields: LaunchRequestFields) {
    this.imageArn = fields.imageArn;
    this.maximumDurationSeconds = fields.maximumDurationSeconds;
    defineHidden(this, "runHookPayload", fields.runHookPayload);
    this.clientToken = fields.clientToken;
    this.logging = fields.logging;
    this.imageVersion = fields.imageVersion;
    this.executionRoleArn = fields.executionRoleArn;
    this.idle = fields.idle;
    this.ingressConnectors = Object.freeze([...(fields.ingressConnectors ?? [])]);
    this.egressConnectors = Object.freeze([...(fields.egressConnectors ?? [])]);
    Object.freeze(this);
  }

  toApi(): RunMicrovmApiInput {
    const params: RunMicrovmApiInput = {
      imageIdentifier: this.imageArn,
      maximumDurationInSeconds: this.maximumDurationSeconds,
      runHookPayload: this.runHookPayload,
      clientToken: this.clientToken,
      logging: this.logging,
    };
    if (this.imageVersion !== undefined) {
      params.imageVersion = this.imageVersion;
    }
    if (this.executionRoleArn !== undefined) {
      params.executionRoleArn = this.executionRoleArn;
    }
    if (this.idle !== undefined) {
      params.idlePolicy = idlePolicyToApi(this.idle);
    }
    if (this.ingressConnectors.length > 0) {
      params.ingressNetworkConnectors = [...this.ingressConnectors];
    }
    if (this.egressConnectors.length > 0) {
      params.egressNetworkConnectors = [...this.egressConnectors];
    }
    return params;
  }
}

export interface ListMicrovmsOptions {
  readonly imageArn?: string | undefined;
  readonly imageVersion?: string | undefined;
  readonly states?: readonly string[] | undefined;
}

/** Una sola llamada a `list-microvms`; `maxResults` entre 1 y 50 (lo fija el paginador, nunca el usuario). */
export interface ListMicrovmsPageOptions {
  readonly imageArn?: string | undefined;
  readonly imageVersion?: string | undefined;
  readonly maxResults: number;
  readonly nextToken?: string | undefined;
}

/**
 * Lo que una llamada al plano de control acepta del caller: `signal` viaja
 * como `abortSignal` al `send` del SDK v3 (una opción del SDK, no un
 * parámetro de la API) y abortarlo rechaza con `signal.reason`.
 */
export interface ControlPlaneCallOptions {
  readonly signal?: AbortSignal | undefined;
}

/**
 * Lo que el dominio necesita del plano de control de AWS. `runMicrovm` y
 * `terminateMicrovm` no aceptan `signal`: cortar `run-microvm` a mitad
 * dejaría un MicroVM sin id que terminar, y la limpieza tiene que correr
 * también después de un aborto.
 */
export interface ControlPlane {
  readonly region: string;
  resolveTemplateArn(template: string, options?: ControlPlaneCallOptions): Promise<string>;
  runMicrovm(request: LaunchRequest): Promise<SandboxInfo>;
  getMicrovm(sandboxId: string, options?: ControlPlaneCallOptions): Promise<SandboxInfo>;
  listMicrovms(options?: ListMicrovmsOptions): AsyncIterable<SandboxListItem>;
  /** Una página sin filtrar por estado, con su `nextToken` (`undefined` en la última). */
  listMicrovmsPage(options: ListMicrovmsPageOptions): Promise<MicrovmListPage>;
  terminateMicrovm(sandboxId: string): Promise<boolean>;
  suspendMicrovm(sandboxId: string): Promise<boolean>;
  resumeMicrovm(sandboxId: string, options?: ControlPlaneCallOptions): Promise<boolean>;
  createAuthToken(
    sandboxId: string,
    ports: readonly PortSpec[],
    options?: ControlPlaneCallOptions,
  ): Promise<string>;
}

export type MonotonicClock = () => number;
export type Sleeper = (seconds: number) => Promise<void>;

function monotonicSeconds(): number {
  return performance.now() / 1000;
}

function sleepSeconds(seconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, seconds * 1000));
}

/**
 * Limitador por operación alineado con la cuota publicada (burst = rate).
 * Los tokens pueden quedar en negativo: cada `acquire` reserva su turno y
 * duerme lo que falte, así N llamadas concurrentes se serializan al ritmo de
 * la cuota sin que ninguna se pierda.
 */
export class TokenBucket {
  readonly #rate: number;
  readonly #capacity: number;
  #tokens: number;
  #updatedAt: number;
  readonly #now: MonotonicClock;
  readonly #sleep: Sleeper;

  constructor(
    ratePerSecond: number,
    options: {
      readonly now?: MonotonicClock | undefined;
      readonly sleep?: Sleeper | undefined;
    } = {},
  ) {
    if (!(ratePerSecond > 0)) {
      throw new RangeError("ratePerSecond debe ser > 0");
    }
    this.#rate = ratePerSecond;
    this.#capacity = ratePerSecond;
    this.#tokens = ratePerSecond;
    this.#now = options.now ?? monotonicSeconds;
    this.#sleep = options.sleep ?? sleepSeconds;
    this.#updatedAt = this.#now();
  }

  async acquire(): Promise<number> {
    this.#refill();
    const wait = Math.max(0, (1 - this.#tokens) / this.#rate);
    this.#tokens -= 1;
    if (wait > 0) {
      await this.#sleep(wait);
    }
    return wait;
  }

  #refill(): void {
    const now = this.#now();
    const elapsed = Math.max(0, now - this.#updatedAt);
    this.#updatedAt = now;
    this.#tokens = Math.min(this.#capacity, this.#tokens + elapsed * this.#rate);
  }
}

/** El subconjunto estructural de `LambdaMicrovmsClient`/`STSClient` que usa el adaptador. */
export interface CommandSender {
  send(command: unknown, options?: { readonly abortSignal?: AbortSignal }): Promise<unknown>;
}

export interface LambdaMicrovmsControlPlaneOptions {
  readonly client: CommandSender;
  readonly region: string;
  /** Las credenciales y el proxy del cliente del SDK, para que otros clientes (S3) los hereden. */
  readonly awsClientSettings?: AwsClientSettings | undefined;
  readonly stsClient?: CommandSender | (() => CommandSender) | undefined;
  readonly now?: MonotonicClock | undefined;
  readonly sleep?: Sleeper | undefined;
}

/**
 * Ajustes del cliente del SDK de AWS que cambian el plano: `retries` (los
 * reintentos, `maxAttempts = retries + 1`), `proxy` (`http://h:puerto`, por
 * un túnel `CONNECT`) e `integration` (se añade al User-Agent).
 */
export interface ControlPlaneClientSettings {
  readonly retries?: number | undefined;
  readonly proxy?: string | undefined;
  readonly integration?: string | undefined;
}

export interface FromRegionOptions extends ControlPlaneClientSettings {
  readonly credentials?: LambdaMicrovmsClientConfig["credentials"] | undefined;
}

/**
 * Lo que otro cliente del SDK de AWS (los de S3 de las transferencias) hereda
 * del plano de control: sus credenciales y su proxy. Vacío = la cadena por
 * defecto y sin proxy, lo mismo que el plano.
 */
export interface AwsClientSettings {
  readonly credentials?: NonNullable<LambdaMicrovmsClientConfig["credentials"]> | undefined;
  readonly proxy?: string | undefined;
}

/** Un plano que expone esos ajustes (`LambdaMicrovmsControlPlane` y los que lo envuelven). */
export interface AwsClientSettingsSource {
  readonly awsClientSettings: AwsClientSettings;
}

/** Los `AwsClientSettings` de un plano, o `{}` si no los expone (un plano falso o uno construido sobre `client`). */
export function awsClientSettingsOf(plane: ControlPlane): AwsClientSettings {
  const settings = (plane as Partial<AwsClientSettingsSource>).awsClientSettings;
  return settings ?? {};
}

export const DEFAULT_MAX_ATTEMPTS = 5;
export const CONNECTION_TIMEOUT_MS = 5_000;
export const REQUEST_TIMEOUT_MS = 60_000;
export const INTEGRATION_USER_AGENT_KEY = "rayito-integration";

export function hasClientSettings(settings: ControlPlaneClientSettings): boolean {
  return (
    settings.retries !== undefined ||
    settings.proxy !== undefined ||
    settings.integration !== undefined
  );
}

/** Las opciones del `NodeHttpHandler`: con `proxy`, el `httpsAgent` es un `ProxyTunnelAgent`. */
export function requestHandlerOptions(proxy: string | undefined): {
  connectionTimeout: number;
  requestTimeout: number;
  httpsAgent?: ProxyTunnelAgent;
} {
  const base = { connectionTimeout: CONNECTION_TIMEOUT_MS, requestTimeout: REQUEST_TIMEOUT_MS };
  return proxy === undefined ? base : { ...base, httpsAgent: new ProxyTunnelAgent(proxy) };
}

interface CallerIdentity {
  readonly account: string;
  readonly partition: string;
}

interface ListPage {
  readonly items?: readonly ListedItem[] | undefined;
  readonly nextToken?: string | undefined;
}

interface ListMicrovmsInput {
  maxResults: number;
  imageIdentifier?: string;
  imageVersion?: string;
  nextToken?: string;
}

/** `maxResults` fuera de 1–50 es un error de programación del SDK, no una entrada del usuario. */
function listMicrovmsInput(options: ListMicrovmsPageOptions): ListMicrovmsInput {
  const maxResults = options.maxResults;
  if (!Number.isInteger(maxResults) || maxResults < 1 || maxResults > LIST_MAX_RESULTS) {
    throw new RangeError(
      `maxResults debe estar entre 1 y ${LIST_MAX_RESULTS}, recibido ${maxResults}`,
    );
  }
  const input: ListMicrovmsInput = { maxResults };
  if (options.imageArn !== undefined) {
    input.imageIdentifier = options.imageArn;
  }
  if (options.imageVersion !== undefined) {
    input.imageVersion = options.imageVersion;
  }
  if (options.nextToken !== undefined) {
    input.nextToken = options.nextToken;
  }
  return input;
}

interface ListedItem {
  readonly microvmId?: string | undefined;
  readonly state?: string | undefined;
  readonly imageArn?: string | undefined;
  readonly imageVersion?: string | undefined;
  readonly startedAt?: Date | undefined;
}

export interface MicrovmResponse extends ListedItem {
  readonly endpoint?: string | undefined;
  readonly maximumDurationInSeconds?: number | undefined;
  readonly terminatedAt?: Date | undefined;
  readonly stateReason?: string | undefined;
  readonly idlePolicy?: Partial<IdlePolicyApi> | undefined;
  readonly executionRoleArn?: string | undefined;
  readonly ingressNetworkConnectors?: readonly string[] | undefined;
  readonly egressNetworkConnectors?: readonly string[] | undefined;
}

/**
 * La forma común que aceptan `LambdaMicrovmsClient` y `STSClient`. Tipo explícito para que el
 * emisor de `.d.ts` nunca tenga que nombrar `@smithy/types` a través de un tipo inferido.
 */
export interface SdkClientConfig {
  readonly region: string;
  readonly credentials?: NonNullable<LambdaMicrovmsClientConfig["credentials"]>;
  readonly maxAttempts: number;
  readonly retryMode: string;
  readonly requestHandler: NodeHttpHandler;
  readonly customUserAgent: Array<[string, string]>;
}

/** La configuración de los clientes del SDK; `retries`, `proxy` e `integration` se validan aquí. */
export function clientConfig(region: string, options: FromRegionOptions = {}): SdkClientConfig {
  const retries = validateRetries(options.retries);
  const integration = validateIntegration(options.integration);
  const proxy = validateProxyUrl(options.proxy);
  const userAgent: Array<[string, string]> = [["rayito", VERSION]];
  if (integration !== undefined) {
    userAgent.push([INTEGRATION_USER_AGENT_KEY, integration]);
  }
  return {
    region,
    ...(options.credentials === undefined ? {} : { credentials: options.credentials }),
    maxAttempts: retries === undefined ? DEFAULT_MAX_ATTEMPTS : retries + 1,
    retryMode: "standard",
    requestHandler: new NodeHttpHandler(requestHandlerOptions(proxy)),
    customUserAgent: userAgent,
  };
}

/** Adaptador del puerto `ControlPlane` sobre el SDK v3. Testable con un `CommandSender` grabador. */
export class LambdaMicrovmsControlPlane implements ControlPlane {
  readonly region: string;
  readonly #client: CommandSender;
  #stsClient: CommandSender | (() => CommandSender) | undefined;
  readonly #buckets: Map<string, TokenBucket>;
  readonly #awsClientSettings: AwsClientSettings;
  #identity: Promise<CallerIdentity> | undefined;

  constructor(options: LambdaMicrovmsControlPlaneOptions) {
    this.region = options.region;
    this.#client = options.client;
    this.#awsClientSettings = Object.freeze({ ...(options.awsClientSettings ?? {}) });
    this.#stsClient = options.stsClient;
    const bucketOptions = { now: options.now, sleep: options.sleep };
    this.#buckets = new Map(
      Object.entries(API_TPS).map(([operation, rate]) => [
        operation,
        new TokenBucket(rate, bucketOptions),
      ]),
    );
  }

  /** El cliente STS se construye sólo si algún template se resuelve por nombre. */
  static fromRegion(region?: string, options: FromRegionOptions = {}): LambdaMicrovmsControlPlane {
    const resolvedRegion = region ?? process.env.AWS_REGION ?? process.env.AWS_DEFAULT_REGION;
    if (!resolvedRegion) {
      throw new InvalidArgumentError("falta la región: pasa `region` o define AWS_REGION");
    }
    const config = clientConfig(resolvedRegion, options);
    return new LambdaMicrovmsControlPlane({
      client: new LambdaMicrovmsClient(config),
      region: resolvedRegion,
      stsClient: () => new STSClient(config),
      awsClientSettings: {
        ...(config.credentials === undefined ? {} : { credentials: config.credentials }),
        ...(options.proxy === undefined ? {} : { proxy: options.proxy }),
      },
    });
  }

  /** Las credenciales y el proxy con los que se construyó (vacío con la cadena por defecto). */
  get awsClientSettings(): AwsClientSettings {
    return this.#awsClientSettings;
  }

  /** El cliente del SDK con el que habla este plano (para inspeccionar su configuración). */
  get client(): CommandSender {
    return this.#client;
  }

  /** Un nombre pelado no vale en ninguna operación: siempre se pasa el ARN. */
  async resolveTemplateArn(
    template: string,
    options: ControlPlaneCallOptions = {},
  ): Promise<string> {
    if (template.startsWith("arn:")) {
      return template;
    }
    if (!IMAGE_NAME_PATTERN.test(template)) {
      throw new InvalidArgumentError(
        `template inválido: ${JSON.stringify(template)} (ARN o nombre [a-zA-Z0-9-_], máx. 64)`,
      );
    }
    options.signal?.throwIfAborted();
    const identity = await raceAbort(this.#callerIdentity(), options.signal);
    return `arn:${identity.partition}:lambda:${this.region}:${identity.account}:microvm-image:${template}`;
  }

  async runMicrovm(request: LaunchRequest): Promise<SandboxInfo> {
    const response = await this.#invoke("RunMicrovm", new RunMicrovmCommand(request.toApi()));
    return sandboxInfoFromResponse(response as MicrovmResponse);
  }

  async getMicrovm(sandboxId: string, options: ControlPlaneCallOptions = {}): Promise<SandboxInfo> {
    const response = await this.#invoke(
      "GetMicrovm",
      new GetMicrovmCommand({ microvmIdentifier: sandboxId }),
      options.signal,
    );
    return sandboxInfoFromResponse(response as MicrovmResponse);
  }

  /**
   * Pagina `list-microvms` con `maxResults 50` y `nextToken` (lo mismo que
   * `paginateListMicrovms`, que exige una instancia real del cliente y no
   * admite el `CommandSender` inyectable); sin `states` omite
   * `TERMINATING|TERMINATED`.
   */
  async *listMicrovms(options: ListMicrovmsOptions = {}): AsyncIterable<SandboxListItem> {
    const wanted = options.states === undefined ? undefined : new Set(options.states);
    let nextToken: string | undefined;
    do {
      const page = await this.listMicrovmsPage({
        imageArn: options.imageArn,
        imageVersion: options.imageVersion,
        maxResults: LIST_MAX_RESULTS,
        nextToken,
      });
      for (const item of page.items) {
        if (listedStateWanted(item.state, wanted)) {
          yield item;
        }
      }
      nextToken = page.nextToken;
    } while (nextToken !== undefined);
  }

  /** `nextToken`, `imageIdentifier` e `imageVersion` sólo viajan si se dan (`AWS_API_NOTES.md` §6). */
  async listMicrovmsPage(options: ListMicrovmsPageOptions): Promise<MicrovmListPage> {
    const page = (await this.#invoke(
      "ListMicrovms",
      new ListMicrovmsCommand(listMicrovmsInput(options)),
    )) as ListPage;
    return Object.freeze({
      items: Object.freeze((page.items ?? []).map(sandboxListItemFromResponse)),
      nextToken: page.nextToken ?? undefined,
    });
  }

  /** Idempotente en el modelo; `false` sólo si el MicroVM no existe. */
  async terminateMicrovm(sandboxId: string): Promise<boolean> {
    try {
      await this.#invoke(
        "TerminateMicrovm",
        new TerminateMicrovmCommand({ microvmIdentifier: sandboxId }),
      );
    } catch (error) {
      if (error instanceof SandboxNotFoundError) {
        return false;
      }
      throw error;
    }
    return true;
  }

  /** `false` cuando AWS responde `ConflictException` (no estaba `RUNNING`). */
  async suspendMicrovm(sandboxId: string): Promise<boolean> {
    try {
      await this.#invoke(
        "SuspendMicrovm",
        new SuspendMicrovmCommand({ microvmIdentifier: sandboxId }),
      );
    } catch (error) {
      if (error instanceof SandboxStateError) {
        return false;
      }
      throw error;
    }
    return true;
  }

  /** `false` cuando AWS responde `ConflictException` (no estaba `SUSPENDED`). */
  async resumeMicrovm(sandboxId: string, options: ControlPlaneCallOptions = {}): Promise<boolean> {
    try {
      await this.#invoke(
        "ResumeMicrovm",
        new ResumeMicrovmCommand({ microvmIdentifier: sandboxId }),
        options.signal,
      );
    } catch (error) {
      if (error instanceof SandboxStateError) {
        return false;
      }
      throw error;
    }
    return true;
  }

  async createAuthToken(
    sandboxId: string,
    ports: readonly PortSpec[],
    options: ControlPlaneCallOptions = {},
  ): Promise<string> {
    if (ports.length === 0) {
      throw new InvalidArgumentError("allowedPorts necesita al menos un puerto");
    }
    const response = (await this.#invoke(
      "CreateMicrovmAuthToken",
      new CreateMicrovmAuthTokenCommand({
        microvmIdentifier: sandboxId,
        expirationInMinutes: TOKEN_TTL_MINUTES,
        allowedPorts: ports.map((spec) => spec.toApi()),
      }),
      options.signal,
    )) as { authToken?: Record<string, string> | undefined };
    return proxyJweFromResponse(response.authToken ?? {});
  }

  /** Con `signal`: uno ya abortado no envía nada y abortar a mitad rechaza con su `reason`, sin traducirlo. */
  async #invoke(operation: string, command: unknown, signal?: AbortSignal): Promise<unknown> {
    signal?.throwIfAborted();
    await raceAbort<unknown>(this.#buckets.get(operation)?.acquire() ?? Promise.resolve(), signal);
    try {
      return await (signal === undefined
        ? this.#client.send(command)
        : this.#client.send(command, { abortSignal: signal }));
    } catch (error) {
      throw abortReasonOr(signal, translateAwsError(error));
    }
  }

  #callerIdentity(): Promise<CallerIdentity> {
    this.#identity ??= this.#fetchCallerIdentity().catch((error: unknown) => {
      this.#identity = undefined;
      throw error;
    });
    return this.#identity;
  }

  async #fetchCallerIdentity(): Promise<CallerIdentity> {
    let response: { Account?: string | undefined; Arn?: string | undefined };
    try {
      response = (await this.#sts().send(new GetCallerIdentityCommand({}))) as typeof response;
    } catch (error) {
      throw translateAwsError(error);
    }
    const account = response.Account ?? "";
    const partition = (response.Arn ?? "").split(":")[1] ?? "";
    if (!account || !partition) {
      throw new SandboxError("GetCallerIdentity no devolvió Account y Arn");
    }
    return { account, partition };
  }

  #sts(): CommandSender {
    if (typeof this.#stsClient === "function") {
      this.#stsClient = this.#stsClient();
    }
    if (this.#stsClient === undefined) {
      throw new InvalidArgumentError(
        "resolver un template por nombre requiere STS; pasa el ARN de la imagen",
      );
    }
    return this.#stsClient;
  }
}

const sharedPlanes = new Map<string, LambdaMicrovmsControlPlane>();

/**
 * Un plano por región y proceso (ARCHITECTURE.md, "Token buckets por
 * proceso"): N `Sandbox.create()` concurrentes sin `controlPlane` explícito
 * comparten los mismos buckets y el mismo cliente del SDK. Con `retries`,
 * `proxy` o `integration` el plano es otro, dedicado a esa combinación y
 * compartido por quien la repita.
 */
export function sharedControlPlane(
  region?: string,
  settings: ControlPlaneClientSettings = {},
): LambdaMicrovmsControlPlane {
  const key = JSON.stringify([
    region ?? "",
    validateRetries(settings.retries) ?? null,
    validateProxyUrl(settings.proxy) ?? null,
    validateIntegration(settings.integration) ?? null,
  ]);
  let plane = sharedPlanes.get(key);
  if (plane === undefined) {
    plane = LambdaMicrovmsControlPlane.fromRegion(region, settings);
    sharedPlanes.set(key, plane);
  }
  return plane;
}

export function idlePolicyToApi(idle: IdlePolicy): IdlePolicyApi {
  if (idle.suspendedDurationSeconds === undefined) {
    throw new InvalidArgumentError(
      "suspendedDurationSeconds debe estar resuelto antes de run-microvm",
    );
  }
  return {
    maxIdleDurationSeconds: idle.maxIdleSeconds,
    suspendedDurationSeconds: idle.suspendedDurationSeconds,
    autoResumeEnabled: idle.autoResume,
  };
}

export function idlePolicyFromResponse(
  payload: Partial<IdlePolicyApi> | undefined,
): IdlePolicy | undefined {
  if (payload === undefined || Object.keys(payload).length === 0) {
    return undefined;
  }
  return Object.freeze({
    maxIdleSeconds: Number(payload.maxIdleDurationSeconds),
    suspendedDurationSeconds: Number(payload.suspendedDurationSeconds),
    autoResume: Boolean(payload.autoResumeEnabled),
  });
}

/** `endpoint` llega como hostname pelado (medido); tolera `https://host/`. */
export function normalizeEndpoint(endpoint: string): string {
  let host = endpoint.trim();
  const scheme = host.indexOf("://");
  if (scheme >= 0) {
    host = host.slice(scheme + 3);
  }
  return host.split("/", 1)[0] ?? "";
}

export function sandboxInfoFromResponse(response: MicrovmResponse): SandboxInfo {
  return sandboxInfo({
    sandboxId: String(response.microvmId),
    state: String(response.state),
    endpoint: normalizeEndpoint(String(response.endpoint ?? "")),
    template: String(response.imageArn),
    templateVersion: String(response.imageVersion),
    startedAt: asDate(response.startedAt),
    maximumDurationSeconds: Number(response.maximumDurationInSeconds),
    terminatedAt: response.terminatedAt === undefined ? undefined : asDate(response.terminatedAt),
    stateReason: response.stateReason,
    idle: idlePolicyFromResponse(response.idlePolicy),
    executionRoleArn: response.executionRoleArn,
    ingress: (response.ingressNetworkConnectors ?? []).map(String),
    egress: (response.egressNetworkConnectors ?? []).map(String),
  });
}

export function sandboxListItemFromResponse(item: ListedItem): SandboxListItem {
  return sandboxListItem({
    sandboxId: String(item.microvmId),
    state: String(item.state),
    template: String(item.imageArn),
    templateVersion: String(item.imageVersion),
    startedAt: asDate(item.startedAt),
  });
}

function asDate(value: Date | string | number | undefined): Date {
  return value instanceof Date ? value : new Date(value ?? 0);
}

/** El map trae una única clave `X-aws-proxy-auth` (medido); se toleran otras. */
export function proxyJweFromResponse(parts: Readonly<Record<string, string>>): string {
  const exact = parts[AUTH_TOKEN_RESPONSE_KEY];
  if (exact) {
    return exact;
  }
  for (const [key, value] of Object.entries(parts)) {
    if (key.toLowerCase() === AUTH_TOKEN_RESPONSE_KEY.toLowerCase()) {
      return value;
    }
  }
  const values = Object.values(parts);
  if (values.length === 1 && values[0] !== undefined) {
    return values[0];
  }
  throw new SandboxError(
    `authToken sin ${AUTH_TOKEN_RESPONSE_KEY}; claves recibidas: ${JSON.stringify(Object.keys(parts).sort())}`,
  );
}

interface AwsErrorShape {
  readonly name?: unknown;
  readonly message?: unknown;
  readonly retryAfterSeconds?: unknown;
  readonly quotaCode?: unknown;
}

const METADATA_KEY = "$metadata";

function isAwsErrorShape(error: unknown): error is AwsErrorShape {
  return typeof error === "object" && error !== null && ("name" in error || METADATA_KEY in error);
}

function httpStatusCode(error: AwsErrorShape): number | undefined {
  const metadata = (error as Record<string, unknown>)[METADATA_KEY];
  if (typeof metadata !== "object" || metadata === null) {
    return undefined;
  }
  const status = (metadata as Record<string, unknown>).httpStatusCode;
  return typeof status === "number" ? status : undefined;
}

/** Tabla de errores por `name` (nunca por status HTTP). */
export function translateAwsError(error: unknown): Error {
  if (
    error instanceof SandboxError ||
    error instanceof AuthenticationError ||
    error instanceof QuotaExceededError ||
    error instanceof CapacityError
  ) {
    return error;
  }
  if (!isAwsErrorShape(error)) {
    return error instanceof Error ? error : new SandboxError(String(error));
  }
  const code = typeof error.name === "string" ? error.name : "";
  const message = typeof error.message === "string" && error.message ? error.message : code;
  const statusCode = httpStatusCode(error);
  switch (code) {
    case "ResourceNotFoundException":
      return new SandboxNotFoundError(message, { statusCode, awsCode: code, cause: error });
    case "ValidationException":
      return new InvalidArgumentError(message, { statusCode, awsCode: code, cause: error });
    case "AccessDeniedException":
      return new AuthenticationError(message, { awsCode: code, cause: error });
    case "ThrottlingException":
      return new RateLimitError(message, {
        retryAfter:
          typeof error.retryAfterSeconds === "number" ? error.retryAfterSeconds : undefined,
        statusCode,
        awsCode: code,
        cause: error,
      });
    case "ConflictException":
      return new SandboxStateError(message, { statusCode, awsCode: code, cause: error });
    case "ServiceQuotaExceededException":
      return new QuotaExceededError(message, {
        quotaCode: typeof error.quotaCode === "string" ? error.quotaCode : undefined,
      });
    case "InsufficientCapacityException":
      return new CapacityError(message);
    default:
      return new SandboxError(message, {
        statusCode,
        awsCode: code || undefined,
        cause: error,
      });
  }
}

function listedStateWanted(state: string, wanted: ReadonlySet<string> | undefined): boolean {
  if (wanted === undefined) {
    return !TERMINAL_STATES.has(state);
  }
  return wanted.has(state);
}
