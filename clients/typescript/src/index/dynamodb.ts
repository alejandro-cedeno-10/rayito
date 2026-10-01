/**
 * `DynamoDbIndex`: índice opcional de metadatos de sandboxes sobre una tabla
 * DynamoDB de tu cuenta (M14). Espejo de `DynamoDbIndex` de
 * `rayito/_index.py`. Sólo usa `PutItem` y `BatchGetItem` con los parámetros
 * de AWS_API_NOTES.md §20.
 *
 * Sin índice, `Sandbox.list({ metadata })` lee los metadatos del agente con
 * una sonda de `Health` por sandbox y sólo mira sandboxes `RUNNING` (sondear
 * uno suspendido lo despertaría). Con `index: new DynamoDbIndex({...})`,
 * `create()` escribe una fila inmutable por sandbox y `list()`/`paginate()`
 * la unen con `list-microvms`: filtran también `SUSPENDED` sin sondear nada.
 *
 * Coste y activación
 * -------------------
 * Activa: `index: new DynamoDbIndex({ tableName: "rayito-sandboxes" })` (en
 *   Python, `index=DynamoDbIndex("rayito-sandboxes")`) en
 *   `Sandbox.create()`, `Sandbox.list()`, `Sandbox.paginate()`, `PoolConfig`
 *   y el shim `rayito/e2b` (`Sandbox.list({ query, index })`, `new E2B({ index })`).
 *   Construirlo no llama a AWS ni carga `@aws-sdk/client-dynamodb` (peer
 *   opcional): el cliente se crea en su primer uso. Sin la opción no se carga
 *   el peer ni se hace ninguna llamada a DynamoDB (el camino de 0.4.0).
 * Recursos y llamadas AWS: ninguno se crea desde el SDK (la tabla la
 *   despliegas tú con `infra/metadata-index.yaml`). `PutItemCommand` una vez
 *   por sandbox creado (condicional `attribute_not_exists(pk)`);
 *   `BatchGetItemCommand` una vez por página de `list-microvms` (≤ 100
 *   claves, eventualmente consistente) al listar con `metadata` e `index`.
 *   Nunca `DeleteItem`: el TTL (`expires_at`) borra las filas gratis.
 * Coste aproximado: DynamoDB on-demand (us-east-1, consultado 2026-09-30,
 *   https://aws.amazon.com/dynamodb/pricing/on-demand/): $0,625 por millón
 *   de WRU (~1 WRU por `create` ≈ $0,000000625) + $0,125 por millón de RRU
 *   (0,5 RRU por ítem leído) + $0,25/GB-mes. 10 000 sandboxes/mes < $0,10.
 *   Tabla vacía: $0.
 * IAM: escritor (`create`, relleno del pool): `dynamodb:PutItem`; lector
 *   (`list`/`paginate`): `dynamodb:BatchGetItem`; ambos sobre el ARN de la
 *   tabla (políticas `RayitoIndexWriter`/`RayitoIndexReader`), en las
 *   credenciales del LLAMANTE.
 * Cómo apagarla: no pases `index` (o pásalo `undefined`, el valor por
 *   defecto); para dejar de pagar el almacenamiento, borra el stack.
 * Ejemplo:
 *   const index = new DynamoDbIndex({ tableName: "rayito-sandboxes" });
 *   const sbx = await Sandbox.create({ metadata: { user: "42" }, index });
 *   await sbx.pause();
 *   for await (const item of Sandbox.list({ metadata: { user: "42" }, states: ["SUSPENDED"], index })) {
 *     console.log(item.sandboxId, item.state, item.metadata);
 *   }
 */

import type { AwsClientSettings } from "../aws/control-plane.js";
import { awsCode, LazyAwsApi, loadOptionalSdkClient } from "../aws/optional-client.js";
import { sanitizeAwsError } from "../aws/sanitize.js";
import { IndexWriteError, InvalidArgumentError, SandboxIndexError } from "../errors.js";
import type { SandboxInfo } from "../models.js";
import {
  DEFAULT_TTL_MARGIN_SECONDS,
  fromItem,
  type IndexItem,
  type IndexRecord,
  recordFor,
  toItem,
} from "./record.js";

