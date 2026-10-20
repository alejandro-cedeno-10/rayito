/**
 * El plan de `--target agentcore` y las reglas que lo validan (ADR-031, D5,
 * D6 y D10). Puro: no importa `@aws-sdk/*`, no llama a AWS y no toca el
 * disco; la fachada le da el proyecto, las opciones, la región, el entorno y
 * la etiqueta de la imagen. Los vectores compartidos con Python están en
 * `testdata/agentcore-deploy/plan-vectors.json`.
 *
 * Las opciones de la llamada se validan en el orden de
 * `contract.json.optionsTable.description`: región, opciones de MicroVMs que no
 * aplican, proveedor del modelo, política de red, nombre, ciclo de vida,
 * etiquetas y entorno.
 */

import { BUCKET_ENV_VAR, type Environ } from "../agent/aws-resolve.js";
import { BASE_IMAGE_VERSION_LATEST } from "../agent/deploy-domain.js";
import type { AgentDeployOptions } from "../agent/deploy-plan.js";
import { bucketSourceOf, projectBucket } from "../agent/deploy-plan.js";
import { AGENT_FLEET_WARM_READY_TIMEOUT_MS } from "../agent/fleet-base.js";
import { type AgentProject, ServeOptions } from "../agent/project.js";
import { templateName } from "../agent/project-service.js";
import { ScalingOptions } from "../agent/scaling-domain.js";
import { AgentCoreError, InvalidArgumentError } from "../errors.js";
import {
  AGENT_MIN_MEMORY_MIB,
  AGENTCORE_ENVIRONMENT_VALUE_MAX_CHARS,
  AGENTCORE_IDLE_SESSION_TIMEOUT_DEFAULT_SECONDS,
  AGENTCORE_IMAGE_RETENTION_COUNT,
  AGENTCORE_LIFECYCLE_MAX_SECONDS,
  AGENTCORE_LIFECYCLE_MIN_SECONDS,
  AGENTCORE_MAX_ENVIRONMENT_VARIABLES,
  AGENTCORE_MAX_TAGS,
  AGENTCORE_NETWORK_MODE,
  AGENTCORE_SUPPORTED_REGIONS,
} from "../limits.js";
import { FIXED_TAG_KEYS } from "../stacks/model.js";
import { formatTemplate } from "../template-text.js";
import { MS_PER_SECOND } from "../units.js";
import { usesEnvCredentials } from "./env-credentials.js";
import { COST_MESSAGES, type SecurityKey, type WarningKey } from "./messages.js";
import type {
  AgentCoreDeployPlan,
  AgentCoreLifecycle,
  AgentCoreOptions,
  AgentCoreStep,
  BucketSource,
  ResolvedAgentCoreOptions,
} from "./models.js";
import {
  buildProjectNameFor,
  repositoryNameFor,
  runtimeNameFor,
  stackNameFor,
  validateAgentName,
} from "./names.js";

/** Las claves de `tags` de `CreateAgentRuntime` (`AWS_API_NOTES.md`, «Despliegue en AgentCore Runtime»): clave de 1 a 128 caracteres, valor de 0 a 256. */
export const TAG_KEY_MIN_CHARS = 1;
export const TAG_KEY_MAX_CHARS = 128;
export const TAG_VALUE_MAX_CHARS = 256;
export const TAG_CHARACTERS = /^[a-zA-Z0-9\s._:/=+@-]*$/;
/** `constants.runtimeRegionEnvironmentVariables`: las dos variables de región que ganan al entorno del proyecto. */
export const RUNTIME_REGION_VARIABLES: readonly string[] = ["AWS_REGION", "AWS_DEFAULT_REGION"];
export const PLANNED_ACTION = "planned";
export const BEDROCK_PROVIDER = "bedrock";
const OPTION_IDLE = "idle_session_timeout";
const OPTION_MAX = "max_session_lifetime";
const REGION_LIST_SEPARATOR = ", ";
const OPTION_AGENTCORE = "agentcore";

/** Las opciones de MicroVMs que la CLI pone en el proyecto con banderas (`--memory`, `--port`, `--timeout`, `--idle`) y `--token-file`, que no llegan en `AgentDeployOptions`. */
export interface CallFlags {
  readonly minVms?: number | undefined;
  readonly maxVms?: number | undefined;
  readonly warmVms?: number | undefined;
  readonly memoryMib?: number | undefined;
  readonly port?: number | undefined;
  readonly timeoutMs?: number | undefined;
  readonly idleSeconds?: number | undefined;
  readonly tokenFile?: string | undefined;
}

