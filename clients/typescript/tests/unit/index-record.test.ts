/**
 * Núcleo puro y adaptador del índice de metadatos (M14), espejo de
 * `test_index_record.py` y `test_index_dynamodb.py`: la fila (TTL
 * determinista, sólo claves permitidas), la unión con `list-microvms`, la
 * huella del token con índice (mismo vector dorado que Python), los trozos
 * de 100, el reintento de `UnprocessedKeys` y el peer perezoso.
 */

import { afterEach, describe, expect, test, vi } from "vitest";
import { chunks } from "../../src/index/dynamodb.js";
import { joined } from "../../src/index/join.js";
import {
  fromItem,
  type IndexRecord,
  RECORD_ATTRIBUTES,
  recordFor,
  SDK_TAG,
  toItem,
} from "../../src/index/record.js";
import {
  DynamoDbIndex,
  IndexWriteError,
  InvalidArgumentError,
  SandboxIndexError,
} from "../../src/index.js";
import { type SandboxListItem, sandboxListItem } from "../../src/models.js";
import * as optional from "../../src/optional.js";
import { ListFilters, listingRequest } from "../../src/sandbox/listing.js";
import { FakeDynamoDb, fakeIndex, TABLE } from "./index-fake.js";

const IMAGE = "arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base";
const OTHER_IMAGE = "arn:aws:lambda:us-east-1:123456789012:microvm-image:other";
const STARTED = new Date(Date.UTC(2026, 8, 30, 12, 0, 0, 250));
const NOW_SECONDS = STARTED.getTime() / 1000 + 60;
const FORBIDDEN = ["token", "env", "secret", "payload", "jwe", "hash"];
/** El mismo vector que `GOLDEN_INDEX_FINGERPRINT` de `test_index_record.py`. */
const GOLDEN_INDEX_FINGERPRINT = "ae65162235b32204";

function info(sandboxId = "microvm-1", maximumDurationSeconds = 900) {
  return {
    sandboxId,
    template: IMAGE,
    templateVersion: "3",
    startedAt: STARTED,
    maximumDurationSeconds,
  };
}

function listed(
  sandboxId: string,
  state = "SUSPENDED",
  options: { image?: string; startedAt?: Date } = {},
) {
  return sandboxListItem({
    sandboxId,
    state,
    template: options.image ?? IMAGE,
    templateVersion: "3",
    startedAt: options.startedAt ?? STARTED,
  });
}

