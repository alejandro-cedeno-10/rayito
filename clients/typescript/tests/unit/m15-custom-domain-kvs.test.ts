/**
 * `src/custom-domain/kvs.ts` (m15-custom-domain, ADR-024): antes de
 * construir el cliente de `cloudfront-keyvaluestore`, `kvsApi()` carga el
 * peer opcional `@aws-sdk/signature-v4a` (AWS_API_NOTES.md §29, D2 de
 * `design.md` del cambio — el servicio exige SigV4A pese a declarar
 * `signatureVersion: v4`). Sin red: `loadOptionalPeer` espiado, como en
 * `secrets-adapter.test.ts`.
 */

import { afterEach, describe, expect, test, vi } from "vitest";
import { CloudFrontKvsWriter } from "../../src/custom-domain/kvs.js";
import { InvalidArgumentError } from "../../src/errors.js";
import * as optional from "../../src/optional.js";

const KVS_PEER = "@aws-sdk/client-cloudfront-keyvaluestore";
const SIGNATURE_V4A_PEER = "@aws-sdk/signature-v4a";

class RecordedCommand {
  constructor(
    readonly name: string,
    readonly input: object,
  ) {}
}

function fakeKvsModule(responses: Record<string, unknown>) {
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
      CloudFrontKeyValueStoreClient: class {
        constructor(config: object) {
          configs.push(config);
        }
        async send(recorded: RecordedCommand): Promise<unknown> {
          sent.push(recorded);
          return responses[recorded.name] ?? {};
        }
      },
      DescribeKeyValueStoreCommand: command("DescribeKeyValueStoreCommand"),
      PutKeyCommand: command("PutKeyCommand"),
      DeleteKeyCommand: command("DeleteKeyCommand"),
    },
  };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("CloudFrontKvsWriter", () => {
  test("loads the SigV4A peer before the KVS client peer, in that order", async () => {
    const fake = fakeKvsModule({ DescribeKeyValueStoreCommand: { ETag: "1" } });
    const loader = vi
      .spyOn(optional, "loadOptionalPeer")
      .mockImplementation(async (peer: string) => (peer === SIGNATURE_V4A_PEER ? {} : fake.module));
    const writer = new CloudFrontKvsWriter({ region: "us-east-1" });
    expect(await writer.describe("arn:x")).toBe("1");
    expect(loader.mock.calls.map((call) => call[0])).toEqual([SIGNATURE_V4A_PEER, KVS_PEER]);
    expect(fake.configs).toMatchObject([{ region: "us-east-1" }]);
  });

  test("a missing SigV4A peer fails before the KVS client peer is ever requested", async () => {
    const loader = vi
      .spyOn(optional, "loadOptionalPeer")
      .mockImplementation(async (peer: string) => {
        if (peer === SIGNATURE_V4A_PEER) {
          throw new InvalidArgumentError(`falta el peer ${SIGNATURE_V4A_PEER}`);
        }
        throw new Error("no debería pedirse el peer del cliente KVS si falta signature-v4a");
      });
    const writer = new CloudFrontKvsWriter({ region: "us-east-1" });
    await expect(writer.describe("arn:x")).rejects.toBeInstanceOf(InvalidArgumentError);
    expect(loader.mock.calls.map((call) => call[0])).toEqual([SIGNATURE_V4A_PEER]);
  });
});
