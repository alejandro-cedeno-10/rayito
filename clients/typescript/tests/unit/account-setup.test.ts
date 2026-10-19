/**
 * `rayito setup` (dominio y servicio): los vectores compartidos con Python
 * (`testdata/account-setup/`), cada constante y mensaje contra
 * `contract.json`, y los flujos de `AccountSetup` sobre dobles en memoria.
 */

import { describe, expect, it } from "vitest";
import * as domain from "../../src/account-setup/domain.js";
import {
  AccountSetupError,
  type ErrorCode,
  type SetupPlan,
  type SetupRequest,
} from "../../src/account-setup/domain.js";
import { AccountSetup } from "../../src/account-setup/service.js";
import { BUILDING_VERSION_STATES } from "../../src/images/gateway.js";
import { LATEST_BASE_IMAGE_VERSION } from "../../src/images/publish-service.js";
import { VERSION } from "../../src/version.js";
import {
  BUILD_ROLE_ARN,
  FAKE_IMAGE_SHA256,
  FakeAccountSetup,
  type FakeBehavior,
  type VectorInput,
  withPlaceholders,
} from "./account-setup-fakes.js";
import { loadAccountSetupTestdata } from "./cli/paths.js";

interface Contract {
  errorExitCodes: Record<string, number>;
  constants: {
    iamStackName: string;
    iamComponentName: string;
    iamCapabilities: string[];
    artifactBucketParameter: string;
    buildRoleOutputKey: string;
    baseImageName: string;
    baseImageVariant: string;
    baseImageVersion: string;
    cdkBootstrapQualifier: string;
    bucketEnvVar: string;
    releaseTagPrefix: string;
    releaseDownloadUrlTemplate: string;
    imageAssetName: string;
    checksumsAssetName: string;
    releaseDownloadTimeoutSeconds: number;
    usableStackStates: string[];
    inProgressStackSuffix: string;
    deletingStackState: string;
    buildingVersionStates: string[];
  };
  bucketSources: string[];
  messages: {
    cost: Record<string, string>;
    progress: Record<string, string>;
    errors: Record<string, string>;
    downloadReasons: Record<string, string>;
    bucketSources: Record<string, string>;
    accountSetupHint: string;
    hintFormat: string;
  };
}

interface PlanVector {
  name: string;
  input: VectorInput;
  expected: {
    steps?: Record<string, unknown>[];
    ready?: boolean;
    needsConfirmation?: boolean;
    cost?: string[];
    error?: string;
  };
}

interface ChecksumCase {
  name: string;
  text: string;
  asset: string;
  expected: string | null;
}

const contract = loadAccountSetupTestdata<Contract>("contract.json");
const planVectors = loadAccountSetupTestdata<{ cases: PlanVector[] }>("plan-vectors.json").cases;
const checksumCases = loadAccountSetupTestdata<{ cases: ChecksumCase[] }>("checksums.json").cases;
const MS_PER_SECOND = 1000;

function requestOf(input: VectorInput): SetupRequest {
  return {
    region: input.region,
    bucket: input.bucket ?? undefined,
    environmentBucket: input.environmentBucket ?? undefined,
    artifact: input.artifact ?? undefined,
    sdkVersion: VERSION,
  };
}

async function planOf(input: VectorInput, behavior?: FakeBehavior): Promise<SetupPlan> {
  return new AccountSetup(new FakeAccountSetup(input, behavior)).plan(requestOf(input));
}

describe("account setup: shared plan vectors", () => {
  it.each(planVectors.map((vector) => [vector.name, vector] as const))("%s", async (_name, raw) => {
    const vector = withPlaceholders(raw, raw.input, VERSION);
    if (vector.expected.error !== undefined) {
      const failure = await planOf(vector.input).catch((error: unknown) => error);
      expect(failure).toBeInstanceOf(AccountSetupError);
      expect((failure as AccountSetupError).code).toBe(vector.expected.error);
      return;
    }
    const plan = await planOf(vector.input);
    expect(plan.steps.map(domain.stepJson)).toEqual(vector.expected.steps);
    expect(plan.ready).toBe(vector.expected.ready);
    expect(plan.needsConfirmation).toBe(vector.expected.needsConfirmation);
    expect([...plan.cost]).toEqual(vector.expected.cost);
  });
});