interface OptionCheck {
  readonly label: string;
  readonly reason: string;
  readonly given: boolean;
}

const DEFAULT_SCALING = new ScalingOptions();
const REASON_NO_FIXED_VMS = "AgentCore escala solo: una microVM por sesión, sin VMs fijos";
const REASON_NO_WARM = "AgentCore no tiene plazas calientes";
const REASON_NO_GATEWAY = "no hay pasarela de secretos";
/** Por qué `access_token` y `--token-file` no aplican: AgentCore autentica con IAM. */
export const REASON_IAM_AUTH = "AgentCore autentica con IAM (SigV4), no con un access token";

/** Un valor cuenta como puesto si difiere del valor por defecto, venga del argumento del SDK (`value`) o de una bandera de la CLI (`flag`). */
function isSet(value: number | undefined, flag: number | undefined, fallback: number): boolean {
  return (value !== undefined && value !== fallback) || (flag !== undefined && flag !== fallback);
}

function optionChecks(options: AgentDeployOptions, flags: CallFlags): OptionCheck[] {
  const scaling = options.scaling ?? DEFAULT_SCALING;
  return [
    {
      label: "scaling.min_vms",
      reason: REASON_NO_FIXED_VMS,
      given: isSet(scaling.minVms, flags.minVms, DEFAULT_SCALING.minVms),
    },
    {
      label: "scaling.max_vms",
      reason: "el tope lo pone la cuota de sesiones activas de la cuenta",
      given: isSet(scaling.maxVms, flags.maxVms, DEFAULT_SCALING.maxVms),
    },
    {
      label: "scaling.warm_vms",
      reason: REASON_NO_WARM,
      given: isSet(scaling.warmVms, flags.warmVms, DEFAULT_SCALING.warmVms),
    },
    {
      label: "scaling.scale_in_after_seconds",
      reason: "usa idle_session_timeout_seconds / --idle-session-timeout",
      given: scaling.scaleInAfterMs !== DEFAULT_SCALING.scaleInAfterMs,
    },
    {
      label: "memory_mib",
      reason: "cada sesión usa hasta 2 vCPU y 8 GB y se cobra por consumo real",
      given: isSet(undefined, flags.memoryMib, AGENT_MIN_MEMORY_MIB),
    },
    {
      label: "serve.port",
      reason: "el contenedor de AgentCore sirve en el 8080",
      given: isSet(undefined, flags.port, DEFAULT_SERVE.port),
    },
    {
      label: "serve.timeout_seconds",
      reason: "usa max_session_lifetime_seconds / --max-session-lifetime",
      given: isSet(undefined, flags.timeoutMs, DEFAULT_SERVE.timeoutMs),
    },
    {
      label: "serve.idle",
      reason: "usa idle_session_timeout_seconds / --idle-session-timeout",
      given: flags.idleSeconds !== undefined,
    },
    {
      label: "secret_names",
      reason: `${REASON_NO_GATEWAY}; para dejar leer un secreto usa readable_secrets / --readable-secret`,
      given: Object.keys(options.secretNames).length > 0,
    },
    {
      label: "create_secret_from_env",
      reason: REASON_NO_GATEWAY,
      given: options.createSecretFromEnv,
    },
    {
      label: "access_token",
      reason: REASON_IAM_AUTH,
      given: options.accessToken !== undefined || flags.tokenFile !== undefined,
    },
    {
      label: "keep_on_failure",
      reason: "AgentCore siempre conserva lo creado si falla; bórralo con rayito agent delete",
      given: options.keepOnFailure,
    },
    ...["base_image_version", "ensure_base_image", "base_publisher"].map((label) => ({
      label,
      reason: "AgentCore no usa la imagen base de MicroVMs",
      given:
        (label === "base_image_version" && options.baseImageVersion !== undefined) ||
        (label === "ensure_base_image" && !options.ensureBaseImage) ||
        (label === "base_publisher" && options.basePublisher !== undefined),
    })),
    {
      label: "session_index",
      reason: "AgentCore enruta las sesiones por su id",
      given: options.sessionIndex !== undefined,
    },
    { label: "pool_backend", reason: REASON_NO_WARM, given: options.poolBackend !== undefined },
    {
      label: "warm_timeout_seconds",
      reason: REASON_NO_WARM,
      given: options.warmTimeoutMs !== AGENT_FLEET_WARM_READY_TIMEOUT_MS,
    },
  ];
}

