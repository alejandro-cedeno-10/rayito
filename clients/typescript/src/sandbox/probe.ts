/**
 * Una llamada suelta a `HealthService` de un sandbox sin abrir un `Sandbox`:
 * un `TokenStore` propio con un JWE de un solo puerto (8080, sin temporizador
 * de renovación), un transporte dedicado, un reintento tras un 403 del proxy y
 * `sessionManager.abort()` al terminar pase lo que pase. La usan el filtro de
 * metadatos de `list()` (sólo `Health`, sin `x-access-token`) y
 * `Sandbox.getMetricsHistory(id)` (con el access token).
 */

import { create } from "@bufbuild/protobuf";
import { type Client, createClient } from "@connectrpc/connect";
import { type ControlPlane, PortSpec } from "../aws/control-plane.js";
import {
  HealthRequestSchema,
  type HealthResponse,
  HealthService,
} from "../gen/rayito/v1/health_pb.js";
import { DEFAULT_PORT } from "../limits.js";
import type { SandboxInfo } from "../models.js";
import { isProxyForbidden } from "../transport/errors.js";
import { proxyAuthInterceptor } from "../transport/headers.js";
import { TokenRefresher, TokenStore } from "../transport/tokens.js";
import { openTransport, type TransportSettings } from "../transport/transport.js";

export type HealthClient = Client<typeof HealthService>;

async function remintingOnce<T>(call: () => Promise<T>, refresher: TokenRefresher): Promise<T> {
  try {
    return await call();
  } catch (error) {
    if (!isProxyForbidden(error)) {
      throw error;
    }
  }
  await refresher.refreshAll();
  return call();
}

/**
 * Los errores de `invoke` salen sin traducir (`ConnectError`): cada caller
 * aplica su tabla. `accessToken` ausente omite `x-access-token`.
 */
export async function withDedicatedHealthClient<T>(
  plane: ControlPlane,
  info: SandboxInfo,
  settings: TransportSettings,
  accessToken: string | undefined,
  invoke: (client: HealthClient) => Promise<T>,
): Promise<T> {
  const store = new TokenStore();
  const refresher = new TokenRefresher(store, (ports) =>
    plane.createAuthToken(info.sandboxId, ports),
  );
  await refresher.mint([PortSpec.single(DEFAULT_PORT)]);
  const opened = openTransport(
    info.endpoint,
    settings,
    proxyAuthInterceptor(store, {
      port: DEFAULT_PORT,
      accessToken,
      extraHeaders: settings.extraHeaders,
    }),
  );
  try {
    const client = createClient(HealthService, opened.transport);
    return await remintingOnce(() => invoke(client), refresher);
  } finally {
    opened.sessionManager.abort();
  }
}

/** Un `Health` anónimo con `timeoutMs` como deadline. */
export function probeHealth(
  plane: ControlPlane,
  info: SandboxInfo,
  settings: TransportSettings,
  timeoutMs: number,
): Promise<HealthResponse> {
  return withDedicatedHealthClient(plane, info, settings, undefined, (client) =>
    client.health(create(HealthRequestSchema, {}), { timeoutMs }),
  );
}
