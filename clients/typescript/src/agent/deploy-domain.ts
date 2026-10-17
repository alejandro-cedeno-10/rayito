/**
 * Comprobaciones tempranas de los valores de despliegue que comparten
 * `AgentProject` (`[deploy]`) y `AgentDeployOptions`: el nombre de un bucket, de
 * un secreto y la versión de la imagen base. Espejo de
 * `rayito._agent._deploy_domain`. Puro: no llama a AWS ni crea clientes.
 */

import { InvalidArgumentError } from "../errors.js";
import { S3_BUCKET_NAME_MAX, S3_BUCKET_NAME_MIN } from "../limits.js";

/** Valor de `baseImageVersion` que pide la última versión activa de la base. */
export const BASE_IMAGE_VERSION_LATEST = "latest";
/** Un nombre de bucket, de secreto o de versión de imagen: sin espacios ni saltos. */
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

/** El nombre de un secreto existente: texto sin espacios ni saltos. Las
 * reglas completas las aplica Secrets Manager. */
export function validateSecretName(name: unknown, label: string): string {
  if (typeof name !== "string" || !TOKEN_PATTERN.test(name)) {
    throw new InvalidArgumentError(`${label} debe ser el nombre de un secreto, sin espacios`);
  }
  return name;
}
