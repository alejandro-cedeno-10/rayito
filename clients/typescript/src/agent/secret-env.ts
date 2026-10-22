/**
 * Secretos de Secrets Manager como variables de entorno del agente
 * (ADR-033 D4). Espejo de `rayito._agent._secret_env_domain` y del algoritmo
 * del host (`_runner/agent_host/secret_env.py`): los valores `SecretEnvRef` y
 * `SecretEnvSource`, la especificación que viaja en `RAYITO_SECRET_ENV` (sólo
 * nombres y ARNs), los secretos que el rol del runtime debe poder leer, la
 * expansión de los valores leídos a variables (`testdata/agent-secret-env/
 * expand.json`) y el lector mínimo de `.env`. Puro: no llama a AWS ni crea
 * clientes; la lectura de cada secreto llega como una función.
 */

import { AgentError, InvalidArgumentError } from "../errors.js";
import {
  AGENT_SECRET_ENV_MAX_SECRETS,
  AGENT_SECRET_ENV_MAX_TOTAL_BYTES,
  AGENT_SECRET_ENV_NAMES_VARIABLE,
  AGENT_SECRET_ENV_RESERVED_NAMES,
  AGENT_SECRET_ENV_RESERVED_PREFIXES,
  AGENT_SECRET_ENV_SPEC_VERSION,
} from "../limits.js";
import { failureMessage } from "./events.js";

/** La herramienta que lee los secretos en AgentCore y la clave de
 * `host.json` que la apunta (`contract.json`, `constants.secretEnv`). */
export const SECRET_ENV_SCRIPT_NAME = "aws_secret_env.py";
export const SECRET_ENV_HOST_KEY = "secret_env";
/** Lo que separa la clave JSON en `--secret-env NOMBRE=<id>#clave`: Secrets
 * Manager no lo admite en nombres ni en ARNs. */
export const SECRET_ENV_KEY_SEPARATOR = "#";
/** Un nombre o ARN de secreto: los caracteres de `NameType` de Secrets
 * Manager (`AWS_API_NOTES.md` §19) más `:` de un ARN. */
export const SECRET_ID_PATTERN = /^[A-Za-z0-9/_+=.@:-]+$/;
/** Una clave JSON o una etiqueta de versión: ASCII imprimible sin espacios,
 * para que la especificación sea la misma en los dos SDK. */
export const SECRET_ENV_KEY_PATTERN = /^[!-~]+$/;
export const SECRET_ENV_NAME_PATTERN = /^[A-Za-z_][A-Za-z0-9_]*$/;
export const SECRET_ENV_FAILURE_REASON = "runtime_error";
export const SECRET_ENV_UNAVAILABLE = "secret_env_unavailable";
export const SECRET_ENV_DENIED = "secret_env_denied";
export const SECRET_ENV_INVALID = "secret_env_invalid";
export const SECRET_ENV_INVALID_KEY = "secret_env_invalid_key";
export const SECRET_ENV_RESERVED = "secret_env_reserved";

export interface SecretEnvRefOptions {
  readonly secret: string;
  readonly key?: string | undefined;
  readonly versionStage?: string | undefined;
}

function text(value: unknown, label: string): string {
  if (typeof value !== "string" || value === "") {
    throw new InvalidArgumentError(`${label} debe ser una cadena no vacía`);
  }
  return value;
}

/**
 * Una variable de entorno del agente con el valor de un secreto de Secrets
 * Manager: `secret` es su nombre completo (sin el prefijo `rayito/` de
 * `SecretStore`) o su ARN; `key`, una clave de un secreto JSON (sin ella, la
 * cadena entera); `versionStage`, la etiqueta de versión (sin ella,
 * `AWSCURRENT`). Nunca lleva el valor.
 *
 * Coste y activación
 * -------------------
 * Activa: `new AgentProject({ secretEnv: { NOMBRE: new SecretEnvRef(...) } })`,
 *   `[secret_env]` en el fichero de proyecto o `--secret-env`; sin ellos no se
 *   lee ningún secreto ni se crea ningún cliente de Secrets Manager.
 * Recursos y llamadas AWS: un `GetSecretValue` por secreto al arrancar cada
 *   sesión (AgentCore, con el rol del runtime) o cada VM (MicroVMs, con tus
 *   credenciales). No crea nada.
 * Coste aproximado: $0,05 por cada 10 000 llamadas a la API (us-east-1,
 *   consultado 2026-09-30 en https://aws.amazon.com/secrets-manager/pricing/);
 *   el secreto ya existe y ya lo pagas.
 * IAM: en AgentCore, `secretsmanager:GetSecretValue` sólo sobre esos secretos,
 *   que Rayito añade al rol del runtime; en MicroVMs, el mismo permiso en tus
 *   credenciales.
 * Cómo apagarla: quita la variable de `secretEnv`.
 * Ejemplo:
 *   new AgentProject({ ...options, secretEnv: { OPENAI_API_KEY: new SecretEnvRef({ secret: "llm", key: "api_key" }) } });
 */
