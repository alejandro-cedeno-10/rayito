/**
 * Dobles en memoria de los seis puertos de `rayito setup`, construidos desde
 * el `input` de los vectores compartidos (`testdata/account-setup/`). Cada
 * llamada queda registrada en `calls`, como los `Stubber` de los tests de
 * Python: una lectura o una escritura que no debía ocurrir se ve en la lista.
 */

import { createHash } from "node:crypto";
import {
  ARTIFACT_STACK_NAME,
  BUCKET_NAME_OUTPUT_KEY,
  BUCKET_NAME_PARAMETER,
  type BucketProbe,
  CAPS_IMAGE_NAME,
  cdkBucketName,
  type DownloadedRelease,
  defaultArtifactBucketName,
  IAM_STACK_NAME,
  type ImageGateResult,
  type ImageObservation,
  type PublishedImage,
  type StackObservation,
  TEMPLATE_FINGERPRINT_PARAMETER,
} from "../../src/account-setup/domain.js";
import type {
  AccountSetupGateways,
  ArtifactBucketGateway,
  ArtifactStackGateway,
  BaseImageGateway,
  BaseImagePublisher,
  IamStackGateway,
  Progress,
  ReleaseArtifactSource,
} from "../../src/account-setup/ports.js";
import { TEMPLATE_BODY as IAM_TEMPLATE_BODY } from "../../src/stacks/templates/iam.gen.js";

export const BUILD_ROLE_ARN = "arn:aws:iam::123456789012:role/rayito-m0-build-us-east-1";
export const FAKE_IMAGE_SHA256 = "a".repeat(64);
const PUBLISHED_VERSION = "3.0";

export interface VectorImageVersion {
  readonly imageVersion: string;
  readonly state: string;
  readonly status: string;
  readonly createdAt: string;
}

/** Una pila observada: `stack` es rayito-m0-iam y `artifactStack` rayito-m0-artifacts. */
export interface VectorStack {
  readonly status: string;
  readonly outputs: Record<string, string>;
  readonly parameters: Record<string, string>;
  /** Las etiquetas de la pila; sin ellas, ninguna. */
  readonly tags?: Record<string, string>;
}

/** El `input` de `plan-vectors.json` y de `cli-cases.json`. */
export interface VectorInput {
  readonly region: string;
  readonly account: string;
  readonly bucket: string | null;
  readonly environmentBucket: string | null;
  readonly artifact: string | null;
  readonly stack: VectorStack | null;
  /** Los buckets que `HeadBucket` encuentra, con su región; los de `forbiddenBuckets` dan 403 y el resto 404. */
  readonly buckets: Record<string, string>;
  readonly forbiddenBuckets: string[];
  readonly artifactStack: VectorStack | null;
  readonly image: VectorImage | null;
  /** `rayito-base-caps`, con la misma forma que `image`; sin la clave, el mismo estado que `image`. */
  readonly capsImage?: VectorImage | null;
  /** `--caps-image` (`capsImage: true` en la petición); sin la clave, el paso es `skip`. */
  readonly capsImageRequested?: boolean;
}

export interface VectorImage {
  readonly state: string;
  readonly versions: VectorImageVersion[];
}

/** Sufijo de las llamadas que tocan `rayito-base-caps` en el registro. */
export const CAPS_CALL_SUFFIX = ":caps";

function capsObservation(input: VectorInput): VectorImage | null {
  return input.capsImage === undefined ? input.image : input.capsImage;
}

/** El sha256 del `infra/iam.yaml` empaquetado: `{iamTemplateFingerprint}` en los vectores. */
export const IAM_TEMPLATE_FINGERPRINT = createHash("sha256")
  .update(IAM_TEMPLATE_BODY, "utf8")
  .digest("hex");

/** Sustituye `{sdkVersion}`, `{cdkBucket}`, `{defaultBucket}` e `{iamTemplateFingerprint}` del texto de un caso compartido. */
export function withPlaceholders<T>(value: T, input: VectorInput, sdkVersion: string): T {
  const text = JSON.stringify(value)
    .replaceAll("{sdkVersion}", sdkVersion)
    .replaceAll("{iamTemplateFingerprint}", IAM_TEMPLATE_FINGERPRINT)
    .replaceAll("{cdkBucket}", cdkBucketName(input.account, input.region))
    .replaceAll("{defaultBucket}", defaultArtifactBucketName(input.account, input.region));
  return JSON.parse(text) as T;
}

export type Call = readonly [string, ...unknown[]];

const HEAD_PREFIX = "head:";
const ARTIFACT_STACK_READ = "artifactStack";

/** Las lecturas que resuelven el bucket, como las lista `bucketReads` de `plan-vectors.json`. */
export function bucketReads(calls: readonly Call[]): string[] {
  const reads: string[] = [];
  for (const [name, bucket] of calls) {
    if (name === "head_bucket") {
      reads.push(`${HEAD_PREFIX}${String(bucket)}`);
    } else if (name === "describe_artifact_stack") {
      reads.push(ARTIFACT_STACK_READ);
    }
  }
  return reads;
}

