/**
 * Puerto `KeyValueStoreWriter` y su adaptador del SDK v3
 * (`CloudFrontKvsWriter`), para `m15-custom-domain` (ADR-024). Espejo de
 * `rayito._custom_domain._kvs`.
 *
 * Verificado contra el modelo `cloudfront-keyvaluestore` (botocore
 * 1.43.103, `AWS_API_NOTES.md` §29): `DescribeKeyValueStore(KvsARN) ->
 * ETag`, `PutKey(KvsARN, Key, Value, IfMatch) -> ETag` y
 * `DeleteKey(KvsARN, Key, IfMatch) -> ETag`, optimistic-concurrency sobre
 * un `ETag` encadenado. El modelo declara `signatureVersion: v4`: a pesar
 * de lo que suponía la investigación previa, este servicio **no** necesita
 * SigV4A — `@aws-sdk/client-cloudfront-keyvaluestore` es un peer opcional
 * normal, sin `@aws-sdk/signature-v4a-crt`.
 */

import type { AwsClientSettings } from "../aws/control-plane.js";
import { awsCode, loadOptionalSdkClient } from "../aws/optional-client.js";
import { sanitizeAwsError } from "../aws/sanitize.js";
import { CustomDomainError } from "../errors.js";

type Credentials = AwsClientSettings["credentials"];

const KVS_PEER = "@aws-sdk/client-cloudfront-keyvaluestore";

/** `ResourceNotFoundException` de `DeleteKey`/`PutKey`: el almacén o la
 * clave no existen. `CustomDomain.unregister` la usa para ser idempotente. */
export const RESOURCE_NOT_FOUND_CODE = "ResourceNotFoundException";

export interface KeyValueStoreWriter {
  describe(kvsArn: string): Promise<string>;
  put(kvsArn: string, key: string, value: string, ifMatch: string): Promise<string>;
  delete(kvsArn: string, key: string, ifMatch: string): Promise<string>;
}

interface KvsApi {
  describeKeyValueStore(input: { KvsARN: string }): Promise<{ ETag?: string }>;
  putKey(input: {
    KvsARN: string;
    Key: string;
    Value: string;
    IfMatch: string;
  }): Promise<{ ETag?: string }>;
  deleteKey(input: { KvsARN: string; Key: string; IfMatch: string }): Promise<{ ETag?: string }>;
}

interface KvsModule {
  readonly CloudFrontKeyValueStoreClient: new (
    config: object,
  ) => { send(command: unknown): Promise<unknown> };
  readonly DescribeKeyValueStoreCommand: new (input: object) => unknown;
  readonly PutKeyCommand: new (input: object) => unknown;
  readonly DeleteKeyCommand: new (input: object) => unknown;
}

async function kvsApi(region: string, credentials: Credentials): Promise<KvsApi> {
  const { sdk, send } = await loadOptionalSdkClient<KvsModule>(
    KVS_PEER,
    "CustomDomain (domain=/register()/unregister())",
    (module) => module.CloudFrontKeyValueStoreClient,
    region,
    credentials,
  );
  return {
    describeKeyValueStore: (input) => send(new sdk.DescribeKeyValueStoreCommand(input)),
    putKey: (input) => send(new sdk.PutKeyCommand(input)),
    deleteKey: (input) => send(new sdk.DeleteKeyCommand(input)),
  };
}

function wrap(error: unknown): CustomDomainError {
  const summary = sanitizeAwsError(error, { includeMessage: true });
  return new CustomDomainError(`${summary.name}: ${summary.message}`, {
    awsCode: awsCode(error),
    cause: error,
  });
}

export interface CloudFrontKvsWriterOptions {
  readonly region?: string | undefined;
  readonly credentials?: Credentials;
  /** Un cliente ya construido, para tests. */
  readonly client?: KvsApi;
}

export class CloudFrontKvsWriter implements KeyValueStoreWriter {
  readonly #region: string | undefined;
  readonly #credentials: Credentials;
  #client: KvsApi | undefined;

  constructor(options: CloudFrontKvsWriterOptions = {}) {
    this.#region = options.region;
    this.#credentials = options.credentials;
    this.#client = options.client;
  }

  async #api(): Promise<KvsApi> {
    if (this.#client === undefined) {
      const region = this.#region ?? process.env.AWS_REGION ?? process.env.AWS_DEFAULT_REGION;
      if (!region) {
        throw new CustomDomainError(
          "falta la región de CustomDomain: pasa region o define AWS_REGION",
        );
      }
      this.#client = await kvsApi(region, this.#credentials);
    }
    return this.#client;
  }

  async describe(kvsArn: string): Promise<string> {
    const api = await this.#api();
    try {
      const response = await api.describeKeyValueStore({ KvsARN: kvsArn });
      return response.ETag ?? "";
    } catch (error) {
      throw wrap(error);
    }
  }

  async put(kvsArn: string, key: string, value: string, ifMatch: string): Promise<string> {
    const api = await this.#api();
    try {
      const response = await api.putKey({
        KvsARN: kvsArn,
        Key: key,
        Value: value,
        IfMatch: ifMatch,
      });
      return response.ETag ?? "";
    } catch (error) {
      throw wrap(error);
    }
  }

  async delete(kvsArn: string, key: string, ifMatch: string): Promise<string> {
    const api = await this.#api();
    try {
      const response = await api.deleteKey({ KvsARN: kvsArn, Key: key, IfMatch: ifMatch });
      return response.ETag ?? "";
    } catch (error) {
      throw wrap(error);
    }
  }
}
