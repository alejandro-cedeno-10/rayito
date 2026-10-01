/**
 * El adaptador por defecto de `SecretStore`: carga
 * `@aws-sdk/client-secrets-manager` con `loadOptionalPeer` sólo en la primera
 * llamada, construye un `SecretsManagerClient` con la región y las
 * credenciales pedidas y manda los `…Command` de AWS_API_NOTES.md §19 con
 * los parámetros de entrada tal cual. Sin red: el módulo es un falso.
 */

import { afterEach, describe, expect, test, vi } from "vitest";
import { InvalidArgumentError, SecretError } from "../../src/errors.js";
import * as optional from "../../src/optional.js";
import { SecretRef } from "../../src/secrets/names.js";
import { SecretStore } from "../../src/secrets/store.js";
import { awsError, SENTINEL_VALUE } from "./secrets-fake.js";

class RecordedCommand {
  constructor(
    readonly name: string,
    readonly input: object,
  ) {}
}

function fakeModule(responses: Record<string, unknown>) {
  const sent: RecordedCommand[] = [];
  const configs: object[] = [];
  const command = (name: string) =>
    class extends RecordedCommand {
      constructor(input: object) {
        super(name, input);
      }
    };
  return {
    sent,
    configs,
    module: {
      SecretsManagerClient: class {
        constructor(config: object) {
          configs.push(config);
        }
        async send(recorded: RecordedCommand): Promise<unknown> {
          sent.push(recorded);
          const response = responses[recorded.name];
          if (response instanceof Error) {
            throw response;
          }
          return response ?? {};
        }
      },
      CreateSecretCommand: command("CreateSecretCommand"),
      PutSecretValueCommand: command("PutSecretValueCommand"),
      GetSecretValueCommand: command("GetSecretValueCommand"),
      DescribeSecretCommand: command("DescribeSecretCommand"),
      UpdateSecretCommand: command("UpdateSecretCommand"),
      ListSecretsCommand: command("ListSecretsCommand"),
      DeleteSecretCommand: command("DeleteSecretCommand"),
    },
  };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("SDK adapter", () => {
  test("the peer loads lazily once and each call sends its documented command", async () => {
    const fake = fakeModule({
      GetSecretValueCommand: { SecretString: SENTINEL_VALUE },
      DeleteSecretCommand: awsError("ResourceNotFoundException", "gone"),
    });
    const loader = vi.spyOn(optional, "loadOptionalPeer").mockResolvedValue(fake.module);
    const credentials = { accessKeyId: "AKIATEST", secretAccessKey: "test" };
    const store = new SecretStore({ region: "eu-west-1", credentials });
    expect(loader).not.toHaveBeenCalled();
    expect(await store.readValue(new SecretRef("a", { versionStage: "AWSPREVIOUS" }))).toBe(
      SENTINEL_VALUE,
    );
    expect(await store.destroy("a")).toBe(false);
    expect(loader).toHaveBeenCalledTimes(1);
    expect(loader.mock.calls[0]?.[0]).toBe("@aws-sdk/client-secrets-manager");
    expect(fake.configs).toHaveLength(1);
    expect(fake.configs[0]).toMatchObject({ region: "eu-west-1", credentials });
    expect(fake.sent.map((command) => [command.name, command.input])).toEqual([
      ["GetSecretValueCommand", { SecretId: "rayito/a", VersionStage: "AWSPREVIOUS" }],
      ["DescribeSecretCommand", { SecretId: "rayito/a" }],
      ["DeleteSecretCommand", { SecretId: "rayito/a", ForceDeleteWithoutRecovery: true }],
    ]);
  });

  test("a failed peer load is retried on the next call", async () => {
    const fake = fakeModule({ DescribeSecretCommand: { ARN: "arn:x", Name: "rayito/a" } });
    const loader = vi
      .spyOn(optional, "loadOptionalPeer")
      .mockRejectedValueOnce(new InvalidArgumentError("falta el peer"))
      .mockResolvedValue(fake.module);
    const store = new SecretStore({ region: "us-east-1" });
    await expect(store.getInfo("a")).rejects.toBeInstanceOf(InvalidArgumentError);
    expect((await store.getInfo("a")).name).toBe("a");
    expect(loader).toHaveBeenCalledTimes(2);
  });

  test("without a region the first call fails before loading anything", async () => {
    const loader = vi.spyOn(optional, "loadOptionalPeer");
    vi.stubEnv("AWS_REGION", "");
    vi.stubEnv("AWS_DEFAULT_REGION", "");
    try {
      await expect(new SecretStore().getInfo("a")).rejects.toBeInstanceOf(InvalidArgumentError);
      expect(loader).not.toHaveBeenCalled();
    } finally {
      vi.unstubAllEnvs();
    }
  });

  test("a binary-only secret is a SecretError", async () => {
    const fake = fakeModule({ GetSecretValueCommand: {} });
    vi.spyOn(optional, "loadOptionalPeer").mockResolvedValue(fake.module);
    await expect(
      new SecretStore({ region: "us-east-1" }).readValue(new SecretRef("a")),
    ).rejects.toBeInstanceOf(SecretError);
  });
});
