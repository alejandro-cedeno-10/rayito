/**
 * Inyección nativa de secretos (M13a): `secrets: { ENV: "nombre" | SecretRef }`
 * en `Sandbox.create()`, `connect()`, `SandboxPool.take()`, `commands.run`,
 * `pty.create`, `runCode` y `createCodeContext` entrega el valor como
 * variable de entorno del proceso que lo pide, por los `envs` que esas
 * llamadas ya mandan a `rayd` por el canal autenticado (sin RPC nueva). El
 * handle guarda sólo las referencias; nunca viajan en el `runHookPayload`,
 * `metadata`, etiquetas ni en el entorno de la imagen. Espejo de la parte de
 * inyección de `rayito/_secrets.py`.
 *
 * Coste y activación
 * -------------------
 * Activa: `secrets` (Python: `secrets=`) inyecta secretos de Secrets Manager
 *   como variables de entorno; `secretCache` fija la caché (si no, una compartida por región y
 *   credenciales con TTL 300 s). Sin `secrets` ni `secretCache`, Rayito no
 *   importa `@aws-sdk/client-secrets-manager` ni hace ninguna llamada (el
 *   camino de 0.4.0).
 * Recursos y llamadas AWS: `GetSecretValueCommand` una vez por secreto y TTL
 *   de `SecretCache`, nunca en cada comando; ningún recurso nuevo.
 * Coste aproximado: $0,05 por 10 000 llamadas + $0,40 por secreto y mes
 *   (us-east-1, consultado 2026-09-30); ≈ $0,04/mes por secreto y proceso
 *   con el TTL por defecto.
 * IAM: `secretsmanager:GetSecretValue` (y `DescribeSecret`) sobre
 *   `…:secret:rayito/*` en las credenciales del LLAMANTE, no en el execution
 *   role (política `RayitoSecretsReader` de `infra/secrets-access.yaml`).
 * Cómo apagarla: no pases `secrets` ni `secretCache` (o pásalos `undefined`).
 * Ejemplo:
 *   await new SecretStore().create("openai", key);
 *   const sbx = await Sandbox.create({ secrets: { OPENAI_API_KEY: "openai" } });
 *   await sbx.commands.run("python agent.py");
 *
 * Fase 1: el código del sandbox PUEDE leer un secreto inyectado. Para código
 * no confiable inyecta sólo tokens de vida corta y mínimo privilegio.
 */

import { InvalidArgumentError } from "../errors.js";
import { SecretCache } from "./cache.js";
import { asRef, MASK, type SecretLike, type SecretRef } from "./names.js";
import type { SecretStoreOptions } from "./store.js";

export type SecretsInput = Readonly<Record<string, SecretLike>>;

export const SECRET_VISIBILITY_WARNING =
  "secrets entrega el valor como variable de entorno: el valor es visible para el código del " +
  "sandbox (fase 1). Para código no confiable inyecta sólo tokens de vida corta y mínimo " +
  "privilegio (docs/site/docs/secrets.md)";
const COMPAT_WARNING_TYPE = "RayitoCompatWarning";

let visibilityWarned = false;

/** Sólo para tests: vuelve a permitir el aviso de la primera vez. */
export function resetVisibilityWarningForTests(): void {
  visibilityWarned = false;
}

function warnVisibilityOnce(): void {
  if (!visibilityWarned) {
    visibilityWarned = true;
    process.emitWarning(SECRET_VISIBILITY_WARNING, { type: COMPAT_WARNING_TYPE });
  }
}

/** `{ ENV: string | SecretRef }` → `Map<ENV, SecretRef>`; los errores nombran la clave, nunca el secreto. */
export function normalizeSecrets(secrets: SecretsInput | undefined): Map<string, SecretRef> {
  const refs = new Map<string, SecretRef>();
  if (secrets === undefined) {
    return refs;
  }
  if (typeof secrets !== "object" || secrets === null || Array.isArray(secrets)) {
    throw new InvalidArgumentError("secrets debe ser un objeto { VARIABLE: nombre | SecretRef }");
  }
  for (const [key, secret] of Object.entries(secrets)) {
    if (key.length === 0 || key.includes("=") || key.includes("\0")) {
      throw new InvalidArgumentError(
        "clave de secrets inválida: una variable de entorno no vacía, sin '=' ni NUL",
      );
    }
    if (typeof secret === "string" ? secret.length === 0 : typeof secret !== "object") {
      throw new InvalidArgumentError(
        `secrets['${key}'] debe ser un nombre no vacío o un SecretRef`,
      );
    }
    refs.set(key, asRef(secret));
  }
  if (refs.size > 0) {
    warnVisibilityOnce();
  }
  return refs;
}

