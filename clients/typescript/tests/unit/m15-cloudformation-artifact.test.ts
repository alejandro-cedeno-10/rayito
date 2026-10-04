/**
 * `CloudFormationProvisioner.putArtifact`: nunca se fía de que exista un
 * objeto con la clave esperada (la clave es pública: el sha256 del zip que
 * viaja con el SDK). Compara el contenido, manda siempre
 * `ExpectedBucketOwner` y sube con `ChecksumSHA256`. Espejo de
 * `test_m15_cloudformation.py`.
 */

import { createHash } from "node:crypto";
import type { GetObjectCommandInput, PutObjectCommandInput } from "@aws-sdk/client-s3";
import { describe, expect, it } from "vitest";
import { StackError } from "../../src/errors.js";
import {
  CloudFormationProvisioner,
  type S3Api,
  type StsApi,
} from "../../src/stacks/cloudformation.js";
import { artifactKey, STACK_ARTIFACT_PREFIX } from "../../src/stacks/packaging.js";
import { componentByName } from "../../src/stacks/registry.js";

const ACCOUNT = "123456789012";
const DATA = new TextEncoder().encode("data");
const DATA_SHA256_B64 = createHash("sha256").update(DATA).digest("base64");

function awsError(name: string): Error {
  const error = new Error(name);
  error.name = name;
  return error;
}

class FakeS3 implements S3Api {
  readonly gets: Array<Parameters<S3Api["getObject"]>[0]> = [];
  readonly puts: Array<Parameters<S3Api["putObject"]>[0]> = [];

  constructor(private readonly stored: Uint8Array | Error) {}

  async getObject(input: Parameters<S3Api["getObject"]>[0]) {
    this.gets.push(input);
    if (this.stored instanceof Error) {
      throw this.stored;
    }
    const bytes = this.stored;
    return { Body: { transformToByteArray: async () => bytes } };
  }

  async putObject(input: Parameters<S3Api["putObject"]>[0]) {
    this.puts.push(input);
    return {};
  }
}

const sts: StsApi = { getCallerIdentity: async () => ({ Account: ACCOUNT }) };

function provisioner(s3: FakeS3, warnings: string[] = []): CloudFormationProvisioner {
  return new CloudFormationProvisioner({
    region: "us-east-1",
    s3Client: s3,
    stsClient: sts,
    logger: { warn: (message) => warnings.push(message) },
  });
}

const BUCKET = "amzn-s3-demo-bucket";
const KEY = "rayito/stacks/events-webhooks/0.zip";
const LOCATED = { Bucket: BUCKET, Key: KEY, ExpectedBucketOwner: ACCOUNT };

describe("putArtifact", () => {
  it("skips the upload only when the stored content matches", async () => {
    const s3 = new FakeS3(DATA);
    await provisioner(s3).putArtifact(BUCKET, KEY, DATA);
    expect(s3.gets).toEqual([LOCATED]);
    expect(s3.puts).toEqual([]);
  });

  it("overwrites a planted object under the same key, with a warning", async () => {
    const s3 = new FakeS3(new TextEncoder().encode("not the sdk's code"));
    const warnings: string[] = [];
    await provisioner(s3, warnings).putArtifact(BUCKET, KEY, DATA);
    expect(s3.puts).toEqual([{ ...LOCATED, Body: DATA, ChecksumSHA256: DATA_SHA256_B64 }]);
    expect(warnings).toHaveLength(1);
    expect(warnings[0]).not.toContain(BUCKET);
    expect(warnings[0]).not.toContain(KEY);
  });

  it("uploads a missing object with the owner and the checksum", async () => {
    const s3 = new FakeS3(awsError("NoSuchKey"));
    await provisioner(s3).putArtifact(BUCKET, KEY, DATA);
    expect(s3.puts).toEqual([{ ...LOCATED, Body: DATA, ChecksumSHA256: DATA_SHA256_B64 }]);
  });

  it("fails on a bucket of another account instead of trusting it", async () => {
    const s3 = new FakeS3(awsError("AccessDenied"));
    await expect(provisioner(s3).putArtifact(BUCKET, KEY, DATA)).rejects.toThrow(StackError);
    expect(s3.puts).toEqual([]);
  });
});

describe("S3 parameter names", () => {
  it("are the ones @aws-sdk/client-s3 declares (AWS_API_NOTES.md §21)", () => {
    const get = LOCATED satisfies GetObjectCommandInput;
    const put = {
      ...LOCATED,
      Body: DATA,
      ChecksumSHA256: DATA_SHA256_B64,
    } satisfies PutObjectCommandInput;
    expect(Object.keys(get).sort()).toEqual(["Bucket", "ExpectedBucketOwner", "Key"]);
    expect(put.ChecksumSHA256).toBe(DATA_SHA256_B64);
  });
});

describe("artifactKey", () => {
  it("lives inside the protected rayito/ namespace, per component", async () => {
    const component = componentByName("events-webhooks");
    expect(component).toBeDefined();
    if (component === undefined) {
      return;
    }
    const key = await artifactKey(component, DATA);
    const hex = createHash("sha256").update(DATA).digest("hex");
    expect(key).toBe(`${STACK_ARTIFACT_PREFIX}/events-webhooks/${hex}.zip`);
    expect(STACK_ARTIFACT_PREFIX).toBe("rayito/stacks");
  });
});
