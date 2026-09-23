/**
 * Núcleo puro del listado paginado (design D8), el mismo que
 * `clients/python/src/rayito/_listing_base.py`: filtros y su huella, el
 * `next_token` opaco (base64url sin padding del JSON canónico, byte a byte
 * igual que el de Python), el `PageWalk` que reanuda por identidad dentro de
 * la página del cursor y el `OrderedWalk` por `(startedAt, sandboxId)`. Sin
 * I/O: `SandboxListPaginator` trae las páginas y sondea los metadatos.
 *
 * Un token lleva el ARN de la imagen y una huella de los filtros, nunca los
 * valores de metadatos, y no se registra en ningún log.
 */

import { createHash } from "node:crypto";
import { InvalidArgumentError } from "../errors.js";
import { LIST_MAX_RESULTS, TERMINAL_STATES } from "../limits.js";
import type { MicrovmListPage, SandboxListItem } from "../models.js";
import { validatedStringMap } from "../payload.js";

export type ListOrder = "asc" | "desc";

export const LIST_PAGE_SIZE: number = LIST_MAX_RESULTS;
export const NEXT_TOKEN_MAX_CHARS = 8192;
export const TOKEN_VERSION = 1;
export const METADATA_LIST_STATE = "RUNNING";
export const INVALID_TOKEN_MESSAGE = "next_token inválido";
export const FOREIGN_TOKEN_MESSAGE = "next_token no corresponde a estos filtros";
export const EXHAUSTED_MESSAGE = "no quedan páginas: hasNext es false";

const FINGERPRINT_PATTERN = /^[0-9a-f]{16}$/;
const DIGEST_PATTERN = /^[0-9a-f]{12}$/;
const BASE64URL_PATTERN = /^[A-Za-z0-9_-]+$/;
const PAGE_KEYS = ["a", "f", "s", "v"];
const KEY_KEYS = ["f", "k", "v"];
const utf8 = new TextDecoder("utf-8", { fatal: true });

/** Cursor dentro de una página de AWS: su `nextToken` (`undefined` = la primera) y lo ya consumido de ella. */
export interface PageCursor {
  readonly kind: "page";
  readonly awsToken: string | undefined;
  readonly consumed: ReadonlySet<string>;
}

/** Cursor de un listado ordenado: la clave del último item servido. */
export interface KeyCursor {
  readonly kind: "key";
  readonly startedAtMs: number;
  readonly sandboxId: string;
}

export type ListCursor = PageCursor | KeyCursor;

export interface DecodedToken {
  readonly fingerprint: string;
  readonly cursor: ListCursor;
}

export interface PageRequest {
  readonly awsToken: string | undefined;
}

export function pageCursor(awsToken: string | undefined, consumed: Iterable<string>): PageCursor {
  return Object.freeze({ kind: "page", awsToken, consumed: new Set(consumed) });
}

export function keyCursor(startedAtMs: number, sandboxId: string): KeyCursor {
  return Object.freeze({ kind: "key", startedAtMs, sandboxId });
}

export const FIRST_PAGE: PageCursor = pageCursor(undefined, []);

/** Orden de Python (`sorted` sobre `str`): por punto de código, no por unidad UTF-16. */
export function compareCodePoints(left: string, right: string): number {
  const a = Array.from(left, (char) => char.codePointAt(0) as number);
  const b = Array.from(right, (char) => char.codePointAt(0) as number);
  const shared = Math.min(a.length, b.length);
  for (let index = 0; index < shared; index += 1) {
    const difference = (a[index] as number) - (b[index] as number);
    if (difference !== 0) {
      return difference;
    }
  }
  return a.length - b.length;
}

/**
 * La salida exacta de `json.dumps(value, sort_keys=True, separators=(",",
 * ":"), ensure_ascii=False)`: claves ordenadas recursivamente por punto de
 * código y `JSON.stringify` para los escalares.
 */
