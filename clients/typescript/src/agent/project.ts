/**
 * `AgentProject`: la descripción declarativa y validada de un agente
 * (ADR-028, `ai-agent-project`). Espejo de `rayito._agent._project_domain`:
 * un valor puro; construirlo, cargarlo (`load`, `fromObject`) y escribirlo
 * (`toObject`) no llaman a AWS, no crean clientes y no leen más que, vía el
 * lector que se inyecta, el fichero de instrucciones. Las operaciones que
 * cuestan (`build`, `run`, `deploy`) viven en los servicios que lo usan.
 *
 * El proyecto contiene el `AgentSpec` existente en vez de copiar sus campos,
 * y las pasarelas del modelo por nombre. Ningún campo lleva un secreto: una
 * pasarela nombra el secreto de Secrets Manager, nunca su valor.
 *
 * El documento es el de `rayito-agent.toml` / `rayito-agent.json` (esquema
 * `rayito.agent-project/1`, `testdata/agent-project/schema.json`): una tabla
 * anidada. `fromObject` rechaza toda clave desconocida con su ruta punteada y
 * un `code` de la lista cerrada `AGENT_PROJECT_ERROR_CODES`. En el fichero
 * los tiempos van en segundos; aquí se convierten a milisegundos.
 */

import { isAbsolute, relative, resolve, sep } from "node:path";
import { InvalidArgumentError } from "../errors.js";
import {
  AGENT_MIN_MEMORY_MIB,
  AGENT_NODE_BUILD_VERSIONS,
  AGENT_PROJECT_SCHEMA,
  AGENT_PYTHON_BUILD_VERSIONS,
  AGENT_SECRET_ENV_MAX_SECRETS,
  AGENT_SERVICE_MAX_CONCURRENCY,
  AGENT_SERVICE_PORT,
  AGENT_SERVICE_QUEUE_MAX,
  AGENT_SERVICE_QUEUE_MAX_DEFAULT,
  AGENT_SERVICE_QUEUE_TIMEOUT_SECONDS_DEFAULT,
  AGENT_SERVICE_QUEUE_TIMEOUT_SECONDS_MAX,
  DEFAULT_AGENT_DEPLOY_TIMEOUT_SECONDS,
  DEFAULT_PORT,
  EGRESS_HOSTNAME_MAX_CHARS,
  EGRESS_MAX_ENTRIES_PER_LIST,
  HOOKS_PORT,
  MAX_DURATION_SECONDS,
  PORT_MAX,
  PORT_MIN,
} from "../limits.js";
import { type IdlePolicy, validateIdlePolicy } from "../models.js";
import { SecretGateway } from "../secret-gateway/domain.js";
import type { SecretLike } from "../secrets/names.js";
import type { BuildInfo } from "../templates/build.js";
import { validateBucketHint, validateImageVersion } from "./deploy-domain.js";
import type { AgentDeployment } from "./deployment.js";
import type { DetectionReport, DetectOverrides } from "./detect-report.js";
import { DETECTION_RULES } from "./detect-rules.js";
import {
  AgentLimits,
  AgentModel,
  AgentPermissions,
  AgentSpec,
  McpLocal,
  McpRemote,
  type McpServer,
  MODEL_ENV_FIELDS,
  MODEL_ENV_KEYS,
  type ModelProvider,
  SubAgent,
  validateName,
} from "./domain.js";
import type { AgentResult } from "./events.js";
import type { AgentExport, AgentExportOptions } from "./export.js";
import {
  anthropicGateway,
  azureOpenaiGateway,
  bedrockGateway,
  deepseekGateway,
  geminiGateway,
  groqGateway,
  litellmGateway,
  mistralGateway,
  openaiCompatibleGateway,
  openaiGateway,
  openrouterGateway,
  xaiGateway,
} from "./gateways.js";
import type { AgentProjectDeployOptions, AgentProjectRunOptions } from "./project-service.js";
import type { AgentRuntime } from "./runtime.js";
import {
  RUNTIME_KINDS,
  RUNTIME_OPTION_KEYS,
  resolveRuntime,
  runtimeFromOptions,
  runtimeToOptions,
} from "./runtimes.js";
import {
  maxSessionsForMemory,
  ScalingOptions,
  type ScalingOptionsInput,
} from "./scaling-domain.js";
import {
  hasReservedPrefix,
  isReservedName,
  SECRET_ENV_KEY_PATTERN,
  SECRET_ENV_NAME_PATTERN,
  SECRET_ID_PATTERN,
  type SecretEnvMap,
  SecretEnvRef,
  SecretEnvSource,
  type SecretEnvValue,
  secretIds,
} from "./secret-env.js";
import { type AgentTemplateBuildOptions, DEFAULT_AGENT_TEMPLATE_BASE } from "./template.js";

/** `[runtime] graph`: el nombre de un grafo de `langgraph.json`, sólo con
 * `kind = "langgraph"` (`testdata/agent-project/schema.json`). Se resuelve al
 * `entrypoint` de ese grafo antes de construir el runtime. */
export const GRAPH_KEY = "graph";
export const GRAPH_RUNTIME_KIND = "langgraph";
export const GRAPH_NAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;
export const ENTRYPOINT_KEY = "entrypoint";
export const LANGGRAPH_FILE = (DETECTION_RULES.entryFiles[GRAPH_RUNTIME_KIND] as { file: string })
  .file;

/** Los códigos de error de lectura de un proyecto: lista cerrada que los dos
 * SDK comparten (`testdata/agent-project/invalid/cases.json`). */
export const AGENT_PROJECT_ERROR_CODES = [
  "unknown_key",
  "missing_key",
  "invalid_type",
  "invalid_value",
  "path_escape",
  "conflict",
  "secret_value",
  "unsupported_schema",
  "file_not_found",
  "ambiguous",
  "not_detected",
  "unsupported",
] as const;
export type AgentProjectErrorCode = (typeof AGENT_PROJECT_ERROR_CODES)[number];
/** Claves que, de aparecer sin estar declaradas, intentan meter un secreto en
 * el fichero: el error es `secret_value` y no `unknown_key`. */
export const SECRET_LIKE_KEYS = [
  "access_key",
  "api_key",
  "apikey",
  "key",
  "password",
  "secret_access_key",
  "secret_value",
  "token",
] as const;
/** Prefijos de credenciales conocidas: un nombre de secreto que empieza así
 * es casi seguro el valor pegado en lugar del nombre. */
export const SECRET_VALUE_PREFIXES = [
  "AIza",
  "AKIA",
  "ASIA",
  "ABSK",
  "Bearer",
  "ghp_",
  "gho_",
  "sk-",
  "xoxb-",
] as const;
/** Pasarela de `[gateways.<nombre>] preset = "custom"`. */
export const CUSTOM_GATEWAY_PRESET = "custom";
/** Milisegundos por segundo: el fichero habla en segundos. */
const MS_PER_SECOND = 1000;
const RATE_KEY = "rate_per_minute";
const MODELS_KEY = "models";
const DEFAULT_SERVE_TIMEOUT_MS = DEFAULT_AGENT_DEPLOY_TIMEOUT_SECONDS * MS_PER_SECOND;
/** Plazo por defecto de la cola del servicio (y de la del cliente de una flota). */
export const DEFAULT_QUEUE_TIMEOUT_MS = AGENT_SERVICE_QUEUE_TIMEOUT_SECONDS_DEFAULT * MS_PER_SECOND;
const MAX_QUEUE_TIMEOUT_MS = AGENT_SERVICE_QUEUE_TIMEOUT_SECONDS_MAX * MS_PER_SECOND;
const ENV_NAME_PATTERN = /^[A-Za-z_][A-Za-z0-9_]*$/;

/**
 * Un documento de proyecto inválido. `path` es la ruta punteada de la clave
 * culpable (`runtime.entrpoint`) y `code` uno de `AGENT_PROJECT_ERROR_CODES`.
 * Es un `InvalidArgumentError`: quien ya capturaba esa clase no necesita
 * cambiar.
 */
export class AgentProjectError extends InvalidArgumentError {
  readonly path: string;
  override readonly code: AgentProjectErrorCode;

  constructor(
    message: string,
    options: { readonly path: string; readonly code: AgentProjectErrorCode },
  ) {
    super(message);
    this.path = options.path;
    this.code = options.code;
  }
}

function fail(path: string, code: AgentProjectErrorCode, detail: string): AgentProjectError {
  return new AgentProjectError(`${path}: ${detail}`, { path, code });
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value);
}

export interface ServeOptionsInput {
  readonly port?: number | undefined;
  readonly timeoutMs?: number | undefined;
  /** `undefined` es la política por defecto; `null` la quita. */
  readonly idle?: IdlePolicy | null | undefined;
  readonly maxConcurrency?: number | undefined;
  readonly metadata?: Readonly<Record<string, string>> | undefined;
  readonly queueMax?: number | undefined;
  readonly queueTimeoutMs?: number | undefined;
}

/**
 * Cómo se sirve el agente (`AgentProject.deploy()`): el `port` del servicio
 * (no 8080 ni 9000, que son de `rayd`), la vida del sandbox (`timeoutMs`,
 * como mucho el máximo de la plataforma), la política de `idle` (suspensión y
 * reanudación automáticas; `null` la quita), la concurrencia por VM
 * (`maxConcurrency`, acotada por las conexiones que admite el MicroVM de
 * `AgentProject.memoryMib`), las etiquetas `metadata` del sandbox y la cola
 * del host: `queueMax` llamadas esperan hasta `queueTimeoutMs` cuando el VM
 * está lleno (con `queueMax: 0`, el valor por defecto, la llamada que no cabe
 * recibe 429 enseguida).
 */
export class ServeOptions {
  readonly port: number;
  readonly timeoutMs: number;
  readonly idle: IdlePolicy | null | undefined;
  readonly maxConcurrency: number;
  readonly metadata: Readonly<Record<string, string>>;
  readonly queueMax: number;
  readonly queueTimeoutMs: number;

