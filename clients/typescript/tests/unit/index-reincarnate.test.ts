/**
 * `reincarnate()` conserva `index` (M14): el sucesor de un sandbox creado con
 * `index: new DynamoDbIndex(...)` escribe su propia fila condicional y el
 * listado con índice lo encuentra; sin `index` el sucesor no toca DynamoDB.
 * Espejo de `test_index_reincarnate.py`.
 */

import { describe, expect, test } from "vitest";
import { S3Prefix, Sandbox } from "../../src/index.js";
import { SANDBOX_ID, STARTED_AT } from "./fake/control-plane.js";
import { createTestSandbox } from "./helpers.js";
import { fakeIndex, TABLE } from "./index-fake.js";

const ROLE = "arn:aws:iam::123456789012:role/rayito-execution";
const SUCCESSOR_ID = "microvm-00000000-0000-0000-0000-000000000002";
const METADATA = { user: "42" };
/** El reloj del índice en el arranque del VM falso: la fila no ha caducado. */
const NOW_MS = STARTED_AT.getTime();

async function collect<T>(items: AsyncIterable<T>): Promise<T[]> {
  const all: T[] = [];
  for await (const item of items) {
    all.push(item);
  }
  return all;
}

function persisted(extra: Parameters<typeof Sandbox.create>[0] = {}) {
  return createTestSandbox({
    create: {
      executionRoleArn: ROLE,
      persist: new S3Prefix({ bucket: "my-bucket" }),
      metadata: METADATA,
      ...extra,
    },
  });
}

describe("reincarnate() with index", () => {
  test("the successor writes its own conditional row and the indexed listing finds it", async () => {
    const { index, api } = fakeIndex({ now: () => NOW_MS });
    const { sandbox, plane } = await persisted({ index });
    plane.sandboxIds.push(SUCCESSOR_ID);
    const successor = await sandbox.reincarnate();
    try {
      const puts = api.calls("putItem");
      expect(puts.map((put) => (put.Item.pk as { S: string }).S)).toEqual([
        SANDBOX_ID,
        SUCCESSOR_ID,
      ]);
      expect(puts.at(-1)?.ConditionExpression).toBe("attribute_not_exists(pk)");
      expect(puts.at(-1)?.Item.metadata).toEqual({ M: { user: { S: "42" } } });

      plane.addListed(SUCCESSOR_ID, "RUNNING");
      const found = await collect(Sandbox.list({ metadata: METADATA, index, controlPlane: plane }));
      expect(found.map((item) => item.sandboxId)).toEqual([SUCCESSOR_ID]);
      const [batch] = api.calls("batchGetItem");
      expect(batch?.RequestItems[TABLE]?.Keys).toEqual([{ pk: { S: SUCCESSOR_ID } }]);
    } finally {
      successor.close();
    }
  });

  test("without index the successor never touches DynamoDB", async () => {
    const { api } = fakeIndex({ now: () => NOW_MS });
    const { sandbox, plane } = await persisted();
    plane.sandboxIds.push(SUCCESSOR_ID);
    const successor = await sandbox.reincarnate();
    try {
      expect(successor.sandboxId).toBe(SUCCESSOR_ID);
      expect(api.requests).toEqual([]);
    } finally {
      successor.close();
    }
  });
});
