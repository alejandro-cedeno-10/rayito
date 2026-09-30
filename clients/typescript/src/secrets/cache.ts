/**
 * `SecretCache`: caché en memoria de valores de secretos, lo que hace que
 * `secrets` nunca traiga el secreto en cada llamada. Espejo de `SecretCache`
 * de `rayito/_secrets.py`.
 *
 * Coste y activación
 * -------------------
 * Activa: `secretCache: new SecretCache({ ttlSeconds: 300 })` fija la caché (y
 *   su TTL) que usa `secrets`; sin ella, `secrets` usa una caché compartida
 *   del proceso por (región, credenciales) con TTL 300, creada la primera vez.
 *   Construirla no llama a AWS ni carga el peer opcional.
 * Recursos y llamadas AWS: `GetSecretValueCommand` en el primer uso de cada
 *   (región, credenciales, secreto, versión) y otra vez sólo al vencer el TTL
 *   o con `refresh()`; un acierto hace 0 llamadas. Una sola promesa en vuelo
 *   por clave (10 llamadas a la vez = 1 lectura).
 * Coste aproximado: con TTL 300, ≤ 12 llamadas/hora por secreto y proceso ≈
 *   $0,04/mes ($0,05 por 10 000 llamadas, us-east-1, 2026-09-30); el secreto
 *   en sí cuesta $0,40/mes aparte.
 * IAM: `secretsmanager:GetSecretValue` (política `RayitoSecretsReader` de
 *   `infra/secrets-access.yaml`) y `kms:Decrypt` si el secreto usa una CMK.
 * Cómo apagarla: no pases `secrets` ni `secretCache`. `ttlSeconds: 0` no
 *   existe (sería traer en cada llamada): 1..86400.
 * Ejemplo:
 *   const secretCache = new SecretCache({ ttlSeconds: 600, region: "us-east-1" });
 *   const sbx = await Sandbox.create({ secrets: { OPENAI_API_KEY: "openai" }, secretCache });
 *   await sbx.commands.run("python agent.py"); // 1 GetSecretValue
 *   await sbx.commands.run("python agent.py"); // 0: acierto
 *   await secretCache.refresh("openai"); // fuerza una lectura nueva
 */

import { InvalidArgumentError } from "../errors.js";
import {
  asRef,
  DEFAULT_TTL_SECONDS,
  MASK,
  MAX_TTL_SECONDS,
  resolveSecretId,
  type SecretLike,
  SecretRef,
} from "./names.js";
import { SecretStore, type SecretStoreOptions } from "./store.js";

export interface SecretCacheOptions {
  /** Segundos de vida de un valor en caché: 1..86400, 300 por defecto. */
  readonly ttlSeconds?: number | undefined;
  /** Un `SecretStore` propio (excluyente con `region`/`credentials`/`prefix`). */
  readonly store?: SecretStore | undefined;
  readonly region?: SecretStoreOptions["region"];
  readonly credentials?: SecretStoreOptions["credentials"];
  readonly prefix?: SecretStoreOptions["prefix"];
  /** Reloj en ms: sólo para tests. */
  readonly now?: (() => number) | undefined;
}

interface Entry {
  readonly value: string;
  readonly expiresAt: number;
}

/** Ver el bloque "Coste y activación" del módulo. Los valores viven sólo en la memoria del proceso. */
export class SecretCache {
  readonly #ttlMs: number;
  readonly #store: SecretStore;
  readonly #now: () => number;
  readonly #entries = new Map<string, Entry>();
  readonly #flights = new Map<string, Promise<string>>();
  readonly #identities = new Map<unknown, number>();
  #generation = 0;

  constructor(options: SecretCacheOptions = {}) {
    const ttl = options.ttlSeconds ?? DEFAULT_TTL_SECONDS;
    if (typeof ttl !== "number" || !Number.isFinite(ttl) || ttl < 1 || ttl > MAX_TTL_SECONDS) {
      throw new InvalidArgumentError(
        `ttlSeconds debe estar en 1..${MAX_TTL_SECONDS}: una caché de 0 s traería el secreto en ` +
          "cada llamada",
      );
    }
    const clientOptions =
      options.region !== undefined ||
      options.credentials !== undefined ||
      options.prefix !== undefined;
    if (options.store !== undefined && clientOptions) {
      throw new InvalidArgumentError(
        "SecretCache: pasa store o region/credentials/prefix, no ambos",
      );
    }
    if (options.store !== undefined && !(options.store instanceof SecretStore)) {
      throw new InvalidArgumentError("store debe ser un SecretStore");
    }
    this.#ttlMs = ttl * 1000;
    this.#store =
      options.store ??
      new SecretStore({
        region: options.region,
        credentials: options.credentials,
        prefix: options.prefix,
      });
    this.#now = options.now ?? (() => performance.now());
  }

