/**
 * La línea de comandos de `rayito-mcp` (espejo de `rayito.mcp._cli`): stdio
 * por defecto y `--http` para streamable HTTP en loopback. Nada configura el
 * sandbox aquí: eso viene sólo del entorno (`parseMcpSettings`); `--host` y
 * `--port` describen el transporte.
 */

import { parseArgs } from "node:util";
import { isWildcard } from "../authority.js";
import { PORT_MAX, PORT_MIN } from "../limits.js";

export const PROGRAM = "rayito-mcp";
export const DEFAULT_HTTP_HOST = "127.0.0.1";
export const DEFAULT_HTTP_PORT = 8000;
export const MCP_PATH = "/mcp";

export interface RunOptions {
  readonly http: boolean;
  readonly host: string;
  readonly port: number;
}

export type ParsedCommandLine =
  | { readonly kind: "help" }
  | { readonly kind: "run"; readonly options: RunOptions };

/** Un argumento inválido: el llamador lo imprime y sale con el código de uso. */
export class McpUsageError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "McpUsageError";
  }
}

export const HELP_TEXT = `uso: ${PROGRAM} [--http] [--host HOST] [--port PORT]

Servidor MCP de Rayito: un sandbox por proceso, configurado por RAYITO_TEMPLATE
y el resto de variables RAYITO_MCP_*.

opciones:
  --http       sirve streamable HTTP en http://<host>:<port>${MCP_PATH} en vez de stdio
  --host HOST  interfaz del modo HTTP (por defecto ${DEFAULT_HTTP_HOST})
  --port PORT  puerto del modo HTTP (por defecto ${DEFAULT_HTTP_PORT})
  -h, --help   muestra esta ayuda
`;

/**
 * `--host`/`--port` sin `--http` es un error de uso; una dirección comodín
 * (`0.0.0.0`, `::`) también, porque `--host` es a la vez la interfaz y la
 * autoridad que el servidor exige en `Host`/`Origin`: un cliente que alcanza
 * un servidor ligado a todas las interfaces escribe la dirección que marcó,
 * nunca la comodín, así que cada petición recibiría 421.
 */
export function parseCommandLine(argv: readonly string[]): ParsedCommandLine {
  const values = readValues(argv);
  if (values.help === true) {
    return { kind: "help" };
  }
  const http = values.http === true;
  if (!http && (values.host !== undefined || values.port !== undefined)) {
    throw new McpUsageError("--host y --port sólo tienen sentido con --http");
  }
  const host = values.host ?? DEFAULT_HTTP_HOST;
  if (isWildcard(host)) {
    throw new McpUsageError(
      `--host ${host} liga todas las interfaces y ningún cliente escribe esa ` +
        "dirección en la cabecera Host: pasa en --host la dirección concreta por " +
        "la que los clientes alcanzan el servidor",
    );
  }
  const port = values.port === undefined ? DEFAULT_HTTP_PORT : parsePort(values.port);
  return { kind: "run", options: { http, host, port } };
}

function readValues(argv: readonly string[]) {
  try {
    return parseArgs({
      args: [...argv],
      options: {
        http: { type: "boolean" },
        host: { type: "string" },
        port: { type: "string" },
        help: { type: "boolean", short: "h" },
      },
      allowPositionals: false,
    }).values;
  } catch (error) {
    throw new McpUsageError(error instanceof Error ? error.message : String(error));
  }
}

function parsePort(raw: string): number {
  const port = Number(raw);
  if (!/^\d+$/.test(raw) || port < PORT_MIN || port > PORT_MAX) {
    throw new McpUsageError(
      `--port debe ser un entero entre ${PORT_MIN} y ${PORT_MAX}, recibido '${raw}'`,
    );
  }
  return port;
}
