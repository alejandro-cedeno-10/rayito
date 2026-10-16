/**
 * Resolución del stream de un sandbox y paginación de sus eventos sobre el
 * puerto `LogsApi`. Si el nombre exacto no existe (un stream creado otro día
 * UTC, Q55) se recorren como mucho `MAX_SCAN_PAGES` páginas de
 * `STREAM_PAGE_SIZE` streams ordenados por último evento, buscando los que
 * `isFallbackStream` acepta. Espejo de `rayito._cloudwatch_logs`.
 */

import type { SandboxInfo } from "../models.js";
import {
  DEFAULT_EVENT_LIMIT,
  expectedStreamName,
  isFallbackStream,
  LogsNotFoundError,
} from "./domain.js";

export const STREAM_PAGE_SIZE = 50;
export const MAX_SCAN_PAGES = 10;
export const FALLBACK_WARNING =
  "rayito: aviso: no existe el stream esperado {expected}; se muestran {streams}, " +
  "encontrados por nombre (el rol de ejecución puede escribir en cualquier stream de /rayito/*)";

/** El grupo de logs no existe (`ResourceNotFoundException`). */
export class LogGroupNotFoundError extends Error {
  constructor() {
    super("el grupo de logs no existe");
    this.name = "LogGroupNotFoundError";
  }
}

export interface LogEvent {
  readonly timestamp: number;
  readonly message: string;
}

export interface StreamsPage {
  readonly names: readonly string[];
  readonly nextToken: string | undefined;
}

export interface EventsPage {
  readonly events: readonly LogEvent[];
  readonly nextForwardToken: string | undefined;
  /** Para leer hacia atrás desde el final (`tailEvents`); un adaptador que no lo trae lo omite. */
  readonly nextBackwardToken?: string | undefined;
}

export interface DescribeStreamsRequest {
  readonly group: string;
  readonly prefix?: string;
  readonly orderByLastEvent?: boolean;
  readonly nextToken?: string | undefined;
}

export interface GetEventsRequest {
  readonly group: string;
  readonly stream: string;
  readonly startTimeMs: number | undefined;
  readonly nextToken: string | undefined;
  /** Lee desde el final del stream (`startFromHead: false`) en vez de desde el principio. */
  readonly fromEnd?: boolean | undefined;
  /** Eventos por página; sin él, el tope de CloudWatch. */
  readonly limit?: number | undefined;
}

/** Puerto: lo que se necesita de CloudWatch Logs. */
export interface LogsApi {
  describeStreams(request: DescribeStreamsRequest): Promise<StreamsPage>;
  getEvents(request: GetEventsRequest): Promise<EventsPage>;
}

async function exactStream(
  api: LogsApi,
  group: string,
  candidate: string,
): Promise<string | undefined> {
  const page = await api.describeStreams({ group, prefix: candidate });
  return page.names.includes(candidate) ? candidate : undefined;
}

async function scanStreams(api: LogsApi, group: string, info: SandboxInfo): Promise<string[]> {
  const found: string[] = [];
  let token: string | undefined;
  for (let scanned = 0; scanned < MAX_SCAN_PAGES; scanned += 1) {
    const page = await api.describeStreams({ group, orderByLastEvent: true, nextToken: token });
    found.push(...page.names.filter((name) => isFallbackStream(name, info)));
    token = page.nextToken;
    if (token === undefined || token === "") {
      break;
    }
  }
  return found;
}

/**
 * El stream con el nombre esperado o, si no existe, los que el recorrido
 * acotado encuentra; `LogsNotFoundError` si no hay ninguno o el grupo no
 * existe.
 */
export async function findStreams(
  api: LogsApi,
  group: string,
  info: SandboxInfo,
): Promise<string[]> {
  let found: string[];
  try {
    const exact = await exactStream(api, group, expectedStreamName(info));
    if (exact !== undefined) {
      return [exact];
    }
    found = await scanStreams(api, group, info);
  } catch (error) {
    if (error instanceof LogGroupNotFoundError) {
      throw new LogsNotFoundError(group);
    }
    throw error;
  }
  if (found.length === 0) {
    throw new LogsNotFoundError(group);
  }
  return found;
}

/**
 * `get-log-events` desde el principio hasta que `nextForwardToken` se repite
 * (fin del stream) o se alcanzan `limit` eventos.
 */
export async function readEvents(
  api: LogsApi,
  request: {
    readonly group: string;
    readonly stream: string;
    readonly startTimeMs?: number | undefined;
    readonly limit?: number;
  },
): Promise<LogEvent[]> {
  const limit = request.limit ?? DEFAULT_EVENT_LIMIT;
  const events: LogEvent[] = [];
  let token: string | undefined;
  while (events.length < limit) {
    const page = await api.getEvents({
      group: request.group,
      stream: request.stream,
      startTimeMs: request.startTimeMs,
      nextToken: token,
    });
    for (const event of page.events) {
      events.push(event);
      if (events.length >= limit) {
        return events;
      }
    }
    if (page.nextForwardToken === undefined || page.nextForwardToken === token) {
      return events;
    }
    token = page.nextForwardToken;
  }
  return events;
}

/**
 * Los últimos `limit` eventos del stream, en orden cronológico:
 * `get-log-events` desde el final, hacia atrás con `nextBackwardToken` hasta
 * juntar `limit` o hasta que el token se repite (principio del stream).
 */
export async function tailEvents(
  api: LogsApi,
  request: { readonly group: string; readonly stream: string; readonly limit: number },
): Promise<LogEvent[]> {
  const chunks: Array<readonly LogEvent[]> = [];
  let collected = 0;
  let token: string | undefined;
  while (collected < request.limit) {
    const page = await api.getEvents({
      group: request.group,
      stream: request.stream,
      startTimeMs: undefined,
      nextToken: token,
      fromEnd: true,
      limit: request.limit - collected,
    });
    chunks.unshift(page.events);
    collected += page.events.length;
    const next = page.nextBackwardToken;
    if (page.events.length === 0 || next === undefined || next === "" || next === token) {
      break;
    }
    token = next;
  }
  return chunks.flat().slice(-request.limit);
}
