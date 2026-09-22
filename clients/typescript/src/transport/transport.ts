/**
 * Transporte gRPC hacia `rayd` a través del proxy de AWS con
 * `createGrpcTransport` de `@connectrpc/connect-node` (HTTP/2 únicamente; la
 * v2 no tiene opción `httpVersion`). Un transporte es un `Http2SessionManager`
 * y por tanto una conexión: el `Sandbox` abre como máximo dos (unarios y
 * streams largos) y las cierra en `close()` con `sessionManager.abort()`, el
 * equivalente de `channel.close()` en Python. No hay backoff de reconexión
 * que acotar: tras un fallo la siguiente petición abre una sesión nueva.
 */

import type { SecureClientSessionOptions } from "node:http2";
import type { Interceptor, Transport } from "@connectrpc/connect";
import { createGrpcTransport, Http2SessionManager } from "@connectrpc/connect-node";
import { InvalidArgumentError } from "../errors.js";
import { ENDPOINT_TLS_PORT } from "../limits.js";

export const IDLE_CONNECTION_TIMEOUT_MS = 15 * 60_000;
const LOOPBACK_HOSTS: ReadonlySet<string> = new Set(["127.0.0.1", "::1", "[::1]", "localhost"]);

/**
 * `scheme: "http"` (h2c en claro) sólo se admite hacia loopback (`127.0.0.1`,
 * `::1`, `localhost`): existe para el `rayd` falso de los tests. Hacia
 * cualquier otro host `openTransport` lanza `InvalidArgumentError`, porque el
 * JWE del proxy y `x-access-token` viajarían sin cifrar; el SDK Python no
 * tiene forma de hacerlo (`grpc.secure_channel` siempre).
 */
export interface TransportSettings {
  readonly scheme: "https" | "http";
  readonly port: number;
  readonly pingIntervalMs: number;
  readonly pingTimeoutMs: number;
  readonly pingIdleConnection: boolean;
  readonly readMaxBytes: number;
  readonly nodeOptions?: SecureClientSessionOptions | undefined;
}

/**
 * `pingIdleConnection` queda en `false` (Python sí pinga canales ociosos con
 * `keepalive_permit_without_calls`): en connect-node 2.2.0 el handler `close`
 * de cada stream re-arma el temporizador de PING cuando la sesión se queda sin
 * streams, también después de que `sessionManager.abort()` haya destruido la
 * sesión y quitado sus listeners; con pings ociosos ese temporizador (unref)
 * llama `ping()` sobre la sesión destruida 30 s después de `close()` y mata el
 * proceso con un `ERR_HTTP2_INVALID_SESSION` no capturado. Sin pings ociosos el
 * re-armado con cero streams es un no-op, y la vida de una sesión callada la
 * sigue comprobando `requiresVerify()` (un PING antes de la primera petición
 * tras más de `pingIntervalMs` sin tráfico), así que el contrato de M5 no
 * cambia.
 */
export const DEFAULT_TRANSPORT_SETTINGS: TransportSettings = Object.freeze({
  scheme: "https",
  port: ENDPOINT_TLS_PORT,
  pingIntervalMs: 30_000,
  pingTimeoutMs: 10_000,
  pingIdleConnection: false,
  readMaxBytes: 64 * 1024 * 1024,
});

/** Un transporte y la sesión HTTP/2 que lo respalda, para poder cerrarla. */
export interface OpenedTransport {
  readonly transport: Transport;
  readonly sessionManager: Http2SessionManager;
}

export function resolveTransportSettings(
  overrides: Partial<TransportSettings> | undefined,
): TransportSettings {
  return Object.freeze({ ...DEFAULT_TRANSPORT_SETTINGS, ...(overrides ?? {}) });
}

const EXPLICIT_PORT = /^(\[[^\]]+\]|[^:]+):(\d+)$/;

/** `host:puerto` o `[v6]:puerto`; una IPv6 pelada (`::1`) no lleva puerto. */
export function hostWithoutPort(host: string): string {
  const match = EXPLICIT_PORT.exec(host);
  return match === null ? host : (match[1] as string);
}

/** Un endpoint con puerto explícito (`127.0.0.1:4242`, los `rayd` falsos por plaza) manda sobre `settings.port`. */
export function baseUrl(host: string, settings: TransportSettings): string {
  if (EXPLICIT_PORT.test(host)) {
    return `${settings.scheme}://${host}`;
  }
  return `${settings.scheme}://${host}:${settings.port}`;
}

export function isLoopbackHost(host: string): boolean {
  return LOOPBACK_HOSTS.has(hostWithoutPort(host).toLowerCase());
}

export function assertPlaintextAllowed(host: string, settings: TransportSettings): void {
  if (settings.scheme === "http" && !isLoopbackHost(host)) {
    throw new InvalidArgumentError(
      `transport.scheme "http" sólo se admite hacia loopback; ${host} requiere "https"`,
    );
  }
}

export function openTransport(
  host: string,
  settings: TransportSettings,
  interceptor: Interceptor,
): OpenedTransport {
  assertPlaintextAllowed(host, settings);
  const url = baseUrl(host, settings);
  const sessionManager = new Http2SessionManager(
    url,
    {
      pingIntervalMs: settings.pingIntervalMs,
      pingTimeoutMs: settings.pingTimeoutMs,
      pingIdleConnection: settings.pingIdleConnection,
      idleConnectionTimeoutMs: IDLE_CONNECTION_TIMEOUT_MS,
    },
    settings.nodeOptions,
  );
  const transport = createGrpcTransport({
    baseUrl: url,
    interceptors: [interceptor],
    readMaxBytes: settings.readMaxBytes,
    sessionManager,
  });
  return { transport, sessionManager };
}
