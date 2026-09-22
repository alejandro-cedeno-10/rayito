/**
 * Cabeceras del proxy de AWS en cada RPC (ARCHITECTURE.md, Capa 3):
 * `x-aws-proxy-auth` (el JWE, leído del `TokenStore` en cada llamada, así
 * rotarlo nunca toca el transporte), `x-aws-proxy-port`,
 * `x-aws-proxy-force-h2` (`rayd` sirve h2c) y `x-access-token`. Node las
 * envía en minúsculas de todas formas; las constantes lo hacen explícito.
 */

import type { Interceptor } from "@connectrpc/connect";
import { AuthenticationError } from "../errors.js";
import type { TokenStore } from "./tokens.js";

export const PROXY_AUTH_HEADER = "x-aws-proxy-auth";
export const PROXY_PORT_HEADER = "x-aws-proxy-port";
export const PROXY_FORCE_H2_HEADER = "x-aws-proxy-force-h2";
export const ACCESS_TOKEN_HEADER = "x-access-token";

export interface ProxyAuthOptions {
  readonly port: number;
  readonly accessToken: string | undefined;
}

/**
 * Añade las cuatro cabeceras a cada request (unarios y streams). Sin token
 * para el puerto falla antes de enviar nada. `accessToken` ausente omite
 * `x-access-token`: sólo tiene sentido para `HealthService.Health`.
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
    return next(request);
  };
}
