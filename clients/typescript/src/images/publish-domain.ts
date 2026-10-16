/**
 * `rayito image publish` (dominio puro): las variantes y sus nombres, la
 * configuración deseada de `create`/`update-microvm-image`, las claves del
 * artefacto en S3, los tamaños adicionales y los resúmenes. Espejo de
 * `rayito.cli._publish`; `testdata/images/publish-vectors.json` fija los
 * mismos valores en los dos lenguajes. Ninguna función de aquí llama a AWS ni
 * toca el disco.
 *
 * Cada nombre de parámetro de AWS que se envía aparece literalmente en
 * `AWS_API_NOTES.md` §4. `hooks` y `cpuConfigurations` van con exactamente la
 * forma de la versión de Python: el reuso de una versión compara la
 * configuración entera, así que cualquier diferencia reconstruiría imágenes
 * ya publicadas.
 */

import { EFS_IMAGE_NAME } from "../role-policy.js";
import {
  BASELINE_MEMORY_MIB,
  NAME_TO_MEMORY_MIB,
  SIZE_NAMES,
  type SizeName,
} from "../sizing/sizing.js";
import { pythonStringRepr } from "../strings.js";
import { IMAGE_VARIANTS, type ImageVariant } from "./artifact.js";
import { DEFAULT_BASE_IMAGE_NAME, DEFAULT_MEMORY_MIB, LOG_GROUP_PREFIX } from "./gateway.js";
import { baseImageArn, IMAGE_ARTIFACT_KEY_PREFIX } from "./managed.js";

export type { ImageVariant };
export { IMAGE_VARIANTS };

/** El nombre por defecto de cada variante (`rayito image publish --variant`). */
export const DEFAULT_IMAGE_NAMES: Readonly<Record<ImageVariant, string>> = {
  full: DEFAULT_BASE_IMAGE_NAME,
  slim: `${DEFAULT_BASE_IMAGE_NAME}-slim`,
  poly: `${DEFAULT_BASE_IMAGE_NAME}-poly`,
};
/** La única variante con la que `--with-efs` se combina: es la imagen caps más una capa. */
export const EFS_BASE_VARIANT: ImageVariant = "full";
export const DEFAULT_STACK_NAME = "rayito-m0-iam";
/** `additionalOsCapabilities` (el único valor que acepta el modelo del servicio). */
export const ALL_OS_CAPABILITIES = "ALL";
export const OS_CAPABILITY_CHOICES: readonly string[] = [ALL_OS_CAPABILITIES];
export const BUILD_ROLE_OUTPUT_KEY = "BuildRoleArn";
/** Segmento de las claves de `--artifact-run-id`: cuelga de `IMAGE_ARTIFACT_KEY_PREFIX`, así que los permisos de `infra/iam.yaml` lo cubren. */
export const RUN_SCOPED_KEY_SEGMENT = "runs";
/** Un id de ejecución es un único segmento de clave S3: letras, dígitos y guiones, de 1 a 64. */
const RUN_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9-]{0,63}$/;
/** El patrón de nombre de variable de entorno de POSIX. */
const ENV_KEY_PATTERN = /^[A-Za-z_][A-Za-z0-9_]*$/;
/** Información declarada para que el código del usuario sepa el baseline de la imagen (Q88). */
export const BASELINE_MEMORY_ENV_VAR = "RAYITO_BASELINE_MEMORY_MIB";
const SHA256_PREFIX_LENGTH = 12;
const SUMMARY_SECONDS_DECIMALS = 10;
const CPU_ARCHITECTURE = "ARM_64";

const HOOKS_PORT = 9000;
const IMAGE_HOOK_TIMEOUT_SECONDS = 600;
const RUN_HOOK_TIMEOUT_SECONDS = 30;
const TERMINATE_HOOK_TIMEOUT_SECONDS = 10;
const ENABLED = "ENABLED";

