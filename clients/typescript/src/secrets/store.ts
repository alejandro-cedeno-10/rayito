/**
 * `SecretStore`: CRUD de secretos de Rayito sobre AWS Secrets Manager, en tu
 * cuenta (M13a). Espejo de `SecretStore` de `rayito/_secrets.py`. Sólo usa
 * las operaciones y los parámetros de AWS_API_NOTES.md §19.
 *
 * Coste y activación
 * -------------------
 * Activa: `new SecretStore({...})` es la opción explícita del CRUD; construirlo
 *   no llama a AWS ni carga `@aws-sdk/client-secrets-manager` (peer opcional):
 *   el cliente se crea en la primera llamada a un método.
 * Recursos y llamadas AWS: un secreto de Secrets Manager por `create`
 *   (`CreateSecretCommand`); `update` = `DescribeSecretCommand` +
 *   `PutSecretValueCommand` (+ `UpdateSecretCommand` con `metadata`);
 *   `getInfo`/`exists` = `DescribeSecretCommand`; `list` = `ListSecretsCommand`;
 *   `destroy` = `DeleteSecretCommand` sin ventana de recuperación.
 * Coste aproximado: $0,40 por secreto y mes hasta `destroy` + $0,05 por 10 000
 *   llamadas (us-east-1, consultado 2026-09-30,
 *   https://aws.amazon.com/secrets-manager/pricing/).
 * IAM: `secretsmanager:CreateSecret`, `PutSecretValue`, `UpdateSecret`,
 *   `DescribeSecret`, `DeleteSecret` sobre `…:secret:rayito/*` y
 *   `secretsmanager:ListSecrets` sobre `*` (política `RayitoSecretsAdmin` de
 *   `infra/secrets-access.yaml`); con `kmsKeyId`, `kms:GenerateDataKey` y
 *   `kms:Decrypt` sobre esa clave.
 * Cómo apagarla: no instancies `SecretStore`; `destroy()` los secretos que ya
 *   no uses (se facturan hasta entonces).
 * Ejemplo:
 *   const store = new SecretStore({ region: "us-east-1" });
 *   await store.create("openai", process.env.OPENAI_API_KEY!, { metadata: { team: "ml" } });
 *   await store.update("openai", "sk-nuevo");
 *   const page = await store.list({ limit: 20 });
 *   await store.destroy("openai");
 */

import type { AwsClientSettings } from "../aws/control-plane.js";
import { awsCode, LazyAwsApi, loadOptionalSdkClient } from "../aws/optional-client.js";
import { sanitizeAwsError } from "../aws/sanitize.js";
import {
  InvalidArgumentError,
  RateLimitError,
  SecretError,
  SecretNotFoundError,
} from "../errors.js";
import {
  currentVersion,
  DEFAULT_SECRET_PREFIX,
  decodeMetadata,
  displayName,
  encodeMetadata,
  LIST_MAX_RESULTS,
  latestVersion,
  resolveSecretId,
  type SecretRef,
  validatePrefix,
  validateValue,
  versionToken,
} from "./names.js";

const SECRETS_MANAGER_PEER = "@aws-sdk/client-secrets-manager";
const UPDATE_WARNING_INTERVAL_MS = 600_000;
const CREATE_RETRY_BUDGET_MS = 30_000;
const CREATE_RETRY_FIRST_DELAY_MS = 500;
const CREATE_RETRY_MAX_DELAY_MS = 8_000;
const NOT_FOUND_MESSAGE = "el secreto no existe o está programado para borrarse";
export const UPDATE_FREQUENCY_WARNING =
  "SecretStore.update se llamó más de una vez en 600 s para el mismo secreto: Secrets Manager " +
  "recomienda no escribir más de una vez cada 10 minutos de forma sostenida y conserva como " +
  "mucho 100 versiones más las de las últimas 24 h (LimitExceededException)";

/** Metadatos de un secreto (los campos de `SecretInfo` de E2B JS). El valor nunca vuelve. */
export interface SecretInfo {
  /** El ARN de Secrets Manager (E2B usa `sec_…`). */
  readonly secretId: string;
  readonly name: string;
  /** 1 al crear, +1 en cada `update`; 0 si la versión actual no la escribió Rayito. */
  readonly version: number;
  readonly metadata: Record<string, string>;
  readonly createdAt: Date;
  readonly updatedAt: Date;
}