  constructor(options: ServeOptionsInput = {}) {
    const port = options.port ?? AGENT_SERVICE_PORT;
    if (!isInteger(port)) {
      throw new InvalidArgumentError("ServeOptions.port debe ser un entero");
    }
    if (port < PORT_MIN || port > PORT_MAX || port === DEFAULT_PORT || port === HOOKS_PORT) {
      throw new InvalidArgumentError(
        `ServeOptions.port debe estar en ${PORT_MIN}..${PORT_MAX} y no ser ${DEFAULT_PORT} ni ${HOOKS_PORT}, que usa rayd`,
      );
    }
    const timeoutMs = options.timeoutMs ?? DEFAULT_SERVE_TIMEOUT_MS;
    if (
      typeof timeoutMs !== "number" ||
      !(timeoutMs > 0) ||
      timeoutMs > MAX_DURATION_SECONDS * MS_PER_SECOND
    ) {
      throw new InvalidArgumentError(
        `ServeOptions.timeoutMs debe estar en (0, ${MAX_DURATION_SECONDS * MS_PER_SECOND}]`,
      );
    }
    const maxConcurrency = options.maxConcurrency ?? AGENT_SERVICE_MAX_CONCURRENCY;
    if (!isInteger(maxConcurrency) || maxConcurrency < 1) {
      throw new InvalidArgumentError("ServeOptions.maxConcurrency debe ser un entero >= 1");
    }
    const metadata = options.metadata ?? {};
    if (
      !isRecord(metadata) ||
      !Object.values(metadata).every((value) => typeof value === "string")
    ) {
      throw new InvalidArgumentError("ServeOptions.metadata debe mapear cadenas a cadenas");
    }
    this.port = port;
    this.timeoutMs = timeoutMs;
    this.idle = options.idle === null ? null : options.idle;
    this.maxConcurrency = maxConcurrency;
    this.metadata = Object.freeze({ ...metadata });
    const queueMax = options.queueMax ?? AGENT_SERVICE_QUEUE_MAX_DEFAULT;
    if (!isInteger(queueMax) || queueMax < 0 || queueMax > AGENT_SERVICE_QUEUE_MAX) {
      throw new InvalidArgumentError(
        `ServeOptions.queueMax debe ser un entero en 0..${AGENT_SERVICE_QUEUE_MAX}`,
      );
    }
    const queueTimeoutMs = options.queueTimeoutMs ?? DEFAULT_QUEUE_TIMEOUT_MS;
    if (
      typeof queueTimeoutMs !== "number" ||
      !(queueTimeoutMs > 0) ||
      queueTimeoutMs > MAX_QUEUE_TIMEOUT_MS
    ) {
      throw new InvalidArgumentError(
        `ServeOptions.queueTimeoutMs debe estar en (0, ${MAX_QUEUE_TIMEOUT_MS}]`,
      );
    }
    this.queueMax = queueMax;
    this.queueTimeoutMs = queueTimeoutMs;
  }
}

/** Los lenguajes de un proyecto de agente. */
export const AGENT_LANGUAGES = ["python", "typescript"] as const;
export type AgentLanguage = (typeof AGENT_LANGUAGES)[number];

export interface RuntimeVersionsInput {
  readonly python?: string | undefined;
  readonly node?: string | undefined;
}

function supportedVersion(
  value: unknown,
  builds: Readonly<Record<string, string>>,
  label: string,
): string | undefined {
  if (value !== undefined && (typeof value !== "string" || !(value in builds))) {
    throw new InvalidArgumentError(
      `${label} debe ser una de ${Object.keys(builds).sort().join(", ")}`,
    );
  }
  return value as string | undefined;
}

/**
 * Las versiones de intérprete del agente: `python` (la menor, `"3.12"`) y
 * `node` (la mayor, `"22"`) entre las compilaciones fijadas de `limits.json`.
 * `undefined` es la de por defecto: la del intérprete de la imagen base (sin
 * descarga) y la que exige Strands para TypeScript.
 */
export class RuntimeVersions {
  readonly python: string | undefined;
  readonly node: string | undefined;

  constructor(options: RuntimeVersionsInput = {}) {
    if (typeof options !== "object" || options === null) {
      throw new InvalidArgumentError("RuntimeVersions necesita un objeto de opciones");
    }
    this.python = supportedVersion(
      options.python,
      AGENT_PYTHON_BUILD_VERSIONS,
      "RuntimeVersions.python",
    );
    this.node = supportedVersion(options.node, AGENT_NODE_BUILD_VERSIONS, "RuntimeVersions.node");
  }

  get isDefault(): boolean {
    return this.python === undefined && this.node === undefined;
  }
}

/** La política de egress `allowOut` / `denyOut` del sandbox del agente (necesita
 * `rayito-base-caps`); sin `egressProxy`, porque sus credenciales no van en el
 * proyecto. */
export interface AgentNetwork {
  readonly allowOut: readonly string[];
  readonly denyOut: readonly string[];
}

export interface AgentNetworkInput {
  readonly allowOut?: readonly string[] | undefined;
  readonly denyOut?: readonly string[] | undefined;
}

function concurrencyProblem(serve: ServeOptions, memoryMib: number): string | undefined {
  const cap = maxSessionsForMemory(memoryMib);
  if (serve.maxConcurrency <= cap) {
    return undefined;
  }
  return `admite como mucho ${cap} con memoryMib=${memoryMib} (conexiones del MicroVM menos las reservadas); sube la memoria o baja la concurrencia`;
}

function secretLikeEnvName(name: string): boolean {
  const lowered = name.toLowerCase();
  return (SECRET_LIKE_KEYS as readonly string[]).some(
    (word) => lowered === word || lowered.endsWith(`_${word}`),
  );
}

/** `AgentProject.env`: nombres de variable válidos, valores de texto y nada que
 * parezca un secreto (un secreto va por una pasarela). */
function validatedEnv(env: unknown): Readonly<Record<string, string>> {
  if (!isRecord(env)) {
    throw new InvalidArgumentError("AgentProject.env debe mapear cadenas a cadenas");
  }
  for (const [name, value] of Object.entries(env)) {
    if (!ENV_NAME_PATTERN.test(name)) {
      throw new InvalidArgumentError(
        "AgentProject.env: cada nombre debe ser un identificador de variable de entorno",
      );
    }
    if (typeof value !== "string") {
      throw new InvalidArgumentError("AgentProject.env debe mapear cadenas a cadenas");
    }
    if (
      secretLikeEnvName(name) ||
      SECRET_VALUE_PREFIXES.some((prefix) => value.startsWith(prefix))
    ) {
      throw new InvalidArgumentError(
        "AgentProject.env no puede llevar secretos: usa una pasarela para las credenciales",
      );
    }
  }
  return env as Readonly<Record<string, string>>;
}

function validatedNetwork(network: AgentNetworkInput | undefined): AgentNetwork | undefined {
  if (network === undefined) {
    return undefined;
  }
  if (typeof network !== "object" || network === null || Array.isArray(network)) {
    throw new InvalidArgumentError("AgentProject.network debe ser un objeto { allowOut, denyOut }");
  }
  const allowOut = network.allowOut ?? [];
  const denyOut = network.denyOut ?? [];
  for (const [label, entries] of [
    ["allowOut", allowOut],
    ["denyOut", denyOut],
  ] as const) {
    if (
      !Array.isArray(entries) ||
      entries.length > EGRESS_MAX_ENTRIES_PER_LIST ||
      !entries.every(
        (entry) =>
          typeof entry === "string" &&
          entry.length > 0 &&
          entry.length <= EGRESS_HOSTNAME_MAX_CHARS,
      )
    ) {
      throw new InvalidArgumentError(
        `AgentProject.network.${label} admite como mucho ${EGRESS_MAX_ENTRIES_PER_LIST} cadenas no vacías`,
      );
    }
  }
  return Object.freeze({
    allowOut: Object.freeze([...allowOut]),
    denyOut: Object.freeze([...denyOut]),
  });
}

export interface AgentProjectOptions {
  readonly name: string;
  readonly runtime: string | AgentRuntime;
  readonly spec: AgentSpec;
  readonly gateways?: Readonly<Record<string, SecretGateway>> | undefined;
  readonly source?: string | undefined;
  readonly exclude?: readonly string[] | undefined;
  readonly requirements?: string | undefined;
  readonly limits?: AgentLimits | undefined;
  readonly serve?: ServeOptions | ServeOptionsInput | undefined;
  readonly allowInternetAccess?: boolean | undefined;
  readonly egress?: readonly string[] | undefined;
  readonly memoryMib?: number | undefined;
  readonly base?: string | undefined;
  readonly template?: string | undefined;
  readonly baseDir?: string | undefined;
  readonly scaling?: ScalingOptions | ScalingOptionsInput | undefined;
  readonly env?: Readonly<Record<string, string>> | undefined;
  readonly network?: AgentNetworkInput | undefined;
  readonly region?: string | undefined;
  readonly bucket?: string | undefined;
  readonly baseImageVersion?: string | undefined;
  readonly language?: AgentLanguage | undefined;
  readonly framework?: string | undefined;
  readonly versions?: RuntimeVersions | RuntimeVersionsInput | undefined;
  readonly graph?: string | undefined;
  readonly secretEnv?: Readonly<Record<string, string | SecretEnvRef>> | undefined;
  readonly secretEnvFrom?: readonly SecretEnvSource[] | undefined;
}

/** El lector de ficheros de texto que `fromObject` usa para
 * `agent.instructions_file`. */
export type TextReader = (path: string) => string;

/** Lo que `fromObject` usa para `[runtime] graph`: el `entrypoint` del grafo
 * de ese nombre en el `langgraph.json` del proyecto. Lo inyecta `load` (como
 * `readText`); lee sólo ese fichero, nunca el código del proyecto. */
export type GraphResolver = (graph: string) => string;

/**
 * Un agente descrito una vez: runtime, `AgentSpec`, pasarelas del modelo,
 * código (`source`), red e imagen. Construirlo valida todo antes de llamar a
 * nadie: `name`, que cada pasarela que el `spec` necesita esté en
 * `gateways`, `serve` y la memoria.
 *
 * `runtime` es un nombre del registro (`"opencode"`, `"deepagents"`,
 * `"langgraph"`...) o un objeto de runtime. `source` es el directorio con el
 * código del agente (se copia a `AGENT_APP_DIR` con `exclude`);
 * `requirements`, un fichero de requisitos suyo. `baseDir` es el directorio
 * contra el que `toObject()` escribe las rutas relativas. `template` es el
 * nombre de una imagen ya construida (por defecto, `name`).
 *
 * Para servir el agente en varios VMs: `scaling` (`ScalingOptions`), `env`
 * (variables no secretas del agente), `network` (política `allowOut`/`denyOut`
 * del sandbox, que necesita `rayito-base-caps`), `region`, `bucket` y
 * `baseImageVersion` (los valores por defecto de `deployAgent()`), `language` y
 * `framework` (lo que la detección o el fichero declaran del proyecto) y
 * `versions` (`RuntimeVersions`). `graph` es el nombre del grafo de
 * `langgraph.json` que se sirve (sólo con el runtime `langgraph`);
 * `toObject()` lo escribe en lugar del `entrypoint`.
 *
 * `secretEnv` (nombre de variable -> nombre o ARN de un secreto, o un
 * `SecretEnvRef`) y `secretEnvFrom` (`SecretEnvSource`) ponen valores de
 * Secrets Manager en el entorno del agente al arrancar (ADR-033 D4): el
 * proyecto sólo lleva nombres, nunca valores, y el agente puede leer su
 * entorno (ver `SecretEnvRef` para el coste).
 *
 * Coste y activación
 * -------------------
 * Activa: nada; `new AgentProject(...)`, `AgentProject.load()`,
 *   `fromObject()` y `toObject()` son puros. Lo que cuesta son `build()`,
 *   `run()` y `deploy()`.
 * Recursos y llamadas AWS: ninguno aquí; ni se crea un cliente de AWS.
 * Coste aproximado: ninguno; el de las operaciones está en la documentación
 *   de `AgentTemplate` (`build()`), de `Sandbox.create()` (`run()`) y de
 *   `deploy()`.
 * IAM: ninguna para este valor.
 * Cómo apagarla: no llames a `build()`, `run()` ni `deploy()`.
 * Ejemplo:
 *   const project = await AgentProject.load("./support-bot");
 *   project.toObject();
 */
