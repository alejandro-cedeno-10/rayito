/**
 * Núcleo puro del listado paginado (design D8): el token opaco con los mismos
 * vectores dorados que el SDK Python, los filtros, el `PageWalk` que reanuda
 * por identidad, el `OrderedWalk` y la validación de `paginate()`.
 */

import { describe, expect, test } from "vitest";
import { InvalidArgumentError } from "../../src/errors.js";
import { type SandboxListItem, sandboxListItem } from "../../src/models.js";
import {
  canonicalJson,
  decodeNextToken,
  EXHAUSTED_MESSAGE,
  encodeNextToken,
  FIRST_PAGE,
  INVALID_TOKEN_MESSAGE,
  itemDigest,
  keyCursor,
  ListFilters,
  type ListOrder,
  listingRequest,
  nextTokenFor,
  OrderedWalk,
  PageWalk,
  pageCursor,
  resumeCursors,
  startedAtMs,
  validateLimit,
  validateOrder,
} from "../../src/sandbox/listing.js";

const GOLDEN_IMAGE = "arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base";
const GOLDEN_FIRST_ID = "microvm-00000000-0000-0000-0000-000000000001";
const GOLDEN_SECOND_ID = "microvm-00000000-0000-0000-0000-000000000002";
const GOLDEN_METADATA_FINGERPRINT = "8a197ac8111983d7";
const GOLDEN_DIGEST = "0559e532996f";
const GOLDEN_PAGE_TOKEN =
  "eyJhIjpudWxsLCJmIjoiOGExOTdhYzgxMTE5ODNkNyIsInMiOlsiMDU1OWU1MzI5OTZmIl0sInYiOjF9";
const GOLDEN_ORDER_FINGERPRINT = "1f1b123b1b744c3a";
const GOLDEN_KEY_TOKEN =
  "eyJmIjoiMWYxYjEyM2IxYjc0NGMzYSIsImsiOlsxNzkwMDAwMDAwMDAwLCJtaWNyb3ZtLTAwMDAwMDAwLTAwMDAtMDAw" +
  "MC0wMDAwLTAwMDAwMDAwMDAwMiJdLCJ2IjoxfQ";
const BASE_TIME_MS = Date.UTC(2026, 8, 22, 12, 0, 0);
const FINGERPRINT = "0123456789abcdef";

interface FilterOverrides {
  readonly states?: readonly string[];
  readonly startedAfterMs?: number;
  readonly metadata?: ReadonlyArray<readonly [string, string]>;
  readonly order?: ListOrder;
}

function filters(overrides: FilterOverrides = {}): ListFilters {
  return new ListFilters({
    imageArn: GOLDEN_IMAGE,
    imageVersion: undefined,
    states: overrides.states,
    startedAfterMs: overrides.startedAfterMs,
    metadata: overrides.metadata,
    order: overrides.order,
  });
}

function item(sandboxId: string, state = "RUNNING", seconds = 0): SandboxListItem {
  return sandboxListItem({
    sandboxId,
    state,
    template: GOLDEN_IMAGE,
    templateVersion: "1",
    startedAt: new Date(BASE_TIME_MS + seconds * 1000),
  });
}

function rawToken(document: unknown): string {
  return Buffer.from(canonicalJson(document), "utf8").toString("base64url");
}

function ids(items: readonly SandboxListItem[]): string[] {
  return items.map((entry) => entry.sandboxId);
}

function drain(walk: PageWalk, pages: ReadonlyMap<string | undefined, MicrovmPage>): string[] {
  const served: string[] = [];
  while (true) {
    const request = walk.pageToFetch();
    if (request !== undefined) {
      walk.acceptPage(pages.get(request.awsToken) as MicrovmPage);
      continue;
    }
    const raw = walk.nextRaw();
    if (raw === undefined) {
      return served;
    }
    served.push(raw.sandboxId);
  }
}

interface MicrovmPage {
  readonly items: readonly SandboxListItem[];
  readonly nextToken: string | undefined;
}

function page(items: readonly SandboxListItem[], nextToken?: string): MicrovmPage {
  return { items, nextToken };
}

