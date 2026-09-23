/**
 * El lado con I/O del listado (design D9/D10, espejo de
 * `rayito/sandbox_sync/listing.py`): trae las páginas de `list-microvms` con
 * `maxResults 50`, filtra con el núcleo puro de `listing.ts` y, con
 * `metadata`, sondea secuencialmente el `Health` de cada candidato `RUNNING`
 * por un transporte dedicado. `SandboxListPaginator` sirve tramos de `limit`
 * con un `nextToken` reanudable; `listSandboxes` es el mismo recorrido como
 * stream perezoso para `Sandbox.list()`.
 *
 * Ningún log lleva metadatos ni tokens.
 */

import type { ControlPlane } from "../aws/control-plane.js";
import { SandboxError, SandboxNotFoundError } from "../errors.js";
import type { HealthResponse } from "../gen/rayito/v1/health_pb.js";
import type { SandboxInfo, SandboxListItem } from "../models.js";
import type { TransportSettings } from "../transport/transport.js";
import {
  EXHAUSTED_MESSAGE,
  LIST_PAGE_SIZE,
  type ListCursor,
  type ListFilters,
  type ListingRequest,
  nextTokenFor,
  OrderedWalk,
  PageWalk,
  resumeCursors,
} from "./listing.js";
import { probeHealth } from "./probe.js";

export const METADATA_PROBE_TIMEOUT_MS = 5000;

/** Lo que el recorrido necesita del mundo: el plano de control y cómo sondear. */
export interface ListingContext {
  readonly plane: ControlPlane;
  readonly transport: TransportSettings;
  readonly probeTimeoutMs: number;
}

export function metadataProbeFailure(sandboxId: string, cause: unknown): SandboxError {
  const name = cause instanceof Error ? cause.name : typeof cause;
  return new SandboxError(
    `no se pudieron leer los metadatos del sandbox ${sandboxId}: Health no respondió (${name})`,
    { cause },
  );
}

/**
 * Los metadatos del agente de un item `RUNNING`, o `undefined` si ya no está
 * (`SandboxNotFoundError`), dejó de estar `RUNNING` o su agente aún no está
 * listo. Un `Health` que falla nunca se convierte en un hueco silencioso.
 */
async function readMetadata(
  context: ListingContext,
  item: SandboxListItem,
): Promise<Readonly<Record<string, string>> | undefined> {
  let info: SandboxInfo;
  try {
    info = await context.plane.getMicrovm(item.sandboxId);
  } catch (error) {
    if (error instanceof SandboxNotFoundError) {
      return undefined;
    }
    throw error;
  }
  if (info.state !== "RUNNING") {
    return undefined;
  }
  let response: HealthResponse;
  try {
    response = await probeHealth(context.plane, info, context.transport, context.probeTimeoutMs);
  } catch (error) {
    throw metadataProbeFailure(item.sandboxId, error);
  }
  return response.agentReady ? { ...response.metadata } : undefined;
}

function withMetadata(
  item: SandboxListItem,
  metadata: Readonly<Record<string, string>>,
): SandboxListItem {
  return Object.freeze({ ...item, metadata: Object.freeze({ ...metadata }) });
}

async function matching(
  context: ListingContext,
  request: ListingRequest,
  filters: ListFilters,
  item: SandboxListItem,
): Promise<SandboxListItem | undefined> {
  if (!filters.accepts(item)) {
    return undefined;
  }
  if (!request.hasMetadata) {
    return item;
  }
  const metadata = await readMetadata(context, item);
  if (metadata === undefined || !request.metadataMatches(metadata)) {
    return undefined;
  }
  return withMetadata(item, metadata);
}

/**
 * Los items que pasan los filtros, página a página y en orden de AWS. Se
 * detiene en cada `yield`, así que el cursor del `PageWalk` refleja
 * exactamente lo consumido cuando el caller deja de pedir.
 */
