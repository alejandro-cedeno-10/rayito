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
import { loadOptionalSdkClient } from "../aws/optional-client.js";
import { InvalidArgumentError, WebhookError } from "../errors.js";
import { DEFAULT_SECRET_PREFIX, resolveSecretId } from "../secrets/names.js";
import type { StackComponent, StackStatus } from "../stacks/model.js";
import { componentByName } from "../stacks/registry.js";
import { OptionalStacks } from "../stacks/service.js";
import { DEFAULT_STACK_NAME, type EventRecord, type WebhookInfo } from "./domain.js";
import {
  type DynamoDbApi,
  deleteWebhook as deleteWebhookItem,
  lazyApi,
  listWebhooks as listWebhookItems,
  putWebhook,
  queryEvents,
} from "./dynamodb.js";
import { deriveSandboxKey } from "./keys.js";
import { type LifecycleEventsSection, lifecycleEventsSection } from "./section.js";

export const WEBHOOK_SECRET_PREFIX = `${DEFAULT_SECRET_PREFIX}webhooks/`;

const COMPONENT: StackComponent = componentByName("events-webhooks") as StackComponent;
const EVENT_TYPE_PATTERN = /^sandbox\.lifecycle\.(created|paused|resumed|killed)$/;
const MAX_GET_EVENTS_LIMIT = 100;

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
 * the optional `@aws-sdk/client-secrets-manager` peer. */
export type SecretValueGetter = (input: {
  readonly SecretId: string;
}) => Promise<{ readonly SecretString?: string }>;

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
 *   directamente a DynamoDB (`PutItem`/`Query`/`DeleteItem`/`Scan`), nunca a
 *   un Lambda.
 * Coste aproximado: ~$0,40/mes el secreto; DynamoDB y Lambda son
 *   on-demand/por invocación ($0 en reposo); ~$1,25 por millón de eventos
 *   escritos (WRU) más las lecturas de `getEvents`; el reconciliador
 *   factura una invocación cada `reconcilerIntervalMinutes` (5 por
 *   defecto, ~$0,0000002 c/u). us-east-1, consultado 2026-09-30.
 * IAM: `secretsmanager:GetSecretValue` sobre el secreto del stack
 *   (`EventsOperatorPolicy`, salida de la pila), en las credenciales del
 *   llamante.
 * Cómo apagarla: no pases `events`; `destroy()` borra el secreto
 *   (force-delete: cualquier webhook registrado deja de poder verificarse),
 *   la tabla, las tres Lambdas, la suscripción y el scheduler.
 * Ejemplo:
 *   const ev = new LifecycleEvents();
 *   await ev.deploy({ artifactBucket: "mi-bucket", logGroupName: "/rayito/rayito-base" });
 *   await ev.registerWebhook("https://hooks.example.com", { secretName: "mi-webhook", types: ["sandbox.lifecycle.killed"] });
 */
export class LifecycleEvents {
  readonly #stackName: string;
  readonly #region: string | undefined;
  readonly #credentials: Credentials;
  readonly #stacks: OptionalStacks;
  readonly #dynamoClient: DynamoDbApi | undefined;
  readonly #getSecretValue: SecretValueGetter | undefined;
  #tableName: string | undefined;
  #stackKeySecretId: string | undefined;
  #stackKeyCache: Buffer | undefined;

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
        ArtifactBucket: options.artifactBucket,
        LogGroupName: options.logGroupName,
        ReconcilerIntervalMinutes: String(options.reconcilerIntervalMinutes ?? 5),
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
    if (!url.startsWith("https://")) {
      throw new InvalidArgumentError("registerWebhook: url debe ser https://");
    }
    const types = validateTypes(options.types);
    if (types === undefined || types.length === 0) {
      throw new InvalidArgumentError("registerWebhook: types no puede estar vacío");
    }
    const webhookId = randomBytes(8).toString("hex");
    const resolvedSecretId = resolveSecretId(options.secretName, WEBHOOK_SECRET_PREFIX);
    const api = await this.#dynamo();
    await putWebhook(api, await this.#resolveTableName(), {
      webhookId,
      url,
      secretName: resolvedSecretId,
      types,
    });
    return { webhookId, url, types };
  }

  async listWebhooks(): Promise<WebhookInfo[]> {
    const api = await this.#dynamo();
    return listWebhookItems(api, await this.#resolveTableName());
  }

  async deleteWebhook(webhookId: string): Promise<void> {
    const api = await this.#dynamo();
    await deleteWebhookItem(api, await this.#resolveTableName(), webhookId);
  }

  async getEvents(
    options: {
      sandboxId?: string;
      types?: readonly string[];
      limit?: number;
      order?: "asc" | "desc";
    } = {},
  ): Promise<EventRecord[]> {
    const limit = options.limit ?? 100;
    if (limit > MAX_GET_EVENTS_LIMIT) {
      throw new InvalidArgumentError(`getEvents: limit <= ${MAX_GET_EVENTS_LIMIT}`);
    }
    const order = options.order ?? "desc";
    if (order !== "asc" && order !== "desc") {
      throw new InvalidArgumentError('getEvents: order debe ser "asc" o "desc"');
    }
    const types = validateTypes(options.types);
    const api = await this.#dynamo();
    return queryEvents(api, await this.#resolveTableName(), {
      sandboxId: options.sandboxId,
      types,
      limit,
      order,
    });
  }

  /** Ver `ADR-020`, "Hueco de integración conocido": no la llama todavía
   * ningún `create()`. */
  async buildSection(options: {
    sandboxId: string;
    imageArn: string;
    imageVersion: string;
  }): Promise<LifecycleEventsSection> {
    const stackKey = await this.#stackKey();
    const sandboxKey = deriveSandboxKey(stackKey, options.sandboxId);
    return lifecycleEventsSection({
      sandboxKey,
      sandboxId: options.sandboxId,
      imageArn: options.imageArn,
      imageVersion: options.imageVersion,
    });
  }

  async #stackKey(): Promise<Buffer> {
    if (this.#stackKeyCache !== undefined) {
      return this.#stackKeyCache;
    }
    const secretId = await this.#resolveStackKeySecretId();
    const response = await this.#getSecret(secretId);
    if (response.SecretString === undefined) {
      throw new WebhookError("el secreto de la clave del stack no tiene SecretString");
    }
    this.#stackKeyCache = Buffer.from(response.SecretString, "utf8");
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
    return lazyApi(this.#region, this.#credentials).get();
  }

  async #getSecret(secretId: string): Promise<{ SecretString?: string }> {
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
    return send<{ SecretString?: string }>(new sdk.GetSecretValueCommand({ SecretId: secretId }));
  }
}