export class SecretEnvRef {
  readonly secret: string;
  readonly key: string | undefined;
  readonly versionStage: string | undefined;

  constructor(options: SecretEnvRefOptions) {
    this.secret = text(options.secret, "SecretEnvRef.secret");
    this.key = options.key === undefined ? undefined : text(options.key, "SecretEnvRef.key");
    this.versionStage =
      options.versionStage === undefined
        ? undefined
        : text(options.versionStage, "SecretEnvRef.versionStage");
    Object.freeze(this);
  }
}

export interface SecretEnvSourceOptions {
  readonly secret: string;
  readonly prefix?: string | undefined;
  readonly keys?: readonly string[] | undefined;
}

/**
 * Cada clave de un secreto JSON de Secrets Manager como una variable de
 * entorno del agente: `secret` es su nombre completo o su ARN; `prefix` se
 * antepone a cada clave; `keys`, si se da, son las únicas claves que se toman
 * (y deben existir). Sin `keys`, cada clave debe ser un nombre de variable
 * válido: ninguna se descarta en silencio.
 *
 * Coste y activación
 * -------------------
 * Activa: `new AgentProject({ secretEnvFrom: [new SecretEnvSource(...)] })`,
 *   `[[secret_env_from]]` en el fichero de proyecto o `--secret-env-from`; sin
 *   ellos no se lee ningún secreto.
 * Recursos y llamadas AWS: un `GetSecretValue` por secreto al arrancar cada
 *   sesión (AgentCore) o cada VM (MicroVMs). No crea nada.
 * Coste aproximado: $0,05 por cada 10 000 llamadas a la API (us-east-1,
 *   consultado 2026-09-30 en https://aws.amazon.com/secrets-manager/pricing/).
 * IAM: `secretsmanager:GetSecretValue` sólo sobre ese secreto (el rol del
 *   runtime en AgentCore; tus credenciales en MicroVMs).
 * Cómo apagarla: quita la entrada de `secretEnvFrom`.
 * Ejemplo:
 *   new SecretEnvSource({ secret: "app/config", prefix: "APP_", keys: ["DB_HOST", "DB_USER"] });
 */
export class SecretEnvSource {
  readonly secret: string;
  readonly prefix: string;
  readonly keys: readonly string[] | undefined;

  constructor(options: SecretEnvSourceOptions) {
    this.secret = text(options.secret, "SecretEnvSource.secret");
    const prefix = options.prefix ?? "";
    if (typeof prefix !== "string") {
      throw new InvalidArgumentError("SecretEnvSource.prefix debe ser una cadena");
    }
    this.prefix = prefix;
    const keys: unknown = options.keys;
    if (keys !== undefined && !Array.isArray(keys)) {
      throw new InvalidArgumentError("SecretEnvSource.keys debe ser una lista de nombres");
    }
    this.keys =
      keys === undefined
        ? undefined
        : Object.freeze(keys.map((key: unknown) => text(key, "SecretEnvSource.keys")));
    Object.freeze(this);
  }

  /** Si dos fuentes dicen lo mismo. */
  equals(other: SecretEnvSource): boolean {
    return (
      this.secret === other.secret &&
      this.prefix === other.prefix &&
      JSON.stringify(this.keys) === JSON.stringify(other.keys)
    );
  }
}

export type SecretEnvValue = string | SecretEnvRef;
export type SecretEnvMap = Readonly<Record<string, SecretEnvValue>>;

/** La referencia de un valor de `secretEnv`: una cadena es el secreto. */
export function referenceOf(value: SecretEnvValue): SecretEnvRef {
  return value instanceof SecretEnvRef ? value : new SecretEnvRef({ secret: value });
}

/** Si `name` es un nombre que un secreto no puede fijar
 * (`testdata/agent-secret-env/reserved.json` más `extra`). */
export function isReservedName(name: string, extra: readonly string[] = []): boolean {
  return (
    (AGENT_SECRET_ENV_RESERVED_NAMES as readonly string[]).includes(name) ||
    AGENT_SECRET_ENV_RESERVED_PREFIXES.some((prefix) => name.startsWith(prefix)) ||
    extra.includes(name)
  );
}

