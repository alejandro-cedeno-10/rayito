/**
 * Secrets Manager falso para los tests de M13a: la forma del cliente agregado
 * `SecretsManager` del SDK v3 (el puerto `SecretsManagerApi`), con los
 * secretos en memoria, llamadas contadas por operación y un retraso opcional
 * de `GetSecretValue` para forzar concurrencia real. Espejo de
 * `tests/unit/fake_secrets.py`.
 */

import type { DescribedSecret, SecretsManagerApi } from "../../src/secrets/store.js";

export const SENTINEL_VALUE = "sk-SENTINEL-4f1c9e0d-never-log-me";
export const SENTINEL_NAME = "confidential-selector-name";
export const REGION = "us-east-1";

export function arnFor(secretId: string): string {
  return `arn:aws:secretsmanager:${REGION}:000000000000:secret:${secretId}-AbCdEf`;
}

export function awsError(name: string, message: string): Error {
  const error = new Error(message);
  error.name = name;
  return error;
}

interface Stored {
  readonly arn: string;
  readonly name: string;
  description: string;
  readonly versions: Map<string, string>;
  current: string;
  readonly created: Date;
}

export class FakeSecretsManager implements SecretsManagerApi {
  readonly secrets = new Map<string, Stored>();
  readonly calls = new Map<string, number>();
  readonly requests: Array<[string, Record<string, unknown>]> = [];
  getDelayMs = 0;

  count(operation: string): number {
    return this.calls.get(operation) ?? 0;
  }

  put(secretId: string, value: string, versionId = "v1"): void {
    const stored = this.secrets.get(secretId) ?? {
      arn: arnFor(secretId),
      name: secretId,
      description: "",
      versions: new Map(),
      current: "",
      created: new Date(),
    };
    stored.versions.set(versionId, value);
    stored.current = versionId;
    this.secrets.set(secretId, stored);
  }

  #record(operation: string, input: object): void {
    this.calls.set(operation, this.count(operation) + 1);
    this.requests.push([operation, { ...input }]);
  }

  #find(secretId: string): Stored {
    for (const stored of this.secrets.values()) {
      if (secretId === stored.name || secretId === stored.arn) {
        return stored;
      }
    }
    throw awsError(
      "ResourceNotFoundException",
      `Secrets Manager can't find the specified secret ${secretId}.`,
    );
  }

  #describe(stored: Stored): DescribedSecret {
    return {
      ARN: stored.arn,
      Name: stored.name,
      Description: stored.description,
      CreatedDate: stored.created,
      LastChangedDate: stored.created,
      VersionIdsToStages: Object.fromEntries(
        [...stored.versions.keys()].map((id) => [
          id,
          id === stored.current ? ["AWSCURRENT"] : ["AWSPREVIOUS"],
        ]),
      ),
    };
  }

  async getSecretValue(input: { SecretId: string; VersionId?: string; VersionStage?: string }) {
    this.#record("GetSecretValue", input);
    if (this.getDelayMs > 0) {
      await new Promise((resolve) => setTimeout(resolve, this.getDelayMs));
    }
    const stored = this.#find(input.SecretId);
    const version = input.VersionId ?? stored.current;
    const value = stored.versions.get(version);
    if (value === undefined) {
      throw awsError("ResourceNotFoundException", "no version");
    }
    return { ARN: stored.arn, Name: stored.name, VersionId: version, SecretString: value };
  }

  async describeSecret(input: { SecretId: string }): Promise<DescribedSecret> {
    this.#record("DescribeSecret", input);
    return this.#describe(this.#find(input.SecretId));
  }

  async createSecret(input: {
    Name: string;
    SecretString: string;
    Description: string;
    ClientRequestToken: string;
    KmsKeyId?: string;
  }) {
    this.#record("CreateSecret", input);
    if (this.secrets.has(input.Name)) {
      throw awsError("ResourceExistsException", `${input.Name} already exists`);
    }
    this.secrets.set(input.Name, {
      arn: arnFor(input.Name),
      name: input.Name,
      description: input.Description,
      versions: new Map([[input.ClientRequestToken, input.SecretString]]),
      current: input.ClientRequestToken,
      created: new Date(),
    });
    return { ARN: arnFor(input.Name), Name: input.Name };
  }

  async putSecretValue(input: {
    SecretId: string;
    SecretString: string;
    ClientRequestToken: string;
  }) {
    this.#record("PutSecretValue", input);
    const stored = this.#find(input.SecretId);
    stored.versions.set(input.ClientRequestToken, input.SecretString);
    stored.current = input.ClientRequestToken;
    return {};
  }

  async updateSecret(input: { SecretId: string; Description: string }) {
    this.#record("UpdateSecret", input);
    this.#find(input.SecretId).description = input.Description;
    return {};
  }

  async listSecrets(input: {
    IncludePlannedDeletion: boolean;
    Filters?: Array<{ Key: "name"; Values: string[] }>;
    MaxResults?: number;
    NextToken?: string;
  }) {
    this.#record("ListSecrets", input);
    const prefixes = (input.Filters ?? []).flatMap((filter) => filter.Values);
    const entries = [...this.secrets.values()]
      .sort((a, b) => a.name.localeCompare(b.name))
      .filter((stored) => prefixes.length === 0 || prefixes.some((p) => stored.name.startsWith(p)))
      .map((stored) => {
        const { VersionIdsToStages, ...rest } = this.#describe(stored);
        return { ...rest, SecretVersionsToStages: VersionIdsToStages };
      });
    const start = Number(input.NextToken ?? "0");
    const size = input.MaxResults ?? 100;
    const page = entries.slice(start, start + size);
    return {
      SecretList: page,
      NextToken: start + size < entries.length ? String(start + size) : undefined,
    };
  }

  /**
   * Como AWS (aceptación de 0.5.0): con `ForceDeleteWithoutRecovery`, un
   * secreto que no existe NO es `ResourceNotFoundException`.
   */
  async deleteSecret(input: { SecretId: string; ForceDeleteWithoutRecovery: boolean }) {
    this.#record("DeleteSecret", input);
    const stored = [...this.secrets.values()].find(
      (entry) => input.SecretId === entry.name || input.SecretId === entry.arn,
    );
    if (stored === undefined) {
      if (input.ForceDeleteWithoutRecovery) {
        return {};
      }
      this.#find(input.SecretId);
    } else {
      this.secrets.delete(stored.name);
    }
    return {};
  }
}
