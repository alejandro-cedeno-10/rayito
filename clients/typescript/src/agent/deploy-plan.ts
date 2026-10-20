/**
 * Las opciones y el plan de `deployAgent()` (ADR-029, D7 y D8). Espejo de
 * `rayito._agent._deploy_plan`: `AgentDeployOptions` (dónde y cómo se
 * despliega, no qué agente es), `AgentDeployPlan` y la estimación de coste que
 * `planDeploy()` y `rayito agent deploy --dry-run` muestran sin llamar a AWS.
 * Todo es puro salvo `projectFor()` (lee los ficheros del proyecto): ninguno
 * llama a AWS.
 */

import { estimateComputeUsd, MIB_PER_GB } from "../dashboard/domain.js";
import { InvalidArgumentError } from "../errors.js";
import { defineHidden } from "../hidden.js";
import type { DynamoDbIndex } from "../index/dynamodb.js";
import {
  AGENT_SOURCE_DEFAULT_EXCLUDES,
  SNAPSHOT_STORAGE_PRICE_USD_PER_GB_MONTH,
  SNAPSHOT_WRITE_PRICE_USD_PER_GB,
} from "../limits.js";
import { validateAccessToken } from "../payload.js";
import type { PoolBackend } from "../pool/backend.js";
import { ACCESS_TOKEN_ENV_VAR } from "../sandbox/launch.js";
import { BUCKET_ENV_VAR, type Environ } from "./aws-resolve.js";
import {
  BASE_IMAGE_VERSION_LATEST,
  validateBucketHint,
  validateImageVersion,
  validateSecretName,
} from "./deploy-domain.js";
import type { DetectionReport, DetectOverrides } from "./detect-report.js";
import { AGENT_FLEET_WARM_READY_TIMEOUT_MS, fleetSettingsOf } from "./fleet-base.js";
import { AgentProject } from "./project.js";
import { copyProject } from "./project-copy.js";
import { ScalingOptions } from "./scaling-domain.js";

export const SECONDS_PER_HOUR = 3600;
/** Días del mes de la lista de precios (`AWS_API_NOTES.md`, sección de coste:
 * el almacenamiento se cotiza por GB-mes de 720 h). */
export const PRICING_DAYS_PER_MONTH = 30;
export const DAYS_PER_WEEK = 7;
/** Tamaño del snapshot de una imagen de agente (≈ 3,0 GB: el aviso de coste de
 * `rayito agent template build`, medido en AWS real). */
export const AGENT_IMAGE_SNAPSHOT_GB_ESTIMATE = 3.0;
/** Plazo por defecto de un build de imagen, el de `AgentTemplate.build()`. */
export const DEPLOY_BUILD_TIMEOUT_MS_DEFAULT = 1_800_000;
export const PARKED_SLOTS_NOTE =
  "las plazas calientes se aparcan en el pool: cuestan almacenamiento de snapshot, no cómputo";
export const IMAGE_MEMORY_NOTE =
  "la memoria se fija en la imagen: cambiarla construye una versión nueva (almacenamiento " +
  "de snapshot de una semana como mínimo)";

/** Un paso de `deployAgent()` que ya ocurrió: `kind` es de una lista cerrada
 * (`base_image`, `secret_created`, `bucket`, `image`, `fleet`, `warm`) y
 * `detail` un dato corto para mostrar (la pasarela, el origen y el bucket, la
 * versión de la imagen); nunca el valor ni el nombre de un secreto. */
export interface AgentDeployStep {
  readonly kind: string;
  readonly detail: string;
}

export interface AgentDeployOptionsInput {
  readonly bucket?: string | undefined;
  readonly baseImageVersion?: string | undefined;
  readonly secretNames?: Readonly<Record<string, string>> | undefined;
  readonly createSecretFromEnv?: boolean | undefined;
  readonly ensureBaseImage?: boolean | undefined;
  readonly basePublisher?: (() => Promise<void>) | undefined;
  readonly accessToken?: string | undefined;
  readonly keepOnFailure?: boolean | undefined;
  readonly buildImage?: boolean | undefined;
  readonly scaling?: ScalingOptions | undefined;
  readonly sessionIndex?: DynamoDbIndex | undefined;
  readonly poolBackend?: PoolBackend | undefined;
  readonly onBuildLogs?: ((line: string) => void) | undefined;
  readonly onStep?: ((step: AgentDeployStep) => void) | undefined;
  readonly buildTimeoutMs?: number | undefined;
  readonly warmTimeoutMs?: number | undefined;
}