export class AgentProject {
  readonly name: string;
  readonly runtime: string | AgentRuntime;
  readonly spec: AgentSpec;
  readonly gateways: Readonly<Record<string, SecretGateway>>;
  readonly source: string | undefined;
  readonly exclude: readonly string[];
  readonly requirements: string | undefined;
  readonly limits: AgentLimits;
  readonly serve: ServeOptions;
  readonly allowInternetAccess: boolean;
  readonly egress: readonly string[] | undefined;
  readonly memoryMib: number;
  readonly base: string;
  readonly template: string | undefined;
  readonly baseDir: string;
  readonly scaling: ScalingOptions;
  readonly env: Readonly<Record<string, string>>;
  readonly network: AgentNetwork | undefined;
  readonly region: string | undefined;
  readonly bucket: string | undefined;
  readonly baseImageVersion: string | undefined;
  readonly language: AgentLanguage | undefined;
  readonly framework: string | undefined;
  readonly versions: RuntimeVersions;
  readonly graph: string | undefined;
  readonly secretEnv: SecretEnvMap;
  readonly secretEnvFrom: readonly SecretEnvSource[];

  constructor(options: AgentProjectOptions) {
    validateName(options.name, "AgentProject.name");
    resolveRuntime(options.runtime);
    if (!(options.spec instanceof AgentSpec)) {
      throw new InvalidArgumentError("AgentProject.spec debe ser un AgentSpec");
    }
    const gateways = validatedGateways(options.gateways ?? {});
    options.spec.requireGateways(Object.keys(gateways));
    optionalText(options.source, "AgentProject.source");
    optionalText(options.requirements, "AgentProject.requirements");
    optionalText(options.template, "AgentProject.template");
    const limits = options.limits ?? new AgentLimits();
    if (!(limits instanceof AgentLimits)) {
      throw new InvalidArgumentError("AgentProject.limits debe ser un AgentLimits");
    }
    const serve = options.serve ?? new ServeOptions();
    const allowInternetAccess = options.allowInternetAccess ?? false;
    if (typeof allowInternetAccess !== "boolean") {
      throw new InvalidArgumentError("AgentProject.allowInternetAccess debe ser un bool");
    }
    const memoryMib = options.memoryMib ?? AGENT_MIN_MEMORY_MIB;
    if (!isInteger(memoryMib) || memoryMib < AGENT_MIN_MEMORY_MIB) {
      throw new InvalidArgumentError(
        `AgentProject.memoryMib debe ser un entero >= ${AGENT_MIN_MEMORY_MIB}`,
      );
    }
    const base = options.base ?? DEFAULT_AGENT_TEMPLATE_BASE;
    const baseDir = options.baseDir ?? ".";
    if (!base || !baseDir) {
      throw new InvalidArgumentError("AgentProject.base y baseDir no pueden ser vacíos");
    }
    this.name = options.name;
    this.runtime = options.runtime;
    this.spec = options.spec;
    this.gateways = Object.freeze({ ...gateways });
    this.source = options.source;
    this.exclude = textList(options.exclude ?? [], "AgentProject.exclude");
    this.requirements = options.requirements;
    this.limits = limits;
    this.serve = serve instanceof ServeOptions ? serve : new ServeOptions(serve);
    this.allowInternetAccess = allowInternetAccess;
    this.egress =
      options.egress === undefined ? undefined : textList(options.egress, "AgentProject.egress");
    this.memoryMib = memoryMib;
    this.base = base;
    this.template = options.template;
    this.baseDir = baseDir;
    const problem = concurrencyProblem(this.serve, memoryMib);
    if (problem !== undefined) {
      throw new InvalidArgumentError(`ServeOptions.maxConcurrency ${problem}`);
    }
    this.scaling =
      options.scaling instanceof ScalingOptions
        ? options.scaling
        : new ScalingOptions(options.scaling);
    this.versions =
      options.versions instanceof RuntimeVersions
        ? options.versions
        : new RuntimeVersions(options.versions);
    this.env = Object.freeze({ ...validatedEnv(options.env ?? {}) });
    this.network = validatedNetwork(options.network);
    optionalText(options.region, "AgentProject.region");
    optionalText(options.framework, "AgentProject.framework");
    this.region = options.region;
    this.framework = options.framework;
    this.bucket =
      options.bucket === undefined
        ? undefined
        : validateBucketHint(options.bucket, "AgentProject.bucket");
    this.baseImageVersion =
      options.baseImageVersion === undefined
        ? undefined
        : validateImageVersion(options.baseImageVersion, "AgentProject.baseImageVersion");
    if (
      options.language !== undefined &&
      !(AGENT_LANGUAGES as readonly string[]).includes(options.language)
    ) {
      throw new InvalidArgumentError(
        `AgentProject.language debe ser uno de ${AGENT_LANGUAGES.join(", ")}`,
      );
    }
    this.language = options.language;
    validateGraph(options.graph, options.runtime);
    this.graph = options.graph;
    this.secretEnv = Object.freeze({ ...validatedSecretEnv(options.secretEnv ?? {}, this.env) });
    this.secretEnvFrom = Object.freeze(validatedSecretEnvFrom(options.secretEnvFrom ?? []));
    if (secretIds(this.secretEnv, this.secretEnvFrom).length > AGENT_SECRET_ENV_MAX_SECRETS) {
      throw fail(
        SECRET_ENV_KEY,
        "invalid_value",
        `admite como mucho ${AGENT_SECRET_ENV_MAX_SECRETS} secretos`,
      );
    }
  }

  /** Lee `rayito-agent.toml` o `rayito-agent.json` de `path` (un fichero, o
   * un directorio donde se busca el primero de `AGENT_PROJECT_FILE_NAMES`).
   * Un `.toml` carga el peer opcional `smol-toml`; el JSON no necesita nada. */
  static async load(path = "."): Promise<AgentProject> {
    const { readProjectDocument } = await import("./project-file.js");
    const document = await readProjectDocument(path);
    return AgentProject.fromObject(document.data, {
      baseDir: document.baseDir,
      readText: document.readText,
      resolveGraph: document.resolveGraph,
    });
  }

  /** El proyecto de un agente que ya tienes y su `DetectionReport` (`undefined`
   * si un fichero de proyecto completo no necesitó detectar nada), sin ejecutar
   * su código ni llamar a AWS. El fichero de proyecto, si existe, gana
   * clave por clave; lo que calla lo completa la detección (lenguaje,
   * framework, punto de entrada, instalación, proveedor y modelo) y
   * `overrides` fuerza lo que tú indiques. Si el caso no es claro lanza
   * `AgentProjectError` con `code` `ambiguous`, `not_detected` o `unsupported` y
   * la clave o bandera que lo resuelve. */
  static async detect(
    path = ".",
    overrides?: DetectOverrides,
    options: { readonly region?: string | undefined } = {},
  ): Promise<readonly [AgentProject, DetectionReport | undefined]> {
    const { detectProject } = await import("./project-file.js");
    return detectProject(path, overrides, options);
  }

  /** El proyecto de un documento ya leído. Puro: las rutas relativas se
   * resuelven contra `baseDir` sin tocar el disco, y `instructions_file` pasa
   * por `readText` (el lector que inyecta `load`; sin él, esa clave es un
   * error), y `[runtime] graph`, por `resolveGraph` (también de `load`). */
  static fromObject(
    data: Readonly<Record<string, unknown>>,
    options: {
      readonly baseDir: string;
      readonly readText?: TextReader | undefined;
      readonly resolveGraph?: GraphResolver | undefined;
    },
  ): AgentProject {
    return new AgentProject({
      ...projectOptions(data, options.baseDir, options.readText, options.resolveGraph),
      baseDir: options.baseDir,
    });
  }

  /** La forma canónica del documento, la que `fromObject` vuelve a leer y que
   * comparten los dos SDK. Omite lo que vale su valor por defecto; el texto
   * de las instrucciones va en línea; los tiempos van en segundos. */
  toObject(): Record<string, unknown> {
    const document: Record<string, unknown> = {
      schema: AGENT_PROJECT_SCHEMA,
      name: this.name,
      runtime: runtimeToObject(this),
    };
    const tables: Array<[string, object]> = [
      ["source", sourceToObject(this)],
      ["model", modelToObject(this.spec.model)],
      ["gateways", gatewaysToObject(this.gateways)],
      ["agent", agentToObject(this.spec)],
      ["limits", limitsToObject(this.limits)],
      ["serve", serveToObject(this.serve)],
      ["scaling", scalingToObject(this.scaling)],
      ["env", Object.fromEntries(sortedEntries(this.env))],
      ["secret_env", secretEnvToObject(this)],
      ["secret_env_from", secretEnvFromToObject(this)],
      ["network", networkToObject(this)],
      ["deploy", deployToObject(this)],
      ["image", imageToObject(this)],
    ];
    for (const [key, table] of tables) {
      if (key === "model" || Object.keys(table).length > 0) {
        document[key] = table;
      }
    }
    return document;
  }

  /** Construye la imagen del proyecto con `AgentTemplate` (su runtime, su
   * código y su memoria). Cuesta un build de imagen: ver el bloque "Coste y
   * activación" de `AgentTemplate`. */
  async build(options: AgentTemplateBuildOptions): Promise<BuildInfo> {
    const { ProjectService } = await import("./project-service.js");
    return new ProjectService(this).build(options);
  }

  /** Una ejecución del agente en un sandbox efímero: lo crea desde la imagen
   * del proyecto con sus pasarelas y su red, llama a `sbx.agent.run()` y lo
   * termina siempre, también ante un error. Para que el sandbox sobreviva,
   * usa `deploy()`. */
  async run(prompt: string, options: AgentProjectRunOptions = {}): Promise<AgentResult> {
    const { ProjectService } = await import("./project-service.js");
    return new ProjectService(this).run(prompt, options);
  }