const DYNAMODB_PEER = "@aws-sdk/client-dynamodb";
export const BATCH_GET_MAX_KEYS = 100;
export const PUT_CONDITION = "attribute_not_exists(pk)";
export const UNPROCESSED_RETRY_ATTEMPTS = 5;
const UNPROCESSED_FIRST_DELAY_MS = 50;
const UNPROCESSED_MAX_DELAY_MS = 1000;
const TABLE_NAME_PATTERN = /^[A-Za-z0-9_.-]{3,255}$/;

export type WriteFailurePolicy = "terminate" | "warn";

type KeysAndAttributes = {
  Keys: Array<{ pk: { S: string } }>;
  ConsistentRead: boolean;
};

/**
 * Lo que Rayito usa de DynamoDB: la forma del cliente agregado `DynamoDB` del
 * SDK v3 (`putItem(input)`, `batchGetItem(input)`), y sólo las operaciones de
 * AWS_API_NOTES.md §20. El adaptador por defecto carga el peer opcional y
 * manda los `…Command`.
 */
export interface DynamoDbApi {
  putItem(input: {
    TableName: string;
    Item: IndexItem;
    ConditionExpression: string;
  }): Promise<unknown>;
  batchGetItem(input: { RequestItems: Record<string, KeysAndAttributes> }): Promise<{
    Responses?: Record<string, Array<Record<string, unknown>>> | undefined;
    UnprocessedKeys?: Record<string, Partial<KeysAndAttributes>> | undefined;
  }>;
}

type Credentials = AwsClientSettings["credentials"];

export interface DynamoDbIndexOptions {
  /** La tabla desplegada con `infra/metadata-index.yaml` (por defecto allí, `rayito-sandboxes`). */
  readonly tableName: string;
  /** La región de la tabla; por defecto `AWS_REGION`/`AWS_DEFAULT_REGION`. */
  readonly region?: string | undefined;
  /** Credenciales o proveedor del SDK v3; por defecto la cadena por defecto. */
  readonly credentials?: Credentials | undefined;
  /**
   * Qué hace `create()` si `PutItem` falla: `"terminate"` (por defecto)
   * termina el MicroVM (salvo `keepOnFailure`) y lanza `IndexWriteError`;
   * `"warn"` avisa en el `logger` y devuelve el sandbox, que no aparecerá en
   * los listados con índice.
   */
  readonly onWriteFailure?: WriteFailurePolicy | undefined;
  /** Margen sobre la vida máxima del sandbox antes de que la fila caduque (3600 s). */
  readonly ttlMarginSeconds?: number | undefined;
  /** Un cliente propio con la forma de `DynamoDB` (el agregado del SDK v3), p. ej. en tests. */
  readonly client?: DynamoDbApi | undefined;
  /** Reloj en ms epoch y espera: sólo para tests. */
  readonly now?: (() => number) | undefined;
  readonly sleep?: ((ms: number) => Promise<void>) | undefined;
}

interface DynamoDbModule {
  readonly DynamoDBClient: new (config: object) => { send(command: unknown): Promise<unknown> };
  readonly PutItemCommand: new (input: object) => unknown;
  readonly BatchGetItemCommand: new (input: object) => unknown;
}

/** El adaptador real: carga el peer opcional en el primer uso y manda cada `…Command`. */
async function sdkApi(region: string, credentials: Credentials): Promise<DynamoDbApi> {
  const { sdk, send } = await loadOptionalSdkClient<DynamoDbModule>(
    DYNAMODB_PEER,
    "el índice de metadatos (DynamoDbIndex / index)",
    (module) => module.DynamoDBClient,
    region,
    credentials,
  );
  return {
    putItem: (input) => send(new sdk.PutItemCommand(input)),
    batchGetItem: (input) => send(new sdk.BatchGetItemCommand(input)),
  };
}

