/**
 * `LifecycleEvents` (M15, m15-events-webhooks): fachada sobre
 * `OptionalStacks` (`deploy`/`status`/`destroy`) más las operaciones
 * propias de la función (`registerWebhook`/`listWebhooks`/`deleteWebhook`/
 * `getEvents`), directas sobre DynamoDB. Construirlo no hace ninguna
 * llamada a AWS. Espejo de `rayito._lifecycle_events._service` (Python),
 * una sola clase async (sin par síncrono, como el resto del SDK TS).
 */

import { randomBytes } from "node:crypto";
import type { AwsClientSettings } from "../aws/control-plane.js";
import { type LazyAwsApi, loadOptionalSdkClient } from "../aws/optional-client.js";
import { InvalidArgumentError, WebhookError } from "../errors.js";
import { resolveSecretId, WEBHOOK_SECRET_PREFIX } from "../secrets/names.js";
import type { StackComponent, StackStatus } from "../stacks/model.js";
import { componentByName } from "../stacks/registry.js";
import { OptionalStacks } from "../stacks/service.js";
import {
  DEFAULT_GET_EVENTS_LIMIT,
  DEFAULT_RECONCILER_INTERVAL_MINUTES,
  DEFAULT_STACK_NAME,
  type EventRecord,
  INVALID_WEBHOOK_URL,
  isDeliverableWebhookUrl,
  type WebhookInfo,
} from "./domain.js";
import {
  awsCall,
  type DynamoDbApi,
  deleteWebhook as deleteWebhookItem,
  lazyApi,
  listWebhooks as listWebhookItems,
  putWebhook,
  queryEvents,
} from "./dynamodb.js";
import { deriveSandboxKey } from "./keys.js";
import {
  LifecycleEventsSection,
  type LifecycleEventsSectionFacts,
  type LifecycleEventsSectionSource,
} from "./section.js";

export { WEBHOOK_SECRET_PREFIX };

const COMPONENT: StackComponent = componentByName("events-webhooks") as StackComponent;
const EVENT_TYPE_PATTERN = /^sandbox\.lifecycle\.(created|paused|resumed|killed)$/;
// Coincides with `DEFAULT_GET_EVENTS_LIMIT` today, but is a different knob
// (the hard ceiling `limit` may never exceed, not the default when it is
// omitted) — a DynamoDB `Query`'s own practical page size for this table
// (AWS_API_NOTES.md §25), kept separate on purpose.
const MAX_GET_EVENTS_LIMIT = 100;
const MIN_GET_EVENTS_LIMIT = 1;

type Credentials = AwsClientSettings["credentials"];

function validateTypes(types: readonly string[] | undefined): readonly string[] | undefined {
  if (types === undefined) {
    return undefined;
  }
  for (const type of types) {
    if (!EVENT_TYPE_PATTERN.test(type)) {
      throw new InvalidArgumentError(
        `tipo de evento desconocido: ${JSON.stringify(type)} ` +
          "(sandbox.lifecycle.{created,paused,resumed,killed})",
      );
    }
  }
  return types;
}

/** A fake `GetSecretValue`, for tests only — the real path goes through
 * the optional `@aws-sdk/client-secrets-manager` peer. `SecretBinary` is
 * `Uint8Array` to match that SDK's own `GetSecretValueCommandOutput`
 * (never a Node `Buffer` specifically — `Buffer` already *is* a
 * `Uint8Array`, so this accepts both without importing the SDK's types). */
export type SecretValueGetter = (input: { readonly SecretId: string }) => Promise<{
  readonly SecretString?: string;
  readonly SecretBinary?: Uint8Array;
}>;

export interface LifecycleEventsOptions {
  readonly stackName?: string;
  readonly region?: string | undefined;
  readonly credentials?: Credentials;
  readonly stacks?: OptionalStacks;
  /** A fake `DynamoDbApi`, for tests only — the real path is `dynamodb.js`'s
   * `lazyApi` over the optional `@aws-sdk/client-dynamodb` peer. */
  readonly dynamoClient?: DynamoDbApi;
  readonly getSecretValue?: SecretValueGetter;
  /** Test-only: skip `deploy()`/`status()` and use this table/secret id
   * directly. There is no public setter for these after construction —
   * the real path always learns them from a deployed stack. */
  readonly tableNameForTests?: string;
  readonly stackKeySecretIdForTests?: string;
}