export interface FakeBehavior {
  /** `create`/`update` dejan la pila sin la salida `BuildRoleArn`. */
  readonly omitBuildRole?: boolean;
  /** `create`/`wait` de la pila del bucket la dejan sin la salida `BucketName`. */
  readonly omitBucketName?: boolean;
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
  /** Las etiquetas recibidas por cada llamada que crea o actualiza una pila, por nombre de llamada. */
  readonly tagsByCall = new Map<string, Readonly<Record<string, string>>>();
  readonly iamStack: IamStackGateway;
  readonly artifactStack: ArtifactStackGateway;
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
      tags: input.stack?.tags ?? {},
    };
    const record = (...call: Call): void => {
      this.calls.push(call);
    };
    const settled = (parameters: Readonly<Record<string, string>>): StackObservation => {
      stack = {
        name: IAM_STACK_NAME,
        status: "CREATE_COMPLETE",
        outputs: behavior.omitBuildRole === true ? {} : { BuildRoleArn: BUILD_ROLE_ARN },
        parameters,
        tags: stack.tags ?? {},
      };
      return stack;
    };
    const deployed = (bucket: string): StackObservation =>
      settled({
        ...stack.parameters,
        ArtifactBucket: bucket,
        [TEMPLATE_FINGERPRINT_PARAMETER]: IAM_TEMPLATE_FINGERPRINT,
      });
    this.iamStack = {
      observe: async () => {
        record("describe");
        return stack;
      },
      templateFingerprint: async () => IAM_TEMPLATE_FINGERPRINT,
      create: async (bucket, tags) => {
        record("create", bucket);
        this.tagsByCall.set("create", tags);
        record("wait");
        return deployed(bucket);
      },
      updateBucket: async (bucket, tags) => {
        record("update", bucket);
        this.tagsByCall.set("update", tags);
        record("wait");
        return deployed(bucket);
      },
      wait: async () => {
        record("wait");
        return settled(stack.parameters);
      },
    };
    let bucketStack: StackObservation = {
      name: ARTIFACT_STACK_NAME,
      status: input.artifactStack?.status,
      outputs: input.artifactStack?.outputs ?? {},
      parameters: input.artifactStack?.parameters ?? {},
    };
    const bucketDeployed = (bucket: string): StackObservation => {
      bucketStack = {
        name: ARTIFACT_STACK_NAME,
        status: "CREATE_COMPLETE",
        outputs: behavior.omitBucketName === true ? {} : { [BUCKET_NAME_OUTPUT_KEY]: bucket },
        parameters: { [BUCKET_NAME_PARAMETER]: bucket },
      };
      return bucketStack;
    };
    this.artifactStack = {
      observe: async () => {
        record("describe_artifact_stack");
        return bucketStack;
      },
      create: async (bucket, tags) => {
        record("create_artifact_stack", bucket);
        this.tagsByCall.set("create_artifact_stack", tags);
        record("wait_artifact_stack");
        return bucketDeployed(bucket);
      },
      wait: async () => {
        record("wait_artifact_stack");
        return bucketDeployed(bucketStack.parameters[BUCKET_NAME_PARAMETER] ?? "");
      },
    };
    this.buckets = {
      accountId: async () => {
        record("accountId");
        return input.account;
      },
      probe: async (bucket): Promise<BucketProbe> => {
        record("head_bucket", bucket);
        if (input.forbiddenBuckets.includes(bucket)) {
          return { state: "forbidden", region: undefined };
        }
        const region = input.buckets[bucket];
        return region === undefined
          ? { state: "missing", region: undefined }
          : { state: "found", region };
      },
    };
    this.images = {
      observe: async (name): Promise<ImageObservation> => {
        const caps = name === CAPS_IMAGE_NAME;
        const suffix = caps ? CAPS_CALL_SUFFIX : "";
        const observed = caps ? capsObservation(input) : input.image;
        record(`get_image${suffix}`);
        const imageArn = `arn:aws:lambda:${input.region}:${input.account}:microvm-image:${name}`;
        if (observed === null) {
          return { name, imageArn, state: undefined, versions: [] };
        }
        record(`list_versions${suffix}`);
        return {
          name,
          imageArn,
          state: observed.state,
          versions: observed.versions.map((item) => ({
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
        const imageArn = `arn:aws:lambda:${input.region}:${input.account}:microvm-image:${options.name}`;
        if (options.name === CAPS_IMAGE_NAME) {
          record(
            `publish${CAPS_CALL_SUFFIX}`,
            artifact,
            options.bucket,
            options.buildRoleArn,
            options.osCapabilities,
          );
          return {
            imageArn,
            imageVersion: PUBLISHED_VERSION,
            artifactUploaded: behavior.publishedArtifactUploaded ?? true,
          };
        }
        record("publish", artifact, options.bucket, options.buildRoleArn);
        return {
          imageArn,
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
      ["create", "update", "create_artifact_stack", "publish", "publish:caps", "download"].includes(
        name,
      ),
    );
  }
}
