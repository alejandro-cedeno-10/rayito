/**
 * Adaptadores de AWS de `--target agentcore` para el build de la imagen y su
 * limpieza (ADR-031, D2 y D8): CodeBuild (`StartBuild`, `BatchGetBuilds`), ECR
 * (`DescribeImages`), S3 (el zip del contexto), CloudWatch Logs
 * (`DeleteLogGroup`) y STS (`GetCallerIdentity`). Cada cliente es un peer
 * opcional que se importa en su primera llamada. Todo parámetro aparece en
 * `AWS_API_NOTES.md`, «Despliegue en AgentCore Runtime».
 */

import { createHash } from "node:crypto";
import { AwsResolver, BUCKET_MISSING_CODES, discoverBucket } from "../agent/aws-resolve.js";
import { CLOUDWATCH_LOGS_PEER } from "../aws/optional-client.js";
import { AgentCoreError } from "../errors.js";
import {
  type AwsSettings,
  awsFailure,
  codeOf,
  LazyClient,
  loadClient,
  NOT_FOUND_CODE,
} from "./aws-support.js";
import type { ImageObservation } from "./models.js";
import {
  type AccountIdentity,
  type BucketDiscovery,
  type BuildObservation,
  type BuildRequest,
  type ImageBuilder,
  type ImageRegistry,
  MissingResourceError,
  type RuntimeLogs,
  type SourceStore,
} from "./ports.js";

const CODEBUILD_PEER = "@aws-sdk/client-codebuild";
const ECR_PEER = "@aws-sdk/client-ecr";
const S3_PEER = "@aws-sdk/client-s3";
const STS_PEER = "@aws-sdk/client-sts";
const FEATURE = "rayito agent deploy --target agentcore";
const IMAGE_NOT_FOUND_CODE = "ImageNotFoundException";
const REPOSITORY_NOT_FOUND_CODE = "RepositoryNotFoundException";
const ACCOUNT_LIMIT_CODE = "AccountLimitExceededException";
const PLAINTEXT_VARIABLE = "PLAINTEXT";
/** Las variables de `StartBuild` que lee el `buildspec` de la plantilla (`constants.buildEnvironmentVariables`). */
export const BUILD_IMAGE_TAG_VARIABLE = "IMAGE_TAG";
export const BUILD_BASE_IMAGE_VARIABLE = "AGENT_BASE_IMAGE";
const CHECKSUM_ALGORITHM = "sha256";

type Command = new (input: object) => unknown;
type Client = new (config: object) => { send(command: unknown): Promise<unknown> };

interface CodeBuildModule {
  readonly CodeBuildClient: Client;
  readonly StartBuildCommand: Command;
  readonly BatchGetBuildsCommand: Command;
}

interface EcrModule {
  readonly ECRClient: Client;
  readonly DescribeImagesCommand: Command;
}

interface S3Module {
  readonly S3Client: Client;
  readonly PutObjectCommand: Command;
  readonly DeleteObjectCommand: Command;
  readonly HeadBucketCommand: Command;
}

interface LogsModule {
  readonly CloudWatchLogsClient: Client;
  readonly DeleteLogGroupCommand: Command;
}

interface StsModule {
  readonly STSClient: Client;
  readonly GetCallerIdentityCommand: Command;
}

/** `ImageBuilder` sobre `@aws-sdk/client-codebuild`. */
export class SdkImageBuilder implements ImageBuilder {
  readonly #client: LazyClient<CodeBuildModule>;

