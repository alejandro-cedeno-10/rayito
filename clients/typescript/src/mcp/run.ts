/**
 * Arranque y parada del servidor MCP con el transporte elegido. `startMcp`
 * devuelve un `RunningServer`; quien lo arranca (el ejecutable, o un test)
 * decide cuándo pararlo, y `stop()` cierra el transporte y termina el
 * sandbox una sola vez.
 */

import { isLoopback } from "../authority.js";
import type { Logger } from "../logger.js";
import { listenHttp } from "./http.js";
import type { RunOptions } from "./options.js";
import type { McpPeers } from "./peers.js";
import type { McpApp } from "./server.js";

export interface RunningServer {
  /** La URL del endpoint en modo HTTP; `undefined` en stdio. */
  readonly url: string | undefined;
  stop(): Promise<void>;
}

export interface StartContext {
  readonly peers: McpPeers;
  readonly app: McpApp;
  readonly logger: Logger;
}

export async function startMcp(options: RunOptions, context: StartContext): Promise<RunningServer> {
  return options.http ? startHttp(options, context) : startStdio(context);
}

function onTransportError(logger: Logger): (error: Error) => void {
  return (error) => logger.error?.("error del transporte", { error: error.name });
}

function startStdio({ peers, app, logger }: StartContext): RunningServer {
  const handle = peers.stdio.serveStdio(() => app.createServer(), {
    onerror: onTransportError(logger),
  });
  return {
    url: undefined,
    stop: async () => {
      try {
        await handle.close();
      } finally {
        await app.lease.close();
      }
    },
  };
}

async function startHttp(
  options: RunOptions,
  { peers, app, logger }: StartContext,
): Promise<RunningServer> {
  const handler = peers.sdk.createMcpHandler(() => app.createServer(), {
    onerror: onTransportError(logger),
  });
  const endpoint = await listenHttp(handler, { host: options.host, port: options.port, logger });
  if (!isLoopback(options.host)) {
    logger.warn?.(
      "sin autenticación: cualquier cliente que alcance el servidor controla el sandbox",
      {
        endpoint: endpoint.url,
      },
    );
  }
  logger.info?.("escuchando", { endpoint: endpoint.url });
  return {
    url: endpoint.url,
    stop: async () => {
      try {
        await endpoint.close();
        await handler.close();
      } finally {
        await app.lease.close();
      }
    },
  };
}