export function validateSecretCache(secretCache: unknown): SecretCache | undefined {
  if (secretCache === undefined || secretCache instanceof SecretCache) {
    return secretCache;
  }
  throw new InvalidArgumentError("secretCache debe ser un SecretCache");
}

/**
 * Lo que guarda un handle de `secrets`/`secretCache`: sólo las referencias
 * ENV → `SecretRef`, nunca valores. `JSON.stringify`/`inspect` enseñan sólo
 * cuántas variables hay.
 */
export class SecretBinding {
  readonly refs: ReadonlyMap<string, SecretRef>;
  readonly cache: SecretCache | undefined;

  constructor(refs: ReadonlyMap<string, SecretRef>, cache: SecretCache | undefined) {
    this.refs = refs;
    this.cache = cache;
    Object.freeze(this);
  }

  /** Las referencias como objeto plano (para `reincarnate()`, que relanza con las del handle). */
  toRecord(): Record<string, SecretRef> {
    return Object.fromEntries(this.refs);
  }

  toJSON(): Record<string, unknown> {
    return { envs: `<${this.refs.size} keys>`, values: MASK };
  }

  [Symbol.for("nodejs.util.inspect.custom")](): string {
    return `SecretBinding(envs=<${this.refs.size} keys>, values=${MASK})`;
  }
}

/** `undefined` cuando no se pidió nada: el camino de 0.4.0. */
export function bindSecrets(
  secrets: SecretsInput | undefined,
  secretCache: SecretCache | undefined,
): SecretBinding | undefined {
  const cache = validateSecretCache(secretCache);
  const refs = normalizeSecrets(secrets);
  if (refs.size === 0 && cache === undefined) {
    return undefined;
  }
  return new SecretBinding(refs, cache);
}

export type CacheFactory = () => SecretCache;

/**
 * Una caché por (región, credenciales), con tope: un proceso que crea un
 * proveedor de credenciales por tenant o por petición no acumula cachés (ni
 * valores) sin límite. Al pasar del tope se descarta la usada hace más tiempo
 * (sólo cuesta releer después).
 */
export const MAX_SHARED_CACHES = 32;

interface SharedEntry {
  readonly region: string | undefined;
  readonly credentials: unknown;
  readonly cache: SecretCache;
}

/** Orden de uso: la primera es la usada hace más tiempo. */
const sharedCaches: SharedEntry[] = [];

/**
 * La caché del proceso para `secrets` sin `secretCache`: una por (región,
 * credenciales), TTL 300. Cada vez que se pide una, todas descartan sus
 * valores vencidos; como mucho hay `MAX_SHARED_CACHES`.
 */
export function sharedSecretCache(
  region: string | undefined,
  credentials: SecretStoreOptions["credentials"],
): SecretCache {
  for (const entry of sharedCaches) {
    entry.cache.sweep();
  }
  const index = sharedCaches.findIndex(
    (entry) => entry.region === region && entry.credentials === credentials,
  );
  if (index !== -1) {
    const [entry] = sharedCaches.splice(index, 1) as [SharedEntry];
    sharedCaches.push(entry);
    return entry.cache;
  }
  const cache = new SecretCache({ region, credentials });
  sharedCaches.push({ region, credentials, cache });
  while (sharedCaches.length > MAX_SHARED_CACHES) {
    sharedCaches.shift();
  }
  return cache;
}

/** Sólo para tests: cuántas cachés compartidas hay vivas. */
export function sharedSecretCacheCount(): number {
  return sharedCaches.length;
}

/**
 * Fija la caché del handle (la de `secretCache` o la compartida) y resuelve
 * sus secretos antes de lanzar, conectar o tomar una plaza: un secreto que
 * falta falla aquí, sin haber facturado un VM.
 */