function rows(...records: IndexRecord[]): Map<string, IndexRecord> {
  return new Map(records.map((record) => [record.sandboxId, record]));
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("IndexRecord", () => {
  test("the TTL is deterministic: startedAt + max duration + margin", () => {
    const record = recordFor(info("microvm-1", 900), { user: "42" }, 3600);
    expect(record.startedAtMs).toBe(STARTED.getTime());
    expect(record.expiresAt).toBe(Math.floor(STARTED.getTime() / 1000) + 900 + 3600);
    expect(recordFor(info("microvm-1", 900), { user: "42" }, 3600)).toEqual(record);
    expect(record.sdk).toBe(SDK_TAG);
    expect(SDK_TAG.startsWith("ts/")).toBe(true);
    expect(recordFor(info(), undefined).metadata).toEqual({});
  });

  test("the item keys are the allow list and never carry credentials", () => {
    const item = toItem(recordFor(info(), { user: "42" }));
    expect(Object.keys(item).sort()).toEqual([...RECORD_ATTRIBUTES].sort());
    const offenders = Object.keys(item).filter((key) =>
      FORBIDDEN.some((bad) => key.toLowerCase().includes(bad)),
    );
    expect(offenders).toEqual([]);
    expect(item.metadata).toEqual({ M: { user: { S: "42" } } });
    expect(fromItem(item)).toEqual(recordFor(info(), { user: "42" }));
  });

  test("malformed rows are treated as missing", () => {
    expect(fromItem({ pk: { S: "x" } })).toBeUndefined();
    const item = { ...toItem(recordFor(info(), {})), started_at_ms: { N: "nope" } };
    expect(fromItem(item)).toBeUndefined();
  });
});

/** `joined` item a item, como hace el paginador: los que conserva, en su orden. */
function joinIndex(
  items: readonly SandboxListItem[],
  records: ReadonlyMap<string, IndexRecord>,
  wanted: Readonly<Record<string, string>>,
  nowSeconds: number,
): SandboxListItem[] {
  return items.flatMap(
    (item) => joined(item, records.get(item.sandboxId), wanted, nowSeconds) ?? [],
  );
}

describe("joined", () => {
  test("keeps matching rows with the state from list-microvms", () => {
    const kept = joinIndex(
      [listed("microvm-1", "SUSPENDED")],
      rows(recordFor(info("microvm-1"), { user: "42", team: "ml" })),
      { user: "42" },
      NOW_SECONDS,
    );
    expect(kept.map((item) => [item.sandboxId, item.state, item.metadata])).toEqual([
      ["microvm-1", "SUSPENDED", { user: "42", team: "ml" }],
    ]);
  });

  test("requires every wanted pair and drops items without a row", () => {
    const records = rows(recordFor(info("microvm-1"), { user: "42" }));
    expect(joinIndex([listed("microvm-1")], records, { user: "43" }, NOW_SECONDS)).toEqual([]);
    expect(joinIndex([listed("microvm-9")], records, { user: "42" }, NOW_SECONDS)).toEqual([]);
    expect(joinIndex([listed("microvm-1")], records, {}, NOW_SECONDS)).toHaveLength(1);
  });

  test("drops image or startedAt mismatches and expired rows", () => {
    const records = rows(
      recordFor(info("a"), { user: "42" }),
      recordFor(info("b"), { user: "42" }),
      recordFor(info("c"), { user: "42" }),
    );
    const items = [
      listed("a", "SUSPENDED", { image: OTHER_IMAGE }),
      listed("b", "SUSPENDED", { startedAt: new Date(STARTED.getTime() + 2000) }),
      listed("c", "SUSPENDED", { startedAt: new Date(STARTED.getTime() + 900) }),
    ];
    expect(joinIndex(items, records, { user: "42" }, NOW_SECONDS).map((i) => i.sandboxId)).toEqual([
      "c",
    ]);
    const expired = recordFor(info("c"), { user: "42" }, 0);
    const late = listed("c", "SUSPENDED", { startedAt: STARTED });
    expect(joinIndex([late], rows(expired), {}, expired.expiresAt + 1)).toEqual([]);
  });
});

describe("listing with an index", () => {
  test("metadata + index accepts suspended states and binds the token to the table", () => {
    const { index, api } = fakeIndex();
    const request = listingRequest({ metadata: { user: "42" }, states: ["SUSPENDED"], index });
    expect(request.index).toBe(index);
    const filters = request.filters(undefined);
    expect(filters.indexTable).toBe(TABLE);
    expect(filters.accepts(listed("x", "SUSPENDED"))).toBe(true);
    expect(filters.accepts(listed("x", "RUNNING"))).toBe(false);
    expect(api.requests).toEqual([]);
  });

  test("the default states with an index are every live state", () => {
    const { index } = fakeIndex();
    const filters = listingRequest({ metadata: { user: "42" }, index }).filters(undefined);
    for (const state of ["RUNNING", "PENDING", "SUSPENDING", "SUSPENDED"]) {
      expect(filters.accepts(listed("x", state))).toBe(true);
    }
    expect(filters.accepts(listed("x", "TERMINATED"))).toBe(false);
  });

  test("terminal states are rejected and suspended without index still is", () => {
    const { index } = fakeIndex();
    expect(() =>
      listingRequest({ metadata: { user: "42" }, states: ["TERMINATED"], index }),
    ).toThrow(/TERMINATED/);
    expect(() => listingRequest({ metadata: { user: "42" }, states: ["SUSPENDED"] })).toThrow(
      /RUNNING/,
    );
  });

  test("index without metadata is the plain listing", () => {
    const { index } = fakeIndex();
    const request = listingRequest({ index });
    expect(request.index).toBeUndefined();
    expect(request.filters(undefined).indexTable).toBeUndefined();
  });

  test("the fingerprint matches the Python golden vector", () => {
    const base = new ListFilters({
      imageArn: undefined,
      imageVersion: undefined,
      states: undefined,
      startedAfterMs: undefined,
      metadata: [["user", "42"]],
      order: undefined,
    });
    const indexed = new ListFilters({
      imageArn: undefined,
      imageVersion: undefined,
      states: undefined,
      startedAfterMs: undefined,
      metadata: [["user", "42"]],
      order: undefined,
      indexTable: TABLE,
    });
    expect(indexed.fingerprint()).not.toBe(base.fingerprint());
    expect(indexed.fingerprint()).toBe(GOLDEN_INDEX_FINGERPRINT);
  });

  test("index must be a DynamoDbIndex", () => {
    expect(() =>
      listingRequest({ metadata: { a: "1" }, index: "tabla" as unknown as DynamoDbIndex }),
    ).toThrow(InvalidArgumentError);
  });
});

describe("DynamoDbIndex", () => {
  test("constructing it loads no peer and makes no call", () => {
    const loader = vi.spyOn(optional, "loadOptionalPeer");
    const index = new DynamoDbIndex({ tableName: TABLE, region: "us-east-1" });
    expect(index.toJSON()).toEqual({ tableName: TABLE, onWriteFailure: "terminate" });
    expect(loader).not.toHaveBeenCalled();
  });

  test("the optional peer is loaded on first use only", async () => {
    const api = new FakeDynamoDb();
    const loader = vi.spyOn(optional, "loadOptionalPeer").mockResolvedValue({
      DynamoDBClient: class {
        send(command: { input: Parameters<FakeDynamoDb["batchGetItem"]>[0] }) {
          return api.batchGetItem(command.input);
        }
      },
      PutItemCommand: class {},
      BatchGetItemCommand: class {
        constructor(readonly input: object) {}
      },
    });
    const index = new DynamoDbIndex({ tableName: TABLE, region: "us-east-1" });
    expect(loader).not.toHaveBeenCalled();
    await index.batchGet(["a"]);
    await index.batchGet(["b"]);
    expect(loader).toHaveBeenCalledTimes(1);
    expect(loader.mock.calls[0]?.[0]).toBe("@aws-sdk/client-dynamodb");
  });

  test("invalid configuration is rejected without AWS", () => {
    expect(() => new DynamoDbIndex({ tableName: "x" })).toThrow(/tableName/);
    expect(
      () =>
        new DynamoDbIndex({
          tableName: TABLE,
          onWriteFailure: "ignore" as unknown as "warn",
        }),
    ).toThrow(/onWriteFailure/);
    expect(() => new DynamoDbIndex({ tableName: TABLE, ttlMarginSeconds: -1 })).toThrow(
      /ttlMarginSeconds/,
    );
  });

  test("put is conditional and never overwrites", async () => {
    const { index, api } = fakeIndex();
    const record = recordFor(info("a"), { k: "v" });
    await index.put(record);
    expect(api.calls("putItem")[0]?.ConditionExpression).toBe("attribute_not_exists(pk)");
    await expect(index.put(record)).rejects.toSatisfy((error: unknown) => {
      expect(error).toBeInstanceOf(IndexWriteError);
      expect((error as IndexWriteError).awsCode).toBe("ConditionalCheckFailedException");
      return true;
    });
  });

  test("batchGet splits into chunks of one hundred and skips empty input", async () => {
    const { index, api } = fakeIndex({ now: () => NOW_SECONDS * 1000 });
    const ids = Array.from({ length: 230 }, (_, n) => `microvm-${n}`);
    for (const id of ids.slice(0, 5)) {
      await index.put(recordFor(info(id), { n: id }));
    }
    const found = await index.batchGet(ids);
    expect(api.calls("batchGetItem").map((call) => call.RequestItems[TABLE]?.Keys.length)).toEqual([
      100, 100, 30,
    ]);
    expect([...found.keys()].sort()).toEqual(ids.slice(0, 5).sort());
    const before = api.requests.length;
    expect((await index.batchGet([])).size).toBe(0);
    expect(api.requests.length).toBe(before);
    expect(chunks(["a", "a", "b"])).toEqual([["a", "b"]]);
  });

  test("unprocessed keys are retried with bounded backoff, then raise", async () => {
    const api = new FakeDynamoDb();
    api.unprocessedRounds = 2;
    const { index, sleeps } = fakeIndex({ api, now: () => NOW_SECONDS * 1000 });
    const ids = Array.from({ length: 8 }, (_, n) => `microvm-${n}`);
    for (const id of ids) {
      await index.put(recordFor(info(id), {}));
    }
    expect([...(await index.batchGet(ids)).keys()].sort()).toEqual(ids.sort());
    expect(sleeps).toEqual([50, 100]);
    api.unprocessedRounds = 100;
    await expect(index.batchGet(ids)).rejects.toThrow(/sin procesar/);
  });

  test("read errors never repeat the AWS message", async () => {
    const { index, api } = fakeIndex();
    api.batchError = "AccessDeniedException";
    await expect(index.batchGet(["a"])).rejects.toSatisfy((error: unknown) => {
      expect(error).toBeInstanceOf(SandboxIndexError);
      expect(error).not.toBeInstanceOf(IndexWriteError);
      expect((error as Error).message).toContain("dynamodb:BatchGetItem");
      expect((error as Error).message).not.toContain("secret-ish");
      return true;
    });
  });
});
