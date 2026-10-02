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
import { EVENT_TYPE_PREFIX, type EventRecord, type WebhookInfo } from "./domain.js";

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
    FilterExpression?: string;
    ExpressionAttributeValues: Item;
    ScanIndexForward?: boolean;
    Limit?: number;
    ExclusiveStartKey?: Item;
  }): Promise<{ Items?: Array<Record<string, Av>>; LastEvaluatedKey?: Item }>;
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

/**
 * Runs one AWS call of `LifecycleEvents.<operation>` and turns any AWS SDK
 * error into a `WebhookError` that carries only the AWS error code: the
 * original message names the table, the secret and the account (§6), so
 * neither the message nor `cause` keeps it (`sanitizeAwsError` without
 * message). Mirror of the Python `_aws._aws_call`.
 */
export async function awsCall<T>(operation: string, invoke: () => Promise<T>): Promise<T> {
  try {
    return await invoke();
  } catch (error) {
    const cause = sanitizeAwsError(error, { includeMessage: false });
    throw new WebhookError(`LifecycleEvents.${operation}: AWS respondió ${cause.name}`, {
      awsCode: awsCode(error),
      cause,
    });
  }
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
  const items: Array<Record<string, Av>> = [];
  let exclusiveStartKey: Item | undefined;
  do {
    const response = await api.query({
      TableName: tableName,
      KeyConditionExpression: "pk = :pk",
      ExpressionAttributeValues: { ":pk": { S: WEBHOOK_PK } },
      ...(exclusiveStartKey === undefined ? {} : { ExclusiveStartKey: exclusiveStartKey }),
    });
    items.push(...(response.Items ?? []));
    exclusiveStartKey = response.LastEvaluatedKey;
  } while (exclusiveStartKey !== undefined);
  return items.map((item) => ({
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

/**
 * DynamoDB's own cap on how many `Query` pages a single `getEvents({
 * types, limit })` call turns into: `FilterExpression` (below) is applied
 * *after* `Limit` on each page, so a type-filtered query that matches
 * rarely could otherwise paginate the entire table one `Limit`-sized page
 * at a time. 25 pages of up to 100 raw rows each is already far more than
 * `getEvents` is meant for (a live tail of recent events, not a bulk
 * export); beyond that, `queryEvents` stops and returns whatever it
 * already found rather than scan without bound.
 */
const MAX_QUERY_PAGES = 25;

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
  const values: Record<string, Av> =
    options.sandboxId !== undefined
      ? { ":pk": { S: `EVENT#${options.sandboxId}` } }
      : { ":pk": { S: GSI1_PARTITION_VALUE } };
  let filterExpression: string | undefined;
  if (options.types !== undefined) {
    const kinds = [...new Set(options.types.map((type) => type.replace(EVENT_TYPE_PREFIX, "")))];
    kinds.forEach((kind, index) => {
      values[`:kind${index}`] = { S: kind };
    });
    filterExpression = `kind IN (${kinds.map((_kind, index) => `:kind${index}`).join(", ")})`;
  }

  // DynamoDB applies `Limit` to the raw rows before `FilterExpression`
  // runs, so fetch pages until `limit` matches are collected, the
  // partition/index is exhausted, or `MAX_QUERY_PAGES` is reached.
  const records: EventRecord[] = [];
  let exclusiveStartKey: Item | undefined;
  for (let page = 0; page < MAX_QUERY_PAGES; page += 1) {
    const response = await api.query({
      TableName: tableName,
      ...(options.sandboxId === undefined
        ? { IndexName: GSI1_NAME, KeyConditionExpression: "gsi1pk = :pk" }
        : { KeyConditionExpression: "pk = :pk" }),
      ...(filterExpression === undefined ? {} : { FilterExpression: filterExpression }),
      ExpressionAttributeValues: values,
      ScanIndexForward: scanForward,
      Limit: options.limit,
      ...(exclusiveStartKey === undefined ? {} : { ExclusiveStartKey: exclusiveStartKey }),
    });
    records.push(...(response.Items ?? []).map(recordFromItem));
    exclusiveStartKey = response.LastEvaluatedKey;
    if (records.length >= options.limit || exclusiveStartKey === undefined) {
      break;
    }
  }
  return records.slice(0, options.limit);
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
