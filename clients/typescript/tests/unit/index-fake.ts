/**
 * DynamoDB falso para los tests del índice de metadatos (M14), espejo de
 * `clients/python/tests/unit/fake_dynamodb.py`: un `DynamoDbApi` en memoria
 * que anota cada petición, comprueba que sólo lleva los parámetros de
 * AWS_API_NOTES.md §20 y puede dejar claves sin procesar o fallar con un
 * código de AWS.
 */

import { expect } from "vitest";
import { type DynamoDbApi, DynamoDbIndex, type DynamoDbIndexOptions } from "../../src/index.js";

export const TABLE = "rayito-sandboxes";

export function awsError(name: string): Error {
  const error = new Error(`${TABLE} secret-ish detail`);
  error.name = name;
  return error;
}

type BatchInput = Parameters<DynamoDbApi["batchGetItem"]>[0];
type PutInput = Parameters<DynamoDbApi["putItem"]>[0];

export class FakeDynamoDb implements DynamoDbApi {
  readonly items = new Map<string, Record<string, unknown>>();
  readonly requests: Array<{ operation: string; input: PutInput | BatchInput }> = [];
  putError: string | undefined;
  batchError: string | undefined;
  unprocessedRounds = 0;

  calls<T extends "putItem" | "batchGetItem">(
    operation: T,
  ): Array<T extends "putItem" ? PutInput : BatchInput> {
    return this.requests
      .filter((request) => request.operation === operation)
      .map((request) => request.input) as Array<T extends "putItem" ? PutInput : BatchInput>;
  }

  async putItem(input: PutInput): Promise<unknown> {
    this.requests.push({ operation: "putItem", input });
    expect(Object.keys(input).sort()).toEqual(["ConditionExpression", "Item", "TableName"]);
    expect(input.TableName).toBe(TABLE);
    if (this.putError !== undefined) {
      throw awsError(this.putError);
    }
    const key = (input.Item.pk as { S: string }).S;
    if (input.ConditionExpression === "attribute_not_exists(pk)" && this.items.has(key)) {
      throw awsError("ConditionalCheckFailedException");
    }
    this.items.set(key, input.Item as Record<string, unknown>);
    return {};
  }

  async batchGetItem(input: BatchInput): ReturnType<DynamoDbApi["batchGetItem"]> {
    this.requests.push({ operation: "batchGetItem", input });
    expect(Object.keys(input)).toEqual(["RequestItems"]);
    if (this.batchError !== undefined) {
      throw awsError(this.batchError);
    }
    const request = input.RequestItems[TABLE];
    expect(request).toBeDefined();
    expect(Object.keys(request ?? {}).sort()).toEqual(["ConsistentRead", "Keys"]);
    expect(request?.ConsistentRead).toBe(false);
    const keys = request?.Keys ?? [];
    expect(keys.length).toBeGreaterThanOrEqual(1);
    expect(keys.length).toBeLessThanOrEqual(100);
    let served = keys;
    let pending: typeof keys = [];
    if (this.unprocessedRounds > 0) {
      this.unprocessedRounds -= 1;
      served = keys.slice(0, Math.floor(keys.length / 2));
      pending = keys.slice(Math.floor(keys.length / 2));
    }
    const found = served
      .map((key) => this.items.get(key.pk.S))
      .filter((item): item is Record<string, unknown> => item !== undefined);
    return {
      Responses: { [TABLE]: found },
      UnprocessedKeys:
        pending.length === 0 ? {} : { [TABLE]: { Keys: pending, ConsistentRead: false } },
    };
  }
}

/** Un `DynamoDbIndex` real sobre la tabla falsa, sin esperas reales. */
export function fakeIndex(
  options: Partial<DynamoDbIndexOptions> & { readonly api?: FakeDynamoDb } = {},
): { index: DynamoDbIndex; api: FakeDynamoDb; sleeps: number[] } {
  const api = options.api ?? new FakeDynamoDb();
  const sleeps: number[] = [];
  const index = new DynamoDbIndex({
    tableName: TABLE,
    client: api,
    sleep: async (ms) => {
      sleeps.push(ms);
    },
    ...options,
  });
  return { index, api, sleeps };
}
