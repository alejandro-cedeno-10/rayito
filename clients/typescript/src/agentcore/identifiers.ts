/**
 * Los identificadores que hacen idempotente `--target agentcore` (ADR-031,
 * D6 y D7): la etiqueta de la imagen por contenido, el `clientToken` de
 * `CreateAgentRuntime` y `UpdateAgentRuntime` y el id de sesión de
 * `InvokeAgentRuntime`. Puro: `node:crypto` sólo calcula resúmenes y números
 * aleatorios, no toca el sistema ni la red. Los vectores compartidos con
 * Python están en `testdata/agentcore-deploy/`.
 */

import { createHash, randomUUID } from "node:crypto";
import { InvalidArgumentError } from "../errors.js";
import {
  AGENTCORE_IMAGE_TAG_HEX_CHARS,
  AGENTCORE_IMAGE_TAG_PREFIX,
  AGENTCORE_SESSION_ID_MAX_LENGTH,
  AGENTCORE_SESSION_ID_MIN_LENGTH,
  AGENTCORE_SESSION_ID_PREFIX,
} from "../limits.js";

/** `sessionIdRules.pattern` del contrato: lo que admite `runtimeSessionId` en la respuesta de AWS. */
export const SESSION_ID_PATTERN = /^[a-zA-Z0-9][a-zA-Z0-9_-]*$/;
/** La clave que añade la versión leída al texto que se hashea al actualizar (`constants.clientTokenDigest`). */
export const BASE_VERSION_KEY = "baseVersion";

/** Caracteres hexadecimales del `clientToken` (`clientTokenTemplate` del contrato; mide 39 con el prefijo, dentro de 33-256). */
export const CLIENT_TOKEN_HEX_CHARS = 32;
const HASH_ALGORITHM = "sha256";
const UTF8 = new TextEncoder();
const NUL = Uint8Array.of(0);

function sha256Hex(...parts: readonly Uint8Array[]): string {
  const hash = createHash(HASH_ALGORITHM);
  for (const part of parts) {
    hash.update(part);
  }
  return hash.digest("hex");
}

function bytesOf(content: string | Uint8Array): Uint8Array {
  return typeof content === "string" ? UTF8.encode(content) : content;
}

function byUtf8(
  left: readonly [Uint8Array, unknown],
  right: readonly [Uint8Array, unknown],
): number {
  return Buffer.compare(left[0], right[0]);
}

/**
 * La etiqueta de imagen por contenido: `src-` y los primeros 16 caracteres
 * hexadecimales del sha256 de cada fichero del árbol de `renderExport`, en
 * orden ascendente de ruta (bytes UTF-8), con su ruta, un NUL, su longitud en
 * decimal, un NUL y su contenido (`constants.imageTagDigest`). Dos árboles
 * iguales dan la misma etiqueta en los dos SDK; mover bytes de un fichero a
 * otro la cambia.
 */
export function imageTagFor(tree: ReadonlyMap<string, string | Uint8Array>): string {
  const files = [...tree.entries()]
    .map(([path, content]) => [UTF8.encode(path), bytesOf(content)] as const)
    .sort(byUtf8);
  const framed = files.flatMap(([path, content]) => [
    path,
    NUL,
    UTF8.encode(String(content.length)),
    NUL,
    content,
  ]);
  return `${AGENTCORE_IMAGE_TAG_PREFIX}${sha256Hex(...framed).slice(0, AGENTCORE_IMAGE_TAG_HEX_CHARS)}`;
}

function sortedKeys(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map(sortedKeys);
  }
  if (typeof value === "object" && value !== null) {
    const record = value as Record<string, unknown>;
    return Object.fromEntries(
      Object.keys(record)
        .sort()
        .map((key) => [key, sortedKeys(record[key])]),
    );
  }
  return value;
}

/**
 * El `clientToken` de una petición de AWS de `CreateAgentRuntime` o
 * `UpdateAgentRuntime`: `rayito-` y 32 hexadecimales del sha256 de su JSON
 * canónico sin `clientToken` (claves ordenadas, sin espacios, UTF-8). Al
 * actualizar se pasa `baseVersion`, la versión que `GetAgentRuntime` dio justo
 * antes, y entra en el hash: un reintento de la misma petición repite el
 * token, pero volver a una configuración anterior tras otra versión no se
 * confunde con la petición vieja.
 */
export function clientTokenFor(
  request: Readonly<Record<string, unknown>>,
  baseVersion?: string,
): string {
  const { clientToken: _omitted, ...rest } = request;
  const unsigned = baseVersion === undefined ? rest : { ...rest, [BASE_VERSION_KEY]: baseVersion };
  const digest = sha256Hex(UTF8.encode(JSON.stringify(sortedKeys(unsigned))));
  return `${AGENTCORE_SESSION_ID_PREFIX}${digest.slice(0, CLIENT_TOKEN_HEX_CHARS)}`;
}

function newSessionId(): string {
  return `${AGENTCORE_SESSION_ID_PREFIX}${randomUUID().replaceAll("-", "")}`;
}

function isKeptSessionId(sessionId: string): boolean {
  return (
    sessionId.length >= AGENTCORE_SESSION_ID_MIN_LENGTH &&
    sessionId.length <= AGENTCORE_SESSION_ID_MAX_LENGTH &&
    SESSION_ID_PATTERN.test(sessionId)
  );
}

/**
 * El `runtimeSessionId` de una llamada (`constants.sessionIdRules`): sin id, uno
 * nuevo (`rayito-` y 32 hexadecimales de un uuid4); uno del llamante que ya
 * cumple el patrón y mide 33-100 caracteres, tal cual; cualquier otro,
 * `rayito-` y los 32 primeros hexadecimales del sha256 de su UTF-8, así que el
 * mismo id del llamante llega siempre a la misma sesión. Vacío es un error.
 */
export function agentcoreSessionId(sessionId: string | undefined): string {
  if (sessionId === undefined) {
    return newSessionId();
  }
  if (sessionId === "") {
    throw new InvalidArgumentError("sessionId no puede ser vacío");
  }
  if (isKeptSessionId(sessionId)) {
    return sessionId;
  }
  const digest = sha256Hex(UTF8.encode(sessionId));
  return `${AGENTCORE_SESSION_ID_PREFIX}${digest.slice(0, CLIENT_TOKEN_HEX_CHARS)}`;
}