  constructor(settings: AwsSettings) {
    this.#client = new LazyClient(() =>
      loadClient<CodeBuildModule>(CODEBUILD_PEER, FEATURE, (sdk) => sdk.CodeBuildClient, settings),
    );
  }

  async startBuild(request: BuildRequest): Promise<{ readonly buildId: string }> {
    const { sdk, send } = await this.#client.get();
    try {
      const output = await send<{ build?: { id?: string } }>(
        new sdk.StartBuildCommand({
          projectName: request.projectName,
          sourceLocationOverride: request.sourceLocation,
          environmentVariablesOverride: [
            { name: BUILD_IMAGE_TAG_VARIABLE, value: request.imageTag, type: PLAINTEXT_VARIABLE },
            { name: BUILD_BASE_IMAGE_VARIABLE, value: request.baseImage, type: PLAINTEXT_VARIABLE },
          ],
          idempotencyToken: request.imageTag,
        }),
      );
      return { buildId: output.build?.id ?? "" };
    } catch (error) {
      const code = codeOf(error);
      if (code === ACCOUNT_LIMIT_CODE) {
        throw new AgentCoreError("build_quota");
      }
      if (code === NOT_FOUND_CODE) {
        throw new MissingResourceError("buildProject");
      }
      throw awsFailure("StartBuild", error);
    }
  }

  async getBuild(buildId: string): Promise<BuildObservation> {
    const { sdk, send } = await this.#client.get();
    try {
      const output = await send<{
        builds?: ReadonlyArray<{
          buildStatus?: string;
          currentPhase?: string;
          logs?: { groupName?: string };
        }>;
      }>(new sdk.BatchGetBuildsCommand({ ids: [buildId] }));
      const [build] = output.builds ?? [];
      return {
        status: build?.buildStatus ?? "",
        phase: build?.currentPhase,
        logGroup: build?.logs?.groupName,
      };
    } catch (error) {
      throw awsFailure("BatchGetBuilds", error);
    }
  }
}

/** `ImageRegistry` sobre `@aws-sdk/client-ecr`. */
export class SdkImageRegistry implements ImageRegistry {
  readonly #client: LazyClient<EcrModule>;

  constructor(settings: AwsSettings) {
    this.#client = new LazyClient(() =>
      loadClient<EcrModule>(ECR_PEER, FEATURE, (sdk) => sdk.ECRClient, settings),
    );
  }

  async describeImage(repositoryName: string, imageTag: string): Promise<ImageObservation> {
    const { sdk, send } = await this.#client.get();
    try {
      const output = await send<{ imageDetails?: ReadonlyArray<{ imageSizeInBytes?: number }> }>(
        new sdk.DescribeImagesCommand({ repositoryName, imageIds: [{ imageTag }] }),
      );
      const [image] = output.imageDetails ?? [];
      return { exists: image !== undefined, sizeBytes: image?.imageSizeInBytes };
    } catch (error) {
      const code = codeOf(error);
      if (code === IMAGE_NOT_FOUND_CODE) {
        return { exists: false, sizeBytes: undefined };
      }
      if (code === REPOSITORY_NOT_FOUND_CODE) {
        throw new MissingResourceError("repository");
      }
      throw awsFailure("DescribeImages", error);
    }
  }
}

/** S3 contesta 301 a un `HeadBucket` de un bucket de otra región y dice cuál en `x-amz-bucket-region`. */
const REDIRECT_STATUS = 301;
const BUCKET_REGION_HEADER = "x-amz-bucket-region";

const METADATA_KEY = "$metadata";
const RESPONSE_KEY = "$response";

function regionFromRedirect(error: unknown): string | undefined {
  const failure = error as Record<string, unknown>;
  const metadata = failure[METADATA_KEY] as { httpStatusCode?: number } | undefined;
  const response = failure[RESPONSE_KEY] as { headers?: Record<string, string> } | undefined;
  return metadata?.httpStatusCode === REDIRECT_STATUS
    ? response?.headers?.[BUCKET_REGION_HEADER]
    : undefined;
}

function checksumOf(body: Uint8Array): string {
  return createHash(CHECKSUM_ALGORITHM).update(body).digest("base64");
}

/** `SourceStore` sobre `@aws-sdk/client-s3`: el zip del contexto de build y la región del bucket. */
export class SdkSourceStore implements SourceStore {
  readonly #client: LazyClient<S3Module>;