/**
 * Lanza `option_not_supported` con la primera opción de MicroVMs de la llamada
 * (argumento del SDK o bandera de la CLI) que AgentCore no admite, en el
 * orden de la tabla del contrato. Un valor igual al valor por defecto cuenta
 * como no puesto.
 */
export function checkCallOptions(options: AgentDeployOptions, flags: CallFlags = {}): void {
  const found = optionChecks(options, flags).find((check) => check.given);
  if (found !== undefined) {
    throw new AgentCoreError("option_not_supported", { option: found.label, reason: found.reason });
  }
}

/** Lanza `option_requires_agentcore` si se pasaron opciones de AgentCore con otro destino. */
export function rejectAgentCoreOptions(
  agentcore: AgentCoreOptions | undefined,
  option = OPTION_AGENTCORE,
): void {
  if (agentcore !== undefined) {
    throw new AgentCoreError("option_requires_agentcore", { option });
  }
}

/** La región soportada o `unsupported_region`. */
export function requireSupportedRegion(region: string): string {
  if (!(AGENTCORE_SUPPORTED_REGIONS as readonly string[]).includes(region)) {
    throw new AgentCoreError("unsupported_region", {
      region,
      regions: AGENTCORE_SUPPORTED_REGIONS.join(REGION_LIST_SEPARATOR),
    });
  }
  return region;
}

const DEFAULT_SERVE = new ServeOptions();

/** Los avisos por valores del fichero de proyecto que AgentCore no usa, en el orden del contrato; un valor igual al de por defecto no avisa. */
export function fileWarnings(project: AgentProject): WarningKey[] {
  const { scaling, serve } = project;
  const warnings: [WarningKey, boolean][] = [
    [
      "fileScaling",
      scaling.minVms !== DEFAULT_SCALING.minVms ||
        scaling.maxVms !== DEFAULT_SCALING.maxVms ||
        scaling.warmVms !== DEFAULT_SCALING.warmVms ||
        scaling.scaleInAfterMs !== DEFAULT_SCALING.scaleInAfterMs,
    ],
    ["fileMemory", project.memoryMib !== AGENT_MIN_MEMORY_MIB],
    ["filePort", serve.port !== DEFAULT_SERVE.port],
    ["fileTimeout", serve.timeoutMs !== DEFAULT_SERVE.timeoutMs],
    ["fileIdle", serve.idle !== undefined],
    [
      "fileBaseImage",
      project.baseImageVersion !== undefined &&
        project.baseImageVersion !== BASE_IMAGE_VERSION_LATEST,
    ],
  ];
  return warnings.filter(([, applies]) => applies).map(([key]) => key);
}

function hasEgressPolicy(project: AgentProject): string | undefined {
  if ((project.egress ?? []).length > 0) {
    return "egress";
  }
  if ((project.network?.allowOut ?? []).length > 0) {
    return "allow_out";
  }
  return (project.network?.denyOut ?? []).length > 0 ? "deny_out" : undefined;
}

/** El proveedor del modelo (`provider_unsupported` salvo `bedrock`) y la política de red (`network_policy_unsupported`). */
export function checkProject(project: AgentProject): void {
  const { provider } = project.spec.model;
  if (provider !== BEDROCK_PROVIDER) {
    throw new AgentCoreError("provider_unsupported", { provider });
  }
  const what = hasEgressPolicy(project);
  if (what !== undefined) {
    throw new AgentCoreError("network_policy_unsupported", { what });
  }
}

function secondsOf(milliseconds: number | undefined, option: string): number | undefined {
  if (milliseconds === undefined) {
    return undefined;
  }
  const seconds = milliseconds / MS_PER_SECOND;
  if (!Number.isInteger(seconds)) {
    throw lifecycleInvalid(option);
  }
  return seconds;
}

function lifecycleInvalid(option: string): AgentCoreError {
  return new AgentCoreError("lifecycle_invalid", {
    option,
    min: AGENTCORE_LIFECYCLE_MIN_SECONDS,
    max: AGENTCORE_LIFECYCLE_MAX_SECONDS,
  });
}

function inLifecycleRange(seconds: number | undefined, option: string): void {
  if (
    seconds !== undefined &&
    (seconds < AGENTCORE_LIFECYCLE_MIN_SECONDS || seconds > AGENTCORE_LIFECYCLE_MAX_SECONDS)
  ) {
    throw lifecycleInvalid(option);
  }
}