export function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) {
    return `[${value.map(canonicalJson).join(",")}]`;
  }
  if (value !== null && typeof value === "object") {
    const record = value as Record<string, unknown>;
    const members = Object.keys(record)
      .sort(compareCodePoints)
      .map((key) => `${JSON.stringify(key)}:${canonicalJson(record[key])}`);
    return `{${members.join(",")}}`;
  }
  return JSON.stringify(value ?? null);
}

function sha256Hex(text: string): string {
  return createHash("sha256").update(text, "utf8").digest("hex");
}

/** Identidad de un item consumido: los 12 primeros hex del sha256 de su id. */
export function itemDigest(sandboxId: string): string {
  return sha256Hex(sandboxId).slice(0, 12);
}

export function startedAtMs(item: SandboxListItem): number {
  return item.startedAt.getTime();
}

export interface ListFiltersFields {
  readonly imageArn: string | undefined;
  readonly imageVersion: string | undefined;
  readonly states: readonly string[] | undefined;
  readonly startedAfterMs: number | undefined;
  readonly metadata: ReadonlyArray<readonly [string, string]> | undefined;
  readonly order: ListOrder | undefined;
}

/**
 * Los filtros de un listado ya canónicos (`states` y `metadata` ordenados).
 * `template` viaja como `imageIdentifier` del lado de AWS; `states`,
 * `startedAfter` y `metadata` se aplican aquí sobre los items.
 */
export class ListFilters implements ListFiltersFields {
  readonly imageArn: string | undefined;
  readonly imageVersion: string | undefined;
  readonly states: readonly string[] | undefined;
  readonly startedAfterMs: number | undefined;
  readonly metadata: ReadonlyArray<readonly [string, string]> | undefined;
  readonly order: ListOrder | undefined;

  constructor(fields: ListFiltersFields) {
    this.imageArn = fields.imageArn;
    this.imageVersion = fields.imageVersion;
    this.states =
      fields.states === undefined
        ? undefined
        : Object.freeze([...fields.states].sort(compareCodePoints));
    this.startedAfterMs = fields.startedAfterMs;
    this.metadata =
      fields.metadata === undefined
        ? undefined
        : Object.freeze(
            [...fields.metadata]
              .sort(([left], [right]) => compareCodePoints(left, right))
              .map(([key, value]) => Object.freeze([key, value] as const)),
          );
    this.order = fields.order;
    Object.freeze(this);
  }

  /** 16 hex del sha256 del JSON canónico de los filtros (golden vectors de D8). */
  fingerprint(): string {
    return sha256Hex(
      canonicalJson({
        image: this.imageArn ?? null,
        version: this.imageVersion ?? null,
        states: this.states === undefined ? null : [...this.states],
        started_after_ms: this.startedAfterMs ?? null,
        metadata: this.metadata === undefined ? null : Object.fromEntries(this.metadata),
        order: this.order ?? null,
      }),
    ).slice(0, 16);
  }

  /**
   * Estado (explícito o, por defecto, sin `TERMINATING|TERMINATED`) y
   * `startedAt >= startedAfter`. Con `metadata` sólo pasa `RUNNING`: los
   * metadatos se comparan aparte porque exigen una sonda de `Health`.
   */
  accepts(item: SandboxListItem): boolean {
    if (this.metadata !== undefined && item.state !== METADATA_LIST_STATE) {
      return false;
    }
    const stateWanted =
      this.states === undefined
        ? !TERMINAL_STATES.has(item.state)
        : this.states.includes(item.state);
    return (
      stateWanted && (this.startedAfterMs === undefined || startedAtMs(item) >= this.startedAfterMs)
    );
  }
}

function tokenDocument(fingerprint: string, cursor: ListCursor): Record<string, unknown> {
  if (cursor.kind === "key") {
    return { v: TOKEN_VERSION, f: fingerprint, k: [cursor.startedAtMs, cursor.sandboxId] };
  }
  return {
    v: TOKEN_VERSION,
    f: fingerprint,
    a: cursor.awsToken ?? null,
    s: [...cursor.consumed].sort(compareCodePoints),
  };
}