describe("golden vectors shared with the Python SDK", () => {
  test("a page cursor with metadata", () => {
    expect(filters({ metadata: [["run", "ñ1"]] }).fingerprint()).toBe(GOLDEN_METADATA_FINGERPRINT);
    expect(itemDigest(GOLDEN_FIRST_ID)).toBe(GOLDEN_DIGEST);
    const cursor = pageCursor(undefined, [GOLDEN_DIGEST]);
    expect(encodeNextToken(GOLDEN_METADATA_FINGERPRINT, cursor)).toBe(GOLDEN_PAGE_TOKEN);
    expect(decodeNextToken(GOLDEN_PAGE_TOKEN)).toEqual({
      fingerprint: GOLDEN_METADATA_FINGERPRINT,
      cursor,
    });
  });

  test("a key cursor with order", () => {
    expect(filters({ order: "desc" }).fingerprint()).toBe(GOLDEN_ORDER_FINGERPRINT);
    const cursor = keyCursor(1_790_000_000_000, GOLDEN_SECOND_ID);
    expect(encodeNextToken(GOLDEN_ORDER_FINGERPRINT, cursor)).toBe(GOLDEN_KEY_TOKEN);
    expect(decodeNextToken(GOLDEN_KEY_TOKEN)).toEqual({
      fingerprint: GOLDEN_ORDER_FINGERPRINT,
      cursor,
    });
  });

  test("canonicalJson sorts keys by code point without spaces or ASCII escapes", () => {
    expect(canonicalJson({ b: 1, a: ["ñ", null] })).toBe('{"a":["ñ",null],"b":1}');
    expect(canonicalJson({ "\u{1F600}": 1, "\uFFFF": 2, z: "\n" })).toBe(
      '{"z":"\\n","\uFFFF":2,"\u{1F600}":1}',
    );
  });

  test("a page cursor round-trips with an AWS token and sorted digests", () => {
    const cursor = pageCursor("aws/next+token==", ["bbbbbbbbbbbb", "a".repeat(12)]);
    const token = encodeNextToken(FINGERPRINT, cursor);
    expect(token).not.toMatch(/[=+/]/);
    expect(JSON.parse(Buffer.from(token, "base64url").toString("utf8"))).toEqual({
      v: 1,
      f: FINGERPRINT,
      a: "aws/next+token==",
      s: ["a".repeat(12), "bbbbbbbbbbbb"],
    });
    expect(decodeNextToken(token)).toEqual({ fingerprint: FINGERPRINT, cursor });
  });

  test("the fingerprint changes with every filter and is 16 hex chars", () => {
    const variants = [
      filters(),
      filters({ states: ["RUNNING"] }),
      filters({ startedAfterMs: 1 }),
      filters({ metadata: [["env", "ci"]] }),
      filters({ order: "asc" }),
      filters({ order: "desc" }),
    ];
    const fingerprints = new Set(variants.map((variant) => variant.fingerprint()));
    expect(fingerprints.size).toBe(variants.length);
    for (const value of fingerprints) {
      expect(value).toMatch(/^[0-9a-f]{16}$/);
    }
  });
});

const VALID_PAGE = { v: 1, f: FINGERPRINT, a: null, s: [] };
const VALID_KEY = { v: 1, f: FINGERPRINT, k: [1, GOLDEN_FIRST_ID] };