/** Una página de `SecretStore.list()`: `nextToken` es `undefined` en la última. */
export interface SecretPage {
  readonly items: SecretInfo[];
  readonly nextToken: string | undefined;
}

type StagesMap = Readonly<Record<string, readonly string[] | undefined>>;

export interface DescribedSecret {
  readonly ARN?: string | undefined;
  readonly Name?: string | undefined;
  readonly Description?: string | undefined;
  readonly CreatedDate?: Date | undefined;
  readonly LastChangedDate?: Date | undefined;
  readonly DeletedDate?: Date | undefined;
  readonly VersionIdsToStages?: StagesMap | undefined;
  readonly SecretVersionsToStages?: StagesMap | undefined;
}

/**
 * Lo que Rayito usa de Secrets Manager: la forma del cliente agregado
 * `SecretsManager` del SDK v3 (`createSecret(input)`, …), y sólo las
 * operaciones de AWS_API_NOTES.md §19. El adaptador por defecto carga el peer
 * opcional y manda los `…Command`.
 */
export interface SecretsManagerApi {
  createSecret(input: {
    Name: string;
    SecretString: string;
    Description: string;
    ClientRequestToken: string;
    KmsKeyId?: string;
  }): Promise<{ ARN?: string | undefined; Name?: string | undefined }>;
  putSecretValue(input: {
    SecretId: string;
    SecretString: string;
    ClientRequestToken: string;
  }): Promise<unknown>;
  getSecretValue(input: {
    SecretId: string;
    VersionId?: string;
    VersionStage?: string;
  }): Promise<{ SecretString?: string | undefined }>;
  describeSecret(input: { SecretId: string }): Promise<DescribedSecret>;
  updateSecret(input: { SecretId: string; Description: string }): Promise<unknown>;
  listSecrets(input: {
    IncludePlannedDeletion: boolean;
    Filters?: Array<{ Key: "name"; Values: string[] }>;
    MaxResults?: number;
    NextToken?: string;
  }): Promise<{ SecretList?: DescribedSecret[] | undefined; NextToken?: string | undefined }>;
  deleteSecret(input: { SecretId: string; ForceDeleteWithoutRecovery: boolean }): Promise<unknown>;
}

type Credentials = AwsClientSettings["credentials"];

export interface SecretStoreOptions {
  /** Por defecto `AWS_REGION`/`AWS_DEFAULT_REGION`. */
  readonly region?: string | undefined;
  /** Credenciales o proveedor del SDK v3; por defecto la cadena por defecto. */
  readonly credentials?: Credentials | undefined;
  /** `rayito/` por defecto; `''` para ninguno. */
  readonly prefix?: string | undefined;
  /** Una CMK de KMS (id, ARN o alias) para `CreateSecret`. */
  readonly kmsKeyId?: string | undefined;
  /** Un cliente propio con la forma de `SecretsManager` (el agregado del SDK v3), p. ej. en tests. */
  readonly client?: SecretsManagerApi | undefined;
  /** Reloj en ms y espera: sólo para tests. */
  readonly now?: (() => number) | undefined;
  readonly sleep?: ((ms: number) => Promise<void>) | undefined;
}

interface SecretsManagerModule {
  readonly SecretsManagerClient: new (
    config: object,
  ) => { send(command: unknown): Promise<unknown> };
  readonly CreateSecretCommand: new (input: object) => unknown;
  readonly PutSecretValueCommand: new (input: object) => unknown;
  readonly GetSecretValueCommand: new (input: object) => unknown;
  readonly DescribeSecretCommand: new (input: object) => unknown;
  readonly UpdateSecretCommand: new (input: object) => unknown;
  readonly ListSecretsCommand: new (input: object) => unknown;
  readonly DeleteSecretCommand: new (input: object) => unknown;
}

/** El adaptador real: carga el peer opcional en el primer uso y manda cada `…Command`. */
async function sdkApi(region: string, credentials: Credentials): Promise<SecretsManagerApi> {
  const { sdk, send } = await loadOptionalSdkClient<SecretsManagerModule>(
    SECRETS_MANAGER_PEER,
    "Secrets Manager (SecretStore / secrets)",
    (module) => module.SecretsManagerClient,
    region,
    credentials,
  );
  return {
    createSecret: (input) => send(new sdk.CreateSecretCommand(input)),
    putSecretValue: (input) => send(new sdk.PutSecretValueCommand(input)),
    getSecretValue: (input) => send(new sdk.GetSecretValueCommand(input)),
    describeSecret: (input) => send(new sdk.DescribeSecretCommand(input)),
    updateSecret: (input) => send(new sdk.UpdateSecretCommand(input)),
    listSecrets: (input) => send(new sdk.ListSecretsCommand(input)),
    deleteSecret: (input) => send(new sdk.DeleteSecretCommand(input)),
  };
}