/** Los hooks de la imagen: `rayd` deriva sus presupuestos de estos plazos. */
export const IMAGE_HOOKS: Readonly<Record<string, unknown>> = {
  port: HOOKS_PORT,
  microvmImageHooks: {
    ready: ENABLED,
    readyTimeoutInSeconds: IMAGE_HOOK_TIMEOUT_SECONDS,
    validate: ENABLED,
    validateTimeoutInSeconds: IMAGE_HOOK_TIMEOUT_SECONDS,
  },
  microvmHooks: {
    run: ENABLED,
    runTimeoutInSeconds: RUN_HOOK_TIMEOUT_SECONDS,
    resume: ENABLED,
    resumeTimeoutInSeconds: RUN_HOOK_TIMEOUT_SECONDS,
    suspend: ENABLED,
    suspendTimeoutInSeconds: RUN_HOOK_TIMEOUT_SECONDS,
    terminate: ENABLED,
    terminateTimeoutInSeconds: TERMINATE_HOOK_TIMEOUT_SECONDS,
  },
};

/** Una opción o combinación inválida; `hint` nombra la opción de la CLI que la causa. */
export class ImageSettingsError extends Error {
  readonly hint: string | undefined;

  constructor(message: string, hint?: string) {
    super(message);
    this.name = "ImageSettingsError";
    this.hint = hint;
  }
}

/** Todo lo que una publicación necesita saber (inmutable). */
export interface PublishSettings {
  readonly artifact: string;
  readonly imageName: string;
  readonly variant: string;
  readonly bucket: string;
  readonly stackName: string;
  readonly buildRoleArn: string | undefined;
  readonly baseImageVersion: string;
  readonly memoryMib: number;
  readonly force: boolean;
  readonly timeoutMs: number;
  readonly osCapabilities: string | undefined;
  readonly environmentVariables: Readonly<Record<string, string>>;
  readonly withEfs: boolean;
  /** `undefined` conserva la clave compartida direccionada por contenido. */
  readonly artifactRunId: string | undefined;
}

export function logGroupOf(settings: Pick<PublishSettings, "imageName">): string {
  return `${LOG_GROUP_PREFIX}/${settings.imageName}`;
}

/**
 * `--with-efs` sólo con la variante `full` y `--os-capabilities ALL`: la imagen
 * es `rayito-base-caps` más la capa `amazon-efs-utils`, y sin `CAP_SYS_ADMIN`
 * `rayd` nunca anunciaría `Health.features.efs_volumes`.
 */
export function requireEfsCombination(variant: string, osCapabilities: string | undefined): void {
  if (variant !== EFS_BASE_VARIANT) {
    throw new ImageSettingsError(
      `--with-efs sólo con --variant ${EFS_BASE_VARIANT}, no ${pythonStringRepr(variant)}`,
    );
  }
  if (osCapabilities !== ALL_OS_CAPABILITIES) {
    throw new ImageSettingsError(
      `--with-efs necesita --os-capabilities ${ALL_OS_CAPABILITIES}: rayd sólo monta EFS con ` +
        "CAP_SYS_ADMIN",
    );
  }
}

export function defaultImageName(variant: string, withEfs: boolean): string {
  const known = IMAGE_VARIANTS.find((candidate) => candidate === variant);
  if (known === undefined) {
    throw new ImageSettingsError(
      `variant desconocida: ${pythonStringRepr(variant)} (admitidas: ${IMAGE_VARIANTS.join(", ")})`,
    );
  }
  if (withEfs) {
    requireEfsCombination(known, ALL_OS_CAPABILITIES);
    return EFS_IMAGE_NAME;
  }
  return DEFAULT_IMAGE_NAMES[known];
}

/** `runId` tal cual si es un segmento de clave válido; si no, `ImageSettingsError`. */
export function validateRunId(runId: string): string {
  if (!RUN_ID_PATTERN.test(runId)) {
    throw new ImageSettingsError(
      `--artifact-run-id inválido: ${pythonStringRepr(runId)} (letras, dígitos y guiones, de 1 a ` +
        "64, empezando por letra o dígito)",
    );
  }
  return runId;
}

/**
 * La clave S3 del zip: por contenido (`rayd-<12 hex del sha256>.zip`) bajo
 * `IMAGE_ARTIFACT_KEY_PREFIX` y, con `runId`, además bajo `runs/<runId>/`, para
 * que el objeto sea sólo de esa ejecución.
 */