describe("malformed tokens", () => {
  const malformed: ReadonlyArray<readonly [string, unknown]> = [
    ["not base64url", "%%%"],
    ["empty", ""],
    ["too long", "a".repeat(8193)],
    ["not JSON", "bm90IGpzb24"],
    ["impossible base64 length", "abcde"],
    ["not UTF-8", Buffer.from([0xff, 0xfe, 0xfd]).toString("base64url")],
    ["not an object", rawToken(["not", "an", "object"])],
    ["null", rawToken(null)],
    ["version 2", rawToken({ ...VALID_PAGE, v: 2 })],
    ["boolean version", rawToken({ ...VALID_PAGE, v: true })],
    ["uppercase fingerprint", rawToken({ ...VALID_PAGE, f: "0123456789ABCDEF" })],
    ["short fingerprint", rawToken({ ...VALID_PAGE, f: "0123" })],
    ["no cursor", rawToken({ v: 1, f: FINGERPRINT })],
    ["both cursors", rawToken({ ...VALID_PAGE, k: [1, GOLDEN_FIRST_ID] })],
    ["numeric aws token", rawToken({ ...VALID_PAGE, a: 5 })],
    ["digests not a list", rawToken({ ...VALID_PAGE, s: "abc" })],
    ["short digest", rawToken({ ...VALID_PAGE, s: ["xyz"] })],
    ["uppercase digest", rawToken({ ...VALID_PAGE, s: ["0559E532996F"] })],
    [
      "51 digests",
      rawToken({
        ...VALID_PAGE,
        s: Array.from({ length: 51 }, (_, index) => index.toString(16).padStart(12, "0")),
      }),
    ],
    ["an extra key", rawToken({ ...VALID_PAGE, extra: 1 })],
    ["a page cursor without digests", rawToken({ v: 1, f: FINGERPRINT, a: null })],
    ["a one-element key", rawToken({ ...VALID_KEY, k: [1] })],
    ["a string started_at", rawToken({ ...VALID_KEY, k: ["1", GOLDEN_FIRST_ID] })],
    ["a boolean started_at", rawToken({ ...VALID_KEY, k: [true, GOLDEN_FIRST_ID] })],
    ["an empty sandbox id", rawToken({ ...VALID_KEY, k: [1, ""] })],
    ["a fractional started_at", rawToken({ ...VALID_KEY, k: [1.5, GOLDEN_FIRST_ID] })],
    ["a negative started_at", rawToken({ ...VALID_KEY, k: [-1, GOLDEN_FIRST_ID] })],
    ["a number", 123],
    ["undefined", undefined],
  ];

  test.each(malformed)("%s is rejected without echoing the token", (_label, token) => {
    let caught: unknown;
    try {
      decodeNextToken(token);
    } catch (error) {
      caught = error;
    }
    expect(caught).toBeInstanceOf(InvalidArgumentError);
    expect((caught as Error).message).toBe(INVALID_TOKEN_MESSAGE);
    expect((caught as Error).cause).toBeUndefined();
  });

  test("fifty digests and an integer key are accepted", () => {
    const digests = Array.from({ length: 50 }, (_, index) => index.toString(16).padStart(12, "0"));
    const decoded = decodeNextToken(rawToken({ ...VALID_PAGE, s: digests }));
    expect(decoded.cursor.kind === "page" && decoded.cursor.consumed.size).toBe(50);
    expect(decodeNextToken(rawToken(VALID_KEY)).cursor).toEqual(keyCursor(1, GOLDEN_FIRST_ID));
  });
});

describe("filters", () => {
  test("the default states drop TERMINATING and TERMINATED", () => {
    const standard = filters();
    expect(standard.accepts(item("a", "RUNNING"))).toBe(true);
    expect(standard.accepts(item("b", "SUSPENDED"))).toBe(true);
    expect(standard.accepts(item("c", "TERMINATING"))).toBe(false);
    expect(standard.accepts(item("d", "TERMINATED"))).toBe(false);
  });

  test("explicit states are a membership test", () => {
    const suspended = filters({ states: ["SUSPENDED", "SUSPENDING"] });
    expect(suspended.accepts(item("a", "SUSPENDED"))).toBe(true);
    expect(suspended.accepts(item("b", "RUNNING"))).toBe(false);
    expect(filters({ states: ["TERMINATED"] }).accepts(item("c", "TERMINATED"))).toBe(true);
  });

  test("a metadata filter only accepts RUNNING items", () => {
    const byMetadata = filters({ metadata: [["env", "ci"]] });
    expect(byMetadata.accepts(item("a", "RUNNING"))).toBe(true);
    expect(byMetadata.accepts(item("b", "SUSPENDED"))).toBe(false);
  });

  test("startedAfter is inclusive at the millisecond", () => {
    const boundary = startedAtMs(item("a", "RUNNING", 15));
    const window = filters({ startedAfterMs: boundary });
    expect(window.accepts(item("a", "RUNNING", 15))).toBe(true);
    expect(window.accepts(item("b", "RUNNING", 20))).toBe(true);
    expect(window.accepts(item("c", "RUNNING", 14.999))).toBe(false);
  });
});

