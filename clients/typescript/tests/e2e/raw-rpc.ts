/**
 * Un cliente gRPC propio con un JWE recién acuñado, ajeno al `Sandbox` del
 * SDK (espejo de `minted_channel` en `test_m9_server_timeout.py`): no reanuda
 * ni reabre nada por su cuenta, así que sirve para observar qué hace `rayd`
 * con tráfico "extraviado" mientras el sandbox está suspendido.
 */

import { type Client, createClient } from "@connectrpc/connect";
import { PortSpec } from "../../src/aws/control-plane.js";
import { HealthService } from "../../src/gen/rayito/v1/health_pb.js";
import { ProcessService } from "../../src/gen/rayito/v1/process_pb.js";
import { PtyService } from "../../src/gen/rayito/v1/pty_pb.js";
import type { ControlPlane } from "../../src/index.js";
import { DEFAULT_PORT } from "../../src/limits.js";
import { buildStartRequest } from "../../src/sandbox/commands.js";
import { asConnectError, isSandboxTimeout } from "../../src/transport/errors.js";
import { proxyAuthInterceptor } from "../../src/transport/headers.js";
import { ProxyToken, TokenRefresher, TokenStore } from "../../src/transport/tokens.js";
import { DEFAULT_TRANSPORT_SETTINGS, openTransport } from "../../src/transport/transport.js";

/** El comando mínimo de `ProcessService.Start` con el que se provoca el rechazo. */
const STRAY_COMMAND = "echo stray";

export interface RawClients {
  readonly health: Client<typeof HealthService>;
  readonly process: Client<typeof ProcessService>;
  readonly pty: Client<typeof PtyService>;
  close(): void;
}

export interface RawAuth {
  /** `x-access-token` del agente; sin él, sólo `Health` pasa la capa de token. */
  readonly accessToken?: string | undefined;
  /** JWE a presentar al proxy en vez de uno acuñado por el plano de control. */
  readonly jwe?: string | undefined;
}

async function storeWith(
  controlPlane: ControlPlane,
  sandboxId: string,
  jwe: string | undefined,
): Promise<TokenStore> {
  const ports = [PortSpec.single(DEFAULT_PORT)];
  if (jwe !== undefined) {
    const store = new TokenStore();
    store.put(new ProxyToken(jwe, ports, Date.now()));
    return store;
  }
  const refresher = new TokenRefresher(new TokenStore(), (minted) =>
    controlPlane.createAuthToken(sandboxId, minted),
  );
  await refresher.mint(ports);
  return refresher.store;
}

/** Clientes gRPC propios del sandbox con la autenticación indicada, ajenos al `Sandbox` del SDK. */
export async function openRawClients(
  controlPlane: ControlPlane,
  sandboxId: string,
  auth: RawAuth,
): Promise<RawClients> {
  const info = await controlPlane.getMicrovm(sandboxId);
  const store = await storeWith(controlPlane, sandboxId, auth.jwe);
  const opened = openTransport(
    info.endpoint,
    DEFAULT_TRANSPORT_SETTINGS,
    proxyAuthInterceptor(store, { port: DEFAULT_PORT, accessToken: auth.accessToken }),
  );
  return {
    health: createClient(HealthService, opened.transport),
    process: createClient(ProcessService, opened.transport),
    pty: createClient(PtyService, opened.transport),
    close: () => opened.sessionManager.abort(),
  };
}

/** `true` si un `Start` por un canal ajeno acaba en `FAILED_PRECONDITION sandbox_timeout`. */
export async function rawStartIsSandboxTimeout(
  controlPlane: ControlPlane,
  sandboxId: string,
  accessToken: string,
): Promise<boolean> {
  const clients = await openRawClients(controlPlane, sandboxId, { accessToken });
  try {
    for await (const _ of clients.process.start(buildStartRequest(STRAY_COMMAND))) {
      return false;
    }
    return false;
  } catch (error) {
    return asConnectError(error) !== undefined && isSandboxTimeout(error);
  } finally {
    clients.close();
  }
}