export interface DeployWebhooksOptions {
  readonly artifactBucket: string;
  readonly logGroupName: string;
  readonly reconcilerIntervalMinutes?: number;
  readonly tags?: Readonly<Record<string, string>>;
  readonly wait?: boolean;
}

interface SecretsManagerModule {
  readonly SecretsManagerClient: new (
    config: object,
  ) => { send(command: unknown): Promise<unknown> };
  readonly GetSecretValueCommand: new (input: object) => unknown;
}

/**
 * Eventos de ciclo de vida firmados y webhooks compatibles con E2B, sobre
 * una pila en tu propia cuenta (`infra/events-webhooks.yaml`). Construirlo
 * no llama a AWS.
 *
 * Coste y activación
 * -------------------
 * Activa: `new LifecycleEvents()` seguido de `deploy({ artifactBucket,
 *   logGroupName })`, o `events: new LifecycleEvents(...)` en
 *   `Sandbox.create()` (además exige `logging: "cloudwatch"`).
 * Recursos y llamadas AWS: `deploy()` crea un secreto de Secrets Manager (la
 *   clave HMAC del stack), una tabla DynamoDB on-demand con streams, tres
 *   funciones Lambda (forwarder/deliverer/reconciler), una suscripción de
 *   CloudWatch Logs y una regla de EventBridge Scheduler.
 *   `registerWebhook`/`listWebhooks`/`deleteWebhook`/`getEvents` llaman
 *   directamente a DynamoDB (`PutItem`/`Query`/`DeleteItem`), nunca a un
 *   Lambda. `deploy()` crea además una cola SQS de fallos del deliverer.
 * Coste aproximado: ~$0,40/mes el secreto; DynamoDB y Lambda son
 *   on-demand/por invocación ($0 en reposo); ~$1,25 por millón de eventos
 *   escritos (WRU) más las lecturas de `getEvents`; el reconciliador
 *   factura una invocación cada `reconcilerIntervalMinutes` (5 por
 *   defecto, mínimo 2, ~$0,0000002 c/u). us-east-1, consultado 2026-09-30.
 * IAM: `EventsOperatorPolicy` (salida de la pila), en las credenciales del
 *   llamante: `PutItem`/`Query`/`DeleteItem` sobre la tabla y su índice,
 *   `cloudformation:DescribeStacks` sobre la pila y
 *   `secretsmanager:GetSecretValue` sobre la clave del stack.
 * Cómo apagarla: no pases `events`; `destroy()` borra el secreto
 *   (force-delete: cualquier webhook registrado deja de poder verificarse),
 *   la tabla, las tres Lambdas, la suscripción y el scheduler (desvincula
 *   antes `EventsOperatorPolicy`, o la pila acaba en `DELETE_FAILED`).
 * Ejemplo:
 *   const ev = new LifecycleEvents();
 *   await ev.deploy({ artifactBucket: "mi-bucket", logGroupName: "/rayito/rayito-base" });
 *   await ev.registerWebhook("https://hooks.example.com", { secretName: "mi-webhook", types: ["sandbox.lifecycle.killed"] });
 */
export class LifecycleEvents implements LifecycleEventsSectionSource {
  readonly #stackName: string;
  readonly #region: string | undefined;
  readonly #credentials: Credentials;
  readonly #stacks: OptionalStacks;
  readonly #dynamoClient: DynamoDbApi | undefined;
  readonly #getSecretValue: SecretValueGetter | undefined;
  #tableName: string | undefined;
  #stackKeySecretId: string | undefined;
  #stackKeyCache: Buffer | undefined;
  #lazyDynamoApi: LazyAwsApi<DynamoDbApi> | undefined;