async function* matchingItems(
  context: ListingContext,
  request: ListingRequest,
  filters: ListFilters,
  walk: PageWalk,
): AsyncGenerator<SandboxListItem> {
  while (true) {
    const page = walk.pageToFetch();
    if (page !== undefined) {
      walk.acceptPage(
        await context.plane.listMicrovmsPage({
          imageArn: filters.imageArn,
          imageVersion: filters.imageVersion,
          maxResults: LIST_PAGE_SIZE,
          nextToken: page.awsToken,
        }),
      );
      continue;
    }
    const raw = walk.nextRaw();
    if (raw === undefined) {
      return;
    }
    const kept = await matching(context, request, filters, raw);
    if (kept !== undefined) {
      yield kept;
    }
  }
}

async function collectAll(items: AsyncIterable<SandboxListItem>): Promise<SandboxListItem[]> {
  const all: SandboxListItem[] = [];
  for await (const item of items) {
    all.push(item);
  }
  return all;
}

interface StartedListing {
  readonly filters: ListFilters;
  readonly fingerprint: string;
  readonly walk: PageWalk;
  readonly items: AsyncGenerator<SandboxListItem>;
  ordered: OrderedWalk | undefined;
}

async function startListing(
  context: ListingContext,
  request: ListingRequest,
): Promise<StartedListing> {
  const imageArn =
    request.template === undefined
      ? undefined
      : await context.plane.resolveTemplateArn(request.template);
  const filters = request.filters(imageArn);
  const cursors = resumeCursors(request.decoded, filters);
  const walk = new PageWalk(cursors.page);
  const items = matchingItems(context, request, filters, walk);
  const ordered =
    request.order === undefined
      ? undefined
      : new OrderedWalk(await collectAll(items), request.order, cursors.key);
  return { filters, fingerprint: filters.fingerprint(), walk, items, ordered };
}

/**
 * El paginador de `Sandbox.paginate()`. `nextToken` es el token recibido
 * hasta la primera página, después el cursor codificado mientras `hasNext`, y
 * `undefined` al final. Con `order` la primera página recorre todas las de
 * AWS (O(páginas), más la sonda O(n) con `metadata`); sin `order` el
 * recorrido es perezoso y sólo sondea lo que consume.
 */
export class SandboxListPaginator {
  readonly #context: ListingContext;
  readonly #request: ListingRequest;
  #started: StartedListing | undefined;
  #hasNext = true;
  #nextToken: string | undefined;

  constructor(context: ListingContext, request: ListingRequest, nextToken: string | undefined) {
    this.#context = context;
    this.#request = request;
    this.#nextToken = nextToken;
  }

  get hasNext(): boolean {
    return this.#hasNext;
  }

  get nextToken(): string | undefined {
    return this.#nextToken;
  }

  async nextItems(): Promise<SandboxListItem[]> {
    if (!this.#hasNext) {
      throw new SandboxError(EXHAUSTED_MESSAGE);
    }
    this.#started ??= await startListing(this.#context, this.#request);
    const started = this.#started;
    return started.ordered === undefined
      ? this.#nextStreamed(started)
      : this.#nextOrdered(started, started.ordered);
  }

  async #nextStreamed(started: StartedListing): Promise<SandboxListItem[]> {
    const limit = this.#request.limit;
    const served: SandboxListItem[] = [];
    while (limit === undefined || served.length < limit) {
      const next = await started.items.next();
      if (next.done) {
        break;
      }
      served.push(next.value);
    }
    this.#settle(started, started.walk.hasMore, started.walk.cursor());
    return served;
  }

  #nextOrdered(started: StartedListing, ordered: OrderedWalk): SandboxListItem[] {
    const served = ordered.take(this.#request.limit);
    this.#settle(started, ordered.hasMore, ordered.cursor());
    return served;
  }

  #settle(started: StartedListing, hasMore: boolean, cursor: ListCursor | undefined): void {
    this.#hasNext = hasMore;
    this.#nextToken = hasMore ? nextTokenFor(started.fingerprint, cursor) : undefined;
  }
}

/** `Sandbox.list()`: sin `order`, un stream perezoso; con `order`, todo primero y luego en orden. */
export async function* listSandboxes(
  context: ListingContext,
  request: ListingRequest,
): AsyncGenerator<SandboxListItem> {
  const started = await startListing(context, request);
  if (started.ordered === undefined) {
    yield* started.items;
    return;
  }
  yield* started.ordered.take(undefined);
}