/** Si todo nombre con `prefix` queda reservado. */
export function hasReservedPrefix(prefix: string): boolean {
  return (
    prefix !== "" && AGENT_SECRET_ENV_RESERVED_PREFIXES.some((item) => prefix.startsWith(item))
  );
}

export function usesSecretEnv(
  secretEnv: SecretEnvMap,
  secretEnvFrom: readonly SecretEnvSource[],
): boolean {
  return Object.keys(secretEnv).length > 0 || secretEnvFrom.length > 0;
}

/** Un mapa sin prototipo: una clave `__proto__` (un nombre de variable válido)
 * es una entrada más, como en un `dict` de Python, y no cambia el prototipo. */
function bareRecord<T>(): Record<string, T> {
  return Object.create(null) as Record<string, T>;
}

function byName(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0;
}

function referenceDocument(reference: SecretEnvRef): Record<string, string> {
  const document: Record<string, string> = {};
  if (reference.key !== undefined) {
    document.key = reference.key;
  }
  document.secret = reference.secret;
  if (reference.versionStage !== undefined) {
    document.version_stage = reference.versionStage;
  }
  return document;
}

function sourceDocument(source: SecretEnvSource): Record<string, unknown> {
  const document: Record<string, unknown> = {};
  if (source.keys !== undefined) {
    document.keys = [...source.keys];
  }
  if (source.prefix !== "") {
    document.prefix = source.prefix;
  }
  document.secret = source.secret;
  return document;
}

/** La especificación (`testdata/agent-secret-env/spec.json`), con las claves
 * en orden: sólo nombres y ARNs; `undefined` sin secretos. */
export function specDocument(
  secretEnv: SecretEnvMap,
  secretEnvFrom: readonly SecretEnvSource[],
): SecretEnvSpec | undefined {
  if (!usesSecretEnv(secretEnv, secretEnvFrom)) {
    return undefined;
  }
  const env = bareRecord<Record<string, string>>();
  for (const name of Object.keys(secretEnv).sort(byName)) {
    env[name] = referenceDocument(referenceOf(secretEnv[name] as SecretEnvValue));
  }
  return { env, from: secretEnvFrom.map(sourceDocument), v: AGENT_SECRET_ENV_SPEC_VERSION };
}

/** Cómo separa `RAYITO_SECRET_ENV_NAMES` los nombres (el host lee el mismo,
 * `NAMES_SEPARATOR` de `secret_env.py`). */
export const SECRET_ENV_NAMES_SEPARATOR = ",";

/** Lo que acompaña a los valores que el SDK exporta en MicroVMs:
 * `RAYITO_SECRET_ENV_NAMES` con sus nombres (nunca valores), para que el host
 * los redacte en un diagnóstico de arranque como en AgentCore; nada sin
 * secretos. */
export function injectedNamesEnvironment(
  values: Readonly<Record<string, string>>,
): Record<string, string> {
  const names = Object.keys(values).sort();
  return names.length === 0
    ? {}
    : { [AGENT_SECRET_ENV_NAMES_VARIABLE]: names.join(SECRET_ENV_NAMES_SEPARATOR) };
}

/** El valor de `RAYITO_SECRET_ENV`: JSON compacto con las claves ordenadas,
 * o `undefined` sin secretos. */
export function specVariable(
  secretEnv: SecretEnvMap,
  secretEnvFrom: readonly SecretEnvSource[],
): string | undefined {
  const document = specDocument(secretEnv, secretEnvFrom);
  return document === undefined ? undefined : JSON.stringify(document);
}

/** Los secretos referenciados, sin repetir: los de `secretEnv` por nombre de
 * variable y luego los de `secretEnvFrom` en orden. */
export function secretIds(
  secretEnv: SecretEnvMap,
  secretEnvFrom: readonly SecretEnvSource[],
): readonly string[] {
  const references = Object.keys(secretEnv)
    .sort(byName)
    .map((name) => referenceOf(secretEnv[name] as SecretEnvValue).secret);
  return [...new Set([...references, ...secretEnvFrom.map((source) => source.secret)])];
}

/** Los nombres de variable que se sabe que se fijan sin leer ningún secreto:
 * los de `secretEnv` y las claves de una lista `keys` con su prefijo. */
export function secretEnvNames(
  secretEnv: SecretEnvMap,
  secretEnvFrom: readonly SecretEnvSource[],
): readonly string[] {
  const listed = secretEnvFrom.flatMap((source) =>
    (source.keys ?? []).map((key) => `${source.prefix}${key}`),
  );
  return [...new Set([...Object.keys(secretEnv), ...listed])].sort(byName);
}