  constructor(options: LifecycleEventsOptions = {}) {
    this.#stackName = options.stackName ?? DEFAULT_STACK_NAME;
    this.#region = options.region;
    this.#credentials = options.credentials;
    this.#stacks =
      options.stacks ??
      new OptionalStacks({ region: options.region, credentials: options.credentials });
    this.#dynamoClient = options.dynamoClient;
    this.#getSecretValue = options.getSecretValue;
    // Test-only: skips a real `deploy()`/`status()` round trip for tests
    // that only exercise the data-plane methods.
    this.#tableName = options.tableNameForTests;
    this.#stackKeySecretId = options.stackKeySecretIdForTests;
  }

  async deploy(options: DeployWebhooksOptions): Promise<StackStatus> {
    const status = await this.#stacks.deploy(COMPONENT, {
      stackName: this.#stackName,
      artifactBucket: options.artifactBucket,
      parameters: {
        LogGroupName: options.logGroupName,
        ReconcilerIntervalMinutes: String(
          options.reconcilerIntervalMinutes ?? DEFAULT_RECONCILER_INTERVAL_MINUTES,
        ),
      },
      tags: options.tags ?? {},
      ...(options.wait === undefined ? {} : { wait: options.wait }),
    });
    this.#tableName = status.outputs.EventsTableName;
    this.#stackKeySecretId = status.outputs.StackKeySecretArn;
    return status;
  }

  async status(): Promise<StackStatus | undefined> {
    return this.#stacks.status(COMPONENT, { stackName: this.#stackName });
  }

  /**
   * Borra la pila entera: el secreto del stack (force-delete), la tabla con
   * todos sus eventos y webhooks, las tres Lambdas, la suscripción, la cola
   * de fallos y el scheduler. No toca los secretos de cada webhook
   * (`rayito/webhooks/...`, de `SecretStore`) ni el log group de la imagen,
   * que esta pila nunca creó. Si `EventsOperatorPolicy` sigue vinculada a
   * algún usuario o rol, CloudFormation no puede borrarla y la pila termina
   * en `DELETE_FAILED` (`StackError`): desvincúlala y repite
   * (`AWS_API_NOTES.md` Q108).
   */
  async destroy(options: { wait?: boolean } = {}): Promise<void> {
    await this.#stacks.destroy(COMPONENT, {
      stackName: this.#stackName,
      ...(options.wait === undefined ? {} : { wait: options.wait }),
    });
  }

  async registerWebhook(
    url: string,
    options: { secretName: string; types: readonly string[] },
  ): Promise<WebhookInfo> {
    if (typeof url !== "string" || !isDeliverableWebhookUrl(url)) {
      throw new InvalidArgumentError(`registerWebhook: ${INVALID_WEBHOOK_URL}`);
    }
    const types = validateTypes(options.types);
    if (types === undefined || types.length === 0) {
      throw new InvalidArgumentError("registerWebhook: types no puede estar vacío");
    }
    const webhookId = randomBytes(8).toString("hex");
    const resolvedSecretId = resolveSecretId(options.secretName, WEBHOOK_SECRET_PREFIX);
    const api = await this.#dynamo();
    const tableName = await this.#resolveTableName();
    await awsCall("registerWebhook", () =>
      putWebhook(api, tableName, { webhookId, url, secretName: resolvedSecretId, types }),
    );
    return { webhookId, url, types };
  }

  async listWebhooks(): Promise<WebhookInfo[]> {
    const api = await this.#dynamo();
    const tableName = await this.#resolveTableName();
    return awsCall("listWebhooks", () => listWebhookItems(api, tableName));
  }

  async deleteWebhook(webhookId: string): Promise<void> {
    const api = await this.#dynamo();
    const tableName = await this.#resolveTableName();
    await awsCall("deleteWebhook", () => deleteWebhookItem(api, tableName, webhookId));
  }

  async getEvents(
    options: {
      sandboxId?: string;
      types?: readonly string[];
      limit?: number;
      order?: "asc" | "desc";
    } = {},
  ): Promise<EventRecord[]> {
    const limit = options.limit ?? DEFAULT_GET_EVENTS_LIMIT;
    if (!Number.isInteger(limit) || limit < MIN_GET_EVENTS_LIMIT || limit > MAX_GET_EVENTS_LIMIT) {
      throw new InvalidArgumentError(
        `getEvents: limit entre ${MIN_GET_EVENTS_LIMIT} y ${MAX_GET_EVENTS_LIMIT}`,
      );
    }
    const order = options.order ?? "desc";
    if (order !== "asc" && order !== "desc") {
      throw new InvalidArgumentError('getEvents: order debe ser "asc" o "desc"');
    }
    const types = validateTypes(options.types);
    const api = await this.#dynamo();
    const tableName = await this.#resolveTableName();
    return awsCall("getEvents", () =>
      queryEvents(api, tableName, { sandboxId: options.sandboxId, types, limit, order }),
    );
  }

  /** La sección de `ConfigureSandbox` de un sandbox ya lanzado:
   * `LifecycleEventsSectionFactory` la pide justo antes del único
   * `Configure` de `create()`. Lee la clave del stack (un `GetSecretValue`,
   * cacheado mientras viva esta instancia) y deriva `k_sbx`; la clave del
   * stack nunca sale de este objeto. */
  async buildSection(facts: LifecycleEventsSectionFacts): Promise<LifecycleEventsSection> {
    const stackKey = await this.#stackKey();
    return new LifecycleEventsSection(deriveSandboxKey(stackKey, facts.sandboxId), facts);
  }

  async #stackKey(): Promise<Buffer> {
    if (this.#stackKeyCache !== undefined) {
      return this.#stackKeyCache;
    }
    const secretId = await this.#resolveStackKeySecretId();
    const response = await awsCall("stackKey", () => this.#getSecret(secretId));
    // Mirrors the Python SDK's own `_stack_key` exactly: `SecretString`
    // first, `SecretBinary` otherwise — a secret rotated or recreated with
    // `--secret-binary` must work here too, not only one created as a
    // string.
    if (response.SecretString !== undefined) {
      this.#stackKeyCache = Buffer.from(response.SecretString, "utf8");
    } else if (response.SecretBinary !== undefined) {
      this.#stackKeyCache = Buffer.from(response.SecretBinary);
    } else {
      throw new WebhookError(
        "LifecycleEvents.stackKey: el secreto no tiene SecretString ni SecretBinary",
      );
    }
    return this.#stackKeyCache;
  }

  async #resolveStackKeySecretId(): Promise<string> {
    if (this.#stackKeySecretId !== undefined) {
      return this.#stackKeySecretId;
    }
    const status = await this.status();
    const secretId = status?.outputs.StackKeySecretArn;
    if (secretId === undefined) {
      throw new WebhookError(
        `la pila ${this.#stackName} no está desplegada (llama a deploy() primero)`,
      );
    }
    this.#stackKeySecretId = secretId;
    return secretId;
  }

  async #resolveTableName(): Promise<string> {
    if (this.#tableName !== undefined) {
      return this.#tableName;
    }
    const status = await this.status();
    const tableName = status?.outputs.EventsTableName;
    if (tableName === undefined) {
      throw new WebhookError(
        `la pila ${this.#stackName} no está desplegada (llama a deploy() primero)`,
      );
    }
    this.#tableName = tableName;
    return tableName;
  }

  async #dynamo(): Promise<DynamoDbApi> {
    if (this.#dynamoClient !== undefined) {
      return this.#dynamoClient;
    }
    // `LazyAwsApi` caches the client on *itself* (`#pending`), so it must
    // be built once per `LifecycleEvents` instance, not fresh on every
    // call — `lazyApi(...)` alone is just a constructor, not a cache.
    this.#lazyDynamoApi ??= lazyApi(this.#region, this.#credentials);
    return this.#lazyDynamoApi.get();
  }

  async #getSecret(
    secretId: string,
  ): Promise<{ SecretString?: string; SecretBinary?: Uint8Array }> {
    if (this.#getSecretValue !== undefined) {
      return this.#getSecretValue({ SecretId: secretId });
    }
    const { sdk, send } = await loadOptionalSdkClient<SecretsManagerModule>(
      "@aws-sdk/client-secrets-manager",
      "eventos de ciclo de vida (LifecycleEvents / events)",
      (module) => module.SecretsManagerClient,
      this.#region ?? process.env.AWS_REGION ?? "",
      this.#credentials,
    );
    return send<{ SecretString?: string; SecretBinary?: Uint8Array }>(
      new sdk.GetSecretValueCommand({ SecretId: secretId }),
    );
  }
}