function stringList(value: readonly string[] | undefined, label: string): readonly string[] {
  if (value === undefined) {
    return [];
  }
  if (!Array.isArray(value) || !value.every((item) => typeof item === "string" && item !== "")) {
    throw new InvalidArgumentError(
      `AgentCoreOptions.${label} debe ser una lista de cadenas no vacías`,
    );
  }
  return [...new Set(value)];
}

/** `AgentCoreOptions` con los tipos comprobados y los plazos en segundos enteros; los rangos los valida `resolveLifecycle`. */
export function resolveAgentCoreOptions(
  agentcore: AgentCoreOptions | undefined,
): ResolvedAgentCoreOptions {
  const given = agentcore ?? {};
  return {
    bedrockModels: stringList(given.bedrockModels, "bedrockModels"),
    readableSecrets: stringList(given.readableSecrets, "readableSecrets"),
    idleSessionTimeoutSeconds: secondsOf(given.idleSessionTimeoutMs, OPTION_IDLE),
    maxSessionLifetimeSeconds: secondsOf(given.maxSessionLifetimeMs, OPTION_MAX),
    tags: { ...(given.tags ?? {}) },
  };
}

/**
 * La vida efectiva de una sesión: `maxSessionLifetimeSeconds` por defecto el
 * máximo de AgentCore (8 h) y `idleSessionTimeoutSeconds` por defecto 15 min
 * sin pasar de él. Fuera de 60-28800 es `lifecycle_invalid`; idle mayor que max,
 * `lifecycle_order`.
 */
export function resolveLifecycle(options: ResolvedAgentCoreOptions): AgentCoreLifecycle {
  inLifecycleRange(options.idleSessionTimeoutSeconds, OPTION_IDLE);
  inLifecycleRange(options.maxSessionLifetimeSeconds, OPTION_MAX);
  const max = options.maxSessionLifetimeSeconds ?? AGENTCORE_LIFECYCLE_MAX_SECONDS;
  const idle =
    options.idleSessionTimeoutSeconds ??
    Math.min(AGENTCORE_IDLE_SESSION_TIMEOUT_DEFAULT_SECONDS, max);
  if (idle > max) {
    throw new AgentCoreError("lifecycle_order", { idle, max });
  }
  return { idleSessionTimeoutSeconds: idle, maxSessionLifetimeSeconds: max };
}

/** Las etiquetas de `--tag`: claves y valores con los caracteres de AWS y, con las tres fijas de la pila, hasta 50. */
export function validateTags(tags: Readonly<Record<string, string>>): void {
  const entries = Object.entries(tags);
  const valid = entries.every(
    ([key, value]) =>
      key.length >= TAG_KEY_MIN_CHARS &&
      key.length <= TAG_KEY_MAX_CHARS &&
      value.length <= TAG_VALUE_MAX_CHARS &&
      TAG_CHARACTERS.test(key) &&
      TAG_CHARACTERS.test(value),
  );
  const total = new Set([...Object.keys(tags), ...FIXED_TAG_KEYS]).size;
  if (!valid || total > AGENTCORE_MAX_TAGS) {
    throw new AgentCoreError("tags_invalid", { max: AGENTCORE_MAX_TAGS });
  }
}

/**
 * `environmentVariables` del runtime: el entorno del proyecto más las dos
 * variables de región con la región del despliegue (ganan al proyecto), con las
 * claves ordenadas. Hasta 50 variables de 5000 caracteres; nunca secretos.
 */
export function runtimeEnvironment(project: AgentProject, region: string): Record<string, string> {
  const merged: Record<string, string> = { ...project.env };
  for (const variable of RUNTIME_REGION_VARIABLES) {
    merged[variable] = region;
  }
  const names = Object.keys(merged).sort();
  const tooLong = names.some(
    (name) => (merged[name] ?? "").length > AGENTCORE_ENVIRONMENT_VALUE_MAX_CHARS,
  );
  if (names.length > AGENTCORE_MAX_ENVIRONMENT_VARIABLES || tooLong) {
    throw new AgentCoreError("env_invalid", {
      maxVariables: AGENTCORE_MAX_ENVIRONMENT_VARIABLES,
      maxChars: AGENTCORE_ENVIRONMENT_VALUE_MAX_CHARS,
    });
  }
  return Object.fromEntries(names.map((name) => [name, merged[name] ?? ""]));
}