export function artifactKey(sha256Hex: string, runId: string | undefined): string {
  const prefix =
    runId === undefined
      ? IMAGE_ARTIFACT_KEY_PREFIX
      : `${IMAGE_ARTIFACT_KEY_PREFIX}/${RUN_SCOPED_KEY_SEGMENT}/${validateRunId(runId)}`;
  return `${prefix}/rayd-${sha256Hex.slice(0, SHA256_PREFIX_LENGTH)}.zip`;
}

/** El cuerpo de `create` y `update-microvm-image`, sin nombre ni descripción. */
export function desiredConfiguration(input: {
  readonly region: string;
  readonly settings: PublishSettings;
  readonly artifactUri: string;
  readonly buildRoleArn: string;
}): Record<string, unknown> {
  const { settings } = input;
  const configuration: Record<string, unknown> = {
    baseImageArn: baseImageArn(input.region),
    baseImageVersion: settings.baseImageVersion,
    buildRoleArn: input.buildRoleArn,
    codeArtifact: { uri: input.artifactUri },
    resources: [{ minimumMemoryInMiB: settings.memoryMib }],
    cpuConfigurations: [{ architecture: CPU_ARCHITECTURE }],
    hooks: IMAGE_HOOKS,
    logging: { cloudWatch: { logGroup: logGroupOf(settings) } },
  };
  if (settings.osCapabilities) {
    configuration.additionalOsCapabilities = [settings.osCapabilities];
  }
  if (Object.keys(settings.environmentVariables).length > 0) {
    configuration.environmentVariables = { ...settings.environmentVariables };
  }
  return configuration;
}

/** `base` con el sufijo de tamaño: nombre `<imagen>-<size>`, memoria del catálogo y el baseline horneado en el entorno de la imagen. */
export function sizedSettings(base: PublishSettings, sizeName: SizeName): PublishSettings {
  const memoryMib = NAME_TO_MEMORY_MIB[sizeName];
  return {
    ...base,
    imageName: `${base.imageName}-${sizeName}`,
    memoryMib,
    environmentVariables: {
      ...base.environmentVariables,
      [BASELINE_MEMORY_ENV_VAR]: String(memoryMib),
    },
  };
}

/** `RAYITO_TEMPLATE_<SIZE>`: la línea de texto de un tamaño adicional; `RAYITO_TEMPLATE` nombra siempre el baseline. */
export function sizeEnvVar(sizeName: string): string {
  return `RAYITO_TEMPLATE_${sizeName.toUpperCase()}`;
}

export interface UploadedArtifact {
  readonly uri: string;
  /** `false` si ya existía y se reutilizó: una limpieza borra sólo lo que subió su ejecución. */
  readonly uploaded: boolean;
}

export interface GateFacts {
  readonly imageState: string;
  readonly versionState: string;
  readonly versionStatus: string;
  readonly stateReason: string | undefined;
  readonly launchable: boolean;
}

export type PublishSummary = Record<string, unknown> & { readonly launchable: boolean };

function gateDocument(gate: GateFacts): Record<string, unknown> {
  return {
    imageState: gate.imageState,
    versionState: gate.versionState,
    versionStatus: gate.versionStatus,
    stateReason: gate.stateReason ?? null,
    launchable: gate.launchable,
  };
}

/** El documento de una imagen publicada o reutilizada. */
export function publishSummary(input: {
  readonly arn: string;
  readonly version: string;
  readonly artifact: UploadedArtifact;
  readonly buildSeconds: number | undefined;
  readonly build: Readonly<Record<string, unknown>>;
  readonly gate: GateFacts | undefined;
}): PublishSummary {
  const { gate, build } = input;
  const summary: Record<string, unknown> = {
    imageArn: input.arn,
    imageVersion: input.version,
    artifact: input.artifact.uri,
    artifactUploaded: input.artifact.uploaded,
    buildSeconds:
      input.buildSeconds === undefined
        ? null
        : Math.round(input.buildSeconds * SUMMARY_SECONDS_DECIMALS) / SUMMARY_SECONDS_DECIMALS,
    buildState: build.buildState ?? null,
    chipsetGeneration: build.chipsetGeneration ?? null,
    snapshotBuild: build.snapshotBuild ?? {},
    launchable: gate === undefined ? true : gate.launchable,
  };
  if (gate !== undefined) {
    summary.gate = gateDocument(gate);
    summary.buildStateReason = build.stateReason ?? null;
  }
  return summary as PublishSummary;
}

