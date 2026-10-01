/**
 * `Secret`, `SecretPaginator` y `SecretInfo` de E2B JS 2.51.0 (`src/secret.ts`
 * del paquete npm `e2b@2.51.0`, descargado y leído el 2026-09-30) sobre
 * `SecretStore`: el CRUD de E2B contra AWS Secrets Manager en tu cuenta.
 * Espejo de `rayito/e2b/_secret.py`.
 *
 * Mismos nombres, parámetros y resultados que E2B; las divergencias, escritas
 * en docs/site/docs/e2b-compat.md:
 *
 * - `SecretInfo.secretId` es el ARN de Secrets Manager, no `sec_…`.
 * - Los nombres se validan como en la API de E2B antes de llamar a AWS (1-128
 *   caracteres `[A-Za-z0-9_-]`, se normalizan a minúsculas, prefijo `sec_`
 *   reservado) y se guardan como `<secretPrefix><nombre>` (`rayito/` por
 *   defecto). Un `secret` que empieza por `arn:` se usa tal cual.
 * - `metadata` ≤ 2048 caracteres codificados (E2B: 8 KiB); sin tope de 100.
 * - `fill(name)` devuelve la cadena literal `${e2b.secrets.<name>}` sin llamar
 *   a AWS, pero **nada la resuelve**: Rayito no tiene el inyector de
 *   `network.rules` y ninguna ruta del SDK sustituye placeholders en `envs`.
 * - `iamToken` sigue siendo `UnimplementedError` (motivo de `iam`).
 * - Las `ConnectionOpts` de E2B no aplican a Secrets Manager: avisan con
 *   `RayitoCompatWarning` y se ignoran. Las opciones propias son `region`,
 *   `credentials`, `secretPrefix` y `kmsKeyId`.
 *
 * El bloque "Coste y activación" está en el TSDoc de `Secret` (lo que
 * enseña el IDE al pasar el ratón).
 */

import { InvalidArgumentError } from "../errors.js";
import { DEFAULT_SECRET_PREFIX } from "../secrets/names.js";
import {
  type SecretInfo,
  SecretStore,
  type SecretStoreOptions,
  type SecretsManagerApi,
} from "../secrets/store.js";
import { emitCompatWarning, IGNORED_CONNECTION_OPTS } from "./compat.js";
import type { ConnectionOpts } from "./connection.js";
import { unimplemented } from "./unimplemented.js";

export type { SecretInfo } from "../secrets/store.js";

const E2B_NAME_PATTERN = /^[A-Za-z0-9_-]{1,128}$/;
const RESERVED_NAME_PREFIX = "sec_";
const INVALID_FILL_CHARS = /[{}\p{Cc}]/u;
const NO_MORE_ITEMS = "No more items to fetch";
const NOT_APPLIED_REASON = "Secret habla con Secrets Manager con las credenciales de AWS";
const RAYITO_OPTIONS = new Set(["region", "credentials", "secretPrefix", "kmsKeyId", "client"]);

/** Las `ConnectionOpts` de E2B más las opciones de Rayito para Secrets Manager. */
export interface SecretConnectionOpts extends ConnectionOpts {
  readonly credentials?: SecretStoreOptions["credentials"];
  /** `rayito/` por defecto; `''` para ninguno. */
  readonly secretPrefix?: string | undefined;
  readonly kmsKeyId?: string | undefined;
  /** Un cliente propio con la forma de `SecretsManager` del SDK v3 (p. ej. en tests). */
  readonly client?: SecretsManagerApi | undefined;
}

export interface SecretCreateOpts extends SecretConnectionOpts {
  /** Metadatos del cliente guardados con el secreto. */
  readonly metadata?: Record<string, string> | undefined;
}

export interface SecretUpdateOpts extends SecretConnectionOpts {
  /** Metadatos del cliente; si se dan, sustituyen a los guardados. */
  readonly metadata?: Record<string, string> | undefined;
}

export type SecretGetInfoOpts = SecretConnectionOpts;
export type SecretExistsOpts = SecretConnectionOpts;
export type SecretDestroyOpts = SecretConnectionOpts;

export interface SecretListOpts extends Omit<SecretConnectionOpts, "signal"> {
  /** Secretos por página (1-100). */
  readonly limit?: number | undefined;
  /** El token de la página siguiente. */
  readonly nextToken?: string | undefined;
}

