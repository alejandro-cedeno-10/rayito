/**
 * Adaptador `@aws-sdk/client-dynamodb` sobre la tabla de
 * `infra/events-webhooks.yaml`. El diseño de claves es el mismo que usan
 * los Lambdas (`infra/lambdas/events_webhooks/domain/schema.py`) y el SDK
 * Python (`rayito._lifecycle_events._dynamodb`) — tres paquetes
 * desplegables distintos, así que no hay un `import` que compartir; un
 * cambio aquí es un cambio allí, fijado por `AWS_API_NOTES.md` §25.
 */

import type { AwsClientSettings } from "../aws/control-plane.js";
import { awsCode, LazyAwsApi, loadOptionalSdkClient } from "../aws/optional-client.js";
import { sanitizeAwsError } from "../aws/sanitize.js";
import { WebhookError } from "../errors.js";
import type { EventRecord, WebhookInfo } from "./domain.js";

type Credentials = AwsClientSettings["credentials"];

const DYNAMODB_PEER = "@aws-sdk/client-dynamodb";
export const GSI1_NAME = "gsi1";
export const GSI1_PARTITION_VALUE = "EVENT";
export const WEBHOOK_PK = "WEBHOOK";

type Av = { readonly S?: string } & { readonly N?: string } & { readonly SS?: readonly string[] };
type Item = Readonly<Record<string, Av>>;

export interface DynamoDbApi {
  putItem(input: { TableName: string; Item: Item }): Promise<unknown>;
  deleteItem(input: { TableName: string; Key: Item }): Promise<unknown>;
  query(input: {
    TableName: string;
    IndexName?: string;
    KeyConditionExpression: string;
    ExpressionAttributeValues: Item;
    ScanIndexForward?: boolean;
    Limit?: number;
  }): Promise<{ Items?: Array<Record<string, Av>> }>;
}

interface DynamoDbModule {
  readonly DynamoDBClient: new (config: object) => { send(command: unknown): Promise<unknown> };
  readonly PutItemCommand: new (input: object) => unknown;
  readonly DeleteItemCommand: new (input: object) => unknown;
  readonly QueryCommand: new (input: object) => unknown;
}

export async function sdkApi(region: string, credentials: Credentials): Promise<DynamoDbApi> {
  const { sdk, send } = await loadOptionalSdkClient<DynamoDbModule>(
    DYNAMODB_PEER,
    "eventos de ciclo de vida (LifecycleEvents / events)",
    (module) => module.DynamoDBClient,
    region,
    credentials,
  );
  return {
    putItem: (input) => send(new sdk.PutItemCommand(input)),
    deleteItem: (input) => send(new sdk.DeleteItemCommand(input)),
    query: (input) => send(new sdk.QueryCommand(input)) as ReturnType<DynamoDbApi["query"]>,
  };
}

export function lazyApi(
  region: string | undefined,
  credentials: Credentials,
): LazyAwsApi<DynamoDbApi> {
  return new LazyAwsApi(
    region,
    "falta la región de LifecycleEvents: pasa region o define AWS_REGION",
    (resolvedRegion) => sdkApi(resolvedRegion, credentials),
  );
}

export function dynamoError(message: string, error: unknown): WebhookError {
  return new WebhookError(message, {
    awsCode: awsCode(error),
    cause: sanitizeAwsError(error, { includeMessage: false }),
  });
}

export async function putWebhook(
  api: DynamoDbApi,
  tableName: string,
  options: { webhookId: string; url: string; secretName: string; types: readonly string[] },
): Promise<void> {
  await api.putItem({
    TableName: tableName,
    Item: {
      pk: { S: WEBHOOK_PK },
      sk: { S: options.webhookId },
      url: { S: options.url },
      secret_name: { S: options.secretName },
      types: { SS: options.types },
    },
  });
}

export async function listWebhooks(api: DynamoDbApi, tableName: string): Promise<WebhookInfo[]> {
  const response = await api.query({
    TableName: tableName,
    KeyConditionExpression: "pk = :pk",
    ExpressionAttributeValues: { ":pk": { S: WEBHOOK_PK } },
  });
  return (response.Items ?? []).map((item) => ({
    webhookId: item.sk?.S ?? "",
    url: item.url?.S ?? "",
    types: item.types?.SS ?? [],
  }));
}

export async function deleteWebhook(
  api: DynamoDbApi,
  tableName: string,
  webhookId: string,
): Promise<void> {
  await api.deleteItem({
    TableName: tableName,
    Key: { pk: { S: WEBHOOK_PK }, sk: { S: webhookId } },
  });
}

export async function queryEvents(
  api: DynamoDbApi,
  tableName: string,
  options: {
    sandboxId: string | undefined;
    types: readonly string[] | undefined;
    limit: number;
    order: "asc" | "desc";
  },
): Promise<EventRecord[]> {
  const scanForward = options.order === "asc";
  const response = await (options.sandboxId !== undefined
    ? api.query({
        TableName: tableName,
        KeyConditionExpression: "pk = :pk",
        ExpressionAttributeValues: { ":pk": { S: `EVENT#${options.sandboxId}` } },
        ScanIndexForward: scanForward,
        Limit: options.limit,
      })
    : api.query({
        TableName: tableName,
        IndexName: GSI1_NAME,
        KeyConditionExpression: "gsi1pk = :pk",
        ExpressionAttributeValues: { ":pk": { S: GSI1_PARTITION_VALUE } },
        ScanIndexForward: scanForward,
        Limit: options.limit,
      }));
  const records = (response.Items ?? []).map(recordFromItem);
  const filtered =
    options.types === undefined
      ? records
      : records.filter((record) => options.types?.includes(`sandbox.lifecycle.${record.kind}`));
  return filtered.slice(0, options.limit);
}

function recordFromItem(item: Record<string, Av>): EventRecord {
  return {
    eventId: item.event_id?.S ?? "",
    sandboxId: item.sandbox_id?.S ?? "",
    kind: (item.kind?.S ?? "created") as EventRecord["kind"],
    killReason: item.kill_reason?.S as EventRecord["killReason"] | undefined,
    generation: Number(item.generation?.N ?? "0"),
    occurredAtMs: Number(item.occurred_at_ms?.N ?? "0"),
    imageArn: item.image_arn?.S ?? "",
    imageVersion: item.image_version?.S ?? "",
  };
}