/** Un único documento para `--json --sizes`: el baseline en la raíz y cada tamaño bajo `sizes[<size>]`; sin tamaños, exactamente el baseline. */
export function aggregateSummary(
  baseline: PublishSummary,
  sized: Readonly<Record<string, PublishSummary>>,
): Record<string, unknown> {
  return Object.keys(sized).length > 0 ? { ...baseline, sizes: { ...sized } } : { ...baseline };
}

const BASELINE_SIZE_NAME: SizeName =
  SIZE_NAMES.find((name) => NAME_TO_MEMORY_MIB[name] === BASELINE_MEMORY_MIB) ?? SIZE_NAMES[0];

/** Los nombres de `--sizes` que se pueden pasar: el catálogo cerrado sin el baseline, que ya publica la imagen sin sufijo. */
export const EXTRA_SIZE_NAMES: readonly SizeName[] = SIZE_NAMES.filter(
  (name) => name !== BASELINE_SIZE_NAME,
);

function isSizeName(name: string): name is SizeName {
  return (SIZE_NAMES as readonly string[]).includes(name);
}

/** `--sizes 512mb,4gb`: nombres separados por comas del catálogo cerrado, sin repetidos ni el baseline. */
export function validateSizes(raw: string | undefined): SizeName[] {
  if (raw === undefined) {
    return [];
  }
  const names = raw
    .split(",")
    .map((piece) => piece.trim())
    .filter((piece) => piece !== "");
  const sizes: SizeName[] = [];
  for (const name of names) {
    if (!isSizeName(name)) {
      throw new ImageSettingsError(
        `${pythonStringRepr(name)}: admitidos ${SIZE_NAMES.join(", ")}`,
        "--sizes",
      );
    }
    if (name === BASELINE_SIZE_NAME) {
      throw new ImageSettingsError(
        `${pythonStringRepr(BASELINE_SIZE_NAME)} ya es el baseline sin sufijo; quítalo de --sizes`,
        "--sizes",
      );
    }
    sizes.push(name);
  }
  if (new Set(sizes).size !== sizes.length) {
    throw new ImageSettingsError("tamaños repetidos", "--sizes");
  }
  return sizes;
}

/** Con `--sizes`, `--memory-mib` sólo admite el baseline: la imagen sin sufijo siempre es el baseline de la regla de tamaños. */
export function validateBaselineMemory(memoryMib: number, sizes: readonly string[]): void {
  if (sizes.length > 0 && memoryMib !== DEFAULT_MEMORY_MIB) {
    throw new ImageSettingsError(
      `--sizes publica el baseline sin sufijo a ${DEFAULT_MEMORY_MIB} MiB (la regla de ` +
        "sizes-catalog: la imagen sin sufijo siempre es el baseline); quita --memory-mib o " +
        "publica sin --sizes para elegir otro valor",
      "--memory-mib",
    );
  }
}

/** `--env KEY=VALUE` (repetible): configuración horneada en la imagen, nunca un interruptor de activación ni un secreto. */
export function parseEnvironmentAssignments(raw: readonly string[]): Record<string, string> {
  const result: Record<string, string> = {};
  for (const item of raw) {
    const separator = item.indexOf("=");
    if (separator < 0) {
      throw new ImageSettingsError(`formato K=V esperado: ${pythonStringRepr(item)}`, "--env");
    }
    const key = item.slice(0, separator);
    if (!ENV_KEY_PATTERN.test(key)) {
      throw new ImageSettingsError(
        `${pythonStringRepr(key)}: nombre de variable inválido (letras, dígitos y \`_\`, sin ` +
          "empezar por un dígito)",
        "--env",
      );
    }
    result[key] = item.slice(separator + 1);
  }
  return result;
}
