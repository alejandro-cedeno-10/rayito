/**
 * `Sandbox`: un MicroVM con `rayd` dentro. Se crea con `await Sandbox.create()`
 * o `await Sandbox.connect(id)`; `await using` lo mata al salir del bloque.
 * La superficie es la del SDK Python en camelCase y milisegundos; toda la
 * mecánica (transportes, readiness, reconexión) vive en `SandboxCore`.
 */

import { create } from "@bufbuild/protobuf";
import { abortReasonOr, raceAbort } from "../abort.js";
import {
  awsClientSettingsOf,
  type CommandSender,
  type ControlPlane,
  type ControlPlaneClientSettings,
  hasClientSettings,
  LambdaMicrovmsControlPlane,
  PortSpec,
  sharedControlPlane,
} from "../aws/control-plane.js";
import {
  buildConfigureRequest,
  CONFIGURE_FEATURE,
  CONFIGURE_SETTLE_POLL_MS,
  type ConfigureSection,
  checkConfigureResponse,
  isPostApplySection,
  type PlannedSection,
  requireCapabilities,
  requireConfigureSupport,
  resolveSections,
  settleTimeoutMs,
  stillPending,
} from "../configure/base.js";
import {
  errorMessage,
  IndexWriteError,
  InvalidArgumentError,
  NotFoundError,
  SandboxNotFoundError,
  SandboxNotReadyError,
  TimeoutError,
} from "../errors.js";
import { planFeatures } from "../feature-options.js";
import {
  type ConfigureRequest,
  type ConfigureResponse,
  ConfigureStatusRequestSchema,
  type ConfigureStatusResponse,
} from "../gen/rayito/v1/configure_pb.js";
import { HealthRequestSchema, MetricsRequestSchema } from "../gen/rayito/v1/health_pb.js";
import { TimeoutMode } from "../gen/rayito/v1/lifecycle_pb.js";
import { GetNetworkRequestSchema, NetworkService } from "../gen/rayito/v1/network_pb.js";
import { S3MountsStatusSchema } from "../gen/rayito/v1/s3_mounts_pb.js";
import { type DynamoDbIndex, validateIndex } from "../index/dynamodb.js";
import { DEFAULT_PORT, SUSPENDED_STATES, TERMINAL_STATES } from "../limits.js";
import type { Logger } from "../logger.js";
import {
  type CodeContext,
  EgressEnforcement,
  type Execution,
  HostAccess,
  type IdlePolicyInput,
  type NetworkPolicyInput,
  type NetworkState,
  planSize,
  type ResolvedS3Staging,
  type S3Staging,
  type SandboxHealth,
  type SandboxInfo,
  type SandboxListItem,
  type SandboxMetrics,
  sandboxInfo,
  sizedTemplateName,
  withLifecycle,
  withSizeFacts,
} from "../models.js";
import {
  type Instrumentation,
  instrumentationFor,
  NOOP,
  type TracerProviderLike,
} from "../otel.js";
import { rejectLaunchOptionsWithPool } from "../pool/core.js";
import type { SandboxPool } from "../pool/pool.js";
import { resolveImageVariant } from "../role-policy.js";
import type { MountStatus, S3MountsOption } from "../s3-mounts/domain.js";
import { fromProtoStatus } from "../s3-mounts/section.js";
import type { SecretGateway } from "../secret-gateway/domain.js";
import {
  EMPTY_GATEWAYS,
  SECTION_NAME as GATEWAY_SECTION,
  GatewayHandle,
} from "../secret-gateway/section.js";
import type { SecretCache } from "../secrets/cache.js";
import {
  bindSecrets,
  type SecretBinding,
  SecretEnvs,
  type SecretOptions,
  sharedSecretCache,
  warm,
} from "../secrets/inject.js";
import { defaultSizeCatalog } from "../sizing/catalog.js";
import type { SizeInput } from "../sizing/sizing.js";
import { translateSetTimeoutError } from "../transport/errors.js";
import { TokenRefresher, TokenStore } from "../transport/tokens.js";
import { resolveTransportSettings, type TransportSettings } from "../transport/transport.js";
import {
  CodeClient,
  type ContextLike,
  type CreateContextOptions,
  type RunCodeOptions,
} from "./code.js";
import { Commands, metricsFromProto, type RequestOptions } from "./commands.js";
import { callOptions, SandboxCore } from "./core.js";
import { Filesystem } from "./filesystem.js";
import { Git } from "./git.js";
import {
  buildLaunchPlan,
  DEFAULT_READY_TIMEOUT_MS,
  DEFAULT_RECONNECT_TIMEOUT_MS,
  DEFAULT_REQUEST_TIMEOUT_MS,
  type LoggingOption,
  type PortLike,
  requireAccessToken,
  resolveTemplate,
  validateHostPort,
  validateSandboxId,
} from "./launch.js";
import {
  connectExtension,
  deadlineMayHaveMoved,
  lifecycleFromProto,
  type OnTimeout,
  olderAgentError,
  optionalSetTimeoutMs,
  setTimeoutFeature,
  setTimeoutRequest,
  suspendedSetTimeoutError,
  validateSetTimeoutMs,
} from "./lifecycle.js";
import { type ListOrder, listingRequest } from "./listing.js";
import {
  assertReadableWithoutWaking,
  fetchMetricsHistory,
  HISTORY_FEATURE,
  historyErrorTranslator,
  type MetricsHistoryOptions,
  metricsHistoryFromProto,
  metricsHistoryRequest,
} from "./metrics.js";
import {
  egressFeature,
  egressGateError,
  GET_NETWORK_FEATURE,
  isEmptyPolicy,
  logAllowOnlyNotice,
  networkRpcError,
  type ResolvedNetworkPolicy,
  rejectNetworkWithPool,
  requiresEnforcement,
  resolveNetwork,
  stateFromProto,
  UPDATE_NETWORK_FEATURE,
  updateNetworkRequest,
  validatePolicyShape,
} from "./network.js";
import {
  type ListingContext,
  listSandboxes,
  METADATA_PROBE_TIMEOUT_MS,
  SandboxListPaginator,
} from "./paginator.js";
import {
  bindPersist,
  type CheckpointFilesOptions,
  type CheckpointResult,
  type LaunchOptions,
  PersistenceClient,
  type ReincarnateOptions,
  type RestoreFilesOptions,
  type RestoreResult,
  reincarnateRequiresCreateError,
  reincarnateRequiresPersistError,
  requireNamedPersist,
  requireRoleForPersist,
  type S3Prefix,
  shouldAutoRestore,
  validatePersistTimeoutMs,
  withReincarnateNote,
} from "./persistence.js";
import { Pty } from "./pty.js";
import {
  alreadySuspended,
  formatSeconds,
  healthFromProto,
  ReadinessPoll,
  terminalStateError,
} from "./readiness.js";
import {
  expiresInFromSignatureExpiration,
  resolveS3Staging,
  validateStagingAgainstPersist,
} from "./transfer.js";

export const STATE_POLL_INTERVAL_MS = 500;

/**
 * `retries` (`maxAttempts = retries + 1`), `proxy` (`http://h:puerto`) e
 * `integration` (User-Agent) construyen un plano dedicado a esa combinación;
 * con `controlPlane` o `client` explícitos son `InvalidArgumentError`, porque
 * ignorarlos en silencio sería mentir.
 */
export interface ControlPlaneOptions extends ControlPlaneClientSettings {
  readonly region?: string | undefined;
  readonly controlPlane?: ControlPlane | undefined;
  /** Un `LambdaMicrovmsClient` propio: construye un plano privado con sus propios buckets. */
  readonly client?: CommandSender | undefined;
}

export interface SandboxConnectOptions extends ControlPlaneOptions, SecretOptions {
  readonly accessToken?: string | undefined;
  readonly readyTimeoutMs?: number | undefined;
  readonly requestTimeoutMs?: number | undefined;
  readonly reconnectTimeoutMs?: number | undefined;
  readonly transport?: Partial<TransportSettings> | undefined;
  readonly logger?: Logger | undefined;
  /**
   * Cancela el arranque: uno ya abortado rechaza antes de tocar AWS; abortado
   * durante el sondeo de readiness rechaza con `signal.reason` y, en
   * `create()`, termina el MicroVM salvo `keepOnFailure`. `run-microvm` nunca
   * se corta a mitad (dejaría un VM sin id que terminar): el aborto se mira
   * justo después.
   */
  readonly signal?: AbortSignal | undefined;
  /**
   * El plazo lógico en ms que impone `rayd` (ADR-011). En `create` lo fija
   * (3 600 000 por defecto, máximo 28 800 000); sin `maxLifetimeMs` ni
   * `onTimeout` también es la vida de la plataforma y no viaja plazo lógico,
   * exactamente como antes de M9. En `connect` nunca acorta: manda
   * `SetTimeout(AT_LEAST)` y el plazo pasa a ser al menos ahora + `timeoutMs`
   * (`InvalidArgumentError` en un sandbox sin plazo lógico,
   * `LifecycleUnsupportedError` en una imagen anterior a 0.3.0).
   */
  readonly timeoutMs?: number | undefined;
  /**
   * Enlaza el `HOME` a `s3://bucket/prefix/name/`. En `create` requiere
   * `executionRoleArn` y, con `name`, restaura el checkpoint que haya
   * (`sandbox.lastRestore`); sin `name` lo fija al `sandboxId`. En `connect`
   * sólo enlaza (necesita `name`) y no restaura nada.
   */
  readonly persist?: S3Prefix | undefined;
  /**
   * El bucket de transferencias de `files.uploadUrl`/`downloadUrl` y de los
   * ficheros grandes (ADR-010); sólo configura el cliente, nada viaja al VM.
   * `undefined` lee `RAYITO_TRANSFER_BUCKET` (y `_PREFIX`, `_REGION`); `null`
   * lo desactiva. Con `persist` en el mismo bucket, los prefijos deben ser
   * disjuntos (`InvalidArgumentError` antes de tocar AWS).
   */
  readonly transfer?: S3Staging | null | undefined;
  /**
   * Spans OpenTelemetry del lado del SDK (M13b), opt-in. Guarda en el handle
   * una `Instrumentation` que envuelve `commands.run`, `run_code`,
   * `files.*` y `kill`/`pause`/`resume` de instancia en spans `rayito.*`
   * (`SpanKind.CLIENT`); `create()`/`connect()` en sí son, cada uno, un
   * único span. Nunca lleva texto de comandos, código, rutas, `envs` ni
   * secretos como atributo.
   *
   * Coste y activación
   * -------------------
   * Activa: `tracerProvider` (un `TracerProvider` de `@opentelemetry/api`,
   *   o cualquier objeto con `getTracer(name, version)`).
   * Recursos y llamadas AWS: ninguno; rayito sólo crea spans en el
   *   proveedor del llamante.
   * Coste aproximado: $0 de AWS; el coste (si lo hay) es el del backend de
   *   exportación que configure el llamante.
   * IAM: ninguno adicional.
   * Cómo apagarla: no pases `tracerProvider` (por defecto `undefined`).
   * Ejemplo:
   *   import { trace } from "@opentelemetry/api";
   *   const sbx = await Sandbox.create({ tracerProvider: trace.getTracerProvider() });
   *   await sbx.commands.run("echo hola"); // span "rayito.commands.run"
   *   await sbx.kill();
   */
  readonly tracerProvider?: TracerProviderLike | undefined;
}