function sortedRecord(values: Readonly<Record<string, string>>): Record<string, string> {
  return Object.fromEntries(
    Object.entries(values).sort(([left], [right]) => (left < right ? -1 : 1)),
  );
}

const NAMED_BUCKET_SOURCES: readonly BucketSource[] = ["argument", "project_file", "environment"];

function bucketPlan(
  options: AgentDeployOptions,
  project: AgentProject,
  environ: Environ,
): { readonly bucket: string | null; readonly source: BucketSource } {
  const fileBucket = projectBucket(project);
  const source = bucketSourceOf(options, fileBucket, environ) as BucketSource | "discovered";
  const named = options.bucket || fileBucket || environ[BUCKET_ENV_VAR];
  return source === "discovered" || !named || !NAMED_BUCKET_SOURCES.includes(source)
    ? { bucket: null, source: "discover" }
    : { bucket: named, source };
}

function securityKeys(project: AgentProject, readableSecrets: readonly string[]): SecurityKey[] {
  const keys: SecurityKey[] = ["noGateway", "bedrockModels"];
  if (readableSecrets.length > 0) {
    keys.push("readableSecrets");
  }
  keys.push("publicNetwork", "sessionIsolation");
  if (usesEnvCredentials(project.runtime)) {
    keys.push("envCredentials");
  }
  return keys;
}

/** Las líneas de coste de lista del plan, en el orden del contrato. */
export function costLines(): string[] {
  return [
    COST_MESSAGES.runtime,
    COST_MESSAGES.build,
    formatTemplate(COST_MESSAGES.image, { retained: AGENTCORE_IMAGE_RETENTION_COUNT }),
    COST_MESSAGES.logs,
    COST_MESSAGES.model,
  ];
}

export interface PlanInput {
  readonly project: AgentProject;
  readonly options: AgentDeployOptions;
  readonly agentcore: AgentCoreOptions | undefined;
  readonly region: string;
  readonly environ: Environ;
  readonly flags?: CallFlags | undefined;
}

/** El plan sin la etiqueta de la imagen, que sale del árbol de `renderExport` y lo calcula quien lo renderiza. */
export type PlanWithoutImageTag = Omit<AgentCoreDeployPlan, "imageTag">;

function plannedSteps(names: {
  readonly stack: string;
  readonly repository: string;
  readonly runtime: string;
}): AgentCoreStep[] {
  return [
    { resource: "stack", name: names.stack, action: PLANNED_ACTION, status: null },
    { resource: "image", name: names.repository, action: PLANNED_ACTION, status: null },
    { resource: "runtime", name: names.runtime, action: PLANNED_ACTION, status: null },
  ];
}

/** El plan de desplegar `project` en AgentCore sin la etiqueta de la imagen (puro): valida todo lo que no necesita AWS y lista los tres pasos. */
export function planWithoutImageTag(input: PlanInput): PlanWithoutImageTag {
  const { project, options, region } = input;
  requireSupportedRegion(region);
  checkCallOptions(options, input.flags);
  checkProject(project);
  const name = validateAgentName(templateName(project));
  const resolved = resolveAgentCoreOptions(input.agentcore);
  const lifecycle = resolveLifecycle(resolved);
  validateTags(resolved.tags);
  const environment = runtimeEnvironment(project, region);
  const bucket = bucketPlan(options, project, input.environ);
  const models = [...new Set([project.spec.model.id, ...resolved.bedrockModels])];
  const names = {
    stack: stackNameFor(name),
    repository: repositoryNameFor(name),
    runtime: runtimeNameFor(name),
  };
  return {
    target: "agentcore",
    name,
    region,
    runtimeName: names.runtime,
    stackName: names.stack,
    repositoryName: names.repository,
    buildProjectName: buildProjectNameFor(name),
    bucket: bucket.bucket,
    bucketSource: bucket.source,
    network: AGENTCORE_NETWORK_MODE,
    bedrockModels: models,
    readableSecrets: resolved.readableSecrets,
    environment,
    lifecycle,
    tags: sortedRecord(resolved.tags),
    steps: plannedSteps(names),
    security: securityKeys(project, resolved.readableSecrets),
    warnings: fileWarnings(project),
    cost: costLines(),
  };
}

/** El plan completo: `planWithoutImageTag` con la etiqueta de la imagen por contenido. */
export function buildPlan(input: PlanInput & { readonly imageTag: string }): AgentCoreDeployPlan {
  return { ...planWithoutImageTag(input), imageTag: input.imageTag };
}