const IAM_ACTIONS: Readonly<Record<keyof DynamoDbApi, string>> = Object.freeze({
  putItem: "dynamodb:PutItem",
  batchGetItem: "dynamodb:BatchGetItem",
});

/** El error de DynamoDB como error propio, sin el mensaje de AWS (puede nombrar la tabla o la clave). */
export function indexError(
  operation: keyof DynamoDbApi,
  error: unknown,
  ErrorType: typeof SandboxIndexError,
): SandboxIndexError {
  const code = awsCode(error);
  const action = IAM_ACTIONS[operation];
  let message: string;
  switch (code) {
    case "ConditionalCheckFailedException":
      message = "ya había una fila con ese sandbox_id en el índice; no se sobrescribe";
      break;
    case "ResourceNotFoundException":
      message = "la tabla del índice no existe en esa región (despliega infra/metadata-index.yaml)";
      break;
    case "AccessDeniedException":
      message = `sin permiso IAM ${action} sobre la tabla del índice (credenciales del llamante; ver infra/metadata-index.yaml)`;
      break;
    case "ProvisionedThroughputExceededException":
    case "ThrottlingException":
      message = `DynamoDB limitó la tasa de ${action} tras los reintentos del SDK`;
      break;
    default:
      message = `DynamoDB falló en ${action} (${code ?? "Error"})`;
  }
  return new ErrorType(message, {
    awsCode: code,
    cause: sanitizeAwsError(error, { includeMessage: false }),
  });
}

/** Ids únicos en trozos de como mucho 100 (el máximo de `BatchGetItem`). */
export function chunks(ids: readonly string[], size = BATCH_GET_MAX_KEYS): string[][] {
  const unique = [...new Set(ids)];
  const parts: string[][] = [];
  for (let start = 0; start < unique.length; start += size) {
    parts.push(unique.slice(start, start + size));
  }
  return parts;
}

/** Ver el bloque "Coste y activación" del módulo. Reutilizable; los errores nunca repiten el mensaje de AWS. */
export class DynamoDbIndex {
  readonly tableName: string;
  readonly onWriteFailure: WriteFailurePolicy;
  readonly ttlMarginSeconds: number;
  readonly #now: () => number;
  readonly #sleep: (ms: number) => Promise<void>;
  readonly #api: LazyAwsApi<DynamoDbApi>;