/** La regla de nombres de la API de E2B, antes de llamar a AWS; el error nunca repite el nombre. */
export function normalizeSecretName(name: string): string {
  if (typeof name !== "string" || !E2B_NAME_PATTERN.test(name)) {
    throw new InvalidArgumentError("nombre de secreto inválido: 1-128 caracteres [A-Za-z0-9_-]");
  }
  const normalized = name.toLowerCase();
  if (normalized.startsWith(RESERVED_NAME_PREFIX)) {
    throw new InvalidArgumentError("el prefijo sec_ está reservado en los nombres de secreto");
  }
  return normalized;
}

/** `secret` de E2B es "ID o nombre": un ARN (nuestro `secretId`) va tal cual. */
function secretSelector(secret: string): string {
  return typeof secret === "string" && secret.startsWith("arn:")
    ? secret
    : normalizeSecretName(secret);
}

const stores = new Map<string, Map<unknown, Map<unknown, SecretStore>>>();

function resolveStore(bound: SecretConnectionOpts, opts: SecretConnectionOpts): SecretStore {
  for (const [name, value] of Object.entries(opts)) {
    if (value === undefined || RAYITO_OPTIONS.has(name) || name === "metadata") {
      continue;
    }
    if (name === "limit" || name === "nextToken") {
      continue;
    }
    emitCompatWarning(name, IGNORED_CONNECTION_OPTS[name] ?? NOT_APPLIED_REASON);
  }
  const merged = {
    region: opts.region ?? bound.region,
    credentials: opts.credentials ?? bound.credentials,
    prefix: opts.secretPrefix ?? bound.secretPrefix ?? DEFAULT_SECRET_PREFIX,
    kmsKeyId: opts.kmsKeyId ?? bound.kmsKeyId,
    client: opts.client ?? bound.client,
  };
  const key = `${merged.region ?? ""}|${merged.prefix}|${merged.kmsKeyId ?? ""}`;
  let byCredentials = stores.get(key);
  if (byCredentials === undefined) {
    byCredentials = new Map();
    stores.set(key, byCredentials);
  }
  let byClient = byCredentials.get(merged.credentials);
  if (byClient === undefined) {
    byClient = new Map();
    byCredentials.set(merged.credentials, byClient);
  }
  let store = byClient.get(merged.client);
  if (store === undefined) {
    store = new SecretStore(merged);
    byClient.set(merged.client, store);
  }
  return store;
}

/** `SecretPaginator` de E2B: `while (paginator.hasNext) await paginator.nextItems()`. */
export class SecretPaginator {
  readonly #store: SecretStore;
  protected readonly limit: number | undefined;
  #hasNext = true;
  #nextToken: string | undefined;

  constructor(opts: SecretListOpts = {}, bound: SecretConnectionOpts = {}) {
    this.#store = resolveStore(bound, opts);
    this.limit = opts.limit;
    this.#nextToken = opts.nextToken;
  }

  /** `true` si quedan elementos por traer. */
  get hasNext(): boolean {
    return this.#hasNext;
  }

  /** El token de la página siguiente. */
  get nextToken(): string | undefined {
    return this.#nextToken;
  }

  /** La página siguiente (`ListSecrets`); lanza si `hasNext` es `false`. */
  async nextItems(_opts?: SecretConnectionOpts): Promise<SecretInfo[]> {
    if (!this.#hasNext) {
      throw new Error(NO_MORE_ITEMS);
    }
    const page = await this.#store.list({ limit: this.limit, nextToken: this.#nextToken });
    this.#nextToken = page.nextToken;
    this.#hasNext = page.nextToken !== undefined;
    return page.items;
  }
}

/**
 * `Secret` de E2B sobre AWS Secrets Manager. Los valores son de sólo
 * escritura: ninguna lectura los devuelve.
 *
 * Coste y activación
 * -------------------
 * Activa: llamar a `Secret.create/update/getInfo/list/exists/destroy`; `fill`
 *   e importar `rayito/e2b` no llaman a AWS ni cargan el peer opcional.
 * Recursos y llamadas AWS: un secreto de Secrets Manager por `create`
 *   (`CreateSecretCommand`); `DescribeSecretCommand`, `PutSecretValueCommand`,
 *   `UpdateSecretCommand`, `ListSecretsCommand`, `DeleteSecretCommand`.
 * Coste aproximado: $0,40 por secreto y mes hasta `destroy` + $0,05 por 10 000
 *   llamadas (us-east-1, 2026-09-30).
 * IAM: la política `RayitoSecretsAdmin` de `infra/secrets-access.yaml`.
 * Cómo apagarla: no llames al CRUD; `Secret.destroy(name)` para dejar de pagar.
 * Ejemplo:
 *   import { Secret } from "rayito/e2b";
 *   await Secret.create("openai-key", "sk-...", { region: "us-east-1" });
 *   Secret.fill("openai-key"); // '${e2b.secrets.openai-key}' (no se resuelve)
 *   await Secret.destroy("openai-key");
 */
