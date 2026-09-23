/**
 * Cabeceras del proxy de AWS en cada RPC (ARCHITECTURE.md, Capa 3):
 * `x-aws-proxy-auth` (el JWE, leído del `TokenStore` en cada llamada, así
 * rotarlo nunca toca el transporte), `x-aws-proxy-port`,
 * `x-aws-proxy-force-h2` (`rayd` sirve h2c) y `x-access-token`. Node las
 * envía en minúsculas de todas formas; las constantes lo hacen explícito.
 */

import type { Interceptor } from "@connectrpc/connect";
import { AuthenticationError, InvalidArgumentError } from "../errors.js";
import type { TokenStore } from "./tokens.js";

export const PROXY_AUTH_HEADER = "x-aws-proxy-auth";
export const PROXY_PORT_HEADER = "x-aws-proxy-port";
export const PROXY_FORCE_H2_HEADER = "x-aws-proxy-force-h2";
export const ACCESS_TOKEN_HEADER = "x-access-token";

/** Lo que `extraHeaders`/`headers=` nunca puede fijar: el proxy, el token y lo que gestiona gRPC. */
export const RESERVED_METADATA_KEYS: ReadonlySet<string> = new Set([
  PROXY_AUTH_HEADER,
  PROXY_PORT_HEADER,
  PROXY_FORCE_H2_HEADER,
  ACCESS_TOKEN_HEADER,
  "rayito-compress",
  "user-agent",
  "content-type",
  "te",
  "host",
]);
export const RESERVED_METADATA_PREFIXES: readonly string[] = ["x-aws-proxy-", "grpc-", ":"];
const HEADER_TOKEN = /^[!#$%&'*+\-.^_`|~0-9a-z]+$/;
const PRINTABLE_ASCII = /^[\x20-\x7e]*$/;

export interface ProxyAuthOptions {
  readonly port: number;
  readonly accessToken: string | undefined;
  /** Metadata extra del caller, ya validada: va detrás de las cuatro cabeceras reservadas. */
  readonly extraHeaders?: Readonly<Record<string, string>> | undefined;
}

function isReservedKey(key: string): boolean {
  return (
    RESERVED_METADATA_KEYS.has(key) ||
    RESERVED_METADATA_PREFIXES.some((prefix) => key.startsWith(prefix)) ||
    key.endsWith("-bin") ||
    !HEADER_TOKEN.test(key)
  );
}

/**
 * Las reglas de `headers=` (D6): claves en minúsculas y token RFC 9110, sin
 * las reservadas ni los prefijos `x-aws-proxy-`, `grpc-` y `:`, ni `-bin`;
 * valores ASCII imprimible. Los mensajes nombran la clave, nunca el valor.
 */
export function validateExtraHeaders(
  headers: Readonly<Record<string, string>> | undefined,
): Readonly<Record<string, string>> | undefined {
  if (headers === undefined) {
    return undefined;
  }
  const validated: Record<string, string> = {};
  for (const [rawKey, value] of Object.entries(headers)) {
    const key = rawKey.toLowerCase();
    if (isReservedKey(key)) {
      throw new InvalidArgumentError(`headers: la clave '${key}' está reservada`);
    }
    if (typeof value !== "string" || !PRINTABLE_ASCII.test(value)) {
      throw new InvalidArgumentError(`headers: el valor de '${key}' no es ASCII imprimible`);
    }
    validated[key] = value;
  }
  return Object.freeze(validated);
}

/**
 * Añade las cuatro cabeceras a cada request (unarios y streams) y después la
 * metadata extra. Sin token para el puerto falla antes de enviar nada.
 * `accessToken` ausente omite `x-access-token`: sólo tiene sentido para
 * `HealthService.Health`.
 */
export function proxyAuthInterceptor(store: TokenStore, options: ProxyAuthOptions): Interceptor {
  return (next) => async (request) => {
    const jwe = store.jweFor(options.port);
    if (jwe === undefined) {
      throw new AuthenticationError(`no hay token del proxy para el puerto ${options.port}`);
    }
    request.header.set(PROXY_AUTH_HEADER, jwe);
    request.header.set(PROXY_PORT_HEADER, String(options.port));
    request.header.set(PROXY_FORCE_H2_HEADER, "true");
    if (options.accessToken !== undefined) {
      request.header.set(ACCESS_TOKEN_HEADER, options.accessToken);
    }
    for (const [key, value] of Object.entries(options.extraHeaders ?? {})) {
      request.header.set(key, value);
    }
    return next(request);
  };
}