  /**
   * Sirve el agente desde un MicroVM y devuelve su `AgentDeployment`: crea un
   * sandbox desde la imagen del proyecto (con `serve.timeoutMs`, `serve.idle`
   * y las etiquetas de despliegue), escribe la configuración del host, lo
   * arranca en modo servicio y espera su marcador de listo. Si cualquier paso
   * falla, termina el sandbox antes de lanzar.
   *
   * `accessToken` es el del sandbox (como el de `Sandbox.create()`; sin él,
   * `RAYITO_ACCESS_TOKEN` o uno nuevo). Quien quiera reconectarse desde otro
   * proceso (`AgentDeployment.connect`, `rayito agent invoke`) lo guarda: sin
   * él sólo `Sandbox.kill(sandboxId)` llega al sandbox.
   *
   * Si el despliegue falla, el sandbox se mata. Con `keepOnFailure: true` (hace
   * falta `accessToken`) se deja vivo para depurarlo, sigue facturando hasta
   * que lo termines, y el mensaje del error dice cuál es.
   *
   * Coste y activación
   * -------------------
   * Activa: `await project.deploy()` (o `rayito agent deploy`); nada más lo
   *     crea.
   * Recursos y llamadas AWS: un sandbox (`run-microvm`) que vive hasta
   *     `serve.timeoutMs` (como mucho 8 horas) o hasta
   *     `AgentDeployment.stop()`, y el token del proxy
   *     (`CreateMicrovmAuthToken`, gratuito); el modelo lo llama la pasarela
   *     del sandbox.
   * Coste aproximado: el del MicroVM por segundo mientras vive (2 GB: ≈ $0,14
   *     por hora en us-east-1, consultado 2026-10-06 en
   *     https://aws.amazon.com/lambda/pricing/; `serve.idle` lo suspende y la
   *     suspensión no factura cómputo) más el del modelo del agente.
   * IAM: los de `Sandbox.create()` (`RayitoSandboxRunner`) y, por cada
   *     pasarela, `secretsmanager:GetSecretValue`.
   * Cómo apagarla: `AgentDeployment.stop()` o `rayito agent stop`; no llames
   *     a `deploy()` y nada se crea.
   * Ejemplo:
   *   const deployment = await (await AgentProject.load("./support-bot")).deploy();
   *   (await deployment.invoke("Resume el README.")).text;
   *   await deployment.stop();
   */
  async deploy(options: AgentProjectDeployOptions = {}): Promise<AgentDeployment> {
    const { ProjectService } = await import("./project-service.js");
    return new ProjectService(this).deploy(options);
  }

  /** Escribe en `outDir` un contexto de build de contenedor `linux/arm64`
   * (`target: "docker"` o `"agentcore"`) sin ninguna llamada de red ni de
   * AWS. Falla si `outDir` no está vacío, salvo con `overwrite: true`. Fuera
   * de Rayito no hay pasarela de secretos: la credencial del modelo la pone
   * la plataforma. */
  async export(options: AgentExportOptions): Promise<AgentExport> {
    const { exportProject } = await import("./export.js");
    return exportProject(this, options);
  }
}

// ------------------------------------------------------------------ validation

const SECRET_ENV_KEY = "secret_env";
const SECRET_ENV_FROM_KEY = "secret_env_from";

/** La ruta de error de una variable de `secret_env` (la tabla, si el nombre
 * está vacío). */
export function secretEnvPath(name: string): string {
  return name === "" ? SECRET_ENV_KEY : `${SECRET_ENV_KEY}.${name}`;
}

export function secretEnvFromPath(index: number): string {
  return `${SECRET_ENV_FROM_KEY}[${index}]`;
}

/** Un nombre de variable válido y no reservado, o `invalid_value`. */
export function checkSecretEnvName(name: unknown, path: string): string {
  if (typeof name !== "string" || !SECRET_ENV_NAME_PATTERN.test(name)) {
    throw fail(path, "invalid_value", "debe ser un nombre de variable de entorno");
  }
  if (isReservedName(name)) {
    throw fail(path, "invalid_value", "es un nombre reservado del runtime");
  }
  return name;
}

/** El nombre completo o el ARN de un secreto: `secret_value` si parece el
 * valor pegado en su lugar, `invalid_value` si no es un nombre. */
export function checkSecretId(value: unknown, path: string): string {
  if (typeof value !== "string") {
    throw fail(path, "invalid_type", "debe ser el nombre o el ARN de un secreto");
  }
  if (SECRET_VALUE_PREFIXES.some((prefix) => value.startsWith(prefix))) {
    throw fail(
      path,
      "secret_value",
      "parece un valor y no el nombre de un secreto; escribe el nombre",
    );
  }
  if (!SECRET_ID_PATTERN.test(value)) {
    throw fail(path, "invalid_value", "debe ser el nombre o el ARN de un secreto");
  }
  return value;
}

function checkSecretEnvKey(value: string | undefined, path: string): void {
  if (value !== undefined && !SECRET_ENV_KEY_PATTERN.test(value)) {
    throw fail(path, "invalid_value", "debe ser ASCII imprimible sin espacios");
  }
}

/** Un valor de `secret_env`: el nombre de un secreto o un `SecretEnvRef`. */
export function checkSecretEnvRef(value: unknown, path: string): SecretEnvValue {
  if (typeof value === "string") {
    return checkSecretId(value, path);
  }
  if (!(value instanceof SecretEnvRef)) {
    throw fail(path, "invalid_type", "debe ser el nombre de un secreto o un SecretEnvRef");
  }
  checkSecretId(value.secret, path);
  checkSecretEnvKey(value.key, path);
  checkSecretEnvKey(value.versionStage, path);
  return value;
}

/** Una entrada de `secret_env_from`: secreto válido, prefijo vacío o de
 * nombre de variable y no reservado, y claves que con el prefijo son nombres
 * válidos y no reservados. */
export function checkSecretEnvSource(source: unknown, path: string): SecretEnvSource {
  if (!(source instanceof SecretEnvSource)) {
    throw fail(path, "invalid_type", "debe ser un SecretEnvSource");
  }
  checkSecretId(source.secret, `${path}.secret`);
  const prefixPath = `${path}.prefix`;
  if (source.prefix !== "" && !SECRET_ENV_NAME_PATTERN.test(source.prefix)) {
    throw fail(prefixPath, "invalid_value", "debe ser un prefijo de nombre de variable");
  }
  if (hasReservedPrefix(source.prefix)) {
    throw fail(prefixPath, "invalid_value", "es un prefijo reservado del runtime");
  }
  for (const key of source.keys ?? []) {
    checkSecretEnvName(`${source.prefix}${key}`, `${path}.keys`);
  }
  return source;
}

function validatedSecretEnv(
  secretEnv: unknown,
  env: Readonly<Record<string, string>>,
): Record<string, SecretEnvValue> {
  if (!isRecord(secretEnv)) {
    throw fail(SECRET_ENV_KEY, "invalid_type", "debe mapear nombres a secretos");
  }
  const checked: Record<string, SecretEnvValue> = {};
  for (const [name, value] of Object.entries(secretEnv)) {
    const path = secretEnvPath(name);
    checkSecretEnvName(name, path);
    checked[name] = checkSecretEnvRef(value, path);
    if (Object.hasOwn(env, name)) {
      throw fail(path, "conflict", "la variable está a la vez en [env] y en [secret_env]");
    }
  }
  return checked;
}

function validatedSecretEnvFrom(sources: unknown): SecretEnvSource[] {
  if (!Array.isArray(sources)) {
    throw fail(SECRET_ENV_FROM_KEY, "invalid_type", "debe ser una lista de SecretEnvSource");
  }
  return sources.map((source: unknown, index) =>
    checkSecretEnvSource(source, secretEnvFromPath(index)),
  );
}

function optionalText(value: unknown, label: string): void {
  if (value !== undefined && (typeof value !== "string" || value === "")) {
    throw new InvalidArgumentError(`${label} debe ser una cadena no vacía`);
  }
}

function textList(values: unknown, label: string): readonly string[] {
  if (!Array.isArray(values)) {
    throw new InvalidArgumentError(`${label} debe ser una lista de cadenas`);
  }
  if (!values.every((item) => typeof item === "string" && item !== "")) {
    throw new InvalidArgumentError(`${label} sólo admite cadenas no vacías`);
  }
  return Object.freeze([...values]);
}

function validatedGateways(gateways: unknown): Readonly<Record<string, SecretGateway>> {
  if (!isRecord(gateways)) {
    throw new InvalidArgumentError("AgentProject.gateways debe ser un objeto nombre -> pasarela");
  }
  for (const [name, gateway] of Object.entries(gateways)) {
    validateName(name, "AgentProject.gateways");
    if (!(gateway instanceof SecretGateway)) {
      throw new InvalidArgumentError(
        "cada valor de AgentProject.gateways debe ser un SecretGateway",
      );
    }
  }
  return gateways as Readonly<Record<string, SecretGateway>>;
}

// --------------------------------------------------------------------- reading

/** Lectura estricta de una tabla: cada clave que se pide queda marcada y
 * `finish()` falla con la primera que sobra. */
class Table {
  readonly path: string;
  readonly #data: Record<string, unknown>;
  readonly #used = new Set<string>();

  constructor(data: unknown, path: string) {
    if (!isRecord(data)) {
      throw fail(path === "" ? "<raíz>" : path, "invalid_type", "debe ser una tabla");
    }
    this.#data = data;
    this.path = path;
  }

  at(key: string): string {
    return this.path === "" ? key : `${this.path}.${key}`;
  }

  raw(key: string): unknown {
    this.#used.add(key);
    return this.#data[key];
  }

  text(key: string, required = false): string | undefined {
    const value = this.#value(key, required);
    if (value === undefined) {
      return undefined;
    }
    if (typeof value !== "string" || value === "") {
      throw fail(this.at(key), "invalid_type", "debe ser una cadena no vacía");
    }
    return value;
  }

  boolean(key: string): boolean | undefined {
    const value = this.#value(key, false);
    if (value !== undefined && typeof value !== "boolean") {
      throw fail(this.at(key), "invalid_type", "debe ser true o false");
    }
    return value;
  }

  integer(key: string): number | undefined {
    const value = this.#value(key, false);
    if (value !== undefined && !isInteger(value)) {
      throw fail(this.at(key), "invalid_type", "debe ser un entero");
    }
    return value;
  }

  number(key: string): number | undefined {
    const value = this.#value(key, false);
    if (value !== undefined && typeof value !== "number") {
      throw fail(this.at(key), "invalid_type", "debe ser un número");
    }
    return value;
  }

  texts(key: string): readonly string[] | undefined {
    const value = this.#value(key, false);
    if (value === undefined) {
      return undefined;
    }
    if (!Array.isArray(value) || !value.every((item) => typeof item === "string" && item !== "")) {
      throw fail(this.at(key), "invalid_type", "debe ser una lista de cadenas");
    }
    return value;
  }

  table(key: string): Table | undefined {
    const value = this.#value(key, false);
    return value === undefined ? undefined : new Table(value, this.at(key));
  }

  entries(key: string): Array<[string, Table]> {
    const table = this.table(key);
    if (table === undefined) {
      return [];
    }
    return table
      .keys()
      .sort()
      .map((name) => [name, new Table(table.raw(name), table.at(name))]);
  }

  keys(): string[] {
    return Object.keys(this.#data);
  }

  #value(key: string, required: boolean): unknown {
    this.#used.add(key);
    const value = this.#data[key];
    if (value === undefined || value === null) {
      if (required) {
        throw fail(this.at(key), "missing_key", "es obligatoria");
      }
      return undefined;
    }
    return value;
  }

