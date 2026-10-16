/**
 * El caso de uso de `rayito sandbox proxy` (capa de aplicación): valida
 * puertos y reserva el socket local ANTES de tocar AWS, resuelve el endpoint
 * (`GetMicrovm`), acuña el primer JWE de un solo puerto (nunca `allPorts`,
 * ADR-006), arranca el refresher (renueva a los 45 min) y sirve hasta que
 * `signal` se cancele, parando el refresher al salir. Espejo de `run_proxy`
 * de `rayito.cli._proxy`.
 */

import { type ControlPlane, PortSpec } from "../aws/control-plane.js";
import { SandboxStateError } from "../errors.js";
import { TERMINAL_STATES } from "../limits.js";
import type { Logger } from "../logger.js";
import { TokenRefresher, TokenStore } from "../transport/tokens.js";
import { buildAccess } from "./access.js";
import { ProxyListener } from "./proxy-listener.js";
import { serveProxy, tlsUpstreamConnector, type UpstreamConnectorFactory } from "./proxy-server.js";
import {
  announcement,
  type ProxySpec,
  validateLocalPort,
  validateMaxConnections,
  validateProxyPort,
} from "./proxy-settings.js";

export interface RunProxyOptions {
  readonly sandboxId: string;
  readonly port: number;
  readonly localPort: number;
  readonly bind: string;
  readonly allowedHosts: readonly string[];
  readonly allowedOrigins: readonly string[];
  readonly maxConnections: number;
  /** Se llama con la URL local una vez que el listener sirve. */
  readonly onReady: (announcement: string) => void;
  /** Una línea fija por rechazo o fallo, sin JWE, cabeceras ni ruta. */
  readonly report: (reason: string) => void;
  readonly signal: AbortSignal;
  /** TLS al endpoint por defecto; los tests inyectan un upstream en loopback. */
  readonly connectorFactory?: UpstreamConnectorFactory;
  readonly logger?: Logger | undefined;
}

/** `get-microvm`; sale con error si el sandbox ya está terminado. */
export async function resolveEndpoint(plane: ControlPlane, sandboxId: string): Promise<string> {
  const info = await plane.getMicrovm(sandboxId);
  if (TERMINAL_STATES.has(info.state)) {
    throw new SandboxStateError(
      `el sandbox ${sandboxId} está ${info.state.toLowerCase()}: no se puede exponer con el proxy`,
    );
  }
  return info.endpoint;
}

/** El mismo `TokenRefresher` que usa el SDK para el canal gRPC, acuñando siempre `PortSpec.single(port)`. */
export async function buildRefresher(
  plane: ControlPlane,
  options: Pick<RunProxyOptions, "sandboxId" | "port" | "logger">,
): Promise<TokenRefresher> {
  const refresher = new TokenRefresher(
    new TokenStore(),
    (ports) => plane.createAuthToken(options.sandboxId, ports),
    { logger: options.logger },
  );
  await refresher.mint([PortSpec.single(options.port)]);
  return refresher;
}

async function prepare(
  plane: ControlPlane,
  options: RunProxyOptions,
): Promise<{ spec: ProxySpec; refresher: TokenRefresher }> {
  const access = buildAccess({
    bind: options.bind,
    localPort: options.localPort,
    sandboxId: options.sandboxId,
    allowedHosts: options.allowedHosts,
    allowedOrigins: options.allowedOrigins,
  });
  const endpoint = await resolveEndpoint(plane, options.sandboxId);
  const refresher = await buildRefresher(plane, options);
  const spec: ProxySpec = {
    sandboxId: options.sandboxId,
    port: options.port,
    endpoint,
    bind: options.bind,
    localPort: options.localPort,
    access,
    maxConnections: options.maxConnections,
  };
  return { spec, refresher };
}

export async function runProxy(plane: ControlPlane, options: RunProxyOptions): Promise<void> {
  validateProxyPort(options.port);
  validateLocalPort(options.localPort);
  validateMaxConnections(options.maxConnections);
  const listener = await ProxyListener.bind(options.bind, options.localPort);
  let prepared: Awaited<ReturnType<typeof prepare>>;
  try {
    prepared = await prepare(plane, options);
  } catch (error) {
    await listener.close();
    throw error;
  }
  const { spec, refresher } = prepared;
  refresher.start();
  const connector = (options.connectorFactory ?? tlsUpstreamConnector)(spec.endpoint);
  try {
    const served = serveProxy(
      listener,
      {
        spec,
        jweProvider: () => refresher.store.jweFor(spec.port),
        connector,
        report: options.report,
        maxConnections: spec.maxConnections,
      },
      options.signal,
    );
    options.onReady(announcement(spec));
    await served;
  } finally {
    refresher.stop();
  }
}