function optionalText(value: unknown, label: string): void {
  if (value !== undefined && (typeof value !== "string" || value === "")) {
    throw new InvalidArgumentError(
      `AgentDeployOptions.${label} debe ser una cadena no vacía o undefined`,
    );
  }
}

function positiveNumber(value: unknown, label: string): number {
  if (typeof value !== "number" || !(value > 0)) {
    throw new InvalidArgumentError(`AgentDeployOptions.${label} debe ser un número > 0`);
  }
  return value;
}

/**
 * Dónde y cómo se despliega (los argumentos de `deployAgent()` que no
 * describen al agente): `bucket` del artefacto de la imagen,
 * `baseImageVersion` (`undefined` = la más reciente de la base), `secretNames`
 * (pasarela → nombre de un secreto que ya existe), `createSecretFromEnv` (crea
 * el que falte desde la variable estándar del proveedor), `ensureBaseImage` y
 * `basePublisher` (quién publica la base si falta), `accessToken`,
 * `keepOnFailure`, `buildImage` (`undefined` = construir si hay bucket;
 * `true` = siempre; `false` = reutilizar la imagen existente), `scaling` (gana
 * al del proyecto), `sessionIndex` (índice de sesiones de los VMs dedicados),
 * `poolBackend` (plazas calientes que sobreviven al proceso), `onBuildLogs`,
 * `onStep` (recibe cada `AgentDeployStep`), `buildTimeoutMs` y `warmTimeoutMs`
 * (espera a que las plazas calientes queden aparcadas).
 *
 * Coste y activación
 * -------------------
 * Activa: nada por sí mismo; el valor es puro. `deployAgent()` es lo que
 *     cuesta: `createSecretFromEnv: true` crea un secreto, `basePublisher` una
 *     imagen base, `sessionIndex` filas de DynamoDB.
 * Recursos y llamadas AWS: ninguno aquí.
 * Coste aproximado: ninguno aquí; ver `deployAgent()`.
 * IAM: ninguna para este valor.
 * Cómo apagarla: los valores por defecto no crean ningún secreto, imagen base
 *     ni índice.
 * Ejemplo:
 *   new AgentDeployOptions({ bucket: "amzn-s3-demo-bucket", secretNames: { bedrock: "mi-clave" } });
 */
export class AgentDeployOptions {
  readonly bucket: string | undefined;
  readonly baseImageVersion: string | undefined;
  readonly secretNames: Readonly<Record<string, string>>;
  readonly createSecretFromEnv: boolean;
  readonly ensureBaseImage: boolean;
  readonly basePublisher: (() => Promise<void>) | undefined;
  /** El token de la flota que se pasó, o `undefined`. No es enumerable:
   * `console.log`, `util.inspect`, `JSON.stringify` y el spread no lo ven. */
  declare readonly accessToken: string | undefined;
  readonly keepOnFailure: boolean;
  readonly buildImage: boolean | undefined;
  readonly scaling: ScalingOptions | undefined;
  readonly sessionIndex: DynamoDbIndex | undefined;
  readonly poolBackend: PoolBackend | undefined;
  readonly onBuildLogs: ((line: string) => void) | undefined;
  readonly onStep: ((step: AgentDeployStep) => void) | undefined;
  readonly buildTimeoutMs: number;
  readonly warmTimeoutMs: number;