export function encodeNextToken(fingerprint: string, cursor: ListCursor): string {
  return Buffer.from(canonicalJson(tokenDocument(fingerprint, cursor)), "utf8").toString(
    "base64url",
  );
}

export function nextTokenFor(
  fingerprint: string,
  cursor: ListCursor | undefined,
): string | undefined {
  return cursor === undefined ? undefined : encodeNextToken(fingerprint, cursor);
}

function invalidToken(): InvalidArgumentError {
  return new InvalidArgumentError(INVALID_TOKEN_MESSAGE);
}

function tokenText(token: unknown): string {
  if (
    typeof token !== "string" ||
    token.length === 0 ||
    token.length > NEXT_TOKEN_MAX_CHARS ||
    token.length % 4 === 1 ||
    !BASE64URL_PATTERN.test(token)
  ) {
    throw invalidToken();
  }
  try {
    return utf8.decode(Buffer.from(token, "base64url"));
  } catch {
    throw invalidToken();
  }
}

function tokenObject(text: string): Record<string, unknown> {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw invalidToken();
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw invalidToken();
  }
  return parsed as Record<string, unknown>;
}

function hasExactKeys(document: Record<string, unknown>, keys: readonly string[]): boolean {
  const present = Object.keys(document).sort();
  return present.length === keys.length && present.every((key, index) => key === keys[index]);
}

function pageCursorFrom(document: Record<string, unknown>): PageCursor {
  const awsToken = document.a;
  const digests = document.s;
  if (
    (awsToken !== null && typeof awsToken !== "string") ||
    !Array.isArray(digests) ||
    digests.length > LIST_PAGE_SIZE ||
    !digests.every((digest) => typeof digest === "string" && DIGEST_PATTERN.test(digest))
  ) {
    throw invalidToken();
  }
  return pageCursor(awsToken ?? undefined, digests as string[]);
}

function keyCursorFrom(document: Record<string, unknown>): KeyCursor {
  const key = document.k;
  if (!Array.isArray(key) || key.length !== 2) {
    throw invalidToken();
  }
  const [started, sandboxId] = key as [unknown, unknown];
  if (
    !Number.isSafeInteger(started) ||
    (started as number) < 0 ||
    typeof sandboxId !== "string" ||
    sandboxId.length === 0
  ) {
    throw invalidToken();
  }
  return keyCursor(started as number, sandboxId);
}

/**
 * Decodifica y valida un `next_token`. Todo defecto (longitud, base64url,
 * UTF-8, JSON, versión, huella, forma del cursor, digests) es
 * `InvalidArgumentError("next_token inválido")`, sin causa y sin repetir el
 * token.
 */
export function decodeNextToken(token: unknown): DecodedToken {
  const document = tokenObject(tokenText(token));
  const fingerprint = document.f;
  if (
    document.v !== TOKEN_VERSION ||
    typeof fingerprint !== "string" ||
    !FINGERPRINT_PATTERN.test(fingerprint)
  ) {
    throw invalidToken();
  }
  if (hasExactKeys(document, PAGE_KEYS)) {
    return Object.freeze({ fingerprint, cursor: pageCursorFrom(document) });
  }
  if (hasExactKeys(document, KEY_KEYS)) {
    return Object.freeze({ fingerprint, cursor: keyCursorFrom(document) });
  }
  throw invalidToken();
}

/**
 * Recorre las páginas de AWS a partir de un `PageCursor`. Con el buffer vacío
 * pide la página del cursor (la primera vez) o la siguiente (vaciando lo
 * consumido); al aceptarla descarta lo ya consumido por identidad, así que un
 * sandbox creado o desaparecido dentro de esa página ni se duplica ni se
 * salta. Un desplazamiento entre páginas no está protegido, como en cualquier
 * cursor de AWS.
 */
export class PageWalk {
  #pageToken: string | undefined;
  #consumed: Set<string>;
  #buffer: SandboxListItem[] = [];
  #loaded = false;
  #nextToken: string | undefined;

  constructor(cursor: PageCursor) {
    this.#pageToken = cursor.awsToken;
    this.#consumed = new Set(cursor.consumed);
  }

