/**
 * El modo `--http`: el manejador de *web standards* del SDK de MCP
 * (`createMcpHandler`, `fetch(Request) → Response`) servido con `node:http`,
 * sin framework ni el paquete `@modelcontextprotocol/node` (ADR-027, D5).
 *
 * Es un servidor local sin autenticación: la defensa contra DNS rebinding
 * es exigir que `Host` sea exactamente la autoridad en la que escucha
 * (`host:puerto`) y que un `Origin` presente sea `http://<autoridad>`. El
 * SDK sólo compara el nombre de host sin el puerto, y esto es más estricto.
 * Nada se registra por petición.
 */

import {
  createServer,
  type IncomingHttpHeaders,
  type IncomingMessage,
  type Server,
  type ServerResponse,
} from "node:http";
import type { AddressInfo } from "node:net";
import { Readable } from "node:stream";
import { httpAuthority } from "../authority.js";
import {
  HTTP_FORBIDDEN,
  HTTP_INTERNAL_ERROR,
  HTTP_MISDIRECTED,
  HTTP_NOT_FOUND,
} from "../http-status.js";
import type { Logger } from "../logger.js";
import { MCP_PATH } from "./options.js";

const JSON_RPC_INTERNAL_ERROR = -32603;
const BODYLESS_METHODS = new Set(["GET", "HEAD"]);

export interface FetchHandler {
  fetch(request: Request): Promise<Response>;
}

export interface HttpEndpoint {
  readonly url: string;
  close(): Promise<void>;
}

export interface Rejection {
  readonly status: number;
  readonly message: string;
}

/** `undefined` si la petición puede seguir; si no, el estado y el motivo. */
export function guardRequest(
  headers: IncomingHttpHeaders,
  pathname: string,
  authority: string,
): Rejection | undefined {
  if (headers.host?.toLowerCase() !== authority.toLowerCase()) {
    return { status: HTTP_MISDIRECTED, message: "Invalid Host header" };
  }
  const origin = headers.origin;
  if (origin !== undefined && origin.toLowerCase() !== `http://${authority}`.toLowerCase()) {
    return { status: HTTP_FORBIDDEN, message: "Invalid Origin header" };
  }
  return pathname === MCP_PATH ? undefined : { status: HTTP_NOT_FOUND, message: "Not Found" };
}

function webHeaders(headers: IncomingHttpHeaders): Headers {
  const result = new Headers();
  for (const [name, value] of Object.entries(headers)) {
    for (const item of Array.isArray(value) ? value : [value]) {
      if (item !== undefined) {
        result.append(name, item);
      }
    }
  }
  return result;
}

function toRequest(req: IncomingMessage, authority: string, abort: AbortSignal): Request {
  const method = req.method ?? "GET";
  const init: RequestInit & { duplex?: "half" } = {
    method,
    headers: webHeaders(req.headers),
    signal: abort,
  };
  if (!BODYLESS_METHODS.has(method)) {
    init.body = Readable.toWeb(req) as ReadableStream<Uint8Array>;
    init.duplex = "half";
  }
  return new Request(`http://${authority}${req.url ?? MCP_PATH}`, init);
}

function reject(res: ServerResponse, rejection: Rejection): void {
  res.writeHead(rejection.status, { "content-type": "text/plain; charset=utf-8" });
  res.end(rejection.message);
}

function internalError(res: ServerResponse): void {
  const body = JSON.stringify({
    jsonrpc: "2.0",
    error: { code: JSON_RPC_INTERNAL_ERROR, message: "Internal server error" },
    id: null,
  });
  res.writeHead(HTTP_INTERNAL_ERROR, { "content-type": "application/json" });
  res.end(body);
}

async function writeResponse(response: Response, res: ServerResponse): Promise<void> {
  res.writeHead(response.status, Object.fromEntries(response.headers));
  if (response.body === null) {
    res.end();
    return;
  }
  const reader = response.body.getReader();
  try {
    for (;;) {
      const chunk = await reader.read();
      if (chunk.done || res.destroyed) {
        break;
      }
      res.write(chunk.value);
    }
  } finally {
    await reader.cancel().catch(() => undefined);
    res.end();
  }
}

export interface HttpOptions {
  readonly host: string;
  /** `0` elige un puerto libre (sólo tests); la autoridad es la del puerto real. */
  readonly port: number;
  readonly logger?: Logger | undefined;
}

export async function listenHttp(
  handler: FetchHandler,
  options: HttpOptions,
): Promise<HttpEndpoint> {
  let authority = "";
  const server = createServer((req, res) => {
    void serve(handler, req, res, authority, options.logger);
  });
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(options.port, options.host, () => {
      server.off("error", reject);
      resolve();
    });
  });
  const port = (server.address() as AddressInfo).port;
  authority = httpAuthority(options.host, port);
  return { url: `http://${authority}${MCP_PATH}`, close: () => closeServer(server) };
}

async function serve(
  handler: FetchHandler,
  req: IncomingMessage,
  res: ServerResponse,
  authority: string,
  logger: Logger | undefined,
): Promise<void> {
  const pathname = new URL(req.url ?? "/", "http://placeholder").pathname;
  const rejection = guardRequest(req.headers, pathname, authority);
  if (rejection !== undefined) {
    reject(res, rejection);
    return;
  }
  const abort = new AbortController();
  res.once("close", () => abort.abort());
  try {
    await writeResponse(await handler.fetch(toRequest(req, authority, abort.signal)), res);
  } catch (error) {
    logger?.error?.("fallo al servir una petición", {
      error: error instanceof Error ? error.name : typeof error,
    });
    if (!res.headersSent) {
      internalError(res);
    } else {
      res.destroy();
    }
  }
}

function closeServer(server: Server): Promise<void> {
  return new Promise((resolve) => {
    server.close(() => resolve());
    server.closeAllConnections();
  });
}