  constructor(options: AgentDeployOptionsInput = {}) {
    if (options.bucket !== undefined) {
      validateBucketHint(options.bucket, "AgentDeployOptions.bucket");
    }
    if (options.baseImageVersion !== undefined) {
      validateImageVersion(options.baseImageVersion, "AgentDeployOptions.baseImageVersion");
    }
    optionalText(options.accessToken, "accessToken");
    if (options.accessToken !== undefined) {
      validateAccessToken(options.accessToken);
    }
    const secretNames = options.secretNames ?? {};
    if (
      typeof secretNames !== "object" ||
      secretNames === null ||
      Array.isArray(secretNames) ||
      Object.keys(secretNames).includes("")
    ) {
      throw new InvalidArgumentError("AgentDeployOptions.secretNames debe mapear cadenas");
    }
    for (const value of Object.values(secretNames)) {
      validateSecretName(value, "AgentDeployOptions.secretNames");
    }
    for (const [label, value] of [
      ["createSecretFromEnv", options.createSecretFromEnv],
      ["ensureBaseImage", options.ensureBaseImage],
      ["keepOnFailure", options.keepOnFailure],
    ] as const) {
      if (value !== undefined && typeof value !== "boolean") {
        throw new InvalidArgumentError(`AgentDeployOptions.${label} debe ser un bool`);
      }
    }
    if (options.buildImage !== undefined && typeof options.buildImage !== "boolean") {
      throw new InvalidArgumentError("AgentDeployOptions.buildImage debe ser un bool o undefined");
    }
    if (options.scaling !== undefined && !(options.scaling instanceof ScalingOptions)) {
      throw new InvalidArgumentError("AgentDeployOptions.scaling debe ser un ScalingOptions");
    }
    this.bucket = options.bucket;
    this.baseImageVersion = options.baseImageVersion;
    this.secretNames = Object.freeze({ ...secretNames });
    this.createSecretFromEnv = options.createSecretFromEnv ?? false;
    this.ensureBaseImage = options.ensureBaseImage ?? true;
    this.basePublisher = options.basePublisher;
    this.keepOnFailure = options.keepOnFailure ?? false;
    this.buildImage = options.buildImage;
    this.scaling = options.scaling;
    this.sessionIndex = options.sessionIndex;
    this.poolBackend = options.poolBackend;
    this.onBuildLogs = options.onBuildLogs;
    this.onStep = options.onStep;
    defineHidden(this, "accessToken", options.accessToken);
    this.buildTimeoutMs = positiveNumber(
      options.buildTimeoutMs ?? DEPLOY_BUILD_TIMEOUT_MS_DEFAULT,
      "buildTimeoutMs",
    );
    this.warmTimeoutMs = positiveNumber(
      options.warmTimeoutMs ?? AGENT_FLEET_WARM_READY_TIMEOUT_MS,
      "warmTimeoutMs",
    );
  }
}

/** `options` como `AgentDeployOptions`: uno ya construido se usa tal cual y un objeto se valida. */
export function deployOptionsOf(
  options: AgentDeployOptions | AgentDeployOptionsInput | undefined,
): AgentDeployOptions {
  return options instanceof AgentDeployOptions ? options : new AgentDeployOptions(options);
}

/** Lo que cuesta lo que `deployAgent()` crea, en dólares, a precios de lista
 * (`limits.json`): `runningUsdPerHour` por los VMs `minVms` mientras viven,
 * `parkedUsdPerDay` por las plazas aparcadas (sólo almacenamiento de
 * snapshot), `imageBuildUsd` por una versión nueva de la imagen y
 * `imageStorageUsdPerWeek` por conservarla (mínimo una semana). No incluye
 * los tokens del modelo. */
export interface AgentDeployCostEstimate {
  readonly runningUsdPerHour: number;
  readonly parkedUsdPerDay: number;
  readonly imageBuildUsd: number;
  readonly imageStorageUsdPerWeek: number;
}

/** La pasarela y de dónde sale el nombre de su secreto: `override` (el de
 * `secretNames`), `project` (el del proyecto). El nombre no se muestra. */
export interface AgentPlannedSecret {
  readonly gateway: string;
  readonly source: string;
}

/** Lo que `deployAgent()` haría, calculado sin llamar a AWS: el agente, el
 * escalado, la región (la del entorno, si la hay), de dónde saldrá el bucket,
 * la imagen base, los secretos, el coste y las notas. */
export interface AgentDeployPlan {
  readonly name: string;
  readonly template: string;
  readonly base: string;
  readonly memoryMib: number;
  readonly region: string | undefined;
  readonly scaling: ScalingOptions;
  readonly bucketSource: string;
  readonly buildImage: boolean | undefined;
  readonly secrets: readonly AgentPlannedSecret[];
  readonly tokenSource: string;
  readonly cost: AgentDeployCostEstimate;
  readonly notes: readonly string[];
  readonly detection: unknown;
}

/** El coste de lista de `scaling` con VMs de `memoryMib`. */
export function estimateCost(memoryMib: number, scaling: ScalingOptions): AgentDeployCostEstimate {
  const gigabytes = memoryMib / MIB_PER_GB;
  const storagePerGbDay = SNAPSHOT_STORAGE_PRICE_USD_PER_GB_MONTH / PRICING_DAYS_PER_MONTH;
  return {
    runningUsdPerHour: scaling.minVms * estimateComputeUsd(memoryMib, SECONDS_PER_HOUR),
    parkedUsdPerDay: scaling.warmVms * gigabytes * storagePerGbDay,
    imageBuildUsd: AGENT_IMAGE_SNAPSHOT_GB_ESTIMATE * SNAPSHOT_WRITE_PRICE_USD_PER_GB,
    imageStorageUsdPerWeek: AGENT_IMAGE_SNAPSHOT_GB_ESTIMATE * storagePerGbDay * DAYS_PER_WEEK,
  };
}