const IAM_ACTIONS: Readonly<Record<keyof SecretsManagerApi, string>> = Object.freeze({
  createSecret: "secretsmanager:CreateSecret",
  putSecretValue: "secretsmanager:PutSecretValue",
  getSecretValue: "secretsmanager:GetSecretValue",
  describeSecret: "secretsmanager:DescribeSecret",
  updateSecret: "secretsmanager:UpdateSecret",
  listSecrets: "secretsmanager:ListSecrets",
  deleteSecret: "secretsmanager:DeleteSecret",
});

/** El error del SDK de AWS como error propio, sin el mensaje de AWS (puede nombrar el secreto). */
export function translateError(operation: keyof SecretsManagerApi, error: unknown): Error {
  const code = awsCode(error);
  const cause = sanitizeAwsError(error, { includeMessage: false });
  const options = { awsCode: code, cause };
  switch (code) {
    case "ResourceNotFoundException":
      return new SecretNotFoundError(NOT_FOUND_MESSAGE, options);
    case "ThrottlingException":
      return new RateLimitError(
        `Secrets Manager limitó la tasa de ${IAM_ACTIONS[operation]}`,
        options,
      );
    case "AccessDeniedException":
      return new SecretError(
        `sin permiso IAM ${IAM_ACTIONS[operation]} sobre el secreto (credenciales del llamante; ` +
          "ver infra/secrets-access.yaml)",
        options,
      );
    case "ResourceExistsException":
      return new SecretError(
        operation === "createSecret"
          ? "ya existe un secreto con ese nombre"
          : "esa versión ya existe con otro valor: si fue un update concurrente, vuelve a llamar a update; si se repite, rotaciones externas dejaron versiones de Rayito sin etiqueta y conviene crear un secreto nuevo",
        options,
      );
    case "LimitExceededException":
      return new SecretError(
        "Secrets Manager rechazó la escritura por límite de versiones: escribe como mucho una vez " +
          "cada 10 minutos",
        options,
      );
    default:
      return new SecretError(
        `Secrets Manager falló en ${IAM_ACTIONS[operation]} (${code ?? "error"})`,
        options,
      );
  }
}

function isScheduledForDeletion(error: unknown): boolean {
  if (awsCode(error) !== "InvalidRequestException") {
    return false;
  }
  const message = (error as { message?: unknown } | null)?.message;
  return typeof message === "string" && message.toLowerCase().includes("delet");
}

function asDate(value: Date | undefined, fallback: Date): Date {
  return value instanceof Date ? value : fallback;
}

export function infoFromDescription(described: DescribedSecret, prefix: string): SecretInfo {
  const created = asDate(described.CreatedDate, new Date());
  return Object.freeze({
    secretId: described.ARN ?? "",
    name: displayName(described.Name ?? "", prefix),
    version: currentVersion(described.VersionIdsToStages ?? described.SecretVersionsToStages),
    metadata: decodeMetadata(described.Description),
    createdAt: created,
    updatedAt: asDate(described.LastChangedDate, created),
  });
}

const lastUpdates = new Map<string, number>();
const warnedUpdates = new Set<string>();

/** Ver el bloque "Coste y activación" del módulo. Reutilizable; los errores nunca repiten el nombre ni el valor. */
export class SecretStore {
  readonly #region: string | undefined;
  readonly #credentials: Credentials;
  readonly #prefix: string;
  readonly #kmsKeyId: string | undefined;
  readonly #now: () => number;
  readonly #sleep: (ms: number) => Promise<void>;
  readonly #api: LazyAwsApi<SecretsManagerApi>;