export interface SandboxCreateOptions extends SandboxConnectOptions {
  /** ARN o nombre de la imagen; por defecto `RAYITO_TEMPLATE`. */
  readonly template?: string | undefined;
  readonly templateVersion?: string | undefined;
  /**
   * El tope de la plataforma (`maximumDurationInSeconds`, running +
   * suspendido): un múltiplo de 1000 entre 120 000 y 28 800 000, fijo desde
   * `create()` porque `UpdateMicrovm` no existe. El plazo lógico nunca pasa
   * de `maxLifetimeMs − 60 s` desde el arranque. Por defecto
   * `timeoutMs + 60 000` (al menos 120 000) cuando se pasa `onTimeout`.
   * Con él o con `onTimeout` la imagen tiene que ser M9: si su `Health` no
   * trae `lifecycle`, `create()` termina el MicroVM (salvo `keepOnFailure`)
   * y lanza `LifecycleUnsupportedError`.
   */
  readonly maxLifetimeMs?: number | undefined;
  /**
   * Qué hace `rayd` al vencer el plazo: `"kill"` (por defecto con
   * `maxLifetimeMs`) sale y el MicroVM termina ≈ 15 s después sin IAM;
   * `"pause"` lo suspende, en el acto con un cliente vivo y como mucho a
   * `idle.maxIdleSeconds` sin él (exige `idle`; `idle.autoResume` es el
   * auto-resume lógico tras el plazo).
   */
  readonly onTimeout?: OnTimeout | undefined;
  /** `null` desactiva el auto-suspend; por defecto `{ maxIdleSeconds: 300, autoResume: true }`. */
  readonly idle?: IdlePolicyInput | null | undefined;
  readonly envs?: Readonly<Record<string, string>> | undefined;
  /** Etiquetas no secretas que viajan en el `runHookPayload` y vuelven en `Health`. */
  readonly metadata?: Readonly<Record<string, string>> | undefined;
  /** Segundos de CPU por proceso (`RLIMIT_CPU`, `1..=28800`), nunca de pared. */
  readonly cpuTimeLimit?: number | undefined;
  readonly executionRoleArn?: string | undefined;
  readonly allowedPorts?: readonly PortLike[] | undefined;
  readonly ingress?: readonly string[] | undefined;
  readonly egress?: readonly string[] | undefined;
  readonly logging?: LoggingOption | undefined;
  readonly keepOnFailure?: boolean | undefined;
  /** Deadline del restore automático de `persist` y de `reincarnate()` (600 000 ms). */
  readonly persistTimeoutMs?: number | undefined;
  /**
   * Política de egress en el guest (sólo `rayito-base-caps`), aplicada antes
   * de que `create()` devuelva el sandbox. Si la imagen no la aplica, el
   * MicroVM se termina (también con `keepOnFailure`) y se lanza
   * `UnimplementedError`: nunca queda un sandbox sin la política pedida.
   */
  readonly network?: NetworkPolicyInput | undefined;
  /** `false` equivale a añadir `ALL_TRAFFIC` a `network.denyOut` (como en E2B). */
  readonly allowInternetAccess?: boolean | undefined;
  /**
   * Azúcar de `pool.take()`: el sandbox sale de una plaza suspendida del
   * `SandboxPool` (ya arrancado) con la configuración de su `PoolConfig`;
   * cualquier otra opción de lanzamiento o de plano es `InvalidArgumentError`.
   * Sólo pasan `readyTimeoutMs`, `requestTimeoutMs`, `reconnectTimeoutMs` y `logger`.
   */
  readonly pool?: SandboxPool | undefined;
  /**
   * Índice opcional de metadatos (M14): tras `run-microvm` y antes de la
   * sonda de readiness escribe una fila inmutable (`metadata`, imagen,
   * `startedAt`) en tu tabla DynamoDB, para que `Sandbox.list({ metadata,
   * index })` encuentre el sandbox también suspendido y sin sondearlo. Si
   * `PutItem` falla, con `onWriteFailure: "terminate"` (por defecto) el VM se
   * termina (salvo `keepOnFailure`) y se lanza `IndexWriteError`; con
   * `"warn"` sólo se avisa en el `logger`. Con `pool` es
   * `InvalidArgumentError`: se configura en `PoolConfig.index`. `kill()` no
   * toca el índice.
   *
   * Coste y activación
   * -------------------
   * Activa: `index: new DynamoDbIndex({...})` escribe la fila de este sandbox.
   * Recursos y llamadas AWS: `dynamodb:PutItem` una vez (condicional
   *   `attribute_not_exists(pk)`); ningún recurso nuevo (la tabla la
   *   despliegas tú con `infra/metadata-index.yaml`). Sin `index` no se carga
   *   `@aws-sdk/client-dynamodb`.
   * Coste aproximado: ~1 WRU ≈ $0,000000625 por sandbox + $0,25/GB-mes
   *   (DynamoDB on-demand, us-east-1, consultado 2026-09-30).
   * IAM: `dynamodb:PutItem` sobre la tabla (`RayitoIndexWriter`) en las
   *   credenciales del llamante.
   * Cómo apagarla: `index: undefined` (por defecto).
   * Ejemplo:
   *   const index = new DynamoDbIndex({ tableName: "rayito-sandboxes" });
   *   const sbx = await Sandbox.create({ metadata: { user: "42" }, index });
   */
  readonly index?: DynamoDbIndex | undefined;
  /**
   * Las siete opciones 0.6 (M15): cada una llega en su propio cambio
   * OpenSpec y, mientras siga siendo un stub, ponerla a algo distinto de
   * `undefined` lanza `UnimplementedError` nombrando ese cambio, antes de
   * `run-microvm`. Ninguna hace ninguna llamada a AWS por sí sola; con las
   * siete ausentes (su valor por defecto) el comportamiento es exactamente
   * el de 0.5.x. `size` ya no es un stub (m15-sizes-catalog): ver
   * `sizing/sizing.ts`.
   */
  readonly mounts?: S3MountsOption | undefined;
  readonly volumes?: Readonly<Record<string, unknown>> | undefined;
  readonly size?: SizeInput | undefined;
  readonly events?: unknown;
  readonly telemetry?: unknown;
  readonly gateways?: Readonly<Record<string, SecretGateway>> | undefined;
  readonly domain?: unknown;
}

/**
 * Los objetos de `create()` que `reincarnate()` reutiliza tal cual (no viajan
 * en `LaunchOptions`). Los secretos no están aquí: `reincarnate()` relanza con
 * los que el handle tenga en ese momento (tras `take()` o `connect()`).
 */
interface LaunchContext {
  readonly controlPlane: ControlPlane;
  readonly transport: Partial<TransportSettings> | undefined;
  readonly logger: Logger | undefined;
  /** El índice del `create()`: el sucesor escribe su propia fila en él. */
  readonly index: DynamoDbIndex | undefined;
}

/**
 * `template` viaja como filtro de AWS; `states`, `startedAfter` (incluido) y
 * `metadata` se aplican en el cliente. `metadata` sólo filtra sandboxes
 * `RUNNING` y sondea el `Health` de cada candidato (O(n), deadline
 * `requestTimeoutMs`, 5 000 ms por defecto). `order` ordena por `startedAt`
 * tras recorrer todas las páginas (O(páginas)).
 */
export interface SandboxListOptions extends ControlPlaneOptions {
  readonly template?: string | undefined;
  readonly templateVersion?: string | undefined;
  readonly states?: readonly string[] | undefined;
  readonly metadata?: Readonly<Record<string, string>> | undefined;
  readonly startedAfter?: Date | undefined;
  readonly order?: ListOrder | undefined;
  readonly requestTimeoutMs?: number | undefined;
  readonly transport?: Partial<TransportSettings> | undefined;
  /**
   * Con `metadata`, resuelve el filtro con el índice opcional de metadatos
   * (M14) en vez de sondear: un `BatchGetItem` por página de
   * `list-microvms` y la unión con sus filas; ningún `get-microvm`, token ni
   * `Health`, así que nada se despierta. `states` admite cualquier estado no
   * terminal (por defecto todos). El estado es siempre el de
   * `list-microvms`; sólo aparecen los sandboxes creados con ese índice. Sin
   * `metadata` no cambia nada. El `nextToken` queda ligado a la tabla.
   *
   * Coste y activación
   * -------------------
   * Activa: `index: new DynamoDbIndex({...})` junto a `metadata`.
   * Recursos y llamadas AWS: `dynamodb:BatchGetItem` una vez por página (≤ 50
   *   claves, eventualmente consistente).
   * Coste aproximado: 0,5 RRU por candidato ($0,125 por millón de RRU,
   *   DynamoDB on-demand, us-east-1, consultado 2026-09-30).
   * IAM: `dynamodb:BatchGetItem` sobre la tabla (`RayitoIndexReader`).
   * Cómo apagarla: `index: undefined` (por defecto): la sonda O(n) de siempre.
   * Ejemplo:
   *   const index = new DynamoDbIndex({ tableName: "rayito-sandboxes" });
   *   for await (const item of Sandbox.list({ metadata: { user: "42" }, states: ["SUSPENDED"], index })) {
   *     console.log(item.sandboxId, item.state);
   *   }
   */
  readonly index?: DynamoDbIndex | undefined;
}