describe("PageWalk", () => {
  test("serves every page once and resets the consumed set on the next page", () => {
    const pages = new Map<string | undefined, MicrovmPage>([
      [undefined, page([item("a"), item("b")], "t2")],
      ["t2", page([item("c")])],
    ]);
    const walk = new PageWalk(FIRST_PAGE);
    expect(walk.hasMore).toBe(true);
    expect(walk.pageToFetch()).toEqual({ awsToken: undefined });
    walk.acceptPage(pages.get(undefined) as MicrovmPage);
    expect(walk.pageToFetch()).toBeUndefined();
    expect(walk.nextRaw()?.sandboxId).toBe("a");
    expect(walk.cursor()).toEqual(pageCursor(undefined, [itemDigest("a")]));
    expect(walk.nextRaw()?.sandboxId).toBe("b");
    expect(walk.hasMore).toBe(true);
    expect(walk.pageToFetch()).toEqual({ awsToken: "t2" });
    expect(walk.cursor()).toEqual(pageCursor("t2", []));
    walk.acceptPage(pages.get("t2") as MicrovmPage);
    expect(walk.nextRaw()?.sandboxId).toBe("c");
    expect(walk.hasMore).toBe(false);
    expect(walk.nextRaw()).toBeUndefined();
    expect(walk.pageToFetch()).toBeUndefined();
  });

  test("resumes by identity and tolerates an inserted item", () => {
    const pages = new Map([[undefined, page([item("new"), item("a"), item("b")])]]);
    expect(drain(new PageWalk(pageCursor(undefined, [itemDigest("a")])), pages)).toEqual([
      "new",
      "b",
    ]);
  });

  test("resumes on the page of the cursor's AWS token", () => {
    expect(new PageWalk(pageCursor("t2", [])).pageToFetch()).toEqual({ awsToken: "t2" });
  });

  test("moves past a fully consumed page", () => {
    const pages = new Map<string | undefined, MicrovmPage>([
      [undefined, page([item("a")], "t2")],
      ["t2", page([item("b")])],
    ]);
    expect(drain(new PageWalk(pageCursor(undefined, [itemDigest("a")])), pages)).toEqual(["b"]);
  });

  test("an empty last page exhausts the walk", () => {
    const walk = new PageWalk(FIRST_PAGE);
    walk.pageToFetch();
    walk.acceptPage(page([]));
    expect(walk.hasMore).toBe(false);
    expect(walk.pageToFetch()).toBeUndefined();
    expect(walk.nextRaw()).toBeUndefined();
  });
});

describe("OrderedWalk", () => {
  const unordered = [
    item("c", "RUNNING", 30),
    item("a", "RUNNING", 10),
    item("z", "RUNNING", 20),
    item("b", "RUNNING", 20),
  ];

  test("sorts ascending and descending with the sandbox id as tie-break", () => {
    expect(ids(new OrderedWalk(unordered, "asc", undefined).take(undefined))).toEqual([
      "a",
      "b",
      "z",
      "c",
    ]);
    expect(ids(new OrderedWalk(unordered, "desc", undefined).take(undefined))).toEqual([
      "c",
      "z",
      "b",
      "a",
    ]);
  });

  test("serves slices and reports the key of the last item served", () => {
    const walk = new OrderedWalk(
      [item("a", "RUNNING", 10), item("b", "RUNNING", 20), item("c", "RUNNING", 30)],
      "asc",
      undefined,
    );
    expect(walk.cursor()).toBeUndefined();
    expect(ids(walk.take(2))).toEqual(["a", "b"]);
    expect(walk.hasMore).toBe(true);
    expect(walk.cursor()).toEqual(keyCursor(startedAtMs(item("b", "RUNNING", 20)), "b"));
    expect(ids(walk.take(2))).toEqual(["c"]);
    expect(walk.hasMore).toBe(false);
    expect(walk.take(2)).toEqual([]);
  });

  test("skips up to the key in either direction", () => {
    const items = [
      item("a", "RUNNING", 10),
      item("b", "RUNNING", 20),
      item("c", "RUNNING", 30),
      item("n", "RUNNING", 20),
    ];
    const afterB = keyCursor(startedAtMs(item("b", "RUNNING", 20)), "b");
    expect(ids(new OrderedWalk(items, "asc", afterB).take(undefined))).toEqual(["n", "c"]);
    expect(ids(new OrderedWalk(items, "desc", afterB).take(undefined))).toEqual(["a"]);
    expect(new OrderedWalk(items, "asc", afterB).cursor()).toEqual(afterB);
  });
});