export async function warm(
  binding: SecretBinding | undefined,
  defaultCache: CacheFactory,
): Promise<SecretBinding | undefined> {
  if (binding === undefined) {
    return undefined;
  }
  const bound =
    binding.cache === undefined ? new SecretBinding(binding.refs, defaultCache()) : binding;
  const cache = bound.cache as SecretCache;
  await Promise.all([...bound.refs.values()].map((ref) => cache.get(ref)));
  return bound;
}

/**
 * Los `envs` de una llamada con los secretos del handle y de la llamada ya
 * resueltos (la llamada gana en una clave repetida). Una clave que además está
 * en `envs` es `InvalidArgumentError` (todo o nada: nunca se pisa en
 * silencio). Sin secretos devuelve `envs` tal cual.
 */
export async function secretEnvs(
  envs: Readonly<Record<string, string>> | undefined,
  options: {
    readonly bound: SecretBinding | undefined;
    readonly secrets: SecretsInput | undefined;
    readonly defaultCache: CacheFactory;
    readonly includeBound?: boolean | undefined;
  },
): Promise<Readonly<Record<string, string>> | undefined> {
  const refs = new Map<string, SecretRef>(
    options.bound !== undefined && (options.includeBound ?? true) ? options.bound.refs : [],
  );
  for (const [key, ref] of normalizeSecrets(options.secrets)) {
    refs.set(key, ref);
  }
  if (refs.size === 0) {
    return envs;
  }
  const clash = [...refs.keys()]
    .sort()
    .find((key) => envs !== undefined && Object.hasOwn(envs, key));
  if (clash !== undefined) {
    throw new InvalidArgumentError(
      `la variable '${clash}' está a la vez en envs y en secrets: pásala sólo en uno`,
    );
  }
  const cache = options.bound?.cache ?? options.defaultCache();
  const entries = await Promise.all(
    [...refs].map(async ([key, ref]) => [key, await cache.get(ref)] as const),
  );
  return { ...(envs ?? {}), ...Object.fromEntries(entries) };
}

const PYTHON_LANGUAGES = new Set(["python", "python3"]);

/**
 * Si una celda de `runCode` puede llevar los secretos del handle:
 * `ExecuteRequest.envs` sólo existe en contextos Python. `contextLanguage` es
 * el lenguaje del contexto destino si se conoce con certeza (`undefined` =
 * desconocido: un id que este cliente no creó ni listó). Con `secrets` en la
 * propia llamada sobre otro lenguaje conocido es `InvalidArgumentError`
 * apuntando a `createCodeContext`; los del handle sólo se añaden a celdas
 * Python conocidas (devuelve `false` en otro caso).
 */
export function codeSecretsScope(
  language: string | undefined,
  contextLanguage: string | undefined,
  secrets: SecretsInput | undefined,
): boolean {
  const target = language ?? contextLanguage;
  if (target === undefined) {
    return false;
  }
  if (PYTHON_LANGUAGES.has(target.toLowerCase())) {
    return true;
  }
  if (secrets !== undefined && Object.keys(secrets).length > 0) {
    throw new InvalidArgumentError(
      "runCode({ secrets }) sólo vale en contextos Python (ExecuteRequest.envs); para otros " +
        "lenguajes usa createCodeContext({ language, secrets })",
    );
  }
  return false;
}

/**
 * Las opciones `secrets`/`secretCache` de `Sandbox.create()`,
 * `Sandbox.connect()`, `sbx.connect()` y `pool.take()`.
 */