  constructor(options: SecretStoreOptions = {}) {
    this.#region = options.region;
    this.#credentials = options.credentials;
    this.#prefix = validatePrefix(options.prefix ?? DEFAULT_SECRET_PREFIX);
    if (
      options.kmsKeyId !== undefined &&
      (typeof options.kmsKeyId !== "string" || options.kmsKeyId.length === 0)
    ) {
      throw new InvalidArgumentError("kmsKeyId debe ser un id, ARN o alias de KMS");
    }
    this.#kmsKeyId = options.kmsKeyId;
    this.#now = options.now ?? (() => performance.now());
    this.#sleep = options.sleep ?? ((ms) => new Promise((resolve) => setTimeout(resolve, ms)));
    const credentials = options.credentials;
    this.#api = new LazyAwsApi(
      options.region,
      "falta la región: pasa `region` o define AWS_REGION",
      (region) => sdkApi(region, credentials),
      options.client,
    );
  }

  get prefix(): string {
    return this.#prefix;
  }

  /** La región pedida; `undefined` = `AWS_REGION` al construir el cliente. */
  get region(): string | undefined {
    return this.#region;
  }

  /** La parte (región, credenciales) de la clave de `SecretCache`. */
  get cacheIdentity(): readonly [string | undefined, unknown] {
    return [this.#region, this.#credentials];
  }

  toJSON(): Record<string, unknown> {
    return { region: this.#region, prefix: this.#prefix };
  }

  /** `CreateSecret` con la versión 1; si el nombre aún se está borrando, reintenta hasta 30 s. */
  async create(
    name: string,
    value: string,
    options: { readonly metadata?: Readonly<Record<string, string>> | undefined } = {},
  ): Promise<SecretInfo> {
    if (typeof name === "string" && name.startsWith("arn:")) {
      throw new InvalidArgumentError("create necesita un nombre, no un ARN");
    }
    const secretId = resolveSecretId(name, this.#prefix);
    const input = {
      Name: secretId,
      SecretString: validateValue(value),
      Description: encodeMetadata(options.metadata),
      ClientRequestToken: versionToken(1),
      ...(this.#kmsKeyId === undefined ? {} : { KmsKeyId: this.#kmsKeyId }),
    };
    const response = await this.#createWithRetry(input);
    const now = new Date();
    return Object.freeze({
      secretId: response.ARN ?? "",
      name: displayName(response.Name ?? secretId, this.#prefix),
      version: 1,
      metadata: { ...(options.metadata ?? {}) },
      createdAt: now,
      updatedAt: now,
    });
  }

  /** Versión `n + 1` (`DescribeSecret` + `PutSecretValue`); `metadata` la sustituye (`UpdateSecret`). */
  async update(
    name: string,
    value: string,
    options: { readonly metadata?: Readonly<Record<string, string>> | undefined } = {},
  ): Promise<SecretInfo> {
    const secretId = resolveSecretId(name, this.#prefix);
    const secretValue = validateValue(value);
    const description =
      options.metadata === undefined ? undefined : encodeMetadata(options.metadata);
    const described = await this.#describe(secretId);
    const version = latestVersion(described.VersionIdsToStages) + 1;
    this.#warnIfFrequent(secretId);
    await this.#call("putSecretValue", (api) =>
      api.putSecretValue({
        SecretId: secretId,
        SecretString: secretValue,
        ClientRequestToken: versionToken(version),
      }),
    );
    if (description !== undefined) {
      await this.#call("updateSecret", (api) =>
        api.updateSecret({ SecretId: secretId, Description: description }),
      );
    }
    const created = asDate(described.CreatedDate, new Date());
    return Object.freeze({
      secretId: described.ARN ?? "",
      name: displayName(described.Name ?? secretId, this.#prefix),
      version,
      metadata:
        options.metadata === undefined
          ? decodeMetadata(described.Description)
          : { ...options.metadata },
      createdAt: created,
      updatedAt: new Date(),
    });
  }

  /** `DescribeSecret`; uno programado para borrarse es `SecretNotFoundError`. */
  async getInfo(name: string): Promise<SecretInfo> {
    return infoFromDescription(
      await this.#describe(resolveSecretId(name, this.#prefix)),
      this.#prefix,
    );
  }

  async exists(name: string): Promise<boolean> {
    try {
      await this.getInfo(name);
      return true;
    } catch (error) {
      if (error instanceof SecretNotFoundError) {
        return false;
      }
      throw error;
    }
  }

  /** Una página de `ListSecrets` filtrada por el prefijo (sin los programados para borrarse). */
  async list(
    options: { readonly limit?: number | undefined; readonly nextToken?: string | undefined } = {},
  ): Promise<SecretPage> {
    const { limit, nextToken } = options;
    if (
      limit !== undefined &&
      (!Number.isInteger(limit) || limit < 1 || limit > LIST_MAX_RESULTS)
    ) {
      throw new InvalidArgumentError(`limit debe estar en 1..${LIST_MAX_RESULTS}`);
    }
    const input = {
      IncludePlannedDeletion: false,
      ...(this.#prefix.length > 0
        ? { Filters: [{ Key: "name" as const, Values: [this.#prefix] }] }
        : {}),
      ...(limit === undefined ? {} : { MaxResults: limit }),
      ...(nextToken ? { NextToken: nextToken } : {}),
    };
    const response = await this.#call("listSecrets", (api) => api.listSecrets(input));
    const items = (response.SecretList ?? [])
      .filter((entry) => (entry.Name ?? "").startsWith(this.#prefix))
      .map((entry) => infoFromDescription(entry, this.#prefix));
    return Object.freeze({ items, nextToken: response.NextToken || undefined });
  }

  /** `DeleteSecret(ForceDeleteWithoutRecovery)`; `false` si no existía. */
  async destroy(name: string): Promise<boolean> {
    const secretId = resolveSecretId(name, this.#prefix);
    try {
      await this.#call("deleteSecret", (api) =>
        api.deleteSecret({ SecretId: secretId, ForceDeleteWithoutRecovery: true }),
      );
      return true;
    } catch (error) {
      if (error instanceof SecretNotFoundError) {
        return false;
      }
      throw error;
    }
  }

  /**
   * `GetSecretValue` de una referencia; lo usa `SecretCache` (y sólo ella).
   * @internal
   */
  async readValue(ref: SecretRef): Promise<string> {
    const input = {
      SecretId: resolveSecretId(ref.name, this.#prefix),
      ...(ref.versionId === undefined ? {} : { VersionId: ref.versionId }),
      ...(ref.versionId === undefined && ref.versionStage !== undefined
        ? { VersionStage: ref.versionStage }
        : {}),
    };
    const response = await this.#call("getSecretValue", (api) => api.getSecretValue(input));
    if (typeof response.SecretString !== "string") {
      throw new SecretError("el secreto no tiene SecretString (Rayito sólo lee texto)");
    }
    return response.SecretString;
  }

  #client(): Promise<SecretsManagerApi> {
    return this.#api.get();
  }

  async #call<T>(
    operation: keyof SecretsManagerApi,
    run: (api: SecretsManagerApi) => Promise<T>,
  ): Promise<T> {
    const api = await this.#client();
    try {
      return await run(api);
    } catch (error) {
      throw translateError(operation, error);
    }
  }

  async #describe(secretId: string): Promise<DescribedSecret> {
    const described = await this.#call("describeSecret", (api) =>
      api.describeSecret({ SecretId: secretId }),
    );
    if (described.DeletedDate !== undefined && described.DeletedDate !== null) {
      throw new SecretNotFoundError(NOT_FOUND_MESSAGE, { awsCode: "ResourceNotFoundException" });
    }
    return described;
  }

  async #createWithRetry(
    input: Parameters<SecretsManagerApi["createSecret"]>[0],
  ): Promise<{ ARN?: string | undefined; Name?: string | undefined }> {
    const api = await this.#client();
    const deadline = this.#now() + CREATE_RETRY_BUDGET_MS;
    let delay = CREATE_RETRY_FIRST_DELAY_MS;
    for (;;) {
      try {
        return await api.createSecret(input);
      } catch (error) {
        if (!isScheduledForDeletion(error)) {
          throw translateError("createSecret", error);
        }
        if (this.#now() + delay > deadline) {
          throw new SecretError(
            "el nombre sigue programado para borrarse tras 30 s de reintentos (DeleteSecret es " +
              "asíncrono): vuelve a intentarlo más tarde",
            {
              awsCode: "InvalidRequestException",
              cause: sanitizeAwsError(error, { includeMessage: false }),
            },
          );
        }
      }
      await this.#sleep(delay);
      delay = Math.min(delay * 2, CREATE_RETRY_MAX_DELAY_MS);
    }
  }

  #warnIfFrequent(secretId: string): void {
    const key = `${this.#region ?? ""}|${secretId}`;
    const now = this.#now();
    const previous = lastUpdates.get(key);
    lastUpdates.set(key, now);
    if (
      previous !== undefined &&
      now - previous < UPDATE_WARNING_INTERVAL_MS &&
      !warnedUpdates.has(key)
    ) {
      warnedUpdates.add(key);
      process.emitWarning(UPDATE_FREQUENCY_WARNING, { type: "RayitoSecretUpdateWarning" });
    }
  }
}