/** `limit` items por `nextItems()` (todos si falta) y un `nextToken` de un paginador anterior con los mismos filtros. */
export interface SandboxPaginateOptions extends SandboxListOptions {
  readonly limit?: number | undefined;
  readonly nextToken?: string | undefined;
}

/** El access token (o `RAYITO_ACCESS_TOKEN`) es obligatorio: `rayd` exige `x-access-token` en `MetricsHistory`. */
export interface StaticMetricsHistoryOptions extends MetricsHistoryOptions, ControlPlaneOptions {
  readonly accessToken?: string | undefined;
  readonly transport?: Partial<TransportSettings> | undefined;
}

export interface PauseOptions {
  readonly wait?: boolean | undefined;
}

/** Las opciones de `sandbox.uploadUrl`/`downloadUrl` con los nombres de E2B. */
export interface SignedUrlOptions {
  readonly user?: string | undefined;
  /** Vida de la URL en segundos (3600 por defecto; topada por `transfer.maxExpiresIn` y 7 días). */
  readonly useSignatureExpiration?: number | undefined;
}

export interface StaticPauseOptions extends ControlPlaneOptions, PauseOptions {
  readonly readyTimeoutMs?: number | undefined;
  /**
   * Spans OpenTelemetry (M13b) como en `SandboxConnectOptions.tracerProvider`;
   * `undefined` por defecto (apagado). $0 de AWS, ningún IAM adicional.
   * Ejemplo: `{ tracerProvider: trace.getTracerProvider() }`.
   */
  readonly tracerProvider?: TracerProviderLike | undefined;
}

/** `Sandbox.kill(sandboxId)`: sin handle, así que `tracerProvider` es su única opción propia. */
export interface StaticKillOptions extends ControlPlaneOptions {
  /**
   * Spans OpenTelemetry (M13b) como en `SandboxConnectOptions.tracerProvider`;
   * `undefined` por defecto (apagado). $0 de AWS, ningún IAM adicional.
   * Ejemplo: `{ tracerProvider: trace.getTracerProvider() }`.
   */
  readonly tracerProvider?: TracerProviderLike | undefined;
}

/** `Sandbox.setTimeout(sandboxId, timeoutMs)`: un canal dedicado que se cierra al terminar. */
export interface SandboxSetTimeoutOptions extends ControlPlaneOptions {
  readonly accessToken?: string | undefined;
  readonly requestTimeoutMs?: number | undefined;
  /** Cancela `get-microvm` y el `SetTimeout`: rechaza con `signal.reason`. */
  readonly signal?: AbortSignal | undefined;
  readonly transport?: Partial<TransportSettings> | undefined;
  readonly logger?: Logger | undefined;
}

/** `sbx.connect({ timeoutMs })`: reabre el handle y extiende el plazo como `Sandbox.connect`. */
export interface InstanceConnectOptions extends RequestOptions, SecretOptions {
  readonly timeoutMs?: number | undefined;
  /** Sustituye la `Instrumentation` del handle (M13b); `undefined` conserva la que ya tenía. */
  readonly tracerProvider?: TracerProviderLike | undefined;
}

export interface UpdateNetworkOptions {
  /** `false` añade `ALL_TRAFFIC` a `denyOut`; `true` o ausente dejan las listas como vienen. */
  readonly allowInternetAccess?: boolean | undefined;
  readonly requestTimeoutMs?: number | undefined;
  readonly signal?: AbortSignal | undefined;
}

export interface StaticUpdateNetworkOptions extends SandboxConnectOptions {
  readonly allowInternetAccess?: boolean | undefined;
}

export function resolveControlPlane(options: ControlPlaneOptions): ControlPlane {
  const explicit = options.controlPlane !== undefined || options.client !== undefined;
  if (explicit && hasClientSettings(options)) {
    throw new InvalidArgumentError(
      "retries/proxy/integration no se combinan con controlPlane ni con client",
    );
  }
  if (options.controlPlane !== undefined) {
    return options.controlPlane;
  }
  if (options.client !== undefined) {
    const region = options.region ?? process.env.AWS_REGION ?? process.env.AWS_DEFAULT_REGION ?? "";
    return new LambdaMicrovmsControlPlane({ client: options.client, region });
  }
  return sharedControlPlane(options.region, {
    retries: options.retries,
    proxy: options.proxy,
    integration: options.integration,
  });
}

function listingContext(options: SandboxListOptions): ListingContext {
  return {
    plane: resolveControlPlane(options),
    transport: resolveTransportSettings(options.transport),
    probeTimeoutMs: options.requestTimeoutMs ?? METADATA_PROBE_TIMEOUT_MS,
  };
}

/** `SUSPENDED` sin auto-resume de la plataforma: sólo `resume-microvm` lo despierta. */
function needsExplicitResume(info: SandboxInfo): boolean {
  return info.state === "SUSPENDED" && !(info.idle?.autoResume ?? false);
}

/**
 * La fila del índice de `create({ index })`, tras `run-microvm` y antes de la
 * sonda de readiness. Si `PutItem` falla: con `onWriteFailure: "terminate"`
 * termina el VM (salvo `keepOnFailure`) y lanza `IndexWriteError`; con
 * `"warn"` avisa (sin metadatos) y sigue.
 */
async function writeIndexRecord(
  index: DynamoDbIndex,
  info: SandboxInfo,
  metadata: Readonly<Record<string, string>> | undefined,
  context: {
    readonly controlPlane: ControlPlane;
    readonly keepOnFailure: boolean;
    readonly logger: Logger | undefined;
  },
): Promise<void> {
  try {
    await index.put(index.record(info, metadata));
  } catch (error) {
    const awsCode = (error as { awsCode?: unknown } | null)?.awsCode;
    const code = typeof awsCode === "string" ? awsCode : errorName(error);
    if (index.onWriteFailure === "warn") {
      context.logger?.warn?.(
        "índice de metadatos: no se pudo escribir la fila; el sandbox sigue vivo pero no aparecerá en list({ metadata, index })",
        { sandboxId: info.sandboxId, reason: code },
      );
      return;
    }
    if (!context.keepOnFailure) {
      await terminateQuietly(context.controlPlane, info.sandboxId, context.logger);
    }
    throw error instanceof IndexWriteError
      ? error
      : new IndexWriteError(`no se pudo escribir la fila del índice (${code})`, { cause: error });
  }
}

function errorName(error: unknown): string {
  return error instanceof Error ? error.name : typeof error;
}

