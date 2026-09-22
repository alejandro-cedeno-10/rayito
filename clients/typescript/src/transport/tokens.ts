/**
 * JWE del proxy: `ProxyToken`, `TokenStore` y `TokenRefresher`.
 *
 * El refresher renueva a los 45 min (TTL duro de 60) con una cadena de
 * `setTimeout` sin referencia (`unref`) para que un proceso ocioso pueda
 * salir; si AWS falla reintenta cada 60 s. No se para en pausa: la expiración
 * del token es en reloj de pared.
 */

import { PortSpec, samePortSet } from "../aws/control-plane.js";
import { errorMessage } from "../errors.js";
import { TOKEN_REFRESH_AFTER_MINUTES, TOKEN_REFRESH_RETRY_SECONDS } from "../limits.js";
import type { Logger } from "../logger.js";

export const TOKEN_REFRESH_AFTER_MS = TOKEN_REFRESH_AFTER_MINUTES * 60_000;
export const TOKEN_REFRESH_RETRY_MS = TOKEN_REFRESH_RETRY_SECONDS * 1000;

export type WallClock = () => number;
export type TokenMinter = (ports: readonly PortSpec[]) => Promise<string>;

/** Un JWE de `create-microvm-auth-token` y los puertos que cubre. */
export class ProxyToken {
  readonly jwe: string;
  readonly ports: readonly PortSpec[];
  readonly mintedAt: number;

  constructor(jwe: string, ports: readonly PortSpec[], mintedAt: number) {
    this.jwe = jwe;
    this.ports = Object.freeze([...ports]);
    this.mintedAt = mintedAt;
    Object.freeze(this);
  }

  covers(port: number): boolean {
    return this.ports.some((spec) => spec.covers(port));
  }

  refreshDue(now: number): boolean {
    return now - this.mintedAt >= TOKEN_REFRESH_AFTER_MS;
  }

  msUntilRefresh(now: number): number {
    return Math.max(0, this.mintedAt + TOKEN_REFRESH_AFTER_MS - now);
  }
}

/** Tokens vivos de un sandbox, indexados por el conjunto de puertos que cubren. */
export class TokenStore {
  #tokens: ProxyToken[] = [];

  put(token: ProxyToken): void {
    this.#tokens = [...this.#tokens.filter((t) => !samePortSet(t.ports, token.ports)), token];
  }

  tokenFor(port: number): ProxyToken | undefined {
    return this.#tokens.find((token) => token.covers(port));
  }

  jweFor(port: number): string | undefined {
    return this.tokenFor(port)?.jwe;
  }

  tokens(): readonly ProxyToken[] {
    return [...this.#tokens];
  }

  clear(): void {
    this.#tokens = [];
  }
}

export interface TokenRefresherOptions {
  readonly now?: WallClock | undefined;
  readonly logger?: Logger | undefined;
}

/**
 * Renueva los JWE del `TokenStore` a los 45 min. `refreshDue` es
 * determinista (reloj inyectable) para poder testearlo; `start()` lo programa
 * con timers sin referencia. Dos acuñaciones concurrentes del mismo conjunto
 * de puertos comparten una sola llamada al plano de control.
 */
export class TokenRefresher {
  readonly store: TokenStore;
  readonly #mint: TokenMinter;
  readonly #now: WallClock;
  readonly #logger: Logger | undefined;
  #timer: ReturnType<typeof setTimeout> | undefined;
  readonly #inflight = new Map<string, Promise<ProxyToken>>();

  constructor(store: TokenStore, mint: TokenMinter, options: TokenRefresherOptions = {}) {
    this.store = store;
    this.#mint = mint;
    this.#now = options.now ?? Date.now;
    this.#logger = options.logger;
  }

  get scheduled(): boolean {
    return this.#timer !== undefined;
  }

  mint(ports: readonly PortSpec[]): Promise<ProxyToken> {
    const key = ports.map((spec) => `${spec.start}-${spec.end}`).join(",");
    const pending = this.#inflight.get(key);
    if (pending !== undefined) {
      return pending;
    }
    const minting = this.#mintNow(ports).finally(() => this.#inflight.delete(key));
    this.#inflight.set(key, minting);
    return minting;
  }

  /** Reutiliza un token que ya cubra el puerto o acuña uno de un solo puerto. */
  ensure(port: number): Promise<ProxyToken> {
    const existing = this.store.tokenFor(port);
    if (existing !== undefined) {
      return Promise.resolve(existing);
    }
    return this.mint([PortSpec.single(port)]);
  }

  async refreshAll(): Promise<void> {
    for (const token of this.store.tokens()) {
      await this.mint(token.ports);
    }
  }

  /** Reacuña los tokens vencidos; `false` si alguno falló (se reintenta). */
  async refreshDue(now?: number): Promise<boolean> {
    const current = now ?? this.#now();
    let ok = true;
    for (const token of this.store.tokens()) {
      if (!token.refreshDue(current)) {
        continue;
      }
      try {
        await this.mint(token.ports);
      } catch (error) {
        this.#logger?.warn?.(
          `no se pudo renovar el token del proxy; reintento en ${TOKEN_REFRESH_RETRY_SECONDS} s`,
          { reason: errorMessage(error) },
        );
        ok = false;
      }
    }
    return ok;
  }

  msUntilNextRefresh(now?: number): number {
    const current = now ?? this.#now();
    const tokens = this.store.tokens();
    if (tokens.length === 0) {
      return TOKEN_REFRESH_AFTER_MS;
    }
    return Math.min(...tokens.map((token) => token.msUntilRefresh(current)));
  }

  start(): void {
    if (this.#timer !== undefined) {
      return;
    }
    this.#schedule(this.msUntilNextRefresh());
  }

  stop(): void {
    if (this.#timer !== undefined) {
      clearTimeout(this.#timer);
      this.#timer = undefined;
    }
  }

  async #mintNow(ports: readonly PortSpec[]): Promise<ProxyToken> {
    const token = new ProxyToken(await this.#mint(ports), ports, this.#now());
    this.store.put(token);
    return token;
  }

  #schedule(delayMs: number): void {
    const timer = setTimeout(() => {
      void this.#tick();
    }, delayMs);
    timer.unref?.();
    this.#timer = timer;
  }

  async #tick(): Promise<void> {
    if (this.#timer === undefined) {
      return;
    }
    const refreshed = await this.refreshDue();
    if (this.#timer === undefined) {
      return;
    }
    this.#schedule(refreshed ? this.msUntilNextRefresh() : TOKEN_REFRESH_RETRY_MS);
  }
}