describe("account setup: shared checksum vectors", () => {
  it.each(checksumCases.map((item) => [item.name, item] as const))("%s", (_name, item) => {
    expect(domain.checksumFor(item.text, item.asset) ?? null).toBe(item.expected);
  });
});

describe("account setup: contract", () => {
  it("constants equal the contract", () => {
    const { constants } = contract;
    expect(domain.IAM_STACK_NAME).toBe(constants.iamStackName);
    expect(domain.IAM_STACK_COMPONENT_NAME).toBe(constants.iamComponentName);
    expect([...domain.IAM_STACK_CAPABILITIES]).toEqual(constants.iamCapabilities);
    expect(domain.ARTIFACT_BUCKET_PARAMETER).toBe(constants.artifactBucketParameter);
    expect(domain.BUILD_ROLE_OUTPUT_KEY).toBe(constants.buildRoleOutputKey);
    expect(domain.BASE_IMAGE_NAME).toBe(constants.baseImageName);
    expect(domain.BASE_IMAGE_VARIANT).toBe(constants.baseImageVariant);
    expect(LATEST_BASE_IMAGE_VERSION).toBe(constants.baseImageVersion);
    expect(domain.CDK_BOOTSTRAP_QUALIFIER).toBe(constants.cdkBootstrapQualifier);
    expect(domain.BUCKET_ENV_VAR).toBe(constants.bucketEnvVar);
    expect(domain.RELEASE_TAG_PREFIX).toBe(constants.releaseTagPrefix);
    expect(domain.RELEASE_DOWNLOAD_URL_TEMPLATE).toBe(constants.releaseDownloadUrlTemplate);
    expect(domain.IMAGE_ASSET_NAME).toBe(constants.imageAssetName);
    expect(domain.CHECKSUMS_ASSET_NAME).toBe(constants.checksumsAssetName);
    expect(domain.RELEASE_DOWNLOAD_TIMEOUT_MS).toBe(
      constants.releaseDownloadTimeoutSeconds * MS_PER_SECOND,
    );
    expect([...domain.USABLE_STACK_STATES]).toEqual(constants.usableStackStates);
    expect(domain.IN_PROGRESS_STACK_SUFFIX).toBe(constants.inProgressStackSuffix);
    expect(domain.DELETING_STACK_STATE).toBe(constants.deletingStackState);
    expect([...BUILDING_VERSION_STATES]).toEqual(constants.buildingVersionStates);
  });

  it("messages, cost texts and exit codes equal the contract", () => {
    expect({ ...domain.ERROR_MESSAGES }).toEqual(contract.messages.errors);
    expect({ ...domain.ERROR_EXIT_CODES }).toEqual(contract.errorExitCodes);
    expect({ ...domain.PROGRESS_MESSAGES }).toEqual(contract.messages.progress);
    expect({ ...domain.DOWNLOAD_REASONS }).toEqual(contract.messages.downloadReasons);
    expect({ ...domain.COST_TEXTS }).toEqual(contract.messages.cost);
    expect({ ...domain.BUCKET_SOURCE_LABELS }).toEqual(contract.messages.bucketSources);
    expect(Object.keys(domain.BUCKET_SOURCE_LABELS)).toEqual(contract.bucketSources);
    expect(domain.ACCOUNT_SETUP_HINT).toBe(contract.messages.accountSetupHint);
    expect(domain.HINT_TEMPLATE).toBe(contract.messages.hintFormat);
  });

  it("every error code formats its message and carries its exit code", () => {
    const fields = {
      region: "sa-east-1",
      regions: "a, b",
      bucket: "amzn-s3-demo-bucket",
      source: "--bucket",
      bucketRegion: "eu-west-1",
      artifact: "image.zip",
      name: "rayito-m0-iam",
      status: "ROLLBACK_COMPLETE",
      asset: "rayito-image.zip",
      release: "rayd-v1.0.0",
      reason: "HTTP 404",
      imageVersion: "2.0",
      state: "FAILED",
    };
    for (const code of Object.keys(contract.errorExitCodes) as ErrorCode[]) {
      const error = new AccountSetupError(code, fields);
      expect(error.exitCode).toBe(contract.errorExitCodes[code]);
      expect(error.message).not.toContain("{");
    }
  });

  it("release helpers build the tag, the URL and the hint", () => {
    expect(domain.releaseTag("1.2.3")).toBe("rayd-v1.2.3");
    expect(domain.releaseAssetUrl("rayd-v1.2.3", "SHA256SUMS")).toBe(
      "https://github.com/alejandro-cedeno-10/rayito/releases/download/rayd-v1.2.3/SHA256SUMS",
    );
    expect(domain.withSetupHint("falta algo")).toBe(
      `falta algo (${contract.messages.accountSetupHint})`,
    );
  });

  it("step JSON keeps the contract key order", () => {
    const step = domain.newStep({ resource: "bucket", name: "b", action: "reuse" });
    expect(Object.keys(domain.stepJson(step))).toEqual([
      "resource",
      "name",
      "action",
      "source",
      "status",
      "imageVersion",
      "release",
      "previousBucket",
    ]);
  });
});