  constructor(options: DynamoDbIndexOptions) {
    const tableName = (options as { tableName?: unknown } | undefined)?.tableName;
    if (typeof tableName !== "string" || !TABLE_NAME_PATTERN.test(tableName)) {
      throw new InvalidArgumentError(
        "tableName debe ser un nombre de tabla DynamoDB (3-255 caracteres [A-Za-z0-9_.-])",
      );
    }
    const onWriteFailure = options.onWriteFailure ?? "terminate";
    if (onWriteFailure !== "terminate" && onWriteFailure !== "warn") {
      throw new InvalidArgumentError(
        `onWriteFailure debe ser "terminate" o "warn", recibido ${JSON.stringify(onWriteFailure)}`,
      );
    }
    const margin = options.ttlMarginSeconds ?? DEFAULT_TTL_MARGIN_SECONDS;
    if (typeof margin !== "number" || !Number.isSafeInteger(margin) || margin < 0) {
      throw new InvalidArgumentError("ttlMarginSeconds debe ser un entero >= 0");
    }
    this.tableName = tableName;
    this.onWriteFailure = onWriteFailure;
    this.ttlMarginSeconds = margin;
    this.#now = options.now ?? (() => Date.now());
    this.#sleep = options.sleep ?? ((ms) => new Promise((resolve) => setTimeout(resolve, ms)));
    const credentials = options.credentials;
    this.#api = new LazyAwsApi(
      options.region,
      "falta la región del índice: pasa `region` o define AWS_REGION",
      (region) => sdkApi(region, credentials),
      options.client,
    );
  }

  toJSON(): Record<string, unknown> {
    return { tableName: this.tableName, onWriteFailure: this.onWriteFailure };
  }

  /** Segundos epoch con los que se descartan filas caducadas. */
  nowSeconds(): number {
    return this.#now() / 1000;
  }

  /** La fila de un sandbox recién lanzado con el margen de este índice. */
  record(info: SandboxInfo, metadata: Readonly<Record<string, string>> | undefined): IndexRecord {
    return recordFor(info, metadata, this.ttlMarginSeconds);
  }

  /** `PutItem` condicional: nunca sobrescribe una fila existente. */
  async put(record: IndexRecord): Promise<void> {
    const api = await this.#client();
    try {
      await api.putItem({
        TableName: this.tableName,
        Item: toItem(record),
        ConditionExpression: PUT_CONDITION,
      });
    } catch (error) {
      throw indexError("putItem", error, IndexWriteError);
    }
  }

  /**
   * Las filas vigentes de `sandboxIds`, en trozos de 100 con lectura
   * eventualmente consistente; reintenta `UnprocessedKeys` con un backoff
   * acotado y, si siguen sin procesar, lanza (nunca una lista incompleta en
   * silencio). Las filas caducadas o con otra forma se descartan.
   */
  async batchGet(sandboxIds: readonly string[]): Promise<Map<string, IndexRecord>> {
    const found = new Map<string, IndexRecord>();
    const now = this.nowSeconds();
    for (const chunk of chunks(sandboxIds)) {
      for (const item of await this.#batchGetChunk(chunk)) {
        const record = fromItem(item);
        if (record !== undefined && record.expiresAt >= now) {
          found.set(record.sandboxId, record);
        }
      }
    }
    return found;
  }

  async #batchGetChunk(ids: string[]): Promise<Array<Record<string, unknown>>> {
    const api = await this.#client();
    let request: KeysAndAttributes = {
      Keys: ids.map((id) => ({ pk: { S: id } })),
      ConsistentRead: false,
    };
    const items: Array<Record<string, unknown>> = [];
    let delay = UNPROCESSED_FIRST_DELAY_MS;
    for (let attempt = 0; attempt <= UNPROCESSED_RETRY_ATTEMPTS; attempt += 1) {
      if (attempt > 0) {
        await this.#sleep(delay);
        delay = Math.min(delay * 2, UNPROCESSED_MAX_DELAY_MS);
      }
      let response: Awaited<ReturnType<DynamoDbApi["batchGetItem"]>>;
      try {
        response = await api.batchGetItem({ RequestItems: { [this.tableName]: request } });
      } catch (error) {
        throw indexError("batchGetItem", error, SandboxIndexError);
      }
      items.push(...(response.Responses?.[this.tableName] ?? []));
      const pending = response.UnprocessedKeys?.[this.tableName];
      if (pending?.Keys === undefined || pending.Keys.length === 0) {
        return items;
      }
      request = { Keys: pending.Keys, ConsistentRead: false };
    }
    throw new SandboxIndexError(
      `DynamoDB dejó claves sin procesar en BatchGetItem tras ${UNPROCESSED_RETRY_ATTEMPTS} reintentos: vuelve a listar más tarde`,
      { awsCode: "UnprocessedKeys" },
    );
  }

  #client(): Promise<DynamoDbApi> {
    return this.#api.get();
  }
}

/** `index` debe ser un `DynamoDbIndex` o `undefined`. */
export function validateIndex(index: unknown): DynamoDbIndex | undefined {
  if (index === undefined || index instanceof DynamoDbIndex) {
    return index;
  }
  throw new InvalidArgumentError("index debe ser un DynamoDbIndex o undefined");
}
