/**
 * Valores puros del despliegue de un agente con una sola llamada
 * (`deployAgent`, ADR-029). Espejo de `rayito._agent._deploy_domain`:
 * `DeployOptions` (dónde y cómo se despliega, no qué es el agente) y los
 * nombres de los valores por defecto que la fachada resuelve. Nada aquí llama
 * a AWS ni crea clientes; construir un `DeployOptions` valida y nada más.
 */

import { InvalidArgumentError } from "../errors.js";
import { defineHidden } from "../hidden.js";
import { ACCESS_TOKEN_MIN_BYTES, S3_BUCKET_NAME_MAX, S3_BUCKET_NAME_MIN } from "../limits.js";

/** Valor de `baseImageVersion` que pide la última versión activa de la base. */
export const BASE_IMAGE_VERSION_LATEST = "latest";
/** Un nombre de proveedor de `secretNames` (un preset de pasarela). */
const PROVIDER_PATTERN = /^[a-z][a-z0-9-]*$/;
/** Un nombre de secreto o de versión de imagen: sin espacios ni saltos. */
const TOKEN_PATTERN = /^\S+$/;

/** Comprobación temprana de un nombre de bucket: longitud y sin espacios ni
 * `/`. Las reglas completas de S3 las aplica quien lo usa. */
export function validateBucketHint(bucket: unknown, label: string): string {
  if (
    typeof bucket !== "string" ||
    bucket.length < S3_BUCKET_NAME_MIN ||
    bucket.length > S3_BUCKET_NAME_MAX ||
    !TOKEN_PATTERN.test(bucket) ||
    bucket.includes("/")
  ) {
    throw new InvalidArgumentError(
      `${label} debe ser un nombre de bucket de ${S3_BUCKET_NAME_MIN} a ${S3_BUCKET_NAME_MAX} caracteres, sin espacios ni '/'`,
    );
  }
  return bucket;
}

/** `"latest"` o el identificador de una versión de imagen. */
export function validateImageVersion(version: unknown, label: string): string {
  if (typeof version !== "string" || !TOKEN_PATTERN.test(version)) {
    throw new InvalidArgumentError(`${label} debe ser 'latest' o una versión, sin espacios`);
  }
  return version;
}

export interface DeployOptionsInput {
  readonly bucket?: string | undefined;
  readonly baseImageVersion?: string | undefined;
  readonly secretNames?: Readonly<Record<string, string>> | undefined;
  readonly createSecretFromEnv?: boolean | undefined;
  readonly ensureBaseImage?: boolean | undefined;
  readonly accessToken?: string | undefined;
  readonly keepOnFailure?: boolean | undefined;
  readonly dryRun?: boolean | undefined;
}

function flag(value: boolean | undefined, fallback: boolean, label: string): boolean {
  const resolved = value ?? fallback;
  if (typeof resolved !== "boolean") {
    throw new InvalidArgumentError(`DeployOptions.${label} debe ser un bool`);
  }
  return resolved;
}

function validatedSecretNames(names: unknown): Readonly<Record<string, string>> {
  if (typeof names !== "object" || names === null || Array.isArray(names)) {
    throw new InvalidArgumentError("DeployOptions.secretNames debe ser un objeto");
  }
  for (const [provider, name] of Object.entries(names)) {
    if (!PROVIDER_PATTERN.test(provider)) {
      throw new InvalidArgumentError(
        "DeployOptions.secretNames: cada clave debe ser un proveedor (por ejemplo 'openai')",
      );
    }
    if (typeof name !== "string" || !TOKEN_PATTERN.test(name)) {
      throw new InvalidArgumentError(
        "DeployOptions.secretNames: cada valor debe ser el nombre de un secreto",
      );
    }
  }
  return Object.freeze({ ...(names as Record<string, string>) });
}

/**
 * Los argumentos de `deployAgent()` que describen dónde y cómo se despliega,
 * no el agente: `bucket` del artefacto de la imagen, `baseImageVersion` de
 * `rayito-base-caps` (`"latest"` por defecto), `secretNames` por proveedor,
 * `createSecretFromEnv` (crea el secreto del modelo desde la variable local
 * del proveedor, apagado por defecto), `ensureBaseImage` (publica la base si
 * falta, tras confirmar), `accessToken`, `keepOnFailure` y `dryRun` (muestra la
 * detección, el plan y el coste sin llamar a AWS).
 *
 * Coste y activación
 * -------------------
 * Activa: nada; construirlo es puro. `createSecretFromEnv: true` y
 *   `ensureBaseImage: true` sólo cuentan cuando `deployAgent()` los usa.
 * Recursos y llamadas AWS: ninguno aquí.
 * Coste aproximado: ninguno aquí; el de `deployAgent()` está en su bloque.
 * IAM: ninguna para este valor.
 * Cómo apagarla: no lo pases; los valores por defecto no crean nada extra.
 * Ejemplo:
 *   const options = new DeployOptions({ bucket: "amzn-s3-demo-bucket", dryRun: true });
 */
export class DeployOptions {
  readonly bucket: string | undefined;
  readonly baseImageVersion: string;
  readonly secretNames: Readonly<Record<string, string>>;
  readonly createSecretFromEnv: boolean;
  readonly ensureBaseImage: boolean;
  readonly keepOnFailure: boolean;
  readonly dryRun: boolean;
  /** El token del sandbox que se pasó, o `undefined`. No es enumerable:
   * `console.log`, `JSON.stringify` y el spread no lo ven. */
  declare readonly accessToken: string | undefined;

  constructor(options: DeployOptionsInput = {}) {
    this.bucket =
      options.bucket === undefined
        ? undefined
        : validateBucketHint(options.bucket, "DeployOptions.bucket");
    this.baseImageVersion = validateImageVersion(
      options.baseImageVersion ?? BASE_IMAGE_VERSION_LATEST,
      "DeployOptions.baseImageVersion",
    );
    this.secretNames = validatedSecretNames(options.secretNames ?? {});
    this.createSecretFromEnv = flag(options.createSecretFromEnv, false, "createSecretFromEnv");
    this.ensureBaseImage = flag(options.ensureBaseImage, true, "ensureBaseImage");
    this.keepOnFailure = flag(options.keepOnFailure, false, "keepOnFailure");
    this.dryRun = flag(options.dryRun, false, "dryRun");
    if (
      options.accessToken !== undefined &&
      (typeof options.accessToken !== "string" ||
        new TextEncoder().encode(options.accessToken).length < ACCESS_TOKEN_MIN_BYTES)
    ) {
      throw new InvalidArgumentError(
        `DeployOptions.accessToken debe tener al menos ${ACCESS_TOKEN_MIN_BYTES} bytes`,
      );
    }
    defineHidden(this, "accessToken", options.accessToken);
  }
}