/** Limpieza best-effort de un MicroVM que no llegó a estar listo: el error original es el que importa. */
async function terminateQuietly(
  controlPlane: ControlPlane,
  sandboxId: string,
  logger: Logger | undefined,
): Promise<void> {
  try {
    await controlPlane.terminateMicrovm(sandboxId);
  } catch (error) {
    logger?.warn?.("no se pudo terminar el sandbox tras un fallo de arranque", {
      sandboxId,
      reason: errorMessage(error),
    });
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** Sondea `get-microvm` hasta `wanted`. Un estado terminal es fatal. */
export async function waitForState(
  controlPlane: ControlPlane,
  sandboxId: string,
  wanted: string,
  timeoutMs: number,
): Promise<SandboxInfo> {
  const deadline = performance.now() + timeoutMs;
  while (true) {
    const info = await controlPlane.getMicrovm(sandboxId);
    if (info.state === wanted) {
      return info;
    }
    if (TERMINAL_STATES.has(info.state)) {
      throw terminalStateError(info);
    }
    if (performance.now() >= deadline) {
      throw new TimeoutError(
        `el sandbox ${sandboxId} sigue ${info.state} tras ${formatSeconds(timeoutMs)} s esperando ${wanted}`,
      );
    }
    await sleep(STATE_POLL_INTERVAL_MS);
  }
}

/** Lo que `Sandbox.#open` necesita; el pool lo usa a través de `Sandbox.openWith`. */
export interface SandboxOpenOptions {
  readonly accessToken: string;
  readonly controlPlane: ControlPlane;
  readonly transport: TransportSettings;
  readonly proxyPorts: readonly PortSpec[];
  readonly requestTimeoutMs: number;
  readonly readyTimeoutMs: number;
  readonly reconnectTimeoutMs: number;
  readonly terminateOnFailure: boolean;
  readonly logger: Logger | undefined;
  readonly readiness?: typeof ReadinessPoll | undefined;
  /** El lanzamiento mandó un bloque `lifecycle`: un `Health` sin `lifecycle` es un agente anterior a 0.3.0. */
  readonly requireLifecycle?: boolean | undefined;
  /** Abortado durante la readiness: se trata como cualquier otro fallo de arranque. */
  readonly signal?: AbortSignal | undefined;
}

export class Sandbox implements AsyncDisposable {
  readonly #core: SandboxCore;
  readonly commands: Commands;
  readonly files: Filesystem;
  readonly pty: Pty;
  /** El módulo git de E2B sobre `commands.run` (ver `Git`). */
  readonly git: Git;
  readonly #code: CodeClient;
  readonly #persistence: PersistenceClient;
  #persist: S3Prefix | undefined;
  #lastRestore: RestoreResult | undefined;
  #launchOptions: LaunchOptions | undefined;
  #launchContext: LaunchContext | undefined;
  #readinessHealth: SandboxHealth | undefined;
  readonly #secrets: SecretEnvs;
  #instrumentation: Instrumentation = NOOP;
  /** Lo que `PostApplySection.afterApply` devolvió, por `section`
   * (`#sendConfigureSections`); cada propiedad pública de una función 0.6
   * (`gateways`, ...) lee su entrada de aquí. */
  readonly #sectionHandles = new Map<string, unknown>();

  private constructor(core: SandboxCore) {
    this.#core = core;
    this.#secrets = new SecretEnvs(() =>
      sharedSecretCache(
        core.controlPlane.region,
        awsClientSettingsOf(core.controlPlane).credentials,
      ),
    );
    this.commands = new Commands(core, this.#secrets, () => this.#instrumentation);
    this.files = new Filesystem(core, undefined, () => this.#instrumentation);
    this.pty = new Pty(core, this.commands, this.#secrets);
    this.git = new Git(this.commands);
    this.#code = new CodeClient(core, this.#secrets, () => this.#instrumentation);
    this.#persistence = new PersistenceClient(core);
  }

  // ------------------------------------------------------------------ create

  static async create(options: SandboxCreateOptions = {}): Promise<Sandbox> {
    options.signal?.throwIfAborted();
    const instrumentation = instrumentationFor(options.tracerProvider);
    const binding = bindSecrets(options.secrets, options.secretCache);
    const transportSettings = resolveTransportSettings(options.transport);
    requireRoleForPersist(options.persist, options.executionRoleArn);
    const transfer = resolveS3Staging(options.transfer);
    validateStagingAgainstPersist(transfer, options.persist);
    const network = resolveNetwork(options.network, {
      allowInternetAccess: options.allowInternetAccess,
    });
    validatePolicyShape(network);
    const index = validateIndex(options.index);
    if (options.pool !== undefined && index !== undefined) {
      throw new InvalidArgumentError(
        "create({ pool }) no admite index: la fila del índice la escribe el pool al rellenar cada plaza; pásalo como PoolConfig.index",
      );
    }
    if (options.pool !== undefined && options.persist !== undefined) {
      throw new InvalidArgumentError(
        "create({ pool }) no admite persist: una plaza del pool no puede restaurar un home con nombre al tomarla",
      );
    }
    if (options.pool !== undefined) {
      rejectNetworkWithPool(network);
      rejectLaunchOptionsWithPool(options);
      const taken = await options.pool.take({
        readyTimeoutMs: options.readyTimeoutMs,
        requestTimeoutMs: options.requestTimeoutMs,
        reconnectTimeoutMs: options.reconnectTimeoutMs,
        logger: options.logger,
        secrets: options.secrets,
        secretCache: options.secretCache,
      });
      taken.#core.transfer = transfer;
      taken.#instrumentation = instrumentation;
      return taken;
    }
    logAllowOnlyNotice(network, options.logger);
    // Sin E/S contra AWS: región y peer del índice antes de lanzar nada.
    await index?.prepare();
    // m15-sizes-catalog: `planSize` (`models.ts`) resuelve `size` aquí, no
    // en `planFeatures`: no es una sección de ConfigureSandbox, es qué
    // imagen lanzar.
    const resolvedSize = planSize(options.size);
    const featurePlan = planFeatures(
      {
        mounts: options.mounts,
        volumes: options.volumes,
        size: options.size,
        events: options.events,
        telemetry: options.telemetry,
        gateways: options.gateways,
        domain: options.domain,
      },
      resolveImageVariant(options.template),
      options.logging,
    );
    const plane = resolveControlPlane(options);
    const secrets = await warm(binding, () =>
      sharedSecretCache(plane.region, awsClientSettingsOf(plane).credentials),
    );
    const resolvedTemplateName = sizedTemplateName(resolveTemplate(options.template), resolvedSize);
    const imageArn = await plane.resolveTemplateArn(resolvedTemplateName, {
      signal: options.signal,
    });
    const plan = buildLaunchPlan({
      imageArn,
      region: plane.region,
      templateVersion: options.templateVersion,
      timeoutMs: options.timeoutMs,
      maxLifetimeMs: options.maxLifetimeMs,
      onTimeout: options.onTimeout,
      idle: options.idle,
      envs: options.envs,
      metadata: options.metadata,
      cpuTimeLimit: options.cpuTimeLimit,
      executionRoleArn: options.executionRoleArn,
      allowedPorts: options.allowedPorts,
      ingress: options.ingress,
      egress: options.egress,
      logging: options.logging,
      accessToken: options.accessToken,
      networkEnforce: requiresEnforcement(network),
    });
    options.signal?.throwIfAborted();
    const sandbox = await instrumentation.span(
      "rayito.sandbox.create",
      { "rayito.region": plane.region, "rayito.operation": "create" },
      async (span) => {
        const info = await plane.runMicrovm(plan.request);
        span?.setAttribute("rayito.sandbox.id", info.sandboxId);
        options.logger?.info?.("run-microvm aceptado", {
          sandboxId: info.sandboxId,
          state: info.state,
        });
        if (index !== undefined) {
          await writeIndexRecord(index, info, options.metadata, {
            controlPlane: plane,
            keepOnFailure: options.keepOnFailure ?? false,
            logger: options.logger,
          });
        }
        const opened = await Sandbox.#open(info, {
          accessToken: plan.accessToken,
          controlPlane: plane,
          transport: transportSettings,
          signal: options.signal,
          proxyPorts: plan.proxyPorts,
          requestTimeoutMs: options.requestTimeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS,
          readyTimeoutMs: options.readyTimeoutMs ?? DEFAULT_READY_TIMEOUT_MS,
          reconnectTimeoutMs: options.reconnectTimeoutMs ?? DEFAULT_RECONNECT_TIMEOUT_MS,
          terminateOnFailure: !(options.keepOnFailure ?? false),
          logger: options.logger,
          requireLifecycle: plan.lifecycleRequested,
        });
        opened.#instrumentation = instrumentation;
        opened.#core.transfer = transfer;
        opened.#secrets.set(secrets);
        if (requiresEnforcement(network)) {
          await opened.#applyInitialNetwork(
            network,
            egressFeature(network, options.allowInternetAccess),
          );
        }
        await opened.#applyConfigureSections(
          featurePlan.configureSections,
          options.requestTimeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS,
          !(options.keepOnFailure ?? false),
        );
        return opened;
      },
    );
    sandbox.#launchOptions = {
      template: imageArn,
      templateVersion: options.templateVersion,
      timeoutMs: options.timeoutMs,
      maxLifetimeMs: options.maxLifetimeMs,
      onTimeout: options.onTimeout,
      idle: options.idle,
      envs: options.envs,
      metadata: options.metadata,
      cpuTimeLimit: options.cpuTimeLimit,
      executionRoleArn: options.executionRoleArn,
      allowedPorts: options.allowedPorts,
      ingress: options.ingress,
      egress: options.egress,
      logging: options.logging,
      accessToken: options.accessToken,
      readyTimeoutMs: options.readyTimeoutMs,
      requestTimeoutMs: options.requestTimeoutMs,
      reconnectTimeoutMs: options.reconnectTimeoutMs,
      keepOnFailure: options.keepOnFailure,
      network: isEmptyPolicy(network) ? undefined : network,
      gateways: options.gateways,
      size: resolvedSize,
    };
    sandbox.#launchContext = {
      controlPlane: plane,
      transport: options.transport,
      logger: options.logger,
      index,
    };
    if (options.persist !== undefined) {
      await sandbox.#bindAndRestore(
        options.persist,
        validatePersistTimeoutMs(options.persistTimeoutMs),
        !(options.keepOnFailure ?? false),
      );
    }
    return sandbox;
  }

  /**
   * Se conecta a un sandbox existente; nunca lo termina si algo falla. Tras
   * la readiness nunca acorta el plazo lógico (ADR-011): con `timeoutMs`
   * manda `SetTimeout(AT_LEAST)`; sin él, un sandbox reanudado después de su
   * plazo (`resumeGrace`/`expired`) se reabre con su propio timeout.
   */
  static async connect(sandboxId: string, options: SandboxConnectOptions = {}): Promise<Sandbox> {
    options.signal?.throwIfAborted();
    const instrumentation = instrumentationFor(options.tracerProvider);
    const token = requireAccessToken(options.accessToken);
    const requestedMs = optionalSetTimeoutMs(options.timeoutMs);
    const transportSettings = resolveTransportSettings(options.transport);
    const bound = options.persist === undefined ? undefined : requireNamedPersist(options.persist);
    const transfer = resolveS3Staging(options.transfer);
    validateStagingAgainstPersist(transfer, bound);
    const binding = bindSecrets(options.secrets, options.secretCache);
    const plane = resolveControlPlane(options);
    const secrets = await warm(binding, () =>
      sharedSecretCache(plane.region, awsClientSettingsOf(plane).credentials),
    );
    const validatedSandboxId = validateSandboxId(sandboxId);
    return instrumentation.span(
      "rayito.sandbox.connect",
      { "rayito.sandbox.id": validatedSandboxId, "rayito.operation": "connect" },
      async () => {
        const info = await plane.getMicrovm(validatedSandboxId, { signal: options.signal });
        if (TERMINAL_STATES.has(info.state)) {
          throw terminalStateError(info);
        }
        if (needsExplicitResume(info)) {
          await plane.resumeMicrovm(sandboxId, { signal: options.signal });
        }
        const sandbox = await Sandbox.#open(info, {
          accessToken: token,
          controlPlane: plane,
          transport: transportSettings,
          signal: options.signal,
          proxyPorts: [PortSpec.single(DEFAULT_PORT)],
          requestTimeoutMs: options.requestTimeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS,
          readyTimeoutMs: options.readyTimeoutMs ?? DEFAULT_READY_TIMEOUT_MS,
          reconnectTimeoutMs: options.reconnectTimeoutMs ?? DEFAULT_RECONNECT_TIMEOUT_MS,
          terminateOnFailure: false,
          logger: options.logger,
        });
        sandbox.#instrumentation = instrumentation;
        sandbox.#persist = bound;
        sandbox.#core.transfer = transfer;
        sandbox.#secrets.set(secrets);
        try {
          await sandbox.#extendAfterReadiness(requestedMs, undefined, options.signal);
        } catch (error) {
          sandbox.close();
          throw error;
        }
        return sandbox;
      },
    );
  }

  /**
   * `SetTimeout(EXACT)` sin handle: exige el access token (o
   * `RAYITO_ACCESS_TOKEN`); un sandbox terminado es `SandboxNotFoundError` y
   * uno suspendido `SandboxStateError` sin despertarlo (`connect()` lo
   * reanuda). Acuña un JWE y usa un canal dedicado que cierra al terminar.
   */
  static async setTimeout(
    sandboxId: string,
    timeoutMs: number,
    options: SandboxSetTimeoutOptions = {},
  ): Promise<void> {
    options.signal?.throwIfAborted();
    const token = requireAccessToken(options.accessToken);
    const validated = validateSetTimeoutMs(timeoutMs);
    const plane = resolveControlPlane(options);
    const info = await plane.getMicrovm(validateSandboxId(sandboxId), { signal: options.signal });
    if (TERMINAL_STATES.has(info.state)) {
      throw terminalStateError(info);
    }
    if (SUSPENDED_STATES.has(info.state)) {
      throw suspendedSetTimeoutError(info.sandboxId);
    }
    const refresher = new TokenRefresher(
      new TokenStore(),
      (ports) => plane.createAuthToken(info.sandboxId, ports),
      { logger: options.logger },
    );
    await raceAbort(refresher.mint([PortSpec.single(DEFAULT_PORT)]), options.signal);
    const core = new SandboxCore({
      info,
      accessToken: token,
      controlPlane: plane,
      transport: resolveTransportSettings(options.transport),
      refresher,
      requestTimeoutMs: options.requestTimeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS,
      readyTimeoutMs: DEFAULT_READY_TIMEOUT_MS,
      reconnectTimeoutMs: DEFAULT_RECONNECT_TIMEOUT_MS,
      logger: options.logger,
    });
    try {
      await core.callUnaryOnce(() =>
        core.clients.lifecycle.setTimeout(
          setTimeoutRequest(TimeoutMode.EXACT, validated),
          callOptions(core.requestTimeoutMs, options.signal),
        ),
      );
    } catch (error) {
      throw abortReasonOr(options.signal, translateSetTimeoutError(error, validated));
    } finally {
      core.close();
    }
  }

  /** Acceso interno para el pool (enlaza los secretos ya resueltos de `take()`); no forma parte de la API pública. */
  static attachSecrets(sandbox: Sandbox, binding: SecretBinding | undefined): void {
    if (binding !== undefined) {
      sandbox.#secrets.set(binding);
    }
  }

  /**
   * Acceso interno para el pool (aplica a la plaza ya tomada las secciones
   * 0.6 de `take({ gateways })`, terminándola si alguna falla: una plaza
   * se lanza siempre sin `keepOnFailure`); no forma parte de la API
   * pública.
   */
  static applyConfigureSections(
    sandbox: Sandbox,
    planned: readonly PlannedSection[],
    timeoutMs: number,
  ): Promise<void> {
    return sandbox.#applyConfigureSections(planned, timeoutMs, true);
  }

  /** Acceso interno para el pool (abre una plaza con su token y el calendario de toma); no forma parte de la API pública. */
  static openWith(info: SandboxInfo, options: SandboxOpenOptions): Promise<Sandbox> {
    return Sandbox.#open(info, options);
  }

  /**
   * Con `terminateOnFailure`, todo fallo previo al primer `agentReady` que no
   * sea `SandboxNotReadyError` termina el MicroVM (ese error ya decidió).
   */
  static async #open(info: SandboxInfo, options: SandboxOpenOptions): Promise<Sandbox> {
    const refresher = new TokenRefresher(
      new TokenStore(),
      (ports) => options.controlPlane.createAuthToken(info.sandboxId, ports),
      { logger: options.logger },
    );
    let sandbox: Sandbox | undefined;
    try {
      await raceAbort(refresher.mint(options.proxyPorts), options.signal);
      sandbox = new Sandbox(
        new SandboxCore({
          info,
          accessToken: options.accessToken,
          controlPlane: options.controlPlane,
          transport: options.transport,
          refresher,
          requestTimeoutMs: options.requestTimeoutMs,
          readyTimeoutMs: options.readyTimeoutMs,
          reconnectTimeoutMs: options.reconnectTimeoutMs,
          logger: options.logger,
        }),
      );
      const ready = await sandbox.#core.waitUntilReady({
        terminateOnFailure: options.terminateOnFailure,
        readiness: options.readiness,
        signal: options.signal,
      });
      sandbox.#readinessHealth = healthFromProto(ready);
      if (options.requireLifecycle === true && sandbox.#readinessHealth.lifecycle === undefined) {
        throw olderAgentError(info.templateName, sandbox.#readinessHealth.agentVersion);
      }
    } catch (error) {
      sandbox?.close();
      if (options.terminateOnFailure && !(error instanceof SandboxNotReadyError)) {
        await terminateQuietly(options.controlPlane, info.sandboxId, options.logger);
      }
      throw error;
    }
    refresher.start();
    return sandbox;
  }

  /** Valida al llamar (sin tocar AWS) y recorre perezosamente; sin opciones nuevas pide las mismas páginas que antes. */
  static list(options: SandboxListOptions = {}): AsyncIterable<SandboxListItem> {
    const request = listingRequest(options);
    return listSandboxes(listingContext(options), request);
  }

  /**
   * Un paginador reanudable (`hasNext`, `nextToken`, `nextItems()`). Valida
   * `limit`, `order`, `metadata` y el token al construirlo, antes de tocar AWS;
   * un token de otros filtros falla en el primer `nextItems()`.
   */
  static paginate(options: SandboxPaginateOptions = {}): SandboxListPaginator {
    const request = listingRequest(options);
    return new SandboxListPaginator(listingContext(options), request, options.nextToken);
  }

  /**
   * `MetricsHistory` de un sandbox `RUNNING` por su id, con el access token y
   * un transporte dedicado que se cierra al volver. Nunca despierta un sandbox
   * suspendido (`SandboxStateError`, sin acuñar JWE); uno terminado es
   * `SandboxNotFoundError`.
   */
  static async getMetricsHistory(
    sandboxId: string,
    options: StaticMetricsHistoryOptions = {},
  ): Promise<SandboxMetrics[]> {
    const id = validateSandboxId(sandboxId);
    const accessToken = requireAccessToken(options.accessToken, "getMetricsHistory(sandboxId)");
    const request = metricsHistoryRequest(options);
    const plane = resolveControlPlane(options);
    const info = await plane.getMicrovm(id);
    assertReadableWithoutWaking(info);
    return fetchMetricsHistory({
      plane,
      info,
      settings: resolveTransportSettings(options.transport),
      accessToken,
      request,
      timeoutMs: options.requestTimeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS,
    });
  }

  /**
   * `Sandbox.kill(sandboxId)` sin handle.
   *
   * Coste y activación (`tracerProvider`, M13b):
   * - Activa: spans OpenTelemetry `rayito.sandbox.kill`, como en
   *   `SandboxConnectOptions.tracerProvider`.
   * - Recursos y llamadas AWS: ninguno adicional.
   * - Coste aproximado: $0 de AWS.
   * - IAM: ninguno adicional.
   * - Cómo apagarla: no pases `tracerProvider` (por defecto `undefined`).
   * - Ejemplo:
   *   `await Sandbox.kill(sandboxId, { tracerProvider: trace.getTracerProvider() });`
   */
  static async kill(sandboxId: string, options: StaticKillOptions = {}): Promise<boolean> {
    const plane = resolveControlPlane(options);
    const validatedSandboxId = validateSandboxId(sandboxId);
    return instrumentationFor(options.tracerProvider).span(
      "rayito.sandbox.kill",
      { "rayito.sandbox.id": validatedSandboxId, "rayito.operation": "kill" },
      () => plane.terminateMicrovm(validatedSandboxId),
    );
  }

  static async getInfo(sandboxId: string, options: ControlPlaneOptions = {}): Promise<SandboxInfo> {
    return resolveControlPlane(options).getMicrovm(validateSandboxId(sandboxId));
  }

  /**
   * `Sandbox.pause(sandboxId)` sin handle.
   *
   * Coste y activación (`tracerProvider`, M13b):
   * - Activa: spans OpenTelemetry `rayito.sandbox.pause`, como en
   *   `SandboxConnectOptions.tracerProvider`.
   * - Recursos y llamadas AWS: ninguno adicional.
   * - Coste aproximado: $0 de AWS.
   * - IAM: ninguno adicional.
   * - Cómo apagarla: no pases `tracerProvider` (por defecto `undefined`).
   * - Ejemplo:
   *   `await Sandbox.pause(sandboxId, { tracerProvider: trace.getTracerProvider() });`
   */
  static async pause(sandboxId: string, options: StaticPauseOptions = {}): Promise<boolean> {
    const plane = resolveControlPlane(options);
    const validatedSandboxId = validateSandboxId(sandboxId);
    return instrumentationFor(options.tracerProvider).span(
      "rayito.sandbox.pause",
      { "rayito.sandbox.id": validatedSandboxId, "rayito.operation": "pause" },
      async () => {
        const info = await plane.getMicrovm(validatedSandboxId);
        if (alreadySuspended(info)) {
          return false;
        }
        const suspended = await plane.suspendMicrovm(sandboxId);
        if (suspended && (options.wait ?? true)) {
          await waitForState(
            plane,
            sandboxId,
            "SUSPENDED",
            options.readyTimeoutMs ?? DEFAULT_READY_TIMEOUT_MS,
          );
        }
        return suspended;
      },
    );
  }

  /**
   * `Sandbox.resume(sandboxId)` sin handle.
   *
   * Coste y activación (`tracerProvider`, M13b):
   * - Activa: spans OpenTelemetry `rayito.sandbox.resume`, como en
   *   `SandboxConnectOptions.tracerProvider`.
   * - Recursos y llamadas AWS: ninguno adicional.
   * - Coste aproximado: $0 de AWS.
   * - IAM: ninguno adicional.
   * - Cómo apagarla: no pases `tracerProvider` (por defecto `undefined`).
   * - Ejemplo:
   *   `await Sandbox.resume(sandboxId, { tracerProvider: trace.getTracerProvider() });`
   */
  static async resume(sandboxId: string, options: StaticPauseOptions = {}): Promise<void> {
    const plane = resolveControlPlane(options);
    const validatedSandboxId = validateSandboxId(sandboxId);
    return instrumentationFor(options.tracerProvider).span(
      "rayito.sandbox.resume",
      { "rayito.sandbox.id": validatedSandboxId, "rayito.operation": "resume" },
      async () => {
        await plane.resumeMicrovm(validatedSandboxId);
        if (options.wait ?? true) {
          await waitForState(
            plane,
            sandboxId,
            "RUNNING",
            options.readyTimeoutMs ?? DEFAULT_READY_TIMEOUT_MS,
          );
        }
      },
    );
  }

  /**
   * `connect` con el access token (o `RAYITO_ACCESS_TOKEN`), `updateNetwork`
   * y `close()`: nunca mata el sandbox. La política se valida antes de tocar
   * AWS.
   */
  static async updateNetwork(
    sandboxId: string,
    network: NetworkPolicyInput | undefined,
    options: StaticUpdateNetworkOptions = {},
  ): Promise<NetworkState> {
    const { allowInternetAccess, ...connectOptions } = options;
    const policy = resolveNetwork(network, { allowInternetAccess });
    validatePolicyShape(policy);
    const sandbox = await Sandbox.connect(sandboxId, connectOptions);
    try {
      return await sandbox.#sendNetworkPolicy(
        policy,
        options.requestTimeoutMs,
        UPDATE_NETWORK_FEATURE,
        options.signal,
      );
    } finally {
      sandbox.close();
    }
  }

  // -------------------------------------------------------------- properties

  get sandboxId(): string {
    return this.#core.sandboxId;
  }

  get accessToken(): string {
    return this.#core.accessToken;
  }

  get endpoint(): string {
    return this.#core.info.endpoint;
  }

  get endpointUrl(): string {
    return this.#core.info.endpointUrl;
  }

  get info(): SandboxInfo {
    return this.#core.info;
  }

  /** La `SandboxInfo` con la que se abrió el handle (`run-microvm` o `connect`), nunca refrescada. */
  get launchInfo(): SandboxInfo {
    return this.#core.launchInfo;
  }

  get region(): string {
    return this.#core.controlPlane.region;
  }

  get resumeGeneration(): number {
    return this.#core.resumeGeneration;
  }

  /** El `S3Prefix` (con `name`) enlazado por `create({ persist })` o `connect(id, { persist })`. */
  get persist(): S3Prefix | undefined {
    return this.#persist;
  }

  /** El resultado del restore automático de `create({ persist })`; `undefined` en la primera vida del `name`. */
  get lastRestore(): RestoreResult | undefined {
    return this.#lastRestore;
  }

  /** El bucket de transferencias resuelto en `create`/`connect`; `undefined` sin staging. */
  get transfer(): ResolvedS3Staging | undefined {
    return this.#core.transfer;
  }

  /** `{ nombre: GatewayStatus }` de `create({ gateways })`: vacío (y su
   * `refresh()` no hace nada) sin esa opción. `GatewayStatus.url` es
   * `"http://127.0.0.1:<puerto>"`, el host al que apuntar
   * `ANTHROPIC_BASE_URL` y similares dentro del sandbox. */
  get gateways(): GatewayHandle {
    const handle = this.#sectionHandles.get(GATEWAY_SECTION);
    return handle instanceof GatewayHandle ? handle : EMPTY_GATEWAYS;
  }

  // --------------------------------------------------------------- transfers

  /**
   * El `sandbox.uploadUrl(path, { user, useSignatureExpiration })` de E2B: la
   * URL prefirmada como `string`, con la importación ya armada. Para esperar
   * la subida o cancelarla usa `files.uploadUrl`, que devuelve el ticket.
   * `useSignatureExpiration` son segundos (3600 por defecto, `<= 0` es
   * `InvalidArgumentError`).
   */
  async uploadUrl(path: string, options: SignedUrlOptions = {}): Promise<string> {
    const ticket = await this.files.uploadUrl(path, {
      user: options.user,
      expiresIn: expiresInFromSignatureExpiration(options.useSignatureExpiration),
    });
    return ticket.url;
  }

  /** El `sandbox.downloadUrl(path, { user, useSignatureExpiration })` de E2B: la URL de una foto del fichero. */
  async downloadUrl(path: string, options: SignedUrlOptions = {}): Promise<string> {
    const link = await this.files.downloadUrl(path, {
      user: options.user,
      expiresIn: expiresInFromSignatureExpiration(options.useSignatureExpiration),
    });
    return link.url;
  }

  // --------------------------------------------------------------- lifecycle

  /** `terminate-microvm` y `close()`, también si la llamada falla. */
  async kill(): Promise<boolean> {
    return this.#instrumentation.span(
      "rayito.sandbox.kill",
      { "rayito.sandbox.id": this.sandboxId, "rayito.operation": "kill" },
      async () => {
        try {
          return await this.#core.controlPlane.terminateMicrovm(this.sandboxId);
        } finally {
          this.close();
        }
      },
    );
  }

  /**
   * `get-microvm` fresco y, si el sandbox está `RUNNING` con plazo lógico
   * gestionado (ADR-011), un `Health` (registrado) que refresca `lifecycle`:
   * `expiresAt` es el plazo vigente aunque otro cliente lo haya movido.
   * `metadata` y los hechos del guest vienen del último `Health` sin RPC
   * extra (quedan fijos en `/run`); sin plazo gestionado no toca el endpoint,
   * así que sondear `getInfo()` no impide la auto-suspensión por idle, y
   * nunca sondea un sandbox que no está `RUNNING` (la sonda lo despertaría).
   */
  async getInfo(): Promise<SandboxInfo> {
    const info = await this.#core.controlPlane.getMicrovm(this.sandboxId);
    if (deadlineMayHaveMoved(info.state, this.#core.lifecycle)) {
      await this.#refreshHealth();
    }
    this.#core.info = withLifecycle(info, this.#core.lifecycle);
    const result = sandboxInfo({
      ...this.#core.info,
      ...this.#core.guestFacts,
      metadata: this.#core.metadata,
    });
    const requestedSize = this.#launchOptions?.size;
    if (requestedSize === undefined) {
      return result;
    }
    // m15-sizes-catalog: única llamada a GetMicrovmImageVersion, cacheada
    // por versión de imagen (defaultSizeCatalog), para confirmar lo que
    // `create({ size })` ya resolvió en cliente.
    const confirmedMib = await defaultSizeCatalog.minimumMemoryMib(
      this.#core.controlPlane,
      result.template,
      result.templateVersion,
    );
    return withSizeFacts(result, { size: requestedSize.name, baselineMemoryMib: confirmedMib });
  }

  /**
   * Fija el plazo lógico en ahora + `timeoutMs` (`SetTimeout(EXACT)`,
   * ADR-011): puede alargarlo o acortarlo, y reabre un sandbox `pause` cuyo
   * plazo venció pero que aún no se suspendió. El tope es `maxLifetimeMs`
   * (fijo desde `create()`, como mucho 28 800 000): más allá,
   * `InvalidArgumentError` con el plazo intacto y `reincarnate()` como
   * salida. Sin `maxLifetimeMs`/`onTimeout` en `create()` también es
   * `InvalidArgumentError`.
   */
  async setTimeout(timeoutMs: number, options: RequestOptions = {}): Promise<void> {
    const validated = validateSetTimeoutMs(timeoutMs);
    await this.#sendSetTimeout(
      TimeoutMode.EXACT,
      validated,
      options.requestTimeoutMs,
      options.signal,
    );
  }

  /**
   * Reabre este handle: `get-microvm`, `resume-microvm` si está `SUSPENDED`
   * sin auto-resume, el sondeo de `Health` y la extensión del plazo de
   * `Sandbox.connect(id, { timeoutMs })`. Devuelve este mismo `Sandbox`.
   */
  async connect(options: InstanceConnectOptions = {}): Promise<Sandbox> {
    const { signal } = options;
    signal?.throwIfAborted();
    if (options.tracerProvider !== undefined) {
      this.#instrumentation = instrumentationFor(options.tracerProvider);
    }
    const requestedMs = optionalSetTimeoutMs(options.timeoutMs);
    await this.#secrets.rebind(options);
    return this.#instrumentation.span(
      "rayito.sandbox.connect",
      { "rayito.sandbox.id": this.sandboxId, "rayito.operation": "connect" },
      async () => {
        const info = await this.#core.controlPlane.getMicrovm(this.sandboxId, { signal });
        if (TERMINAL_STATES.has(info.state)) {
          throw terminalStateError(info);
        }
        this.#core.info = info;
        this.#core.paused = false;
        if (needsExplicitResume(info)) {
          await this.#core.controlPlane.resumeMicrovm(this.sandboxId, { signal });
          await raceAbort(this.#core.refresher.refreshAll(), signal);
        }
        await this.#core.waitUntilReady({ terminateOnFailure: false, signal });
        await this.#extendAfterReadiness(requestedMs, options.requestTimeoutMs, signal);
        return this;
      },
    );
  }

  /**
   * `false` si ya estaba `SUSPENDING|SUSPENDED`. Mientras la pausa esté
   * pendiente ningún stream en curso de este `Sandbox` sondea `Health`.
   */
  async pause(options: PauseOptions = {}): Promise<boolean> {
    return this.#instrumentation.span(
      "rayito.sandbox.pause",
      { "rayito.sandbox.id": this.sandboxId, "rayito.operation": "pause" },
      async () => {
        this.#core.info = await this.#core.controlPlane.getMicrovm(this.sandboxId);
        if (alreadySuspended(this.#core.info)) {
          return false;
        }
        const suspended = await this.#core.suspendMarkingPaused();
        if (suspended && (options.wait ?? true)) {
          this.#core.info = await waitForState(
            this.#core.controlPlane,
            this.sandboxId,
            "SUSPENDED",
            this.#core.readyTimeoutMs,
          );
        }
        return suspended;
      },
    );
  }

  /**
   * `resume-microvm` (un conflicto no es error), reacuña los JWE y, con
   * `wait`, espera a `Health`; entonces un sandbox reanudado después de su
   * plazo lógico (`resumeGrace`/`expired`) se reabre con su propio timeout,
   * como en `connect()`.
   */
  async resume(options: PauseOptions = {}): Promise<void> {
    return this.#instrumentation.span(
      "rayito.sandbox.resume",
      { "rayito.sandbox.id": this.sandboxId, "rayito.operation": "resume" },
      async () => {
        this.#core.paused = false;
        await this.#core.controlPlane.resumeMicrovm(this.sandboxId);
        await this.#core.refresher.refreshAll();
        if (options.wait ?? true) {
          await this.#core.waitUntilReady({ terminateOnFailure: false });
          await this.#extendAfterReadiness(undefined, undefined);
        }
      },
    );
  }

  /** Un `Health` acotado por `requestTimeoutMs` (y por el tope del sondeo de readiness). */
  async isRunning(options: RequestOptions = {}): Promise<boolean> {
    const response = await this.#core.probeHealth(
      Math.min(
        ReadinessPoll.maxRpcTimeoutMs,
        this.#core.resolveRequestTimeout(options.requestTimeoutMs),
      ),
      options.signal,
    );
    return response?.agentReady ?? false;
  }

  /**
   * El JWE del proxy que el `TokenStore` tiene para `port` (8080, el de
   * `rayd`, por defecto), sin acuñar nada; `undefined` si no hay. Es una
   * credencial al portador para el endpoint, válida 60 min como mucho.
   */
  currentProxyToken(port: number = DEFAULT_PORT): string | undefined {
    return this.#core.refresher.store.jweFor(port);
  }

  async getHealth(options: RequestOptions = {}): Promise<SandboxHealth> {
    const timeoutMs = this.#core.resolveRequestTimeout(options.requestTimeoutMs);
    const response = await this.#core.translatedUnary(
      () =>
        this.#core.clients.health.health(
          create(HealthRequestSchema, {}),
          callOptions(timeoutMs, options.signal),
        ),
      { signal: options.signal },
    );
    this.#core.recordHealth(response);
    return healthFromProto(response);
  }

  /**
   * Sondea `ConfigureStatus` cada `CONFIGURE_SETTLE_POLL_MS` hasta que
   * ninguna sección de `pending` siga pendiente; agotado el mayor
   * `settleTimeoutMs`, la última lectura es `final` y cada sección que siga
   * sin asentarse lanza su propio error de timeout.
   */
  async #waitSettled(pending: ConfigureSection[], timeoutMs: number): Promise<void> {
    const deadline = Date.now() + settleTimeoutMs(pending);
    let remaining = pending;
    while (remaining.length > 0) {
      const final = Date.now() >= deadline;
      remaining = stillPending(await this.#configureStatus(timeoutMs), remaining, final);
      if (remaining.length > 0) {
        await new Promise((resolve) => setTimeout(resolve, CONFIGURE_SETTLE_POLL_MS));
      }
    }
  }

  /**
   * Estado en vivo de cada `mounts` (`m15-s3-mounts`): una `ConfigureStatus`
   * por lectura, nunca cacheada — un montaje puede pasar de `"pending"` a
   * `"mounted"`/`"failed"` entre dos lecturas de este método. Vacío si
   * `create()` no recibió `mounts`.
   */
  async mounts(): Promise<ReadonlyMap<string, MountStatus>> {
    const response = await this.#configureStatus(this.#core.resolveRequestTimeout(undefined));
    return fromProtoStatus(response.s3Mounts ?? create(S3MountsStatusSchema, {}));
  }

  /** Un `HostAccess` con el JWE que cubre `port` (acuñado si hace falta); 9000 está prohibido (ADR-006). */
  async getHost(port: number): Promise<HostAccess> {
    const validated = validateHostPort(port);
    await this.#core.refresher.ensure(validated);
    return new HostAccess(this.endpoint, validated, () => this.#core.currentJwe(validated));
  }

  async getMetrics(options: RequestOptions = {}): Promise<SandboxMetrics> {
    const timeoutMs = this.#core.resolveRequestTimeout(options.requestTimeoutMs);
    const response = await this.#core.translatedUnary(
      () =>
        this.#core.clients.health.metrics(
          create(MetricsRequestSchema, {}),
          callOptions(timeoutMs, options.signal),
        ),
      { signal: options.signal },
    );
    return metricsFromProto(response);
  }

  /**
   * La serie que `rayd` muestrea cada 5 s desde `/run` (8 h como mucho), en
   * orden ascendente, con un hueco mientras estuvo suspendido. Una imagen
   * anterior a 0.3.0 es `UnimplementedError`.
   */
  async getMetricsHistory(options: MetricsHistoryOptions = {}): Promise<SandboxMetrics[]> {
    const request = metricsHistoryRequest(options);
    const timeoutMs = this.#core.resolveRequestTimeout(options.requestTimeoutMs);
    const response = await this.#core.signalledUnary(
      () =>
        this.#core.clients.health.metricsHistory(request, callOptions(timeoutMs, options.signal)),
      options.signal,
      historyErrorTranslator(HISTORY_FEATURE),
    );
    return metricsHistoryFromProto(response);
  }

  /**
   * Lo que `connect()` y `resume()` mandan tras la readiness, bien dentro de
   * los 30 s de gracia de un sandbox reanudado después de su plazo.
   */
  async #extendAfterReadiness(
    requestedMs: number | undefined,
    requestTimeoutMs: number | undefined,
    signal?: AbortSignal,
  ): Promise<void> {
    const timeoutMs = connectExtension(this.#core.lifecycle, requestedMs, Date.now());
    if (timeoutMs !== undefined) {
      await this.#sendSetTimeout(TimeoutMode.AT_LEAST, timeoutMs, requestTimeoutMs, signal);
    }
  }

  /** El `LifecycleState` que devuelve `SetTimeout` se registra y rearma el disparador del modo `pause`. */
  async #sendSetTimeout(
    mode: TimeoutMode,
    timeoutMs: number,
    requestTimeoutMs: number | undefined,
    signal?: AbortSignal,
  ): Promise<void> {
    const deadlineMs = this.#core.resolveRequestTimeout(requestTimeoutMs);
    const state = await this.#core.signalledUnary(
      () =>
        this.#core.clients.lifecycle.setTimeout(
          setTimeoutRequest(mode, timeoutMs),
          callOptions(deadlineMs, signal),
        ),
      signal,
      (error) => translateSetTimeoutError(error, timeoutMs, setTimeoutFeature(mode)),
    );
    this.#core.recordLifecycle(lifecycleFromProto(state));
  }

  async #refreshHealth(): Promise<void> {
    const response = await this.#core.probeHealth(
      Math.min(ReadinessPoll.maxRpcTimeoutMs, this.#core.requestTimeoutMs),
    );
    if (response !== undefined) {
      this.#core.recordHealth(response);
    }
  }

  // ----------------------------------------------------------------- network

  /**
   * `NetworkService.UpdateNetwork`: sustituye la política entera (lo omitido
   * se borra; sin argumentos, sin restricciones) y afecta a las conexiones
   * nuevas. `UnimplementedError` en una imagen sin `CAP_NET_ADMIN` o anterior
   * a 0.3.0; `InvalidArgumentError` si `rayd` rechaza una entrada.
   */
  async updateNetwork(
    network?: NetworkPolicyInput,
    options: UpdateNetworkOptions = {},
  ): Promise<NetworkState> {
    const policy = resolveNetwork(network, { allowInternetAccess: options.allowInternetAccess });
    validatePolicyShape(policy);
    return this.#sendNetworkPolicy(
      policy,
      options.requestTimeoutMs,
      UPDATE_NETWORK_FEATURE,
      options.signal,
    );
  }

  /** `NetworkService.GetNetwork`: nunca devuelve la dirección ni las credenciales del proxy. */
  async getNetwork(options: RequestOptions = {}): Promise<NetworkState> {
    const timeoutMs = this.#core.resolveRequestTimeout(options.requestTimeoutMs);
    const client = this.#core.clientFor(NetworkService, false);
    const response = await this.#core.signalledUnary(
      () =>
        client.getNetwork(
          create(GetNetworkRequestSchema, {}),
          callOptions(timeoutMs, options.signal),
        ),
      options.signal,
      (error) => networkRpcError(error, GET_NETWORK_FEATURE),
    );
    return stateFromProto(response);
  }

  async #sendNetworkPolicy(
    policy: ResolvedNetworkPolicy,
    requestTimeoutMs: number | undefined,
    feature = UPDATE_NETWORK_FEATURE,
    signal?: AbortSignal,
  ): Promise<NetworkState> {
    signal?.throwIfAborted();
    logAllowOnlyNotice(policy, this.#core.logger);
    const timeoutMs = this.#core.resolveRequestTimeout(requestTimeoutMs);
    const client = this.#core.clientFor(NetworkService, false);
    const response = await this.#core.signalledUnary(
      () => client.updateNetwork(updateNetworkRequest(policy), callOptions(timeoutMs, signal)),
      signal,
      (error) => networkRpcError(error, feature),
    );
    return stateFromProto(response);
  }

  /**
   * Pasos 5a–5b de la puerta de `create()`: el `Health` de readiness debe
   * decir que el guest aplica la política y `UpdateNetwork` debe confirmarla.
   * Cualquier fallo cierra el cliente y termina el MicroVM aunque haya
   * `keepOnFailure`: un sandbox sin la política pedida nunca queda vivo.
   */
  async #applyInitialNetwork(policy: ResolvedNetworkPolicy, feature: string): Promise<void> {
    try {
      this.#throwUnlessEnforced(
        this.#readinessHealth?.egressEnforcement ?? EgressEnforcement.UNSPECIFIED,
        feature,
      );
      const state = await this.#sendNetworkPolicy(policy, undefined, feature);
      this.#throwUnlessEnforced(state.enforcement, feature);
    } catch (error) {
      this.close();
      await terminateQuietly(this.#core.controlPlane, this.sandboxId, this.#core.logger);
      throw error;
    }
  }

  /**
   * Único punto de `create()`/`take()` que llama a `ConfigureSandbox`:
   * agrupa en una sola llamada las secciones 0.6 que `planFeatures` dejó en
   * `FeaturePlan.configureSections` (`mounts`, `gateways`; una función
   * futura añade su propia entrada ahí, nunca toca este método).
   *
   * Igual que `#applyInitialNetwork`: un fallo en cualquier punto (imagen
   * anterior a 0.6.0, flag no soportado, un secreto que falta al resolver
   * una cabecera, una sección `INVALID`/`FAILED`, un montaje que no se
   * asienta) cierra el cliente y, salvo `keepOnFailure`, termina el VM
   * antes de relanzar — el caller nunca recibe un handle de un sandbox
   * cuya configuración pedida no se aplicó.
   */
  async #applyConfigureSections(
    planned: readonly PlannedSection[],
    timeoutMs: number,
    terminateOnFailure: boolean,
  ): Promise<void> {
    if (planned.length === 0) {
      return;
    }
    try {
      await this.#sendConfigureSections(planned, timeoutMs);
    } catch (error) {
      this.close();
      if (terminateOnFailure) {
        await terminateQuietly(this.#core.controlPlane, this.sandboxId, this.#core.logger);
      }
      throw error;
    }
  }

  /**
   * La parte sin compuerta de `#applyConfigureSections`: resuelve cada
   * entrada con la `SecretCache` del handle (`resolveSections`, sólo si
   * alguna la necesita), exige su flag en `Health.features`
   * (`requireCapabilities`) antes de construir el `ConfigureRequest`, la
   * aplica (`#configureSections`) y, si alguna sección es una
   * `PostApplySection`, pide `ConfigureStatus` una vez y guarda lo que
   * devuelva su `afterApply` en `#sectionHandles`.
   */
  async #sendConfigureSections(
    planned: readonly PlannedSection[],
    timeoutMs: number,
  ): Promise<void> {
    const features = requireConfigureSupport(this.#core.agentFeatures, CONFIGURE_FEATURE);
    const sections = resolveSections(planned, () => this.#sectionSecretCache());
    requireCapabilities(sections, features);
    await this.#configureSections(sections, timeoutMs);
    const postApply = sections.filter(isPostApplySection);
    if (postApply.length === 0) {
      return;
    }
    const status = await this.#configureStatus(timeoutMs);
    for (const section of postApply) {
      this.#sectionHandles.set(
        section.section,
        section.afterApply({ status, reapply: () => this.#reapplySection(section, timeoutMs) }),
      );
    }
  }

  /**
   * Una `Configure` con `sections`, cada `SectionResult` traducido al error
   * de su propia sección (`checkConfigureResponse`) y, para las que el
   * agente dejó en `PENDING`, la espera acotada a que se asienten
   * (`#waitSettled`): nunca vuelve con un montaje todavía sin montar. Lo
   * comparten `create()`/`take()` y `#reapplySection`.
   */
  async #configureSections(
    sections: readonly ConfigureSection[],
    timeoutMs: number,
  ): Promise<void> {
    const request = await buildConfigureRequest(sections);
    const response = await this.#configure(request, timeoutMs);
    await this.#waitSettled(checkConfigureResponse(response, sections), timeoutMs);
  }

  /** La `SecretCache` con la que se construye cada `ConfigureSectionFactory`:
   * la de `secrets` si el handle la tiene, la compartida del proceso si no
   * (la misma regla que `secrets`, nunca una caché aparte). */
  #sectionSecretCache(): SecretCache {
    return (
      this.#secrets.binding?.cache ??
      sharedSecretCache(
        this.#core.controlPlane.region,
        awsClientSettingsOf(this.#core.controlPlane).credentials,
      )
    );
  }

  /**
   * El `SectionApplied.reapply` de cada `PostApplySection` (p. ej.
   * `sbx.gateways.refresh()`): vuelve a mandar sólo esa sección por el
   * mismo camino que `create()` (`#configureSections`) — una sección
   * `INVALID` (un valor rotado con CR/LF) o `FAILED` (`listen_failed`)
   * lanza aquí, nunca devuelve un estado vacío o a medio aplicar en
   * silencio. Sólo si se aplicó pide el `ConfigureStatus` nuevo.
   */
  async #reapplySection(
    section: ConfigureSection,
    timeoutMs: number,
  ): Promise<ConfigureStatusResponse> {
    await this.#configureSections([section], timeoutMs);
    return this.#configureStatus(timeoutMs);
  }

  #configure(request: ConfigureRequest, timeoutMs: number): Promise<ConfigureResponse> {
    return this.#core.translatedUnary(() =>
      this.#core.clients.configure.configure(request, callOptions(timeoutMs, undefined)),
    );
  }

  #configureStatus(timeoutMs: number): Promise<ConfigureStatusResponse> {
    return this.#core.translatedUnary(() =>
      this.#core.clients.configure.configureStatus(
        create(ConfigureStatusRequestSchema, {}),
        callOptions(timeoutMs, undefined),
      ),
    );
  }

  #throwUnlessEnforced(enforcement: EgressEnforcement, feature: string): void {
    const gate = egressGateError(this.sandboxId, enforcement, feature);
    if (gate !== undefined) {
      throw gate;
    }
  }

  // ------------------------------------------------------------ persistence

  /**
   * `FilesystemService.Checkpoint`: `rayd` empaqueta el `HOME` (tar.gz, sin
   * `.cache`, `__pycache__`, `.ipynb_checkpoints` ni el runtime de Jupyter) y
   * lo sube como root con el execution role a `home.tar.gz` + `manifest.json`
   * bajo `target` (por defecto `persist`). Un solo checkpoint o restore a la
   * vez por sandbox (`PersistenceError` `failed_precondition`).
   */
  checkpointFiles(options: CheckpointFilesOptions = {}): Promise<CheckpointResult> {
    return this.#persistence.checkpoint(this.#persist, options);
  }

  /**
   * `FilesystemService.Restore`: extrae el checkpoint de `source` (por defecto
   * `persist`) sobre el `HOME`; `NotFoundError` si no hay ninguno. Un fallo a
   * mitad deja el `HOME` parcialmente restaurado: `kill()` + `create({ persist })`.
   */
  restoreFiles(options: RestoreFilesOptions = {}): Promise<RestoreResult> {
    return this.#persistence.restore(this.#persist, options);
  }

  /**
   * La respuesta a lo que `setTimeout` no puede dar, pasar de `maxLifetimeMs`
   * (ADR-011): `checkpointFiles()` → `create({ persist })` con las mismas
   * opciones (que restaura, incluidos `maxLifetimeMs` y `onTimeout`) →
   * `kill()` de este sandbox. El nuevo tiene un tope fresco, otro `sandboxId`
   * y otro token salvo que el original fuera explícito (con `index` en el
   * `create()`, el nuevo escribe su propia fila en el mismo índice); kernels, procesos y
   * PTY no sobreviven (ADR-007). Si el
   * `create()` falla, este sandbox sigue vivo y se relanza el mismo error
   * (con sus campos tipados: `code`, `state`...) con la `uri` del checkpoint
   * completo añadida a su `message`.
   */
  async reincarnate(options: ReincarnateOptions = {}): Promise<Sandbox> {
    const launch = this.#launchOptions;
    const context = this.#launchContext;
    if (launch === undefined || context === undefined) {
      throw reincarnateRequiresCreateError();
    }
    const persist = this.#persist;
    if (persist === undefined) {
      throw reincarnateRequiresPersistError();
    }
    const persistTimeoutMs = validatePersistTimeoutMs(options.persistTimeoutMs);
    await this.checkpointFiles({ exclude: options.exclude, timeoutMs: persistTimeoutMs });
    let successor: Sandbox;
    try {
      successor = await Sandbox.create({
        ...launch,
        controlPlane: context.controlPlane,
        transport: context.transport,
        logger: context.logger,
        index: context.index,
        persist,
        persistTimeoutMs,
        transfer: this.transfer ?? null,
        secrets: this.#secrets.binding?.refs.size ? this.#secrets.binding.toRecord() : undefined,
        secretCache: this.#secrets.binding?.cache,
      });
    } catch (error) {
      throw withReincarnateNote(error, persist.uri);
    }
    try {
      await this.kill();
    } catch (error) {
      if (!(error instanceof SandboxNotFoundError)) {
        throw error;
      }
      this.#core.logger?.info?.("el sandbox ya no existía al reencarnar", {
        sandboxId: this.sandboxId,
      });
    }
    return successor;
  }

  /** Reglas 2 y 3 de D10: enlaza el prefijo y, con `name`, restaura lo que haya. */
  async #bindAndRestore(
    persist: S3Prefix,
    persistTimeoutMs: number,
    terminateOnFailure: boolean,
  ): Promise<void> {
    this.#persist = bindPersist(persist, this.sandboxId);
    if (!shouldAutoRestore(persist)) {
      return;
    }
    try {
      this.#lastRestore = await this.restoreFiles({ timeoutMs: persistTimeoutMs });
    } catch (error) {
      if (error instanceof NotFoundError) {
        this.#core.logger?.info?.("sin checkpoint bajo el prefijo", {
          sandboxId: this.sandboxId,
          uri: this.#persist.uri,
        });
        this.#lastRestore = undefined;
        return;
      }
      this.close();
      if (terminateOnFailure) {
        await terminateQuietly(this.#core.controlPlane, this.sandboxId, this.#core.logger);
      }
      throw error;
    }
  }

  /**
   * Cierra las dos sesiones HTTP/2, los watches, los streams y el disparador
   * del modo `pause` sin tocar el VM; idempotente.
   */
  close(): void {
    this.#core.close();
  }

  async [Symbol.asyncDispose](): Promise<void> {
    await this.kill();
  }

  // -------------------------------------------------------------------- code

  runCode(code: string, options: RunCodeOptions = {}): Promise<Execution> {
    return this.#code.runCode(code, options);
  }

  createCodeContext(options: CreateContextOptions = {}): Promise<CodeContext> {
    return this.#code.createContext(options);
  }

  listCodeContexts(options: RequestOptions = {}): Promise<CodeContext[]> {
    return this.#code.listContexts(options);
  }

  removeCodeContext(context: ContextLike, options: RequestOptions = {}): Promise<void> {
    return this.#code.removeContext(context, options);
  }

  restartCodeContext(context: ContextLike, options: RequestOptions = {}): Promise<void> {
    return this.#code.restartContext(context, options);
  }

  toString(): string {
    return `Sandbox(${this.sandboxId}, ${this.#core.info.state})`;
  }

  /** Acceso interno para los tests del paquete; no forma parte de la API pública. */
  static coreOf(sandbox: Sandbox): SandboxCore {
    return sandbox.#core;
  }
}
