/**
 * `ConnectionOpts` y `ConnectionConfig` de E2B JS 2.51 sobre Rayito:
 * `requestTimeoutMs`, `retries`, `logger`, `headers` (metadata gRPC hacia
 * `rayd`), `proxy` y `signal` se aplican; `apiKey`, `domain`, `debug`,
 * `apiUrl`, `sandboxUrl`, `apiHeaders` y `validateApiKey` avisan y se
 * ignoran. `region`, `controlPlane`, `accessToken` y `transport` son los
 * enlaces propios de Rayito.
 */

import type { ControlPlane } from "../aws/control-plane.js";
import type { Logger } from "../logger.js";
import { DEFAULT_REQUEST_TIMEOUT_MS } from "../sandbox/launch.js";
import { validateExtraHeaders } from "../transport/headers.js";
import { validateProxyUrl } from "../transport/proxy-tunnel.js";
import type { TransportSettings } from "../transport/transport.js";
import { validateIntegration, validateRetries } from "../validation.js";

/** El nombre E2B de un usuario del sandbox. */
export type Username = string;

export interface ConnectionOpts {
  readonly requestTimeoutMs?: number | undefined;
  readonly retries?: number | undefined;
  readonly logger?: Logger | undefined;
  /** Metadata gRPC extra hacia `rayd`; nunca `x-aws-proxy-*` ni `x-access-token`. */
  readonly headers?: Readonly<Record<string, string>> | undefined;
  /** `http://[user:pass@]host:puerto` para el endpoint y el plano de control. */
  readonly proxy?: string | undefined;
  readonly signal?: AbortSignal | undefined;
  readonly apiKey?: string | undefined;
  readonly validateApiKey?: boolean | undefined;
  readonly domain?: string | undefined;
  readonly debug?: boolean | undefined;
  readonly apiUrl?: string | undefined;
  readonly sandboxUrl?: string | undefined;
  readonly apiHeaders?: Readonly<Record<string, string>> | undefined;
  readonly region?: string | undefined;
  readonly controlPlane?: ControlPlane | undefined;
  readonly accessToken?: string | undefined;
  readonly transport?: Partial<TransportSettings> | undefined;
}

/**
 * La configuración efectiva de un sandbox del shim. `setIntegration` se
 * llama una vez al arrancar: los planos de control construidos después
 * añaden el nombre al User-Agent (los ya construidos no cambian, como en E2B).
 */
export class ConnectionConfig {
  static #integration: string | undefined;

  readonly requestTimeoutMs: number;
  readonly retries: number | undefined;
  readonly headers: Readonly<Record<string, string>>;
  readonly proxy: string | undefined;
  readonly logger: Logger | undefined;
  readonly region: string | undefined;
  /** El `setIntegration` vigente al construir esta configuración. */
  readonly integration: string | undefined;

  constructor(opts: ConnectionOpts = {}) {
    this.requestTimeoutMs = opts.requestTimeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS;
    this.retries = validateRetries(opts.retries);
    this.headers = validateExtraHeaders(opts.headers) ?? Object.freeze({});
    this.proxy = validateProxyUrl(opts.proxy);
    this.logger = opts.logger;
    this.region = opts.region;
    this.integration = ConnectionConfig.#integration;
    Object.freeze(this);
  }

  static setIntegration(integration: string | undefined): void {
    ConnectionConfig.#integration = validateIntegration(integration);
  }

  static get integration(): string | undefined {
    return ConnectionConfig.#integration;
  }

  getRequestTimeoutMs(requestTimeoutMs?: number): number {
    return requestTimeoutMs ?? this.requestTimeoutMs;
  }
}
