/**
 * El access token de `rayito sandbox create|exec|metrics` y los pares `K=V`
 * de sus opciones. Espejo de `rayito.cli._tokens`.
 *
 * El token nunca llega por argv (se ve en `ps`), nunca se imprime y nunca se
 * loguea: viene de `--token-file` o de `RAYITO_ACCESS_TOKEN`, y `create
 * --detach` lo guarda en un fichero nuevo con modo `0600` (en Windows el
 * modo lo ignora el sistema; el fichero hereda los permisos del directorio).
 */

import { closeSync, constants, openSync, readFileSync, writeSync } from "node:fs";

export const TOKEN_ENV_VAR = "RAYITO_ACCESS_TOKEN";
export const TOKEN_FILE_MODE = 0o600;
const TOKEN_FILE_FLAGS = constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL;
const ALREADY_EXISTS = "EEXIST";

/** El fichero de token no se pudo leer o ya existía al crearlo; nunca nombra el contenido. */
export class TokenFileError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "TokenFileError";
  }
}

/** Un `--env`/`--metadata` sin `=` o sin clave; el mensaje no repite el valor. */
export class InvalidPairError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "InvalidPairError";
  }
}

function errorCode(error: unknown): string {
  return String((error as { code?: unknown } | null)?.code ?? "Error");
}

export function readTokenFile(path: string): string {
  try {
    return readFileSync(path, "utf8").trim();
  } catch (error) {
    throw new TokenFileError(`no se pudo leer el fichero de token ${path}: ${errorCode(error)}`);
  }
}

/** `--token-file` gana a `RAYITO_ACCESS_TOKEN`; un fichero o una variable vacíos cuentan como ausentes. */
export function resolveToken(
  tokenFile: string | undefined,
  env: Readonly<Record<string, string | undefined>>,
): string | undefined {
  if (tokenFile !== undefined) {
    return readTokenFile(tokenFile) || undefined;
  }
  return env[TOKEN_ENV_VAR] || undefined;
}

/** Crea el fichero (`O_EXCL`, `0600`); uno existente es `TokenFileError` y no se toca. */
export function writeTokenFile(path: string, token: string): void {
  let descriptor: number;
  try {
    descriptor = openSync(path, TOKEN_FILE_FLAGS, TOKEN_FILE_MODE);
  } catch (error) {
    if (errorCode(error) === ALREADY_EXISTS) {
      throw new TokenFileError(`el fichero de token ya existe: ${path}`);
    }
    throw new TokenFileError(`no se pudo crear el fichero de token ${path}: ${errorCode(error)}`);
  }
  try {
    writeSync(descriptor, token, 0, "utf8");
  } finally {
    closeSync(descriptor);
  }
}

/** `["A=1", "B=x=y"]` a `{ A: "1", B: "x=y" }`. */
export function parsePairs(values: readonly string[], option: string): Record<string, string> {
  const pairs: Record<string, string> = {};
  values.forEach((value, index) => {
    const separator = value.indexOf("=");
    if (separator <= 0) {
      throw new InvalidPairError(`${option}: se esperaba K=V (entrada n.º ${index + 1})`);
    }
    pairs[value.slice(0, separator)] = value.slice(separator + 1);
  });
  return pairs;
}