const FRESH: VectorInput = {
  region: "us-east-1",
  account: "123456789012",
  bucket: null,
  environmentBucket: null,
  artifact: null,
  stack: null,
  buckets: { [domain.cdkBucketName("123456789012", "us-east-1")]: "us-east-1" },
  image: null,
};

const PREPARED_STACK = {
  status: "CREATE_COMPLETE",
  outputs: { BuildRoleArn: BUILD_ROLE_ARN },
  parameters: { ArtifactBucket: "amzn-s3-demo-bucket" },
};

const LAUNCHABLE_IMAGE = {
  state: "UPDATED",
  versions: [
    {
      imageVersion: "1.0",
      state: "SUCCESSFUL",
      status: "ACTIVE",
      createdAt: "2026-10-01T00:00:00Z",
    },
  ],
};

function inputWith(overrides: Partial<VectorInput>): VectorInput {
  return { ...FRESH, ...overrides };
}

async function applied(input: VectorInput, behavior?: FakeBehavior, artifact?: string) {
  const fake = new FakeAccountSetup(input, behavior);
  const service = new AccountSetup(fake);
  const plan = await service.plan({ ...requestOf(input), artifact });
  const lines: string[] = [];
  const result = await service.apply(plan, { workdir: "/work", onProgress: (m) => lines.push(m) });
  return { fake, plan, result, lines };
}

function names(calls: readonly (readonly unknown[])[]): unknown[] {
  return calls.map((call) => call[0]);
}

describe("AccountSetup.plan", () => {
  it("only reads: a fresh account describes, probes one bucket and gets the image", async () => {
    const fake = new FakeAccountSetup(FRESH);
    await new AccountSetup(fake).plan(requestOf(FRESH));
    expect(names(fake.calls)).toEqual(["accountId", "describe", "head_bucket", "get_image"]);
    expect(fake.writes).toEqual([]);
  });

  it("rejects an unsupported region before any call", async () => {
    const input = inputWith({ region: "sa-east-1" });
    const fake = new FakeAccountSetup(input);
    await expect(new AccountSetup(fake).plan(requestOf(input))).rejects.toMatchObject({
      code: "unsupported_region",
    });
    expect(fake.calls).toEqual([]);
  });

  it("construction makes no call", () => {
    const fake = new FakeAccountSetup(FRESH);
    new AccountSetup(fake);
    expect(fake.calls).toEqual([]);
  });
});

