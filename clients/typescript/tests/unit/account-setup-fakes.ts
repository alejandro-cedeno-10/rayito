/**
 * Dobles en memoria de los cinco puertos de `rayito setup`, construidos desde
 * el `input` de los vectores compartidos (`testdata/account-setup/`). Cada
 * llamada queda registrada en `calls`, como los `Stubber` de los tests de
 * Python: una lectura o una escritura que no debía ocurrir se ve en la lista.
 */

import {
  type BucketProbe,
  cdkBucketName,
  type DownloadedRelease,
  IAM_STACK_NAME,
  type ImageGateResult,
  type ImageObservation,
  type PublishedImage,
  type StackObservation,
} from "../../src/account-setup/domain.js";
import type {
  AccountSetupGateways,
  ArtifactBucketGateway,
  BaseImageGateway,
  BaseImagePublisher,
  IamStackGateway,
  Progress,
  ReleaseArtifactSource,
} from "../../src/account-setup/ports.js";

export const BUILD_ROLE_ARN = "arn:aws:iam::123456789012:role/rayito-m0-build-us-east-1";
export const FAKE_IMAGE_SHA256 = "a".repeat(64);
const PUBLISHED_VERSION = "3.0";

export interface VectorImageVersion {
  readonly imageVersion: string;
  readonly state: string;
  readonly status: string;
  readonly createdAt: string;
}

/** El `input` de `plan-vectors.json` y de `cli-cases.json`. */
export interface VectorInput {
  readonly region: string;
  readonly account: string;
  readonly bucket: string | null;
  readonly environmentBucket: string | null;
  readonly artifact: string | null;
  readonly stack: {
    readonly status: string;
    readonly outputs: Record<string, string>;
    readonly parameters: Record<string, string>;
  } | null;
  readonly buckets: Record<string, string>;
  readonly image: { readonly state: string; readonly versions: VectorImageVersion[] } | null;
}

/** Sustituye `{sdkVersion}` y `{cdkBucket}` del texto de un caso compartido. */
export function withPlaceholders<T>(value: T, input: VectorInput, sdkVersion: string): T {
  const text = JSON.stringify(value)
    .replaceAll("{sdkVersion}", sdkVersion)
    .replaceAll("{cdkBucket}", cdkBucketName(input.account, input.region));
  return JSON.parse(text) as T;
}

export type Call = readonly [string, ...unknown[]];

export interface FakeBehavior {
  /** `create`/`update` dejan la pila sin la salida `BuildRoleArn`. */
  readonly omitBuildRole?: boolean;
  /** La espera de un build en curso termina sin quedar lanzable. */
  readonly waitedBuildFails?: boolean;
  /** El sha256 del zip descargado; por defecto el que lista `SHA256SUMS`. */
  readonly downloadedSha256?: string;
  /** El texto de `SHA256SUMS`; por defecto lista el zip con `FAKE_IMAGE_SHA256`. */
  readonly checksumsText?: string;
  readonly publishedArtifactUploaded?: boolean;
}

export class FakeAccountSetup implements AccountSetupGateways {
  readonly calls: Call[] = [];
  readonly iamStack: IamStackGateway;
  readonly buckets: ArtifactBucketGateway;
  readonly images: BaseImageGateway;
  readonly publisher: BaseImagePublisher;
  readonly releases: ReleaseArtifactSource;

  constructor(input: VectorInput, behavior: FakeBehavior = {}) {
    let stack: StackObservation = {
      name: IAM_STACK_NAME,
      status: input.stack?.status,
      outputs: input.stack?.outputs ?? {},
      parameters: input.stack?.parameters ?? {},
    };
    const record = (...call: Call): void => {
      this.calls.push(call);
    };
    const deployed = (bucket: string): StackObservation => {
      stack = {
        name: IAM_STACK_NAME,
        status: "CREATE_COMPLETE",
        outputs: behavior.omitBuildRole === true ? {} : { BuildRoleArn: BUILD_ROLE_ARN },
        parameters: { ...stack.parameters, ArtifactBucket: bucket },
      };
      return stack;
    };
    this.iamStack = {
      observe: async () => {
        record("describe");
        return stack;
      },
      create: async (bucket) => {
        record("create", bucket);
        record("wait");
        return deployed(bucket);
      },
      updateBucket: async (bucket) => {
        record("update", bucket);
        record("wait");
        return deployed(bucket);
      },
      wait: async () => {
        record("wait");
        return deployed(stack.parameters.ArtifactBucket ?? "");
      },
    };
    this.buckets = {
      accountId: async () => {
        record("accountId");
        return input.account;
      },
      probe: async (bucket): Promise<BucketProbe> => {
        record("head_bucket", bucket);
        const region = input.buckets[bucket];
        return { exists: region !== undefined, region };
      },
    };
    this.images = {
      observe: async (name): Promise<ImageObservation> => {
        record("get_image");
        const imageArn = `arn:aws:lambda:${input.region}:${input.account}:microvm-image:${name}`;
        if (input.image === null) {
          return { name, imageArn, state: undefined, versions: [] };
        }
        record("list_versions");
        return {
          name,
          imageArn,
          state: input.image.state,
          versions: input.image.versions.map((item) => ({
            ...item,
            createdAt: new Date(item.createdAt),
          })),
        };
      },
      wait: async (_arn, version, onProgress: Progress): Promise<ImageGateResult> => {
        record("gate", version);
        onProgress(`  image=UPDATING version=${version}`);
        return {
          launchable: behavior.waitedBuildFails !== true,
          versionState: behavior.waitedBuildFails === true ? "FAILED" : "SUCCESSFUL",
        };
      },
    };
    this.publisher = {
      publish: async (artifact, options): Promise<PublishedImage> => {
        record("publish", artifact, options.bucket, options.buildRoleArn);
        return {
          imageArn: `arn:aws:lambda:${input.region}:${input.account}:microvm-image:rayito-base`,
          imageVersion: PUBLISHED_VERSION,
          artifactUploaded: behavior.publishedArtifactUploaded ?? true,
        };
      },
    };
    this.releases = {
      fetch: async (_tag, directory): Promise<DownloadedRelease> => {
        record("download", "SHA256SUMS");
        record("download", "rayito-image.zip");
        return {
          checksumsText: behavior.checksumsText ?? `${FAKE_IMAGE_SHA256}  rayito-image.zip\n`,
          artifact: `${directory}/rayito-image.zip`,
          artifactSha256: behavior.downloadedSha256 ?? FAKE_IMAGE_SHA256,
        };
      },
    };
  }

  /** Las llamadas que cambian algo en la cuenta o descargan algo. */
  get writes(): Call[] {
    return this.calls.filter(([name]) =>
      ["create", "update", "publish", "download"].includes(name),
    );
  }
}
