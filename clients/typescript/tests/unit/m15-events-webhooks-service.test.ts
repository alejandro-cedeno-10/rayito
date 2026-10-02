/**
 * `LifecycleEvents`: construirlo no llama a AWS; `registerWebhook`/
 * `listWebhooks`/`deleteWebhook`/`getEvents` son directas sobre DynamoDB
 * (fake); `buildSection` deriva `k_sbx` correctamente.
 */

import { create } from "@bufbuild/protobuf";
import { describe, expect, it } from "vitest";
import { InvalidArgumentError, WebhookError } from "../../src/errors.js";
import { ConfigureRequestSchema } from "../../src/gen/rayito/v1/configure_pb.js";
import type { DynamoDbApi } from "../../src/lifecycle-events/dynamodb.js";
import { deriveSandboxKey } from "../../src/lifecycle-events/keys.js";
import { LifecycleEvents } from "../../src/lifecycle-events/service.js";
import { OptionalStacks } from "../../src/stacks/service.js";
import { FakeStackProvisioner } from "./m15-fake-stacks.js";

type Av = { S?: string; N?: string; SS?: readonly string[] };

class FakeTable implements DynamoDbApi {
  readonly items = new Map<string, Record<string, Av>>();

  private key(pk: string, sk: string): string {
    return `${pk}\u0000${sk}`;
  }

  async putItem(input: { TableName: string; Item: Record<string, Av> }): Promise<void> {
    const pk = input.Item.pk?.S ?? "";
    const sk = input.Item.sk?.S ?? "";
    this.items.set(this.key(pk, sk), input.Item);
  }

  async deleteItem(input: { TableName: string; Key: Record<string, Av> }): Promise<void> {
    this.items.delete(this.key(input.Key.pk?.S ?? "", input.Key.sk?.S ?? ""));
  }

  /**
   * Enough of real `Query` to exercise `queryEvents`'s pagination: `Limit`
   * caps the *raw* page before `FilterExpression` runs (the real DynamoDB
   * behaviour `queryEvents` works around), and `ExclusiveStartKey`/
   * `LastEvaluatedKey` carry an opaque cursor across calls. Only ever
   * parses the one `FilterExpression` shape `queryEvents` builds
   * (`"kind IN (:kind0, :kind1, ...)"`) — this is a fake for this one
   * caller, not a DynamoDB expression evaluator.
   */
  async query(input: {
    TableName: string;
    IndexName?: string;
    KeyConditionExpression: string;
    FilterExpression?: string;
    ExpressionAttributeValues: Record<string, Av>;
    ScanIndexForward?: boolean;
    Limit?: number;
    ExclusiveStartKey?: Record<string, Av>;
  }): Promise<{ Items?: Array<Record<string, Av>>; LastEvaluatedKey?: Record<string, Av> }> {
    const wantedPk = input.ExpressionAttributeValues[":pk"]?.S;
    const sortKey = input.IndexName === "gsi1" ? "gsi1sk" : "sk";
    const pkKey = input.IndexName === "gsi1" ? "gsi1pk" : "pk";
    let items = [...this.items.values()].filter((item) => item[pkKey]?.S === wantedPk);
    items.sort((a, b) => (a[sortKey]?.S ?? "").localeCompare(b[sortKey]?.S ?? ""));
    if (input.ScanIndexForward === false) {
      items.reverse();
    }

    const startAfter = input.ExclusiveStartKey?.[sortKey]?.S;
    if (startAfter !== undefined) {
      const startIndex = items.findIndex((item) => item[sortKey]?.S === startAfter);
      items = items.slice(startIndex + 1);
    }

    const page = input.Limit !== undefined ? items.slice(0, input.Limit) : items;
    const lastEvaluatedKey =
      input.Limit !== undefined && items.length > page.length
        ? ({ [sortKey]: page.at(-1)?.[sortKey] } as Record<string, Av>)
        : undefined;

    let filtered = page;
    if (input.FilterExpression !== undefined) {
      const wantedKinds = new Set(
        input.FilterExpression.replace("kind IN (", "")
          .replace(")", "")
          .split(",")
          .map((token) => input.ExpressionAttributeValues[token.trim()]?.S),
      );
      filtered = page.filter((item) => wantedKinds.has(item.kind?.S));
    }

    return {
      Items: filtered,
      ...(lastEvaluatedKey === undefined ? {} : { LastEvaluatedKey: lastEvaluatedKey }),
    };
  }
}

const STACK_KEY = Buffer.from("the-stack-wide-hmac-secret", "utf8");
const SECRET_ID = "arn:aws:secretsmanager:us-east-1:123456789012:secret:stack-key-abc123";
const TABLE_NAME = "rayito-events-webhooks-table";
const TABLE_ARN = `arn:aws:dynamodb:us-east-1:123456789012:table/${TABLE_NAME}`;

/** Answers every `Query` one item per page, with a `LastEvaluatedKey`
 * cursor, so a caller that reads only the first page misses the rest. */
