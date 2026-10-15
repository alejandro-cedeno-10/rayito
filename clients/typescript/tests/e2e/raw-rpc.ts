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
import type { ControlPlane } from "../../src/index.js";
import { DEFAULT_PORT } from "../../src/limits.js";
import { buildStartRequest } from "../../src/sandbox/commands.js";
import { asConnectError, isSandboxTimeout } from "../../src/transport/errors.js";
import { proxyAuthInterceptor } from "../../src/transport/headers.js";
import { TokenRefresher, TokenStore } from "../../src/transport/tokens.js";
import { DEFAULT_TRANSPORT_SETTINGS, openTransport } from "../../src/transport/transport.js";

/** El comando mínimo de `ProcessService.Start` con el que se provoca el rechazo. */
const STRAY_COMMAND = "echo stray";

interface RawClients {
  readonly health: Client<typeof HealthService>;
  readonly process: Client<typeof ProcessService>;
  close(): void;
}

async function openRawClients(
  controlPlane: ControlPlane,
  sandboxId: string,
  accessToken: string | undefined,
): Promise<RawClients> {
  const info = await controlPlane.getMicrovm(sandboxId);
  const refresher = new TokenRefresher(new TokenStore(), (ports) =>
    controlPlane.createAuthToken(sandboxId, ports),
  );
  await refresher.mint([PortSpec.single(DEFAULT_PORT)]);
  const opened = openTransport(
    info.endpoint,
    DEFAULT_TRANSPORT_SETTINGS,
    proxyAuthInterceptor(refresher.store, { port: DEFAULT_PORT, accessToken }),
  );
  return {
    health: createClient(HealthService, opened.transport),
    process: createClient(ProcessService, opened.transport),
    close: () => opened.sessionManager.abort(),
  };
}

/** `true` si un `Start` por un canal ajeno acaba en `FAILED_PRECONDITION sandbox_timeout`. */
export async function rawStartIsSandboxTimeout(
  controlPlane: ControlPlane,
  sandboxId: string,
  accessToken: string,
): Promise<boolean> {
  const clients = await openRawClients(controlPlane, sandboxId, accessToken);
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
