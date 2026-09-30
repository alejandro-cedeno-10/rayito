/**
 * Dominio puro de los secretos de Rayito (M13a): referencias, nombres bajo un
 * prefijo, la versión entera codificada en `ClientRequestToken` y los
 * metadatos en `Description` (AWS_API_NOTES.md §19). Sin I/O; espejo de la
 * parte pura de `rayito/_secrets.py`.
 */

import { InvalidArgumentError } from "../errors.js";

export const DEFAULT_SECRET_PREFIX = "rayito/";
export const DEFAULT_TTL_SECONDS = 300;
export const MAX_TTL_SECONDS = 86_400;
export const VERSION_TOKEN_PREFIX = "rayito-secret-version-";
export const METADATA_PREFIX = "rayito:v1:";
export const DESCRIPTION_MAX_CHARS = 2048;
export const SECRET_STRING_MAX_BYTES = 65_536;
export const SECRET_ID_MAX_CHARS = 512;
export const LIST_MAX_RESULTS = 100;
export const CURRENT_STAGE = "AWSCURRENT";
export const MASK = "***";

const SECRET_NAME_PATTERN = /^[A-Za-z0-9/_+=.@-]+$/;
const VERSION_TOKEN_PATTERN = /^rayito-secret-version-(\d{20})$/;

export interface SecretRefOptions {
  /** Un `VersionId` concreto (excluyente con `versionStage`). */
  readonly versionId?: string | undefined;
  /** Una etiqueta (`AWSCURRENT` por defecto, `AWSPREVIOUS`, …). */
  readonly versionStage?: string | undefined;
}

/**
 * Una referencia a un secreto, nunca su valor: `name` es un nombre bajo el
 * prefijo del `SecretStore` (`"openai"` → `rayito/openai`) o un ARN completo
 * (`arn:…`, que se usa tal cual). Una cadena simple en `secrets` equivale a
 * `new SecretRef(name)`.
 */
export class SecretRef {
  readonly name: string;
  readonly versionId: string | undefined;
  readonly versionStage: string | undefined;

  constructor(name: string, options: SecretRefOptions = {}) {
    if (typeof name !== "string" || name.length === 0) {
      throw new InvalidArgumentError("SecretRef.name debe ser una cadena no vacía");
    }
    if (options.versionId !== undefined && options.versionStage !== undefined) {
      throw new InvalidArgumentError("SecretRef admite versionId o versionStage, no los dos");
    }
    for (const [label, value] of [
      ["versionId", options.versionId],
      ["versionStage", options.versionStage],
    ] as const) {
      if (value !== undefined && (typeof value !== "string" || value.length === 0)) {
        throw new InvalidArgumentError(`SecretRef.${label} debe ser una cadena no vacía`);
      }
    }
    this.name = name;
    this.versionId = options.versionId;
    this.versionStage = options.versionStage;
    Object.freeze(this);
  }

  /** La versión pedida, como parte de la clave de caché. */
  get selector(): string {
    return this.versionId === undefined
      ? `VersionStage:${this.versionStage ?? CURRENT_STAGE}`
      : `VersionId:${this.versionId}`;
  }
}

export type SecretLike = string | SecretRef;

export function asRef(secret: SecretLike): SecretRef {
  if (secret instanceof SecretRef) {
    return secret;
  }
  if (typeof secret === "string") {
    return new SecretRef(secret);
  }
  throw new InvalidArgumentError("un secreto se referencia con un string o un SecretRef");
}

/** `rayito-secret-version-{n:020d}`: 42 caracteres, dentro de los 32-64 de `ClientRequestToken`. */
export function versionToken(version: number): string {
  if (!Number.isInteger(version) || version < 1) {
    throw new InvalidArgumentError("la versión de un secreto empieza en 1");
  }
  return `${VERSION_TOKEN_PREFIX}${String(version).padStart(20, "0")}`;
}