/** El proyecto con `AGENT_SOURCE_DEFAULT_EXCLUDES` delante de sus propias
 * exclusiones, sin repetir ninguna. */
export function withDefaultExcludes(project: AgentProject): AgentProject {
  const merged = [...new Set([...AGENT_SOURCE_DEFAULT_EXCLUDES, ...project.exclude])];
  return copyProject(project, { exclude: merged });
}

/** `[deploy].bucket` del fichero de proyecto (`AgentProject.bucket`); un
 * proyecto sin ese campo no fija bucket. */
export function projectBucket(project: AgentProject): string | undefined {
  return project.bucket;
}

/** La versión de la imagen base: el argumento, si no `[deploy]
 * baseImageVersion` del proyecto y, si ninguno la fija o pide `"latest"`,
 * `undefined` (la más reciente). */
export function baseVersionOf(
  options: AgentDeployOptions,
  project: AgentProject,
): string | undefined {
  const version = options.baseImageVersion ?? project.baseImageVersion;
  return version === BASE_IMAGE_VERSION_LATEST ? undefined : version;
}

/** De dónde saldrá el bucket sin preguntar a AWS (el orden de `resolveBucket`). */
export function bucketSourceOf(
  options: AgentDeployOptions,
  projectFile: string | undefined,
  environ: Environ,
): string {
  if (options.bucket) {
    return "argument";
  }
  if (projectFile) {
    return "project_file";
  }
  return environ[BUCKET_ENV_VAR] ? "environment" : "discovered";
}

export function tokenSourceOf(options: AgentDeployOptions, environ: Environ): string {
  if (options.accessToken) {
    return "argument";
  }
  return environ[ACCESS_TOKEN_ENV_VAR] ? "environment" : "generated";
}

export function plannedSecrets(
  project: AgentProject,
  options: AgentDeployOptions,
): AgentPlannedSecret[] {
  const overridden = new Set(Object.keys(options.secretNames));
  return Object.keys(project.gateways)
    .sort()
    .map((gateway) => ({ gateway, source: overridden.has(gateway) ? "override" : "project" }));
}

/** El plan de desplegar `project` con `options` (puro). */
export function buildPlan(
  project: AgentProject,
  options: AgentDeployOptions,
  context: {
    readonly region: string | undefined;
    readonly detection: unknown;
    readonly environ?: Environ | undefined;
  },
): AgentDeployPlan {
  const environ = context.environ ?? process.env;
  const scaling = fleetSettingsOf(project, options.scaling).scaling;
  const notes: string[] = options.buildImage === false ? [] : [IMAGE_MEMORY_NOTE];
  if (scaling.warmVms > 0) {
    notes.push(PARKED_SLOTS_NOTE);
  }
  return {
    name: project.name,
    template: project.template ?? project.name,
    base: project.base,
    memoryMib: project.memoryMib,
    region: context.region,
    scaling,
    bucketSource: bucketSourceOf(options, projectBucket(project), environ),
    buildImage: options.buildImage,
    secrets: plannedSecrets(project, options),
    tokenSource: tokenSourceOf(options, environ),
    cost: estimateCost(project.memoryMib, scaling),
    notes,
    detection: context.detection,
  };
}

/** El proyecto a desplegar y, si salió de la detección, su informe. Un
 * `project` explícito gana a todo; si no, `AgentProject.detect()` (que lee el
 * fichero de proyecto y completa lo que falte sin ejecutar código del
 * usuario). Es el único sitio donde el despliegue conoce la detección. */
export async function projectFor(
  path: string,
  project: AgentProject | undefined,
  overrides: DetectOverrides | undefined,
  region?: string | undefined,
): Promise<{ readonly project: AgentProject; readonly report: DetectionReport | undefined }> {
  if (project !== undefined) {
    return { project, report: undefined };
  }
  const [detected, report] = await AgentProject.detect(path, overrides, { region });
  return { project: detected, report };
}

/** La región de la llamada: argumento > `region` del proyecto > `AWS_REGION` /
 * `AWS_DEFAULT_REGION`. */
export function resolvedRegion(
  region: string | undefined,
  project: AgentProject,
  environ: Environ,
): string | undefined {
  const projectRegion = (project as unknown as { region?: string }).region;
  return region ?? projectRegion ?? environ.AWS_REGION ?? environ.AWS_DEFAULT_REGION;
}