/** Los prefijos de las fuentes sin lista `keys`. */
export function unlistedPrefixes(secretEnvFrom: readonly SecretEnvSource[]): readonly string[] {
  return secretEnvFrom.filter((source) => source.keys === undefined).map((source) => source.prefix);
}

// ------------------------------------------------------------- expansion

export interface SecretEnvSpec {
  readonly env: Readonly<Record<string, Readonly<Record<string, string>>>>;
  readonly from: readonly Readonly<Record<string, unknown>>[];
  readonly v: number;
}

/** Un secreto pedido: su nombre o ARN y la etiqueta de versión. */
export type SecretRequest = readonly [string, string | undefined];
export type SecretReader = (secret: string, versionStage: string | undefined) => Promise<string>;

/** Un fallo al expandir: `AgentError` con `reason: "runtime_error"` y el
 * `detailCode` del host; nunca nombra un secreto ni un valor. */
function failure(code: string): AgentError {
  return new AgentError(failureMessage(SECRET_ENV_FAILURE_REASON, code), {
    reason: SECRET_ENV_FAILURE_REASON,
    detailCode: code,
  });
}

function jsonObject(secretString: string): Record<string, unknown> {
  let document: unknown;
  try {
    document = JSON.parse(secretString);
  } catch {
    throw failure(SECRET_ENV_INVALID);
  }
  if (typeof document !== "object" || document === null || Array.isArray(document)) {
    throw failure(SECRET_ENV_INVALID);
  }
  return document as Record<string, unknown>;
}

/** El valor de una referencia: la cadena entera o, con `key`, esa clave de
 * tipo cadena del objeto JSON. */
export function expandReference(
  reference: Readonly<Record<string, unknown>>,
  secretString: string,
): string {
  const key = reference.key;
  if (key === undefined || key === null) {
    return secretString;
  }
  const document = jsonObject(secretString);
  if (!Object.hasOwn(document, key as string)) {
    throw failure(SECRET_ENV_INVALID_KEY);
  }
  const value = document[key as string];
  if (typeof value !== "string") {
    throw failure(SECRET_ENV_INVALID);
  }
  return value;
}

function allowListed(
  document: Record<string, unknown>,
  keys: readonly string[],
): Record<string, unknown> {
  if (keys.some((key) => !Object.hasOwn(document, key))) {
    throw failure(SECRET_ENV_INVALID_KEY);
  }
  return Object.fromEntries(keys.map((key) => [key, document[key]]));
}

/** Las variables de un `secret_env_from`: cada clave del objeto JSON (o sólo
 * las de `keys`, que deben existir), con el prefijo, como nombre válido y no
 * reservado. */
export function expandSource(
  source: Readonly<Record<string, unknown>>,
  secretString: string,
  reserved: readonly string[] = [],
): Record<string, string> {
  const document = jsonObject(secretString);
  const keys = source.keys as readonly string[] | undefined | null;
  const chosen = keys === undefined || keys === null ? document : allowListed(document, keys);
  const prefix = (source.prefix as string | undefined) ?? "";
  const variables = bareRecord<string>();
  for (const [key, value] of Object.entries(chosen)) {
    const name = `${prefix}${key}`;
    if (!SECRET_ENV_NAME_PATTERN.test(name)) {
      throw failure(SECRET_ENV_INVALID_KEY);
    }
    if (isReservedName(name, reserved)) {
      throw failure(SECRET_ENV_RESERVED);
    }
    if (typeof value !== "string") {
      throw failure(SECRET_ENV_INVALID);
    }
    variables[name] = value;
  }
  return variables;
}

function requestOf(item: Readonly<Record<string, unknown>>): SecretRequest {
  return [item.secret as string, item.version_stage as string | undefined];
}

function requestKey(request: SecretRequest): string {
  return JSON.stringify(request);
}

/** Los secretos a pedir, sin repetir: los de `env` por nombre de variable y
 * luego los de `from`. Más de `AGENT_SECRET_ENV_MAX_SECRETS` es inválido. */
export function secretRequests(spec: SecretEnvSpec): readonly SecretRequest[] {
  const items = [
    ...Object.keys(spec.env)
      .sort(byName)
      .map((name) => spec.env[name] as Readonly<Record<string, unknown>>),
    ...spec.from,
  ];
  const unique = new Map<string, SecretRequest>();
  for (const item of items) {
    const request = requestOf(item);
    unique.set(requestKey(request), unique.get(requestKey(request)) ?? request);
  }
  if (unique.size > AGENT_SECRET_ENV_MAX_SECRETS) {
    throw failure(SECRET_ENV_INVALID);
  }
  return [...unique.values()];
}