  get hasMore(): boolean {
    return this.#buffer.length > 0 || !this.#loaded || this.#nextToken !== undefined;
  }

  pageToFetch(): PageRequest | undefined {
    if (this.#buffer.length > 0) {
      return undefined;
    }
    if (!this.#loaded) {
      return { awsToken: this.#pageToken };
    }
    if (this.#nextToken === undefined) {
      return undefined;
    }
    this.#pageToken = this.#nextToken;
    this.#consumed = new Set();
    this.#loaded = false;
    this.#nextToken = undefined;
    return { awsToken: this.#pageToken };
  }

  acceptPage(page: MicrovmListPage): void {
    this.#buffer = page.items.filter((item) => !this.#consumed.has(itemDigest(item.sandboxId)));
    this.#nextToken = page.nextToken;
    this.#loaded = true;
  }

  nextRaw(): SandboxListItem | undefined {
    const item = this.#buffer.shift();
    if (item !== undefined) {
      this.#consumed.add(itemDigest(item.sandboxId));
    }
    return item;
  }

  cursor(): PageCursor {
    return pageCursor(this.#pageToken, this.#consumed);
  }
}

function compareKeys(leftMs: number, leftId: string, rightMs: number, rightId: string): number {
  return leftMs === rightMs ? compareCodePoints(leftId, rightId) : leftMs - rightMs;
}

/**
 * Un listado ordenado ya completo: orden `(startedAt, sandboxId)` ascendente
 * o invertido, `after` descarta todo lo que no va estrictamente detrás de esa
 * clave (keyset, robusto a inserciones) y `take(limit)` sirve el siguiente
 * tramo.
 */
export class OrderedWalk {
  readonly #items: readonly SandboxListItem[];
  #index = 0;
  #last: KeyCursor | undefined;

  constructor(items: Iterable<SandboxListItem>, order: ListOrder, after: KeyCursor | undefined) {
    const direction = order === "asc" ? 1 : -1;
    const keyed = (item: SandboxListItem, other: KeyCursor): number =>
      direction *
      compareKeys(startedAtMs(item), item.sandboxId, other.startedAtMs, other.sandboxId);
    this.#items = [...items]
      .sort(
        (left, right) =>
          direction *
          compareKeys(startedAtMs(left), left.sandboxId, startedAtMs(right), right.sandboxId),
      )
      .filter((item) => after === undefined || keyed(item, after) > 0);
    this.#last = after;
  }

  get hasMore(): boolean {
    return this.#index < this.#items.length;
  }

  take(limit: number | undefined): SandboxListItem[] {
    const end = limit === undefined ? this.#items.length : this.#index + limit;
    const served = this.#items.slice(this.#index, end);
    this.#index += served.length;
    const last = served.at(-1);
    if (last !== undefined) {
      this.#last = keyCursor(startedAtMs(last), last.sandboxId);
    }
    return served;
  }

  cursor(): KeyCursor | undefined {
    return this.#last;
  }
}

export function validateOrder(order: unknown): ListOrder | undefined {
  if (order === undefined || order === "asc" || order === "desc") {
    return order;
  }
  throw new InvalidArgumentError(
    `order inválido: ${JSON.stringify(order)}; se esperaba 'asc' o 'desc'`,
  );
}

export function validateLimit(limit: unknown): number | undefined {
  if (limit === undefined) {
    return undefined;
  }
  if (typeof limit !== "number" || !Number.isSafeInteger(limit) || limit < 1) {
    throw new InvalidArgumentError(`limit debe ser un entero >= 1, recibido ${String(limit)}`);
  }
  return limit;
}

function validateStartedAfter(startedAfter: unknown): number | undefined {
  if (startedAfter === undefined) {
    return undefined;
  }
  const ms = startedAfter instanceof Date ? startedAfter.getTime() : Number.NaN;
  if (!Number.isFinite(ms) || ms < 0) {
    throw new InvalidArgumentError(
      "startedAfter debe ser un Date válido no anterior a 1970-01-01T00:00:00Z",
    );
  }
  return ms;
}

/**
 * `metadata` sólo filtra sandboxes `RUNNING`: los metadatos viven en el
 * agente y sondear uno suspendido lo despertaría (SPEC §4 prohíbe guardarlos
 * del lado del cliente).
 */
function validateMetadataStates(states: readonly string[] | undefined): void {
  const unsupported = (states ?? []).filter((state) => state !== METADATA_LIST_STATE);
  if (unsupported.length > 0) {
    throw new InvalidArgumentError(
      "list({ metadata }) sólo filtra sandboxes RUNNING: los metadatos viven en el agente y " +
        `sondear un sandbox suspendido lo despertaría (recibido ${JSON.stringify(unsupported)})`,
    );
  }
}

function validateCursorForm(decoded: DecodedToken | undefined, order: ListOrder | undefined): void {
  if (decoded === undefined) {
    return;
  }
  if ((order === undefined) !== (decoded.cursor.kind === "page")) {
    throw invalidToken();
  }
}

export interface ListingRequestOptions {
  readonly template?: string | undefined;
  readonly templateVersion?: string | undefined;
  readonly states?: readonly string[] | undefined;
  readonly metadata?: Readonly<Record<string, string>> | undefined;
  readonly startedAfter?: Date | undefined;
  readonly order?: ListOrder | undefined;
  readonly limit?: number | undefined;
  readonly nextToken?: string | undefined;
}

/** Un listado validado; el ARN de la plantilla se resuelve después, con I/O. */
export interface ListingRequest {
  readonly template: string | undefined;
  readonly limit: number | undefined;
  readonly order: ListOrder | undefined;
  readonly decoded: DecodedToken | undefined;
  readonly hasMetadata: boolean;
  metadataMatches(candidate: Readonly<Record<string, string>>): boolean;
  filters(imageArn: string | undefined): ListFilters;
}

/**
 * Valida todo lo de `paginate()`/`list()` antes de tocar AWS: `limit`,
 * `order`, `metadata` (con `states ⊆ {RUNNING}`), `startedAfter` y el token
 * (incluida su forma: cursor de página sin `order`, de clave con `order`).
 */
export function listingRequest(options: ListingRequestOptions): ListingRequest {
  const limit = validateLimit(options.limit);
  const order = validateOrder(options.order);
  const metadata =
    options.metadata === undefined ? undefined : validatedStringMap(options.metadata, "metadata");
  if (metadata !== undefined) {
    validateMetadataStates(options.states);
  }
  const startedAfterMs = validateStartedAfter(options.startedAfter);
  const decoded = options.nextToken === undefined ? undefined : decodeNextToken(options.nextToken);
  validateCursorForm(decoded, order);
  const states = options.states === undefined ? undefined : [...options.states];
  const wanted = metadata === undefined ? undefined : Object.entries(metadata);
  return Object.freeze({
    template: options.template,
    limit,
    order,
    decoded,
    hasMetadata: wanted !== undefined,
    metadataMatches: (candidate: Readonly<Record<string, string>>) =>
      (wanted ?? []).every(
        ([key, value]) => Object.hasOwn(candidate, key) && candidate[key] === value,
      ),
    filters: (imageArn: string | undefined) =>
      new ListFilters({
        imageArn,
        imageVersion: options.templateVersion,
        states,
        startedAfterMs,
        metadata: wanted,
        order,
      }),
  });
}

/** Dónde empieza un listado: el cursor del token (si su huella casa) o la primera página. */
export function resumeCursors(
  decoded: DecodedToken | undefined,
  filters: ListFilters,
): { readonly page: PageCursor; readonly key: KeyCursor | undefined } {
  if (decoded === undefined) {
    return { page: FIRST_PAGE, key: undefined };
  }
  if (decoded.fingerprint !== filters.fingerprint()) {
    throw new InvalidArgumentError(FOREIGN_TOKEN_MESSAGE);
  }
  return decoded.cursor.kind === "page"
    ? { page: decoded.cursor, key: undefined }
    : { page: FIRST_PAGE, key: decoded.cursor };
}