export interface SecretOptions {
  /**
   * Secretos de Secrets Manager como variables de entorno de cada comando, PTY,
   * celda Python o contexto de este handle: `{ ENV: "nombre" | SecretRef }`.
   * El handle guarda sólo las referencias; se resuelven (y quedan en caché)
   * antes de lanzar o conectar, y nunca viajan en el `runHookPayload`.
   *
   * Coste y activación
   * -------------------
   * Activa: la inyección de secretos (apagada si falta).
   * Recursos y llamadas AWS: `GetSecretValueCommand` una vez por secreto y TTL
   *   de `SecretCache` (300 s por defecto); ningún recurso nuevo.
   * Coste aproximado: $0,05 por 10 000 llamadas + $0,40 por secreto y mes
   *   (us-east-1, 2026-09-30).
   * IAM: `secretsmanager:GetSecretValue` en las credenciales del llamante.
   * Cómo apagarla: no pases `secrets` (por defecto `undefined`).
   * Ejemplo:
   *   const sbx = await Sandbox.create({ secrets: { OPENAI_API_KEY: "openai" } });
   *   await sbx.commands.run("python agent.py");
   */
  readonly secrets?: SecretsInput | undefined;
  /**
   * La caché (y su TTL) de `secrets`; sin ella, una compartida por región y
   * credenciales con TTL 300 s. Clave: (región, credenciales, secreto,
   * versión/etapa); una sola lectura en vuelo por clave.
   *
   * Coste y activación
   * -------------------
   * Activa: fija la caché que usan los `secrets` de este handle (sólo tiene
   *   efecto junto con `secrets`); construir `SecretCache` no llama a AWS.
   * Recursos y llamadas AWS: `GetSecretValueCommand` en el primer uso de cada
   *   secreto y otra vez sólo al vencer `ttlSeconds` (300 por defecto,
   *   1..86400) o tras `refresh()`/`invalidate()`; un acierto hace 0 llamadas.
   * Coste aproximado: ≤ 12 llamadas/hora por secreto y proceso con TTL 300 ≈
   *   $0,04/mes ($0,05 por 10 000 llamadas, us-east-1, 2026-09-30).
   * IAM: `secretsmanager:GetSecretValue` en las credenciales del llamante (y
   *   `kms:Decrypt` con una CMK).
   * Cómo apagarla: no la pases (por defecto `undefined`) ni pases `secrets`.
   * Ejemplo:
   *   const secretCache = new SecretCache({ ttlSeconds: 600 });
   *   const sbx = await Sandbox.create({ secrets: { OPENAI_API_KEY: "openai" }, secretCache });
   */
  readonly secretCache?: SecretCache | undefined;
}

/**
 * Los secretos de un handle y cómo resolverlos para cada llamada. Lo
 * comparten `Commands`, `Pty` y `CodeClient` del mismo `Sandbox`.
 */
export class SecretEnvs {
  #binding: SecretBinding | undefined;
  readonly #defaultCache: CacheFactory;

  constructor(defaultCache: CacheFactory) {
    this.#defaultCache = defaultCache;
  }

  get binding(): SecretBinding | undefined {
    return this.#binding;
  }

  /** Fija una vinculación ya resuelta (`create`, `take`). */
  set(binding: SecretBinding | undefined): void {
    this.#binding = binding;
  }

  /**
   * `connect({ secrets, secretCache })` sobre el handle: `secrets` sustituye
   * las referencias y `secrets: {}` las borra todas (conservando la caché);
   * sin `secrets` se conservan (y un `secretCache` sólo cambia la caché). Sin
   * nada pedido no toca nada.
   */
  async rebind(options: SecretOptions): Promise<void> {
    const cache = validateSecretCache(options.secretCache);
    if (options.secrets === undefined) {
      if (cache === undefined) {
        return;
      }
      this.#binding = await warm(
        new SecretBinding(this.#binding?.refs ?? new Map(), cache),
        this.#defaultCache,
      );
      return;
    }
    const binding = bindSecrets(options.secrets, cache);
    if (binding !== undefined && binding.refs.size > 0) {
      this.#binding = await warm(binding, this.#defaultCache);
      return;
    }
    // `secrets: {}`: el handle se queda sin secretos; la caché (la pasada o la
    // que ya tenía) se conserva para los `secrets` por llamada.
    const kept = cache ?? this.#binding?.cache;
    this.#binding = kept === undefined ? undefined : new SecretBinding(new Map(), kept);
  }

  /** Los `envs` de una llamada con los secretos ya resueltos desde la caché. */
  apply(
    envs: Readonly<Record<string, string>> | undefined,
    secrets: SecretsInput | undefined,
    includeBound = true,
  ): Promise<Readonly<Record<string, string>> | undefined> {
    if (this.#binding === undefined && secrets === undefined) {
      return Promise.resolve(envs);
    }
    return secretEnvs(envs, {
      bound: this.#binding,
      secrets,
      defaultCache: this.#defaultCache,
      includeBound,
    });
  }

  toJSON(): Record<string, unknown> {
    return { envs: `<${this.#binding?.refs.size ?? 0} keys>`, values: MASK };
  }
}