// biome-ignore lint/complexity/noStaticOnlyClass: nombre y forma públicos de E2B JS (`Secret.create`, …)
export class Secret {
  /** Las opciones que fija `new E2B({...}).Secret`. @internal */
  static readonly boundOpts: SecretConnectionOpts = Object.freeze({});

  /** Crea el secreto con su primer valor (versión 1). */
  static async create(
    name: string,
    value: string,
    opts: SecretCreateOpts = {},
  ): Promise<SecretInfo> {
    // biome-ignore lint/complexity/noThisInStatic: `this` es la subclase ligada de `new E2B(...).Secret`
    const store = resolveStore(this.boundOpts, opts);
    return store.create(normalizeSecretName(name), value, { metadata: opts.metadata });
  }

  /** Guarda `value` como versión nueva; `metadata`, si se da, sustituye la guardada. */
  static async update(
    secret: string,
    value: string,
    opts: SecretUpdateOpts = {},
  ): Promise<SecretInfo> {
    // biome-ignore lint/complexity/noThisInStatic: `this` es la subclase ligada de `new E2B(...).Secret`
    const store = resolveStore(this.boundOpts, opts);
    return store.update(secretSelector(secret), value, { metadata: opts.metadata });
  }

  /** Metadatos del secreto (`SecretNotFoundError` si no existe). */
  static async getInfo(secret: string, opts: SecretGetInfoOpts = {}): Promise<SecretInfo> {
    // biome-ignore lint/complexity/noThisInStatic: `this` es la subclase ligada de `new E2B(...).Secret`
    return resolveStore(this.boundOpts, opts).getInfo(secretSelector(secret));
  }

  /** Paginador sobre los secretos del prefijo. */
  static list(opts: SecretListOpts = {}): SecretPaginator {
    // biome-ignore lint/complexity/noThisInStatic: `this` es la subclase ligada de `new E2B(...).Secret`
    return new SecretPaginator(opts, this.boundOpts);
  }

  /** `true` si el secreto existe. */
  static async exists(secret: string, opts: SecretExistsOpts = {}): Promise<boolean> {
    // biome-ignore lint/complexity/noThisInStatic: `this` es la subclase ligada de `new E2B(...).Secret`
    return resolveStore(this.boundOpts, opts).exists(secretSelector(secret));
  }

  /** Lo borra sin ventana de recuperación; `false` si no existía. */
  static async destroy(secret: string, opts: SecretDestroyOpts = {}): Promise<boolean> {
    // biome-ignore lint/complexity/noThisInStatic: `this` es la subclase ligada de `new E2B(...).Secret`
    return resolveStore(this.boundOpts, opts).destroy(secretSelector(secret));
  }

  /** `${e2b.secrets.<name>}` sin llamar a AWS. Rayito no lo resuelve. */
  static fill(secret: string): string {
    if (typeof secret !== "string" || secret.length === 0 || INVALID_FILL_CHARS.test(secret)) {
      throw new InvalidArgumentError(
        "nombre de secreto no utilizable en un placeholder: no vacío y sin '{', '}' ni caracteres " +
          "de control",
      );
    }
    return `\${e2b.secrets.${secret}}`;
  }

  /** `UnimplementedError`: los MicroVMs no emiten tokens con audiencia. */
  static iamToken(_token: unknown): never {
    throw unimplemented("iam");
  }
}

/** `new E2B({...}).Secret`: la clase con `region` (y demás opciones de Rayito) del cliente. */
export function bindSecret(opts: SecretConnectionOpts): typeof Secret {
  const bound = Object.freeze({ ...opts });
  return class BoundSecret extends Secret {
    static override readonly boundOpts: SecretConnectionOpts = bound;
  };
}