const UTF8 = new TextEncoder();

/** Las variables de toda la especificación con el valor de cada secreto
 * (clave: `secretRequests`). Un nombre repetido entre `env` y `from` es
 * `secret_env_invalid_key`; pasar de `AGENT_SECRET_ENV_MAX_TOTAL_BYTES`,
 * `secret_env_invalid`. */
export function expandSpec(
  spec: SecretEnvSpec,
  values: ReadonlyMap<string, string>,
  reserved: readonly string[] = [],
): Record<string, string> {
  const secretValueOf = (item: Readonly<Record<string, unknown>>): string =>
    values.get(requestKey(requestOf(item))) as string;
  const variables = bareRecord<string>();
  for (const name of Object.keys(spec.env).sort(byName)) {
    if (!SECRET_ENV_NAME_PATTERN.test(name)) {
      throw failure(SECRET_ENV_INVALID_KEY);
    }
    if (isReservedName(name, reserved)) {
      throw failure(SECRET_ENV_RESERVED);
    }
    const reference = spec.env[name] as Readonly<Record<string, unknown>>;
    variables[name] = expandReference(reference, secretValueOf(reference));
  }
  for (const source of spec.from) {
    const expanded = expandSource(source, secretValueOf(source), reserved);
    if (Object.keys(expanded).some((name) => Object.hasOwn(variables, name))) {
      throw failure(SECRET_ENV_INVALID_KEY);
    }
    Object.assign(variables, expanded);
  }
  const total = Object.values(variables).reduce(
    (bytes, value) => bytes + UTF8.encode(value).length,
    0,
  );
  if (total > AGENT_SECRET_ENV_MAX_TOTAL_BYTES) {
    throw failure(SECRET_ENV_INVALID);
  }
  return variables;
}

/** Las variables de `spec`, leyendo cada secreto una vez con `read`. */
export async function resolveSecretEnv(
  spec: SecretEnvSpec,
  read: SecretReader,
  reserved: readonly string[] = [],
): Promise<Record<string, string>> {
  const values = new Map<string, string>();
  for (const request of secretRequests(spec)) {
    values.set(requestKey(request), await read(request[0], request[1]));
  }
  return expandSpec(spec, values, reserved);
}

// ------------------------------------------------------------------ .env

/** Una línea `[export ]NOMBRE=valor` de un `.env`. */
const DOTENV_ASSIGNMENT = /^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$/;
const DOTENV_COMMENT = "#";
const DOTENV_QUOTES: readonly string[] = ["'", '"'];
/** Un comentario al final de un valor sin comillas: `#` tras un espacio. */
const DOTENV_INLINE_COMMENT = /\s#/;

function dotenvValue(raw: string): string {
  const value = raw.trim();
  const quote = value.slice(0, 1);
  if (DOTENV_QUOTES.includes(quote)) {
    const closing = value.indexOf(quote, 1);
    if (closing > 0) {
      return value.slice(1, closing);
    }
  }
  const comment = DOTENV_INLINE_COMMENT.exec(value);
  return (comment === null ? value : value.slice(0, comment.index)).trim();
}

function dotenvAssignments(textValue: string): Array<[string, string]> {
  const assignments: Array<[string, string]> = [];
  for (const line of textValue.split(/\r\n|\r|\n/)) {
    if (line.trim().startsWith(DOTENV_COMMENT)) {
      continue;
    }
    const match = DOTENV_ASSIGNMENT.exec(line);
    if (match !== null) {
      assignments.push([match[1] as string, match[2] as string]);
    }
  }
  return assignments;
}

/** Los nombres de variable de un `.env` (o `.env.example`), en orden y sin
 * repetir; nunca interpreta un valor. */
export function dotenvNames(textValue: string): readonly string[] {
  return [...new Set(dotenvAssignments(textValue).map(([name]) => name))];
}

/** Las variables de un `.env` (`testdata/agent-secret-env/dotenv.json`):
 * `NOMBRE=valor`, `export`, comillas, comentarios; sin interpolación. Una
 * variable repetida gana la última. */
export function parseDotenv(textValue: string): Record<string, string> {
  const variables = bareRecord<string>();
  for (const [name, raw] of dotenvAssignments(textValue)) {
    variables[name] = dotenvValue(raw);
  }
  return variables;
}