  finish(): void {
    const unknown = this.keys()
      .filter((key) => !this.#used.has(key))
      .sort();
    const key = unknown[0];
    if (key === undefined) {
      return;
    }
    if ((SECRET_LIKE_KEYS as readonly string[]).includes(key.toLowerCase())) {
      throw fail(
        this.at(key),
        "secret_value",
        "un fichero de proyecto nunca lleva un secreto; usa el nombre de un secreto",
      );
    }
    throw fail(this.at(key), "unknown_key", "clave desconocida");
  }
}

function guard<T>(path: string, build: () => T): T {
  try {
    return build();
  } catch (error) {
    if (error instanceof AgentProjectError || !(error instanceof InvalidArgumentError)) {
      throw error;
    }
    throw fail(path, "invalid_value", error.message);
  }
}

/** `relative` resuelto contra `baseDir` sin tocar el disco; una ruta absoluta
 * o que sale de `baseDir` es `path_escape`. */
function inside(baseDir: string, target: string, path: string): string {
  if (isAbsolute(target)) {
    throw fail(path, "path_escape", "debe ser relativa al directorio del fichero");
  }
  const root = resolve(baseDir);
  const resolved = resolve(root, target);
  if (resolved !== root && !resolved.startsWith(root + sep)) {
    throw fail(path, "path_escape", "sale del directorio del fichero");
  }
  return resolved;
}

function projectOptions(
  data: Readonly<Record<string, unknown>>,
  baseDir: string,
  readText: TextReader | undefined,
  resolveGraph: GraphResolver | undefined,
): AgentProjectOptions {
  const root = new Table(data, "");
  const schema = root.text("schema", true);
  if (schema !== AGENT_PROJECT_SCHEMA) {
    throw fail("schema", "unsupported_schema", `debe ser '${AGENT_PROJECT_SCHEMA}'`);
  }
  const name = root.text("name", true) as string;
  const { runtime, ...runtimeExtras } = readRuntime(root.table("runtime"), resolveGraph);
  const source = readSource(root.table("source"), baseDir);
  const gateways = readGateways(root);
  const modelTable = root.table("model");
  if (modelTable === undefined) {
    throw fail("model", "missing_key", "es obligatoria");
  }
  const model = readModel(modelTable);
  const spec = readAgent(root.table("agent"), model, readText, baseDir);
  const limits = readLimits(root.table("limits"));
  const serve = readServe(root.table("serve"));
  const scaling = readScaling(root.table("scaling"));
  const env = readEnv(root.table("env"));
  const secretEnv = readSecretEnv(root);
  const network = readNetwork(root.table("network"));
  const deploy = readDeploy(root.table("deploy"));
  const image = readImage(root.table("image"));
  root.finish();
  checkGateways(spec, gateways);
  checkConcurrency(serve, image);
  checkModel(
    spec.model,
    runtime,
    gatewayPresets(data),
    env.env as Readonly<Record<string, string>>,
  );
  return {
    name,
    runtime,
    ...runtimeExtras,
    spec,
    gateways,
    ...source,
    ...limits,
    ...serve,
    ...scaling,
    ...env,
    ...secretEnv,
    ...network,
    ...deploy,
    ...image,
  };
}

/** `[gateways.<nombre>] preset` de cada pasarela del documento (ya
 * validado): `AgentProject` sólo guarda la pasarela construida. */
function gatewayPresets(data: Readonly<Record<string, unknown>>): Record<string, string> {
  const gateways = data.gateways;
  if (!isRecord(gateways)) {
    return {};
  }
  const presets: Record<string, string> = {};
  for (const [name, entry] of Object.entries(gateways)) {
    if (isRecord(entry) && typeof entry.preset === "string") {
      presets[name] = entry.preset;
    }
  }
  return presets;
}

/** El id del modelo donde Rayito lo usa y los nombres de `[model]` que no
 * repiten una variable de `[env]` (`conflict`). */
function checkModel(
  model: AgentModel,
  runtime: AgentRuntime | string,
  presets: Readonly<Record<string, string>>,
  env: Readonly<Record<string, string>> | undefined,
): void {
  const options = runtimeToOptions(runtime);
  requireModelId(model.id, {
    runtime: String(options.kind),
    entrypoint: options.entrypoint !== undefined,
    gatewayPreset: presets[model.gateway],
  });
  for (const [key, name] of Object.entries(model.envNames())) {
    if (env !== undefined && Object.hasOwn(env, name)) {
      throw fail(`model.${key}`, "conflict", "esa variable ya está en [env]");
    }
  }
}

function checkConcurrency(serveFields: Fields, imageFields: Fields): void {
  const serve = (serveFields.serve as ServeOptions | undefined) ?? new ServeOptions();
  const memoryMib = (imageFields.memoryMib as number | undefined) ?? AGENT_MIN_MEMORY_MIB;
  const problem = concurrencyProblem(serve, memoryMib);
  if (problem !== undefined) {
    throw fail("serve.max_concurrency", "invalid_value", problem);
  }
}

function checkGateways(spec: AgentSpec, gateways: Readonly<Record<string, SecretGateway>>): void {
  if (!(spec.model.gateway in gateways)) {
    throw fail("model.gateway", "invalid_value", "la pasarela no está declarada en [gateways]");
  }
  for (const [name, server] of Object.entries(spec.mcp).sort(([a], [b]) => (a < b ? -1 : 1))) {
    if (server instanceof McpRemote && !(server.gateway in gateways)) {
      throw fail(
        `agent.mcp.${name}.gateway`,
        "invalid_value",
        "la pasarela no está declarada en [gateways]",
      );
    }
  }
}

interface RuntimeRead {
  readonly runtime: AgentRuntime;
  readonly language?: AgentLanguage;
  readonly framework?: string;
  readonly versions?: RuntimeVersions;
  readonly graph?: string;
}

function readRuntime(
  table: Table | undefined,
  resolveGraph: GraphResolver | undefined,
): RuntimeRead {
  if (table === undefined) {
    throw fail("runtime", "missing_key", "es obligatoria");
  }
  const kind = table.text("kind", true) as string;
  if (!(RUNTIME_KINDS as readonly string[]).includes(kind)) {
    throw fail("runtime.kind", "invalid_value", `debe ser uno de ${RUNTIME_KINDS.join(", ")}`);
  }
  const extras = readRuntimeExtras(table);
  const options: Record<string, unknown> = {};
  for (const key of RUNTIME_OPTION_KEYS[kind] ?? []) {
    const value = table.raw(key);
    if (value !== undefined && value !== null) {
      options[key] = value;
    }
  }
  table.finish();
  if (extras.graph !== undefined) {
    options[ENTRYPOINT_KEY] = graphEntrypointOf(extras.graph, resolveGraph, table.at(GRAPH_KEY));
  }
  return { runtime: guard("runtime", () => runtimeFromOptions(kind, options)), ...extras };
}

/** El `entrypoint` del grafo `graph`, con el resolvedor que inyecta `load`
 * (sin él, `invalid_value`, como `instructions_file`). */
function graphEntrypointOf(
  graph: string,
  resolveGraph: GraphResolver | undefined,
  path: string,
): string {
  if (resolveGraph === undefined) {
    throw fail(path, "invalid_value", "no hay lector de ficheros disponible");
  }
  return resolveGraph(graph);
}

/** `[runtime] graph`: un nombre válido, sólo con `kind = "langgraph"` y nunca
 * junto a `entrypoint` (`conflict`). */
function readGraph(table: Table): string | undefined {
  const graph = table.text(GRAPH_KEY);
  if (graph === undefined) {
    return undefined;
  }
  const path = table.at(GRAPH_KEY);
  if (table.raw("kind") !== GRAPH_RUNTIME_KIND) {
    throw fail(path, "invalid_value", `sólo vale con kind = '${GRAPH_RUNTIME_KIND}'`);
  }
  if (!GRAPH_NAME_PATTERN.test(graph)) {
    throw fail(path, "invalid_value", `debe casar con ${GRAPH_NAME_PATTERN.source}`);
  }
  if (table.raw(ENTRYPOINT_KEY) !== undefined) {
    throw fail(path, "conflict", DETECTION_RULES.messages.graph_conflict as string);
  }
  return graph;
}

/** `AgentProject.graph`: un nombre de grafo válido y sólo con el runtime
 * `langgraph`. */
function validateGraph(graph: unknown, runtime: string | AgentRuntime): void {
  if (graph === undefined) {
    return;
  }
  if (typeof graph !== "string" || !GRAPH_NAME_PATTERN.test(graph)) {
    throw new InvalidArgumentError(
      `AgentProject.graph debe casar con ${GRAPH_NAME_PATTERN.source}`,
    );
  }
  if (runtimeToOptions(runtime).kind !== GRAPH_RUNTIME_KIND) {
    throw new InvalidArgumentError(
      `AgentProject.graph sólo vale con el runtime '${GRAPH_RUNTIME_KIND}'`,
    );
  }
}

/** Las claves de `[runtime]` que son del proyecto y no de un runtime:
 * `language`, `framework` y las versiones de intérprete. */
function readRuntimeExtras(table: Table): Omit<RuntimeRead, "runtime"> {
  const language = table.text("language");
  if (language !== undefined && !(AGENT_LANGUAGES as readonly string[]).includes(language)) {
    throw fail(
      table.at("language"),
      "invalid_value",
      `debe ser uno de ${AGENT_LANGUAGES.join(", ")}`,
    );
  }
  const framework = table.text("framework");
  const python = readVersion(table, "python_version", "python");
  const node = readVersion(table, "node_version", "node");
  const extras: { -readonly [K in keyof Omit<RuntimeRead, "runtime">]: RuntimeRead[K] } = {};
  if (language !== undefined) {
    extras.language = language as AgentLanguage;
  }
  if (framework !== undefined) {
    extras.framework = framework;
  }
  if (python !== undefined || node !== undefined) {
    extras.versions = new RuntimeVersions({ python, node });
  }
  const graph = readGraph(table);
  if (graph !== undefined) {
    extras.graph = graph;
  }
  return extras;
}

function readVersion(table: Table, key: string, field: "python" | "node"): string | undefined {
  const value = table.text(key);
  if (value !== undefined) {
    guard(table.at(key), () => new RuntimeVersions({ [field]: value }));
  }
  return value;
}

type Fields = Record<string, unknown>;

function readSource(table: Table | undefined, baseDir: string): Fields {
  if (table === undefined) {
    return {};
  }
  const path = table.text("path") ?? ".";
  const requirements = table.text("requirements");
  const exclude = table.texts("exclude") ?? [];
  table.finish();
  const fields: Fields = { source: inside(baseDir, path, "source.path"), exclude };
  if (requirements !== undefined) {
    fields.requirements = inside(baseDir, requirements, "source.requirements");
  }
  return fields;
}

function readModel(table: Table): AgentModel {
  const provider = table.text("provider", true) as string;
  const id = table.text("id");
  const gateway = table.text("gateway", true) as string;
  const region = table.text("region");
  const basePath = table.text("base_path");
  const caching = table.boolean("prompt_caching");
  const names = readModelEnvNames(table);
  table.finish();
  return guard(
    "model",
    () =>
      new AgentModel({
        provider: provider as ModelProvider,
        id,
        gateway,
        region,
        basePath: basePath ?? "",
        promptCaching: caching,
        ...names,
      }),
  );
}

/** Las claves `MODEL_ENV_KEYS` de `[model]` que el fichero fija: nombres de
 * variable, nunca valores. */
function readModelEnvNames(table: Table): Record<string, string> {
  const names: Record<string, string> = {};
  for (const key of MODEL_ENV_KEYS) {
    const name = table.text(key);
    if (name === undefined) {
      continue;
    }
    if (!ENV_NAME_PATTERN.test(name)) {
      throw fail(table.at(key), "invalid_value", "debe ser un nombre de variable");
    }
    names[MODEL_ENV_FIELDS[key]] = name;
  }
  return names;
}

function readAgent(
  table: Table | undefined,
  model: AgentModel,
  readText: TextReader | undefined,
  baseDir: string,
): AgentSpec {
  if (table === undefined) {
    return new AgentSpec({ model });
  }
  const instructions = readInstructions(table, readText, baseDir);
  const smallModel = table.text("small_model");
  const runtimeVersion = table.text("runtime_version");
  const permissionsTable = table.table("permissions");
  const permissions = readPermissions(permissionsTable, table.at("permissions"));
  const mcp = Object.fromEntries(
    table.entries("mcp").map(([name, entry]) => [name, readMcp(entry)]),
  );
  const agents = Object.fromEntries(
    table.entries("agents").map(([name, entry]) => [name, readSubagent(entry)]),
  );
  const rawConfig = table.raw("raw_config");
  if (rawConfig !== undefined && rawConfig !== null && !isRecord(rawConfig)) {
    throw fail(table.at("raw_config"), "invalid_type", "debe ser una tabla");
  }
  table.finish();
  return guard(
    table.path,
    () =>
      new AgentSpec({
        model,
        smallModel,
        instructions,
        mcp,
        permissions,
        agents,
        rawConfig: (rawConfig ?? undefined) as Record<string, unknown> | undefined,
        runtimeVersion,
      }),
  );
}

function readInstructions(
  table: Table,
  readText: TextReader | undefined,
  baseDir: string,
): string | undefined {
  const inline = table.text("instructions");
  const file = table.text("instructions_file");
  if (inline !== undefined && file !== undefined) {
    throw fail(table.at("instructions_file"), "conflict", "es excluyente con agent.instructions");
  }
  if (file === undefined) {
    return inline;
  }
  const resolved = inside(baseDir, file, table.at("instructions_file"));
  if (readText === undefined) {
    throw fail(
      table.at("instructions_file"),
      "invalid_value",
      "no hay lector de ficheros disponible",
    );
  }
  return readText(resolved);
}

function readPermissions(table: Table | undefined, path: string): AgentPermissions {
  if (table === undefined) {
    return new AgentPermissions();
  }
  const permissionDefault = table.text("default");
  const tools = table.raw("tools");
  if (tools !== undefined && tools !== null && !isRecord(tools)) {
    throw fail(table.at("tools"), "invalid_type", "debe ser una tabla");
  }
  table.finish();
  return guard(
    path,
    () =>
      new AgentPermissions({
        default: permissionDefault as "allow" | "deny" | undefined,
        tools: (tools ?? undefined) as Record<string, never> | undefined,
      }),
  );
}

function readMcp(entry: Table): McpServer {
  const kind = entry.text("type", true);
  if (kind === "local") {
    const command = entry.texts("command");
    const envs = entry.raw("envs");
    const timeout = entry.number("timeout_seconds");
    entry.finish();
    if (envs !== undefined && envs !== null && !isRecord(envs)) {
      throw fail(entry.at("envs"), "invalid_type", "debe ser una tabla");
    }
    return guard(
      entry.path,
      () =>
        new McpLocal({
          command: command ?? [],
          envs: (envs ?? undefined) as Record<string, string> | undefined,
          timeoutMs: timeout === undefined ? undefined : timeout * MS_PER_SECOND,
        }),
    );
  }
  if (kind === "remote") {
    const gateway = entry.text("gateway", true) as string;
    const path = entry.text("path");
    entry.finish();
    return guard(entry.path, () => new McpRemote({ gateway, path: path ?? "/" }));
  }
  throw fail(entry.at("type"), "invalid_value", "debe ser 'local' o 'remote'");
}

function readSubagent(entry: Table): SubAgent {
  const description = entry.text("description", true) as string;
  const instructions = entry.text("instructions", true) as string;
  const model = entry.text("model");
  const permissionsTable = entry.table("permissions");
  const permissions =
    permissionsTable === undefined
      ? undefined
      : readPermissions(permissionsTable, permissionsTable.path);
  entry.finish();
  return guard(entry.path, () => new SubAgent({ description, instructions, model, permissions }));
}

function readLimits(table: Table | undefined): Fields {
  if (table === undefined) {
    return {};
  }
  const maxSteps = table.integer("max_steps");
  const timeout = table.number("timeout_seconds");
  const maxOutputBytes = table.integer("max_output_bytes");
  const maxTotalTokens = table.integer("max_total_tokens");
  table.finish();
  const options: Record<string, unknown> = {};
  if (maxSteps !== undefined) {
    options.maxSteps = maxSteps;
  }
  if (timeout !== undefined) {
    options.timeoutMs = timeout * MS_PER_SECOND;
  }
  if (maxOutputBytes !== undefined) {
    options.maxOutputBytes = maxOutputBytes;
  }
  if (maxTotalTokens !== undefined) {
    options.maxTotalTokens = maxTotalTokens === 0 ? null : maxTotalTokens;
  }
  return { limits: guard("limits", () => new AgentLimits(options)) };
}

function readServe(table: Table | undefined): Fields {
  if (table === undefined) {
    return {};
  }
  const options: Record<string, unknown> = {};
  const port = table.integer("port");
  const maxConcurrency = table.integer("max_concurrency");
  const timeout = table.number("timeout_seconds");
  const idleSeconds = table.integer("idle_seconds");
  const metadata = table.raw("metadata");
  const queueMax = table.integer("queue_max");
  const queueTimeout = table.number("queue_timeout_seconds");
  table.finish();
  if (port !== undefined) {
    options.port = port;
  }
  if (maxConcurrency !== undefined) {
    options.maxConcurrency = maxConcurrency;
  }
  if (timeout !== undefined) {
    options.timeoutMs = timeout * MS_PER_SECOND;
  }
  if (idleSeconds !== undefined) {
    options.idle =
      idleSeconds === 0
        ? null
        : guard("serve.idle_seconds", () => validateIdlePolicy({ maxIdleSeconds: idleSeconds }));
  }
  if (metadata !== undefined && metadata !== null) {
    options.metadata = metadata;
  }
  if (queueMax !== undefined) {
    options.queueMax = queueMax;
  }
  if (queueTimeout !== undefined) {
    options.queueTimeoutMs = queueTimeout * MS_PER_SECOND;
  }
  return { serve: guard("serve", () => new ServeOptions(options as ServeOptionsInput)) };
}

function readNetwork(table: Table | undefined): Fields {
  if (table === undefined) {
    return {};
  }
  const internet = table.boolean("allow_internet_access");
  const egress = table.texts("egress");
  const allowOut = table.texts("allow_out");
  const denyOut = table.texts("deny_out");
  table.finish();
  const fields: Fields = {};
  if (internet !== undefined) {
    fields.allowInternetAccess = internet;
  }
  if (egress !== undefined) {
    fields.egress = egress;
  }
  if (allowOut !== undefined || denyOut !== undefined) {
    fields.network = { allowOut: allowOut ?? [], denyOut: denyOut ?? [] };
  }
  return fields;
}

function readScaling(table: Table | undefined): Fields {
  if (table === undefined) {
    return {};
  }
  const options: Record<string, unknown> = {};
  for (const [key, field] of [
    ["min_vms", "minVms"],
    ["max_vms", "maxVms"],
    ["warm_vms", "warmVms"],
  ] as const) {
    const value = table.integer(key);
    if (value !== undefined) {
      options[field] = value;
    }
  }
  const sessions = table.text("sessions");
  if (sessions !== undefined) {
    options.sessions = sessions;
  }
  const seconds = table.number("scale_in_after_seconds");
  if (seconds !== undefined) {
    options.scaleInAfterMs = seconds * MS_PER_SECOND;
  }
  table.finish();
  return { scaling: guard("scaling", () => new ScalingOptions(options as ScalingOptionsInput)) };
}

function readEnv(table: Table | undefined): Fields {
  if (table === undefined) {
    return {};
  }
  const env: Record<string, string> = {};
  for (const name of table.keys().sort()) {
    const path = table.at(name);
    const value = table.raw(name);
    if (typeof value !== "string") {
      throw fail(path, "invalid_type", "debe ser una cadena");
    }
    if (
      secretLikeEnvName(name) ||
      SECRET_VALUE_PREFIXES.some((prefix) => value.startsWith(prefix))
    ) {
      throw fail(
        path,
        "secret_value",
        "un fichero de proyecto nunca lleva un secreto; usa una pasarela",
      );
    }
    env[name] = value;
  }
  table.finish();
  return { env: guard("env", () => validatedEnv(env)) };
}

function readSecretEnvValue(table: Table, name: string): SecretEnvValue {
  const path = table.at(name);
  checkSecretEnvName(name, path);
  const value = table.raw(name);
  if (!isRecord(value)) {
    return checkSecretId(value, path);
  }
  const reference = new Table(value, path);
  const secret = checkSecretId(reference.text("secret", true), reference.at("secret"));
  const key = reference.text("key");
  const versionStage = reference.text("version_stage");
  reference.finish();
  return checkSecretEnvRef(new SecretEnvRef({ secret, key, versionStage }), path);
}

function readSecretEnvSource(entry: Table): SecretEnvSource {
  const secret = checkSecretId(entry.text("secret", true), entry.at("secret"));
  const prefix = entry.raw("prefix");
  if (prefix !== undefined && prefix !== null && typeof prefix !== "string") {
    throw fail(entry.at("prefix"), "invalid_type", "debe ser una cadena");
  }
  const keys = entry.texts("keys");
  entry.finish();
  const source = new SecretEnvSource({ secret, prefix: prefix ?? "", keys });
  return checkSecretEnvSource(source, entry.path);
}

/** `[secret_env]` y `[[secret_env_from]]` (ADR-033 D4), sólo si están. */
function readSecretEnv(root: Table): Fields {
  const fields: Fields = {};
  const table = root.table(SECRET_ENV_KEY);
  if (table !== undefined) {
    const secretEnv: Record<string, SecretEnvValue> = {};
    for (const name of table.keys().sort()) {
      secretEnv[name] = readSecretEnvValue(table, name);
    }
    table.finish();
    fields.secretEnv = secretEnv;
  }
  const sources = root.raw(SECRET_ENV_FROM_KEY);
  if (sources !== undefined && sources !== null) {
    if (!Array.isArray(sources)) {
      throw fail(SECRET_ENV_FROM_KEY, "invalid_type", "debe ser una lista de tablas");
    }
    fields.secretEnvFrom = sources.map((item: unknown, index) =>
      readSecretEnvSource(new Table(item, secretEnvFromPath(index))),
    );
  }
  return fields;
}

const DEPLOY_CHECKS: Readonly<Record<string, (value: string, label: string) => string>> = {
  bucket: validateBucketHint,
  base_image_version: validateImageVersion,
};

function readDeploy(table: Table | undefined): Fields {
  if (table === undefined) {
    return {};
  }
  const fields: Fields = {};
  for (const [key, field] of [
    ["region", "region"],
    ["bucket", "bucket"],
    ["base_image_version", "baseImageVersion"],
  ] as const) {
    const value = table.text(key);
    if (value === undefined) {
      continue;
    }
    const check = DEPLOY_CHECKS[key];
    fields[field] =
      check === undefined ? value : guard(table.at(key), () => check(value, table.at(key)));
  }
  table.finish();
  return fields;
}

function readImage(table: Table | undefined): Fields {
  if (table === undefined) {
    return {};
  }
  const fields: Fields = {};
  const base = table.text("base");
  const template = table.text("template");
  const memory = table.integer("memory_mib");
  table.finish();
  if (base !== undefined) {
    fields.base = base;
  }
  if (template !== undefined) {
    fields.template = template;
  }
  if (memory !== undefined) {
    fields.memoryMib = memory;
  }
  return fields;
}

// -------------------------------------------------------------------- gateways

function secretName(value: unknown, path: string): string {
  if (typeof value !== "string" || value === "") {
    throw fail(path, "invalid_type", "debe ser el nombre de un secreto");
  }
  if (/\s/.test(value) || SECRET_VALUE_PREFIXES.some((prefix) => value.startsWith(prefix))) {
    throw fail(
      path,
      "secret_value",
      "parece un valor y no el nombre de un secreto; escribe el nombre",
    );
  }
  return value;
}

interface Preset {
  readonly keys: readonly string[];
  readonly required: readonly string[];
  readonly build: (secret: string, options: Record<string, unknown>) => SecretGateway;
}

type AnyOptions = Record<string, unknown>;

/** `preset` -> claves que admite (además de `secret` y `rate_per_minute`),
 * cuáles son obligatorias y la función de `gateways.ts` que lo construye. */
const GATEWAY_PRESETS: Readonly<Record<string, Preset>> = Object.freeze({
  bedrock: {
    keys: ["region", "models"],
    required: ["region", "models"],
    build: (secret, o) =>
      bedrockGateway(secret, o as unknown as Parameters<typeof bedrockGateway>[1]),
  },
  anthropic: { keys: [], required: [], build: (secret, o) => anthropicGateway(secret, o) },
  openai: { keys: [], required: [], build: (secret, o) => openaiGateway(secret, o) },
  "openai-compatible": {
    keys: ["upstream", "base_path"],
    required: ["upstream"],
    build: (secret, o) =>
      openaiCompatibleGateway(
        secret,
        o as unknown as Parameters<typeof openaiCompatibleGateway>[1],
      ),
  },
  gemini: {
    keys: ["models"],
    required: ["models"],
    build: (secret, o) =>
      geminiGateway(secret, o as unknown as Parameters<typeof geminiGateway>[1]),
  },
  azure: {
    keys: ["resource"],
    required: ["resource"],
    build: (secret, o) =>
      azureOpenaiGateway(secret, o as unknown as Parameters<typeof azureOpenaiGateway>[1]),
  },
  openrouter: { keys: [], required: [], build: (secret, o) => openrouterGateway(secret, o) },
  groq: { keys: [], required: [], build: (secret, o) => groqGateway(secret, o) },
  mistral: { keys: [], required: [], build: (secret, o) => mistralGateway(secret, o) },
  deepseek: { keys: [], required: [], build: (secret, o) => deepseekGateway(secret, o) },
  xai: { keys: [], required: [], build: (secret, o) => xaiGateway(secret, o) },
  litellm: {
    keys: ["upstream", "base_path"],
    required: ["upstream"],
    build: (secret, o) =>
      litellmGateway(secret, o as unknown as Parameters<typeof litellmGateway>[1]),
  },
});

/** Las pasarelas que filtran por modelo (la lista `models` de su ruta): con
 * ellas el id del modelo es obligatorio (ADR-033 D2). */
export const MODEL_ID_REQUIRED_PRESETS: readonly string[] = Object.freeze(
  Object.entries(GATEWAY_PRESETS)
    .filter(([, preset]) => preset.required.includes(MODELS_KEY))
    .map(([name]) => name),
);
/** Los runtimes que siempre construyen ellos el modelo, y los que lo
 * construyen sólo cuando no tienen `entrypoint` (sin código tuyo). */
export const MODEL_BUILDING_RUNTIMES: readonly string[] = Object.freeze(["opencode", "hermes"]);
export const DEFAULT_AGENT_RUNTIMES: readonly string[] = Object.freeze([
  "deepagents",
  "strands",
  "strands-harness",
]);
const MODEL_ID_PATH = "model.id";

/** Lo que decide si hace falta el id del modelo. */
export interface ModelIdRequirement {
  readonly runtime: string;
  readonly entrypoint: boolean;
  readonly gatewayPreset: string | undefined;
  readonly detected?: boolean | undefined;
}

/** Si Rayito necesita el id del modelo: lo pide una pasarela que filtra por
 * modelo o un runtime que construye el modelo
 * (`testdata/agent-model-optional/requirement.json`). */
export function modelIdRequired(requirement: ModelIdRequirement): boolean {
  const { runtime, entrypoint, gatewayPreset } = requirement;
  if (
    (gatewayPreset !== undefined && MODEL_ID_REQUIRED_PRESETS.includes(gatewayPreset)) ||
    MODEL_BUILDING_RUNTIMES.includes(runtime)
  ) {
    return true;
  }
  return DEFAULT_AGENT_RUNTIMES.includes(runtime) && !entrypoint;
}

/** `AgentProjectError` en `model.id` si falta y hace falta: `not_detected` si
 * el proyecto sale de la detección y `missing_key` si sale de un fichero. */
export function requireModelId(modelId: string | undefined, requirement: ModelIdRequirement): void {
  if (modelId !== undefined || !modelIdRequired(requirement)) {
    return;
  }
  if (requirement.detected === true) {
    throw fail(
      MODEL_ID_PATH,
      "not_detected",
      "no se encontró el id del modelo en el código: escríbelo como literal, pásalo con " +
        "--model-id o define [model] id en rayito-agent.toml (rayito agent detect --write)",
    );
  }
  throw fail(
    MODEL_ID_PATH,
    "missing_key",
    "es obligatoria con esta pasarela o con un runtime que construye el modelo",
  );
}

/** Las claves de un preset de pasarela (además de `secret` y `rate_per_minute`):
 * lo que `AgentProject.detect` tiene que rellenar con la región y los modelos. */
export function gatewayPresetKeys(preset: string): readonly string[] {
  return Object.hasOwn(GATEWAY_PRESETS, preset) ? (GATEWAY_PRESETS[preset]?.keys ?? []) : [];
}

/** Las claves obligatorias de un preset de pasarela (las demás son
 * opcionales y la detección las deja fuera). */
export function gatewayPresetRequiredKeys(preset: string): readonly string[] {
  return Object.hasOwn(GATEWAY_PRESETS, preset) ? (GATEWAY_PRESETS[preset]?.required ?? []) : [];
}

/** El nombre de cada opción del fichero (snake_case) en `gateways.ts`. */
const GATEWAY_OPTION_NAMES: Readonly<Record<string, string>> = Object.freeze({
  base_path: "basePath",
  [RATE_KEY]: "ratePerMinute",
});

function readGateways(root: Table): Record<string, SecretGateway> {
  return Object.fromEntries(
    root.entries("gateways").map(([name, entry]) => [name, readGateway(entry)]),
  );
}

function readGateway(entry: Table): SecretGateway {
  const preset = entry.text("preset", true) as string;
  const rate = entry.integer(RATE_KEY);
  if (preset === CUSTOM_GATEWAY_PRESET) {
    return readCustomGateway(entry, rate);
  }
  const spec = Object.hasOwn(GATEWAY_PRESETS, preset) ? GATEWAY_PRESETS[preset] : undefined;
  if (spec === undefined) {
    const allowed = [...Object.keys(GATEWAY_PRESETS), CUSTOM_GATEWAY_PRESET];
    throw fail(entry.at("preset"), "invalid_value", `debe ser uno de ${allowed.join(", ")}`);
  }
  const secret = secretName(entry.text("secret", true), entry.at("secret"));
  const options: AnyOptions = {};
  for (const key of spec.keys) {
    const value = key === "models" ? entry.texts(key) : entry.text(key);
    if (value === undefined && spec.required.includes(key)) {
      throw fail(entry.at(key), "missing_key", "es obligatoria con este preset");
    }
    if (value !== undefined) {
      options[GATEWAY_OPTION_NAMES[key] ?? key] = value;
    }
  }
  entry.finish();
  if (rate !== undefined) {
    options[GATEWAY_OPTION_NAMES[RATE_KEY] as string] = rate;
  }
  return guard(entry.path, () => spec.build(secret, options));
}

function readCustomGateway(entry: Table, rate: number | undefined): SecretGateway {
  const upstream = entry.text("upstream", true) as string;
  const headers = entry.table("headers");
  if (headers === undefined) {
    throw fail(entry.at("headers"), "missing_key", "es obligatoria con preset 'custom'");
  }
  const secrets: Record<string, string> = {};
  for (const header of headers.keys().sort()) {
    secrets[header] = secretName(headers.raw(header), headers.at(header));
  }
  const allow = readAllow(entry.raw("allow"), entry.at("allow"));
  entry.finish();
  return guard(
    entry.path,
    () => new SecretGateway({ upstream, headers: secrets, allow, ratePerMinute: rate }),
  );
}

function readAllow(value: unknown, path: string): Array<readonly [string, string]> {
  if (value === undefined || value === null) {
    throw fail(path, "missing_key", "es obligatoria con preset 'custom'");
  }
  if (!Array.isArray(value)) {
    throw fail(path, "invalid_type", "debe ser una lista de [método, ruta]");
  }
  return value.map((rule) => {
    if (
      !Array.isArray(rule) ||
      rule.length !== 2 ||
      !rule.every((part) => typeof part === "string")
    ) {
      throw fail(path, "invalid_type", "cada regla debe ser [método, ruta]");
    }
    return [rule[0] as string, rule[1] as string] as const;
  });
}

// ---------------------------------------------------------------------- writing

function relativeTo(path: string, baseDir: string): string {
  const shown = relative(resolve(baseDir), resolve(path)).split(sep).join("/");
  return shown === "" ? "." : shown;
}

function sourceToObject(project: AgentProject): Record<string, unknown> {
  const table: Record<string, unknown> = {};
  if (project.source !== undefined) {
    table.path = relativeTo(project.source, project.baseDir);
  }
  if (project.requirements !== undefined) {
    table.requirements = relativeTo(project.requirements, project.baseDir);
  }
  if (project.exclude.length > 0) {
    table.exclude = [...project.exclude];
  }
  return table;
}

/** `[model]` canónico: `id` y los nombres de variable sólo si están
 * puestos, así que un proyecto con id se escribe igual que antes. */
function modelToObject(model: AgentModel): Record<string, unknown> {
  const table: Record<string, unknown> = { provider: model.provider };
  if (model.id !== undefined) {
    table.id = model.id;
  }
  table.gateway = model.gateway;
  if (model.region !== undefined) {
    table.region = model.region;
  }
  if (model.basePath !== "") {
    table.base_path = model.basePath;
  }
  if (!model.promptCaching) {
    table.prompt_caching = false;
  }
  return { ...table, ...model.envNames() };
}

function secretEnvToObject(project: AgentProject): Record<string, unknown> {
  const table: Record<string, unknown> = {};
  for (const [name, value] of sortedEntries(project.secretEnv)) {
    if (typeof value === "string") {
      table[name] = value;
      continue;
    }
    const reference: Record<string, unknown> = { secret: value.secret };
    if (value.key !== undefined) {
      reference.key = value.key;
    }
    if (value.versionStage !== undefined) {
      reference.version_stage = value.versionStage;
    }
    table[name] = reference;
  }
  return table;
}

function secretEnvFromToObject(project: AgentProject): Array<Record<string, unknown>> {
  return project.secretEnvFrom.map((source) => {
    const entry: Record<string, unknown> = { secret: source.secret };
    if (source.prefix !== "") {
      entry.prefix = source.prefix;
    }
    if (source.keys !== undefined) {
      entry.keys = [...source.keys];
    }
    return entry;
  });
}

function secretRefName(value: SecretLike): string {
  if (typeof value === "string") {
    return value;
  }
  if (value.versionId !== undefined || value.versionStage !== undefined) {
    throw new InvalidArgumentError(
      "una pasarela con un SecretRef de versión fija no se puede escribir en el fichero",
    );
  }
  return value.name;
}

function sortedEntries<T>(record: Readonly<Record<string, T>>): Array<[string, T]> {
  return Object.entries(record).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
}

function gatewaysToObject(
  gateways: Readonly<Record<string, SecretGateway>>,
): Record<string, unknown> {
  return Object.fromEntries(
    sortedEntries(gateways).map(([name, gateway]) => {
      const table: Record<string, unknown> = {
        preset: CUSTOM_GATEWAY_PRESET,
        upstream: gateway.upstream,
        headers: Object.fromEntries(
          sortedEntries(gateway.headers).map(([header, ref]) => [header, secretRefName(ref)]),
        ),
        allow: gateway.allow.map((rule) => [rule.method, rule.path]),
      };
      if (gateway.ratePerMinute !== 0) {
        table[RATE_KEY] = gateway.ratePerMinute;
      }
      return [name, table];
    }),
  );
}

function permissionsToObject(permissions: AgentPermissions): Record<string, unknown> {
  const table: Record<string, unknown> = {};
  if (permissions.default !== new AgentPermissions().default) {
    table.default = permissions.default;
  }
  if (Object.keys(permissions.tools).length > 0) {
    table.tools = Object.fromEntries(
      sortedEntries(permissions.tools).map(([tool, rule]) => [
        tool,
        typeof rule === "string" ? rule : { ...rule },
      ]),
    );
  }
  return table;
}

function mcpToObject(server: McpServer): Record<string, unknown> {
  if (server instanceof McpRemote) {
    const remote: Record<string, unknown> = { type: "remote", gateway: server.gateway };
    if (server.path !== "/") {
      remote.path = server.path;
    }
    return remote;
  }
  const local: Record<string, unknown> = { type: "local", command: [...server.command] };
  if (Object.keys(server.envs).length > 0) {
    local.envs = { ...server.envs };
  }
  if (server.timeoutMs !== undefined) {
    local.timeout_seconds = server.timeoutMs / MS_PER_SECOND;
  }
  return local;
}

function subagentToObject(agent: SubAgent): Record<string, unknown> {
  const table: Record<string, unknown> = {
    description: agent.description,
    instructions: agent.instructions,
  };
  if (agent.model !== undefined) {
    table.model = agent.model;
  }
  if (agent.permissions !== undefined) {
    table.permissions = permissionsToObject(agent.permissions);
  }
  return table;
}

function agentToObject(spec: AgentSpec): Record<string, unknown> {
  const table: Record<string, unknown> = {};
  if (spec.instructions !== undefined) {
    table.instructions = spec.instructions;
  }
  if (spec.smallModel !== undefined) {
    table.small_model = spec.smallModel;
  }
  if (spec.runtimeVersion !== undefined) {
    table.runtime_version = spec.runtimeVersion;
  }
  const permissions = permissionsToObject(spec.permissions);
  if (Object.keys(permissions).length > 0) {
    table.permissions = permissions;
  }
  if (Object.keys(spec.mcp).length > 0) {
    table.mcp = Object.fromEntries(
      sortedEntries(spec.mcp).map(([name, server]) => [name, mcpToObject(server)]),
    );
  }
  if (Object.keys(spec.agents).length > 0) {
    table.agents = Object.fromEntries(
      sortedEntries(spec.agents).map(([name, agent]) => [name, subagentToObject(agent)]),
    );
  }
  if (spec.rawConfig !== undefined && Object.keys(spec.rawConfig).length > 0) {
    table.raw_config = { ...spec.rawConfig };
  }
  return table;
}

function limitsToObject(limits: AgentLimits): Record<string, unknown> {
  const defaults = new AgentLimits();
  const table: Record<string, unknown> = {};
  if (limits.maxSteps !== defaults.maxSteps) {
    table.max_steps = limits.maxSteps;
  }
  if (limits.timeoutMs !== defaults.timeoutMs) {
    table.timeout_seconds = limits.timeoutMs / MS_PER_SECOND;
  }
  if (limits.maxOutputBytes !== defaults.maxOutputBytes) {
    table.max_output_bytes = limits.maxOutputBytes;
  }
  if (limits.maxTotalTokens !== defaults.maxTotalTokens) {
    table.max_total_tokens = limits.maxTotalTokens ?? 0;
  }
  return table;
}

function idleSeconds(idle: IdlePolicy | null): number {
  if (idle === null) {
    return 0;
  }
  if (idle.suspendedDurationSeconds !== undefined || !idle.autoResume) {
    throw new InvalidArgumentError(
      "ServeOptions.idle con suspendedDurationSeconds o sin autoResume no se puede escribir en el fichero",
    );
  }
  return idle.maxIdleSeconds;
}

function runtimeToObject(project: AgentProject): Record<string, unknown> {
  const table = runtimeToOptions(project.runtime);
  if (project.graph !== undefined) {
    delete table[ENTRYPOINT_KEY];
    table[GRAPH_KEY] = project.graph;
  }
  for (const [key, value] of [
    ["language", project.language],
    ["framework", project.framework],
    ["python_version", project.versions.python],
    ["node_version", project.versions.node],
  ] as const) {
    if (value !== undefined && !(key in table)) {
      table[key] = value;
    }
  }
  return table;
}

function scalingToObject(scaling: ScalingOptions): Record<string, unknown> {
  const defaults = new ScalingOptions();
  const table: Record<string, unknown> = {};
  for (const [key, field] of [
    ["min_vms", "minVms"],
    ["max_vms", "maxVms"],
    ["warm_vms", "warmVms"],
    ["sessions", "sessions"],
  ] as const) {
    if (scaling[field] !== defaults[field]) {
      table[key] = scaling[field];
    }
  }
  if (scaling.scaleInAfterMs !== defaults.scaleInAfterMs) {
    table.scale_in_after_seconds = scaling.scaleInAfterMs / MS_PER_SECOND;
  }
  return table;
}

function deployToObject(project: AgentProject): Record<string, unknown> {
  const table: Record<string, unknown> = {};
  for (const [key, value] of [
    ["region", project.region],
    ["bucket", project.bucket],
    ["base_image_version", project.baseImageVersion],
  ] as const) {
    if (value !== undefined) {
      table[key] = value;
    }
  }
  return table;
}

function serveToObject(serve: ServeOptions): Record<string, unknown> {
  const table: Record<string, unknown> = {};
  if (serve.port !== AGENT_SERVICE_PORT) {
    table.port = serve.port;
  }
  if (serve.timeoutMs !== DEFAULT_SERVE_TIMEOUT_MS) {
    table.timeout_seconds = serve.timeoutMs / MS_PER_SECOND;
  }
  if (serve.maxConcurrency !== AGENT_SERVICE_MAX_CONCURRENCY) {
    table.max_concurrency = serve.maxConcurrency;
  }
  if (serve.idle !== undefined) {
    table.idle_seconds = idleSeconds(serve.idle);
  }
  if (Object.keys(serve.metadata).length > 0) {
    table.metadata = Object.fromEntries(sortedEntries(serve.metadata));
  }
  if (serve.queueMax !== AGENT_SERVICE_QUEUE_MAX_DEFAULT) {
    table.queue_max = serve.queueMax;
  }
  if (serve.queueTimeoutMs !== DEFAULT_QUEUE_TIMEOUT_MS) {
    table.queue_timeout_seconds = serve.queueTimeoutMs / MS_PER_SECOND;
  }
  return table;
}

function networkToObject(project: AgentProject): Record<string, unknown> {
  const table: Record<string, unknown> = {};
  if (project.allowInternetAccess) {
    table.allow_internet_access = true;
  }
  if (project.egress !== undefined) {
    table.egress = [...project.egress];
  }
  if (project.network !== undefined) {
    if (project.network.allowOut.length > 0) {
      table.allow_out = [...project.network.allowOut];
    }
    if (project.network.denyOut.length > 0) {
      table.deny_out = [...project.network.denyOut];
    }
  }
  return table;
}

function imageToObject(project: AgentProject): Record<string, unknown> {
  const table: Record<string, unknown> = {};
  if (project.base !== DEFAULT_AGENT_TEMPLATE_BASE) {
    table.base = project.base;
  }
  if (project.template !== undefined) {
    table.template = project.template;
  }
  if (project.memoryMib !== AGENT_MIN_MEMORY_MIB) {
    table.memory_mib = project.memoryMib;
  }
  return table;
}
