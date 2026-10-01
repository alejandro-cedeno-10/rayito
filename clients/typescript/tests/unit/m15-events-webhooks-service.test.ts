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

  async query(input: {
    TableName: string;
    IndexName?: string;
    KeyConditionExpression: string;
    ExpressionAttributeValues: Record<string, Av>;
    ScanIndexForward?: boolean;
    Limit?: number;
  }): Promise<{ Items?: Array<Record<string, Av>> }> {
    const wantedPk = input.ExpressionAttributeValues[":pk"]?.S;
    let items: Array<Record<string, Av>>;
    if (input.IndexName === "gsi1") {
      items = [...this.items.values()].filter((item) => item.gsi1pk?.S === wantedPk);
      items.sort((a, b) => (a.gsi1sk?.S ?? "").localeCompare(b.gsi1sk?.S ?? ""));
    } else {
      items = [...this.items.values()].filter((item) => item.pk?.S === wantedPk);
      items.sort((a, b) => (a.sk?.S ?? "").localeCompare(b.sk?.S ?? ""));
    }
    if (input.ScanIndexForward === false) {
      items.reverse();
    }
    return { Items: input.Limit !== undefined ? items.slice(0, input.Limit) : items };
  }
}

const STACK_KEY = Buffer.from("the-stack-wide-hmac-secret", "utf8");
const SECRET_ID = "arn:aws:secretsmanager:us-east-1:123456789012:secret:stack-key-abc123";
const TABLE_NAME = "rayito-events-webhooks-table";

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

  it("rejects limit above the maximum", async () => {
    const ev = eventsClient(new FakeTable());
    await expect(ev.getEvents({ limit: 101 })).rejects.toThrow(InvalidArgumentError);
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

  it("data-plane methods require deploy() first", async () => {
    const ev = new LifecycleEvents({
      region: "us-east-1",
      stacks: new OptionalStacks({ provisioner: new FakeStackProvisioner() }),
      dynamoClient: new FakeTable(),
    });
    await expect(ev.listWebhooks()).rejects.toThrow(WebhookError);
  });
});