class OnePerPageTable extends FakeTable {
  override async query(
    input: Parameters<FakeTable["query"]>[0],
  ): Promise<{ Items?: Array<Record<string, Av>>; LastEvaluatedKey?: Record<string, Av> }> {
    const { ExclusiveStartKey: _cursor, ...firstPage } = input;
    const all = (await super.query(firstPage)).Items ?? [];
    const after = input.ExclusiveStartKey?.sk?.S;
    const start = after === undefined ? 0 : all.findIndex((item) => item.sk?.S === after) + 1;
    const page = all.slice(start, start + 1);
    return start + 1 < all.length
      ? { Items: page, LastEvaluatedKey: { sk: page[0]?.sk ?? {} } }
      : { Items: page };
  }
}

/** Fails every call like DynamoDB would, naming the table ARN and the
 * account in the message — what must never reach the caller. */
class FailingTable extends FakeTable {
  private fail(): never {
    const error = new Error(`User is not authorized on ${TABLE_ARN}`);
    error.name = "AccessDeniedException";
    throw error;
  }

  override async putItem(): Promise<void> {
    this.fail();
  }

  override async query(): Promise<{ Items?: Array<Record<string, Av>> }> {
    this.fail();
  }
}

function eventsClient(table: FakeTable): LifecycleEvents {
  return new LifecycleEvents({
    region: "us-east-1",
    stacks: new OptionalStacks({ provisioner: new FakeStackProvisioner() }),
    dynamoClient: table,
    getSecretValue: async ({ SecretId }) => {
      expect(SecretId).toBe(SECRET_ID);
      return { SecretString: STACK_KEY.toString("utf8") };
    },
    tableNameForTests: TABLE_NAME,
    stackKeySecretIdForTests: SECRET_ID,
  });
}