  get ttlSeconds(): number {
    return this.#ttlMs / 1000;
  }

  get store(): SecretStore {
    return this.#store;
  }

  /** El valor (de la caché si sigue vigente; si no, `GetSecretValue`, una sola vez por clave en vuelo). */
  get(secret: SecretLike): Promise<string> {
    const ref = asRef(secret);
    const key = this.#key(ref);
    const hit = this.#fresh(key);
    if (hit !== undefined) {
      return Promise.resolve(hit);
    }
    const inFlight = this.#flights.get(key);
    if (inFlight !== undefined) {
      return inFlight;
    }
    const generation = this.#generation;
    const land = (): void => {
      // Una lectura olvidada por `invalidate()` no borra la que la sustituyó.
      if (this.#flights.get(key) === flight) {
        this.#flights.delete(key);
      }
    };
    const flight: Promise<string> = this.#store.readValue(ref).then(
      (value) => {
        if (generation === this.#generation) {
          this.#entries.set(key, { value, expiresAt: this.#now() + this.#ttlMs });
        }
        land();
        return value;
      },
      (error: unknown) => {
        land();
        throw error;
      },
    );
    this.#flights.set(key, flight);
    return flight;
  }

  /**
   * Fuerza una lectura nueva: con `name`, la hace ya (y rechaza si no
   * existe); sin él, descarta todo y cada secreto se relee en su siguiente uso.
   */
  async refresh(name?: SecretLike): Promise<void> {
    this.invalidate(name);
    if (name !== undefined) {
      await this.get(name);
    }
  }

  /**
   * Descarta los valores de `name` (todas sus versiones si es un string) o de
   * todos. Sólo se olvidan las lecturas en vuelo de ese secreto: las de otros
   * siguen compartidas (una sola petición en vuelo por clave), y el contador
   * de generación impide que una lectura empezada antes guarde su valor.
   */
  invalidate(name?: SecretLike): void {
    this.#generation += 1;
    if (name === undefined) {
      this.#flights.clear();
      this.#entries.clear();
      return;
    }
    const ref = asRef(name);
    const secretPart = `|${resolveSecretId(ref.name, this.#store.prefix)}|`;
    const exact = name instanceof SecretRef ? ref.selector : undefined;
    const matches = (key: string): boolean =>
      key.includes(secretPart) && (exact === undefined || key.endsWith(`|${exact}`));
    for (const map of [this.#entries, this.#flights]) {
      for (const key of [...map.keys()]) {
        if (matches(key)) {
          map.delete(key);
        }
      }
    }
  }

  toJSON(): Record<string, unknown> {
    return { ttlSeconds: this.ttlSeconds, entries: this.#entries.size, values: MASK };
  }

  toString(): string {
    return `SecretCache(ttlSeconds=${this.ttlSeconds}, entries=${this.#entries.size}, values=${MASK})`;
  }

  [Symbol.for("nodejs.util.inspect.custom")](): string {
    return this.toString();
  }

  #key(ref: SecretRef): string {
    const [region, credentials] = this.#store.cacheIdentity;
    return [
      region ?? "",
      this.#identity(credentials),
      resolveSecretId(ref.name, this.#store.prefix),
      ref.selector,
    ].join("|");
  }

  /** Un número estable por objeto de credenciales (proveedor) dentro de esta caché. */
  #identity(credentials: unknown): number {
    let id = this.#identities.get(credentials);
    if (id === undefined) {
      id = this.#identities.size;
      this.#identities.set(credentials, id);
    }
    return id;
  }

  #fresh(key: string): string | undefined {
    const entry = this.#entries.get(key);
    if (entry === undefined) {
      return undefined;
    }
    if (entry.expiresAt <= this.#now()) {
      this.#entries.delete(key);
      return undefined;
    }
    return entry.value;
  }
}