describe("AccountSetup.apply", () => {
  it("prepares a fresh account: create, download twice, verify and publish", async () => {
    const { fake, result, lines } = await applied(FRESH);
    const bucket = domain.cdkBucketName("123456789012", "us-east-1");
    expect(fake.calls.filter(([name]) => ["create", "update", "wait"].includes(name))).toEqual([
      ["create", bucket],
      ["wait"],
    ]);
    expect(fake.calls.filter(([name]) => name === "download")).toHaveLength(2);
    expect(fake.calls.at(-1)).toEqual([
      "publish",
      "/work/rayito-image.zip",
      bucket,
      BUILD_ROLE_ARN,
    ]);
    expect(result).toMatchObject({ bucket, imageVersion: "3.0", artifactUploaded: true });
    expect(lines).toContain(
      `sha256 de rayito-image.zip verificado contra SHA256SUMS: ${FAKE_IMAGE_SHA256}`,
    );
  });

  it("an updated bucket sends only ArtifactBucket and reuses the image", async () => {
    const input = inputWith({
      bucket: "amzn-s3-demo-bucket1",
      stack: PREPARED_STACK,
      buckets: { "amzn-s3-demo-bucket1": "us-east-1" },
      image: LAUNCHABLE_IMAGE,
    });
    const { fake, result } = await applied(input);
    expect(fake.writes).toEqual([["update", "amzn-s3-demo-bucket1"]]);
    expect(result).toMatchObject({ imageVersion: "1.0", artifactUploaded: undefined });
  });

  it("waits for a stack in progress without creating or updating it", async () => {
    const input = inputWith({
      stack: { ...PREPARED_STACK, status: "CREATE_IN_PROGRESS", outputs: {} },
      buckets: { "amzn-s3-demo-bucket": "us-east-1" },
      image: LAUNCHABLE_IMAGE,
    });
    const { fake } = await applied(input);
    expect(fake.calls.filter(([name]) => ["create", "update"].includes(name))).toEqual([]);
    expect(names(fake.calls)).toContain("wait");
  });

  it("waits for a build in progress and never publishes", async () => {
    const input = inputWith({
      stack: PREPARED_STACK,
      buckets: { "amzn-s3-demo-bucket": "us-east-1" },
      image: {
        state: "UPDATING",
        versions: [
          ...LAUNCHABLE_IMAGE.versions,
          {
            imageVersion: "2.0",
            state: "IN_PROGRESS",
            status: "INACTIVE",
            createdAt: "2026-10-02T00:00:00Z",
          },
        ],
      },
    });
    const { fake, plan, result } = await applied(input);
    expect(plan.needsConfirmation).toBe(false);
    expect(fake.calls.filter(([name]) => name === "gate")).toEqual([["gate", "2.0"]]);
    expect(fake.writes).toEqual([]);
    expect(result.imageVersion).toBe("2.0");
  });

  it("a waited build that fails raises image_build_failed", async () => {
    const input = inputWith({
      stack: PREPARED_STACK,
      buckets: { "amzn-s3-demo-bucket": "us-east-1" },
      image: {
        state: "UPDATING",
        versions: [
          {
            imageVersion: "2.0",
            state: "IN_PROGRESS",
            status: "INACTIVE",
            createdAt: "2026-10-02T00:00:00Z",
          },
        ],
      },
    });
    await expect(applied(input, { waitedBuildFails: true })).rejects.toMatchObject({
      code: "image_build_failed",
    });
  });

  it("a checksum mismatch publishes nothing", async () => {
    await expect(applied(FRESH, { downloadedSha256: "b".repeat(64) })).rejects.toMatchObject({
      code: "checksum_mismatch",
    });
  });

  it("a release without the zip entry publishes nothing", async () => {
    const failing = new FakeAccountSetup(FRESH, { checksumsText: `${"1".repeat(64)}  rayd\n` });
    const service = new AccountSetup(failing);
    const plan = await service.plan(requestOf(FRESH));
    await expect(
      service.apply(plan, { workdir: "/w", onProgress: () => undefined }),
    ).rejects.toMatchObject({
      code: "checksum_missing",
    });
    expect(names(failing.calls)).not.toContain("publish");
  });

  it("--artifact downloads nothing", async () => {
    const { fake } = await applied(FRESH, undefined, "image/rayito-image.zip");
    expect(names(fake.calls)).not.toContain("download");
    expect(fake.calls.at(-1)?.[1]).toBe("image/rayito-image.zip");
  });

  it("a prepared account writes nothing", async () => {
    const input = inputWith({
      stack: PREPARED_STACK,
      buckets: { "amzn-s3-demo-bucket": "us-east-1" },
      image: LAUNCHABLE_IMAGE,
    });
    const { fake, plan, result } = await applied(input);
    expect(plan.ready).toBe(true);
    expect(fake.writes).toEqual([]);
    expect(result.imageVersion).toBe("1.0");
  });

  it("a stack created without BuildRoleArn raises stack_incomplete", async () => {
    await expect(applied(FRESH, { omitBuildRole: true })).rejects.toMatchObject({
      code: "stack_incomplete",
    });
  });

  it("publishing over a reused stack reads its build role first", async () => {
    const input = inputWith({
      stack: PREPARED_STACK,
      buckets: { "amzn-s3-demo-bucket": "us-east-1" },
      image: { state: "UPDATE_FAILED", versions: [] },
    });
    const { fake } = await applied(input);
    expect(fake.calls.at(-1)).toEqual([
      "publish",
      "/work/rayito-image.zip",
      "amzn-s3-demo-bucket",
      BUILD_ROLE_ARN,
    ]);
  });
});