  constructor(settings: AwsSettings) {
    this.#client = new LazyClient(() =>
      loadClient<S3Module>(S3_PEER, FEATURE, (sdk) => sdk.S3Client, settings),
    );
  }

  async putSource(bucket: string, key: string, body: Uint8Array, owner: string): Promise<void> {
    const { sdk, send } = await this.#client.get();
    try {
      await send(
        new sdk.PutObjectCommand({
          Bucket: bucket,
          Key: key,
          ExpectedBucketOwner: owner,
          Body: body,
          ChecksumSHA256: checksumOf(body),
        }),
      );
    } catch (error) {
      throw awsFailure("PutObject", error);
    }
  }

  async deleteSource(bucket: string, key: string, owner: string): Promise<void> {
    const { sdk, send } = await this.#client.get();
    try {
      await send(
        new sdk.DeleteObjectCommand({ Bucket: bucket, Key: key, ExpectedBucketOwner: owner }),
      );
    } catch (error) {
      throw awsFailure("DeleteObject", error);
    }
  }

  async bucketRegion(bucket: string): Promise<string | undefined> {
    const { sdk, send } = await this.#client.get();
    try {
      const output = await send<{ BucketRegion?: string }>(
        new sdk.HeadBucketCommand({ Bucket: bucket }),
      );
      return output.BucketRegion;
    } catch (error) {
      const redirected = regionFromRedirect(error);
      if (redirected !== undefined) {
        return redirected;
      }
      const code = codeOf(error);
      if (code !== undefined && BUCKET_MISSING_CODES.has(code)) {
        return undefined;
      }
      throw awsFailure("HeadBucket", error);
    }
  }
}

/** El bucket de artefactos de la cuenta: el de CDK o el de `rayito setup`, con el mismo descubrimiento que el despliegue de MicroVMs. */
export class SdkBucketDiscovery implements BucketDiscovery {
  readonly #resolver: AwsResolver;

  constructor(settings: AwsSettings) {
    this.#resolver = new AwsResolver({
      region: settings.region,
      ...(settings.credentials === undefined ? {} : { credentials: settings.credentials }),
    });
  }

  discover(region: string): Promise<string | undefined> {
    return discoverBucket(this.#resolver, region);
  }
}

/** `RuntimeLogs` sobre `@aws-sdk/client-cloudwatch-logs`. */
export class SdkRuntimeLogs implements RuntimeLogs {
  readonly #client: LazyClient<LogsModule>;

  constructor(settings: AwsSettings) {
    this.#client = new LazyClient(() =>
      loadClient<LogsModule>(
        CLOUDWATCH_LOGS_PEER,
        FEATURE,
        (sdk) => sdk.CloudWatchLogsClient,
        settings,
      ),
    );
  }

  async deleteLogGroup(logGroupName: string): Promise<boolean> {
    const { sdk, send } = await this.#client.get();
    try {
      await send(new sdk.DeleteLogGroupCommand({ logGroupName }));
      return true;
    } catch (error) {
      if (codeOf(error) === NOT_FOUND_CODE) {
        return false;
      }
      throw awsFailure("DeleteLogGroup", error);
    }
  }
}

/** `AccountIdentity` sobre `@aws-sdk/client-sts`; la cuenta se lee una vez. */
export class SdkAccountIdentity implements AccountIdentity {
  readonly #client: LazyClient<StsModule>;
  #account: Promise<string> | undefined;

  constructor(settings: AwsSettings) {
    this.#client = new LazyClient(() =>
      loadClient<StsModule>(STS_PEER, FEATURE, (sdk) => sdk.STSClient, settings),
    );
  }

  accountId(): Promise<string> {
    this.#account ??= this.#read().catch((error: unknown) => {
      this.#account = undefined;
      throw error;
    });
    return this.#account;
  }

  async #read(): Promise<string> {
    const { sdk, send } = await this.#client.get();
    try {
      const output = await send<{ Account?: string }>(new sdk.GetCallerIdentityCommand({}));
      return output.Account ?? "";
    } catch (error) {
      throw awsFailure("GetCallerIdentity", error);
    }
  }
}
