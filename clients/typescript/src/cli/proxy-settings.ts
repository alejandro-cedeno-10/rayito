/**
 * Lo que `rayito sandbox proxy` valida antes de tocar AWS y lo que anuncia al
 * arrancar (dominio puro, sin E/S). Espejo de las validaciones y de
 * `announcement` de `rayito.cli._proxy`; los dos ejecutan los vectores de
 * `testdata/cli/proxy-vectors.json`.
 */

import { InvalidArgumentError } from "../errors.js";
import { HOOKS_PORT, MAX_CONCURRENT_CONNECTIONS_1_VCPU, PORT_MAX, PORT_MIN } from "../limits.js";
import { ALLOWED_HOST_OPTION, type ProxyAccess } from "./access.js";

export const DEFAULT_BIND = "127.0.0.1";
/** El tope de conexiones de una MicroVM de 1 vCPU (AWS_API_NOTES.md §7): lo que cualquier sandbox aguanta. */
export const DEFAULT_MAX_CONNECTIONS = MAX_CONCURRENT_CONNECTIONS_1_VCPU;
export const ALLOW_REMOTE_OPTION = "--allow-remote";
export const MAX_CONNECTIONS_OPTION = "--max-connections";
const PORT_OPTION = "--port";
const LOCAL_PORT_OPTION = "--local-port";
const MIN_MAX_CONNECTIONS = 1;
const HTTP_SCHEME = "http";

export const BIND_NEEDS_ALLOW_REMOTE_MESSAGE =
  `--bind fuera de loopback necesita ${ALLOW_REMOTE_OPTION}: ` +
  "cualquiera que llegue a ese puerto usa el sandbox mientras el proxy esté vivo";
export const WILDCARD_NEEDS_ALLOWED_HOST_MESSAGE =
  `--bind 0.0.0.0/:: necesita ${ALLOWED_HOST_OPTION} <nombre>: ningún cliente escribe ` +
  "Host: 0.0.0.0, así que el proxy no sabría qué Host aceptar";

/** 1..65535, para `--port` y `--local-port`. Se llama antes de tocar AWS. */
function validatePortRange(port: number, option: string): number {
  if (!Number.isInteger(port) || port < PORT_MIN || port > PORT_MAX) {
    throw new InvalidArgumentError(`${option} fuera de rango (${PORT_MIN}-${PORT_MAX}): ${port}`);
  }
  return port;
}

/** `N` de `--port`: nunca el de los hooks, siempre en rango. */
export function validateProxyPort(port: number): number {
  if (port === HOOKS_PORT) {
    throw new InvalidArgumentError(
      `el puerto ${HOOKS_PORT} es el de los lifecycle hooks (ADR-006): ` +
        "no se puede exponer con el proxy",
    );
  }
  return validatePortRange(port, PORT_OPTION);
}

/** `N` de `--local-port`: sólo el rango; es un puerto del operador, no del guest. */
export function validateLocalPort(port: number): number {
  return validatePortRange(port, LOCAL_PORT_OPTION);
}

/** `--max-connections`: al menos 1. */
export function validateMaxConnections(value: number): number {
  if (!Number.isInteger(value) || value < MIN_MAX_CONNECTIONS) {
    throw new InvalidArgumentError(
      `${MAX_CONNECTIONS_OPTION} tiene que ser al menos ${MIN_MAX_CONNECTIONS}: ${value}`,
    );
  }
  return value;
}

/** Configuración resuelta de un `rayito sandbox proxy` en marcha. */
export interface ProxySpec {
  readonly sandboxId: string;
  readonly port: number;
  readonly endpoint: string;
  readonly bind: string;
  readonly localPort: number;
  readonly access: ProxyAccess;
  readonly maxConnections: number;
}

export function localUrl(bind: string, port: number): string {
  const host = bind.includes(":") ? `[${bind}]` : bind;
  return `${HTTP_SCHEME}://${host}:${port}`;
}

/**
 * Lo que la CLI imprime al arrancar: la URL local y, con un `--bind` de
 * loopback, la de `<id>.localhost`, que no comparte cookies con otras apps
 * locales.
 */
export function announcement(spec: ProxySpec): string {
  const line = `${localUrl(spec.bind, spec.localPort)} → ${spec.sandboxId}:${spec.port}`;
  if (spec.access.sandboxHost === undefined) {
    return line;
  }
  const isolated = `${HTTP_SCHEME}://${spec.access.sandboxHost}:${spec.localPort}`;
  return `${line}\n  con cookies aisladas de otras apps locales: ${isolated}`;
}