describe("LifecycleEvents", () => {
  it("constructing it does not throw (no AWS call)", () => {
    expect(() => new LifecycleEvents()).not.toThrow();
  });

  it("register, list then delete a webhook", async () => {
    const ev = eventsClient(new FakeTable());
    const webhook = await ev.registerWebhook("https://hooks.example.com/rayito", {
      secretName: "mi-webhook",
      types: ["sandbox.lifecycle.killed"],
    });
    expect(webhook.url).toBe("https://hooks.example.com/rayito");
    const listed = await ev.listWebhooks();
    expect(listed.map((w) => w.webhookId)).toEqual([webhook.webhookId]);
    await ev.deleteWebhook(webhook.webhookId);
    expect(await ev.listWebhooks()).toEqual([]);
  });

  it("rejects a non-https webhook url", async () => {
    const ev = eventsClient(new FakeTable());
    await expect(
      ev.registerWebhook("http://hooks.example.com", {
        secretName: "x",
        types: ["sandbox.lifecycle.killed"],
      }),
    ).rejects.toThrow(InvalidArgumentError);
  });

  it("rejects an unknown event type", async () => {
    const ev = eventsClient(new FakeTable());
    await expect(
      ev.registerWebhook("https://hooks.example.com", {
        secretName: "x",
        types: ["sandbox.lifecycle.exploded"],
      }),
    ).rejects.toThrow(InvalidArgumentError);
  });

  it.each([0, -1, 101, 1.5])("rejects a limit outside 1..100 (%d)", async (limit) => {
    const ev = eventsClient(new FakeTable());
    await expect(ev.getEvents({ limit })).rejects.toThrow(InvalidArgumentError);
  });

  it("lists webhooks across every page", async () => {
    const table = new OnePerPageTable();
    const ev = eventsClient(table);
    for (const url of ["https://a.example.com", "https://b.example.com"]) {
      await ev.registerWebhook(url, { secretName: "x", types: ["sandbox.lifecycle.killed"] });
    }
    expect((await ev.listWebhooks()).map((w) => w.url).sort()).toEqual([
      "https://a.example.com",
      "https://b.example.com",
    ]);
  });

  it.each([
    ["listWebhooks", (ev: LifecycleEvents) => ev.listWebhooks()],
    ["getEvents", (ev: LifecycleEvents) => ev.getEvents({ sandboxId: "sbx-1" })],
    [
      "registerWebhook",
      (ev: LifecycleEvents) =>
        ev.registerWebhook("https://hooks.example.com", {
          secretName: "x",
          types: ["sandbox.lifecycle.killed"],
        }),
    ],
  ])("%s surfaces an AWS error as WebhookError with only its code", async (_name, call) => {
    const raised = await call(eventsClient(new FailingTable())).then(
      () => undefined,
      (error: unknown) => error,
    );
    expect(raised).toBeInstanceOf(WebhookError);
    const error = raised as WebhookError;
    expect(error.awsCode).toBe("AccessDeniedException");
    expect(error.message).not.toContain(TABLE_ARN);
    expect(String((error.cause as Error).message)).not.toContain(TABLE_ARN);
  });

  it("filters get_events by type", async () => {
    const table = new FakeTable();
    const ev = eventsClient(table);
    for (const [index, kind] of ["created", "paused", "killed"].entries()) {
      await table.putItem({
        TableName: TABLE_NAME,
        Item: {
          pk: { S: "EVENT#sbx-1" },
          sk: { S: `${String(index).padStart(20, "0")}#evt-${index}` },
          gsi1pk: { S: "EVENT" },
          gsi1sk: { S: `${String(index).padStart(20, "0")}#evt-${index}` },
          event_id: { S: `evt-${index}` },
          sandbox_id: { S: "sbx-1" },
          kind: { S: kind },
          generation: { N: "0" },
          occurred_at_ms: { N: String(index) },
          image_arn: { S: "arn:test" },
          image_version: { S: "1" },
        },
      });
    }
    const events = await ev.getEvents({ sandboxId: "sbx-1", types: ["sandbox.lifecycle.killed"] });
    expect(events.map((e) => e.kind)).toEqual(["killed"]);
  });

  it("paginates past non-matching rows to fill the limit", async () => {
    // Regression: `Limit` is a DynamoDB `Query`'s cap on *raw* rows per
    // page, applied before any `FilterExpression` — filtering only after
    // fetching `limit` rows could return fewer than `limit` matches (here,
    // zero) even though a match exists further down the same partition.
    const table = new FakeTable();
    const ev = eventsClient(table);
    for (const [index, kind] of ["created", "created", "created", "killed"].entries()) {
      await table.putItem({
        TableName: TABLE_NAME,
        Item: {
          pk: { S: "EVENT#sbx-1" },
          sk: { S: `${String(index).padStart(20, "0")}#evt-${index}` },
          gsi1pk: { S: "EVENT" },
          gsi1sk: { S: `${String(index).padStart(20, "0")}#evt-${index}` },
          event_id: { S: `evt-${index}` },
          sandbox_id: { S: "sbx-1" },
          kind: { S: kind },
          generation: { N: "0" },
          occurred_at_ms: { N: String(index) },
          image_arn: { S: "arn:test" },
          image_version: { S: "1" },
        },
      });
    }
    const events = await ev.getEvents({
      sandboxId: "sbx-1",
      types: ["sandbox.lifecycle.killed"],
      limit: 1,
      order: "asc",
    });
    expect(events.map((e) => e.kind)).toEqual(["killed"]);
  });

  it("builds a section with the derived sandbox key", async () => {
    const ev = eventsClient(new FakeTable());
    const section = await ev.buildSection({
      sandboxId: "sbx-1",
      imageArn: "arn:test",
      imageVersion: "1",
    });
    expect(section.section).toBe("lifecycle_events");
    const expectedKey = deriveSandboxKey(STACK_KEY, "sbx-1");
    const request = create(ConfigureRequestSchema, {});
    section.fill(request);
    const sandboxKey = Buffer.from(request.lifecycleEvents?.sandboxKey ?? new Uint8Array());
    expect(sandboxKey.equals(expectedKey)).toBe(true);
  });

  it("accepts a stack key delivered as SecretBinary, not only SecretString", async () => {
    const ev = new LifecycleEvents({
      region: "us-east-1",
      stacks: new OptionalStacks({ provisioner: new FakeStackProvisioner() }),
      dynamoClient: new FakeTable(),
      getSecretValue: async ({ SecretId }) => {
        expect(SecretId).toBe(SECRET_ID);
        return { SecretBinary: new Uint8Array(STACK_KEY) };
      },
      tableNameForTests: TABLE_NAME,
      stackKeySecretIdForTests: SECRET_ID,
    });
    const section = await ev.buildSection({
      sandboxId: "sbx-1",
      imageArn: "arn:test",
      imageVersion: "1",
    });
    const expectedKey = deriveSandboxKey(STACK_KEY, "sbx-1");
    const request = create(ConfigureRequestSchema, {});
    section.fill(request);
    const sandboxKey = Buffer.from(request.lifecycleEvents?.sandboxKey ?? new Uint8Array());
    expect(sandboxKey.equals(expectedKey)).toBe(true);
  });

  it("rejects a secret with neither SecretString nor SecretBinary", async () => {
    const ev = new LifecycleEvents({
      region: "us-east-1",
      stacks: new OptionalStacks({ provisioner: new FakeStackProvisioner() }),
      dynamoClient: new FakeTable(),
      getSecretValue: async () => ({}),
      tableNameForTests: TABLE_NAME,
      stackKeySecretIdForTests: SECRET_ID,
    });
    await expect(
      ev.buildSection({ sandboxId: "sbx-1", imageArn: "arn:test", imageVersion: "1" }),
    ).rejects.toThrow(WebhookError);
  });

  it("data-plane methods require deploy() first", async () => {
    const ev = new LifecycleEvents({
      region: "us-east-1",
      stacks: new OptionalStacks({ provisioner: new FakeStackProvisioner() }),
      dynamoClient: new FakeTable(),
    });
    await expect(ev.listWebhooks()).rejects.toThrow(WebhookError);
  });
});