/** El entero de un `VersionId` escrito por Rayito; 0 para cualquier otro. */
export function versionFromId(versionId: string | undefined): number {
  const match = versionId === undefined ? null : VERSION_TOKEN_PATTERN.exec(versionId);
  return match?.[1] === undefined ? 0 : Number.parseInt(match[1], 10);
}

/** La versión que lleva `AWSCURRENT` en `VersionIdsToStages`/`SecretVersionsToStages`. */
export function currentVersion(
  stages: Readonly<Record<string, readonly string[] | undefined>> | undefined,
): number {
  for (const [versionId, labels] of Object.entries(stages ?? {})) {
    if (labels?.includes(CURRENT_STAGE)) {
      return versionFromId(versionId);
    }
  }
  return 0;
}

/** `rayito:v1:` + JSON compacto con claves ordenadas, validado contra los 2048 caracteres de `Description`. */
export function encodeMetadata(metadata: Readonly<Record<string, string>> | undefined): string {
  const values = metadata ?? {};
  const sorted: Record<string, string> = {};
  for (const key of Object.keys(values).sort()) {
    const value = values[key];
    if (key.length === 0 || typeof value !== "string") {
      throw new InvalidArgumentError("metadata de un secreto: claves y valores string");
    }
    sorted[key] = value;
  }
  const encoded = METADATA_PREFIX + JSON.stringify(sorted);
  if (encoded.length > DESCRIPTION_MAX_CHARS) {
    throw new InvalidArgumentError(
      `metadata de un secreto: ${encoded.length} caracteres codificados, máximo ` +
        `${DESCRIPTION_MAX_CHARS} (Description de Secrets Manager)`,
    );
  }
  return encoded;
}

export function decodeMetadata(description: string | undefined): Record<string, string> {
  if (description === undefined || !description.startsWith(METADATA_PREFIX)) {
    return {};
  }
  try {
    const decoded: unknown = JSON.parse(description.slice(METADATA_PREFIX.length));
    if (typeof decoded !== "object" || decoded === null || Array.isArray(decoded)) {
      return {};
    }
    return Object.fromEntries(
      Object.entries(decoded as Record<string, unknown>).map(([key, value]) => [
        key,
        String(value),
      ]),
    );
  } catch {
    return {};
  }
}

export function validatePrefix(prefix: string): string {
  if (typeof prefix !== "string") {
    throw new InvalidArgumentError("prefix debe ser un string ('' permitido)");
  }
  if (prefix.length > 0 && !SECRET_NAME_PATTERN.test(prefix)) {
    throw new InvalidArgumentError(
      "prefix sólo admite letras, dígitos y /_+=.@- (nombres de Secrets Manager)",
    );
  }
  return prefix;
}

/** El `SecretId` de un nombre: un ARN tal cual; si no, `prefix + name`. El error nunca repite el nombre. */
export function resolveSecretId(name: string, prefix: string): string {
  if (typeof name !== "string" || name.length === 0) {
    throw new InvalidArgumentError("el nombre de un secreto debe ser una cadena no vacía");
  }
  if (name.startsWith("arn:")) {
    return name;
  }
  const secretId = prefix + name;
  if (secretId.length > SECRET_ID_MAX_CHARS || !SECRET_NAME_PATTERN.test(secretId)) {
    throw new InvalidArgumentError(
      "nombre de secreto inválido: 1-512 caracteres (con el prefijo) de letras, dígitos y /_+=.@-",
    );
  }
  return secretId;
}

export function displayName(awsName: string, prefix: string): string {
  return prefix.length > 0 && awsName.startsWith(prefix) ? awsName.slice(prefix.length) : awsName;
}

export function validateValue(value: string): string {
  if (typeof value !== "string" || value.length === 0) {
    throw new InvalidArgumentError("el valor de un secreto debe ser un string no vacío");
  }
  if (Buffer.byteLength(value, "utf8") > SECRET_STRING_MAX_BYTES) {
    throw new InvalidArgumentError(
      `el valor de un secreto no puede pasar de ${SECRET_STRING_MAX_BYTES} bytes`,
    );
  }
  return value;
}