describe("listingRequest", () => {
  test("validateOrder and validateLimit", () => {
    expect(validateOrder(undefined)).toBeUndefined();
    expect(validateOrder("asc")).toBe("asc");
    expect(validateOrder("desc")).toBe("desc");
    expect(() => validateOrder("ASC")).toThrow(/se esperaba 'asc' o 'desc'/);
    expect(validateLimit(undefined)).toBeUndefined();
    expect(validateLimit(3)).toBe(3);
    for (const bad of [0, -1, true, 1.5, "2", Number.NaN]) {
      expect(() => validateLimit(bad)).toThrow(InvalidArgumentError);
    }
  });

  test("everything is validated before any call", () => {
    expect(() => listingRequest({ metadata: { env: "ci" }, states: ["SUSPENDED"] })).toThrow(
      /RUNNING/,
    );
    expect(() => listingRequest({ metadata: { "": "x" } })).toThrow(/metadata/);
    expect(() => listingRequest({ startedAfter: new Date(-1) })).toThrow(/startedAfter/);
    expect(() => listingRequest({ startedAfter: new Date(Number.NaN) })).toThrow(/startedAfter/);
    expect(() => listingRequest({ nextToken: "%%%" })).toThrow(INVALID_TOKEN_MESSAGE);
    expect(() => listingRequest({ limit: 0 })).toThrow(/limit/);
    expect(() => listingRequest({ order: "sideways" as ListOrder })).toThrow(/order/);
  });

  test("a cursor form that does not match the order is rejected", () => {
    const keyToken = encodeNextToken(FINGERPRINT, keyCursor(1, "x"));
    const pageToken = encodeNextToken(FINGERPRINT, FIRST_PAGE);
    expect(() => listingRequest({ nextToken: keyToken })).toThrow(INVALID_TOKEN_MESSAGE);
    expect(() => listingRequest({ nextToken: pageToken, order: "asc" })).toThrow(
      INVALID_TOKEN_MESSAGE,
    );
  });

  test("builds canonical filters", () => {
    const request = listingRequest({
      template: "rayito-base",
      templateVersion: "3",
      states: ["SUSPENDED", "RUNNING"],
      startedAfter: new Date(BASE_TIME_MS),
      order: "asc",
      limit: 2,
    });
    expect(request.limit).toBe(2);
    expect(request.template).toBe("rayito-base");
    expect(request.filters(GOLDEN_IMAGE)).toEqual(
      new ListFilters({
        imageArn: GOLDEN_IMAGE,
        imageVersion: "3",
        states: ["RUNNING", "SUSPENDED"],
        startedAfterMs: BASE_TIME_MS,
        metadata: undefined,
        order: "asc",
      }),
    );
    const byMetadata = listingRequest({ metadata: { run: "2", env: "ci" } }).filters(undefined);
    expect(byMetadata.metadata).toEqual([
      ["env", "ci"],
      ["run", "2"],
    ]);
    expect(byMetadata.states).toBeUndefined();
  });

  test("resumeCursors checks the fingerprint", () => {
    const wanted = filters();
    expect(resumeCursors(undefined, wanted)).toEqual({ page: FIRST_PAGE, key: undefined });
    const resumed = pageCursor("t", []);
    expect(resumeCursors({ fingerprint: wanted.fingerprint(), cursor: resumed }, wanted)).toEqual({
      page: resumed,
      key: undefined,
    });
    const key = keyCursor(5, "x");
    const ordered = filters({ order: "asc" });
    expect(resumeCursors({ fingerprint: ordered.fingerprint(), cursor: key }, ordered)).toEqual({
      page: FIRST_PAGE,
      key,
    });
    expect(() =>
      resumeCursors(
        { fingerprint: ordered.fingerprint(), cursor: key },
        filters({ order: "desc" }),
      ),
    ).toThrow(/no corresponde a estos filtros/);
  });

  test("nextTokenFor encodes only a known cursor", () => {
    expect(nextTokenFor(FINGERPRINT, undefined)).toBeUndefined();
    const token = nextTokenFor(FINGERPRINT, FIRST_PAGE) as string;
    expect(decodeNextToken(token)).toEqual({ fingerprint: FINGERPRINT, cursor: FIRST_PAGE });
    expect(EXHAUSTED_MESSAGE).toBe("no quedan páginas: hasNext es false");
  });
});
