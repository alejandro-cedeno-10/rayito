/**
 * El adaptador de `DoctorAws` sobre el SDK de AWS v3. Cada clave de cliente
 * (STS, IAM, S3, Service Quotas, Lambda MicroVMs) se construye al usarla, y
 * IAM, S3 y Service Quotas se importan como peers en ese momento: `rayito
 * doctor` es la única orden que los necesita. Cada fallo de AWS sale como
 * `AwsOperationError` con el mensaje ya redactado.
 */

import {
  GetMicrovmImageBuildCommand,
  GetMicrovmImageCommand,
  GetMicrovmImageVersionCommand,
  LambdaMicrovmsClient,
  ListManagedMicrovmImagesCommand,
  ListManagedMicrovmImageVersionsCommand,
  ListMicrovmImageBuildsCommand,
} from "@aws-sdk/client-lambda-microvms";
import { GetCallerIdentityCommand, STSClient } from "@aws-sdk/client-sts";
import {
  type AwsClientSettings,
  type ControlPlane,
  clientConfig,
  type PortSpec,
} from "../../aws/control-plane.js";
import { awsCode, isAwsSdkError, loadOptionalSdkClient } from "../../aws/optional-client.js";
import { redactAwsText } from "../../aws/sanitize.js";
import { listMicrovmQuotas, type QuotaPage } from "../../dashboard/aws.js";
import type { AppliedQuota } from "../../dashboard/domain.js";
import { InvalidArgumentError } from "../../errors.js";
import type { SandboxInfo } from "../../models.js";
import { metadataProbeFailure } from "../../sandbox/paginator.js";
import { probeHealth } from "../../sandbox/probe.js";
import { Sandbox } from "../../sandbox/sandbox.js";
import { COMPONENT as EFS_VOLUMES_COMPONENT } from "../../stacks/components/efs-volumes.js";
import { resolveTransportSettings } from "../../transport/transport.js";
import { type EfsNetworkReport, inspectNetwork } from "../../volumes/network.js";
import { Ec2NetworkInspector } from "../../volumes/vpc.js";
import { HEALTH_PROBE_TIMEOUT_MS } from "./constants.js";
import {
  type AgentHealth,
  AwsOperationError,
  type BucketFacts,
  type CallerIdentity,
  type DoctorAws,
  type ImageDetail,
  type ImageVersionDetail,
  type LaunchedSandbox,
  type LaunchRequest,
  type ManagedImageVersion,
  MissingPeerError,
  type PolicyDecision,
} from "./ports.js";

type Credentials = AwsClientSettings["credentials"];

const IAM_PEER = "@aws-sdk/client-iam";
const S3_PEER = "@aws-sdk/client-s3";
const SERVICE_QUOTAS_PEER = "@aws-sdk/client-service-quotas";
const NOT_FOUND_CODE = "ResourceNotFoundException";
const UNKNOWN_CODE = "Unknown";

export async function operationCall<T>(operation: string, call: () => Promise<T>): Promise<T> {
  try {
    return await call();
  } catch (error) {
    if (error instanceof InvalidArgumentError && error.cause !== undefined) {
      throw new MissingPeerError(error.message, error.cause);
    }
    if (!isAwsSdkError(error)) {
      throw error;
    }
    throw new AwsOperationError(
      operation,
      awsCode(error) ?? UNKNOWN_CODE,
      redactAwsText(error.message),
    );
  }
}

interface IamModule {
  readonly IAMClient: new (config: object) => { send(command: unknown): Promise<unknown> };
  readonly GetRoleCommand: new (input: object) => unknown;
  readonly SimulatePrincipalPolicyCommand: new (input: object) => unknown;
}

interface S3Module {
  readonly S3Client: new (config: object) => { send(command: unknown): Promise<unknown> };
  readonly HeadBucketCommand: new (input: object) => unknown;
}

interface QuotasModule {
  readonly ServiceQuotasClient: new (
    config: object,
  ) => { send(command: unknown): Promise<unknown> };
  readonly ListServiceQuotasCommand: new (input: object) => unknown;
}

interface SimulationPage {
  readonly EvaluationResults?: ReadonlyArray<{
    readonly EvalActionName?: string;
    readonly EvalResourceName?: string;
    readonly EvalDecision?: string;
    readonly OrganizationsDecisionDetail?: { readonly AllowedByOrganizations?: boolean };
  }>;
  readonly IsTruncated?: boolean;
  readonly Marker?: string;
}

export interface SdkDoctorAwsOptions {
  readonly region: string;
  readonly credentials: Credentials;
  readonly plane: ControlPlane;
}

class SdkLaunchedSandbox implements LaunchedSandbox {
  readonly sandboxId: string;
  readonly info: SandboxInfo;
  readonly #sandbox: Sandbox;

  constructor(sandbox: Sandbox) {
    this.#sandbox = sandbox;
    this.sandboxId = sandbox.sandboxId;
    this.info = sandbox.info;
  }

  async kill(): Promise<void> {
    try {
      await this.#sandbox.kill();
    } finally {
      this.#sandbox.close();
    }
  }
}

/** El plano cuya primera acuñación devuelve el JWE de la comprobación `token`, sin llamada nueva. */
export function withPreminted(plane: ControlPlane, token: string | undefined): ControlPlane {
  let pending = token;
  return new Proxy(plane, {
    get(target, property) {
      if (property === "createAuthToken") {
        return async (
          sandboxId: string,
          ports: readonly PortSpec[],
          options?: Parameters<ControlPlane["createAuthToken"]>[2],
        ): Promise<string> => {
          if (pending !== undefined) {
            const minted = pending;
            pending = undefined;
            return minted;
          }
          return target.createAuthToken(sandboxId, ports, options);
        };
      }
      const value = Reflect.get(target, property, target) as unknown;
      return typeof value === "function" ? (value as () => unknown).bind(target) : value;
    },
  });
}

export class SdkDoctorAws implements DoctorAws {
  readonly region: string;
  readonly plane: ControlPlane;
  readonly #credentials: Credentials;
  #sts: STSClient | undefined;
  #microvms: LambdaMicrovmsClient | undefined;

  constructor(options: SdkDoctorAwsOptions) {
    this.region = options.region;
    this.plane = options.plane;
    this.#credentials = options.credentials;
  }

  #config(): object {
    return clientConfig(
      this.region,
      this.#credentials === undefined ? {} : { credentials: this.#credentials },
    );
  }

  #microvmsClient(): LambdaMicrovmsClient {
    this.#microvms ??= new LambdaMicrovmsClient(this.#config());
    return this.#microvms;
  }

  async callerIdentity(): Promise<CallerIdentity> {
    this.#sts ??= new STSClient(this.#config());
    const sts = this.#sts;
    const response = await operationCall("GetCallerIdentity", () =>
      sts.send(new GetCallerIdentityCommand({})),
    );
    return { account: String(response.Account), arn: String(response.Arn) };
  }

  async #iam(): Promise<{ sdk: IamModule; send: <T>(command: unknown) => Promise<T> }> {
    return loadOptionalSdkClient<IamModule>(
      IAM_PEER,
      "rayito doctor (simulación de IAM)",
      (sdk) => sdk.IAMClient,
      this.region,
      this.#credentials,
    );
  }

  async roleArn(roleName: string): Promise<string> {
    return operationCall("GetRole", async () => {
      const { sdk, send } = await this.#iam();
      const response = await send<{ Role?: { Arn?: string } }>(
        new sdk.GetRoleCommand({ RoleName: roleName }),
      );
      return String(response.Role?.Arn);
    });
  }

  async simulatePrincipalPolicy(
    sourceArn: string,
    actions: readonly string[],
    resources: readonly string[],
  ): Promise<PolicyDecision[]> {
    return operationCall("SimulatePrincipalPolicy", async () => {
      const { sdk, send } = await this.#iam();
      const decisions: PolicyDecision[] = [];
      let marker: string | undefined;
      do {
        const page: SimulationPage = await send<SimulationPage>(
          new sdk.SimulatePrincipalPolicyCommand({
            PolicySourceArn: sourceArn,
            ActionNames: [...actions],
            ...(resources.length === 0 ? {} : { ResourceArns: [...resources] }),
            ...(marker === undefined ? {} : { Marker: marker }),
          }),
        );
        for (const result of page.EvaluationResults ?? []) {
          decisions.push({
            action: String(result.EvalActionName),
            resource: result.EvalResourceName ?? "*",
            decision: String(result.EvalDecision),
            allowedByOrganizations: result.OrganizationsDecisionDetail?.AllowedByOrganizations,
          });
        }
        marker = page.IsTruncated === true ? page.Marker : undefined;
      } while (marker !== undefined);
      return decisions;
    });
  }

  async listManagedImageArns(): Promise<string[]> {
    return operationCall("ListManagedMicrovmImages", async () => {
      const arns: string[] = [];
      let token: string | undefined;
      do {
        const page = await this.#microvmsClient().send(
          new ListManagedMicrovmImagesCommand(token === undefined ? {} : { nextToken: token }),
        );
        arns.push(...(page.items ?? []).map((item) => String(item.imageArn)));
        token = page.nextToken;
      } while (token !== undefined);
      return arns;
    });
  }

  async listManagedImageVersions(imageArn: string): Promise<ManagedImageVersion[]> {
    return operationCall("ListManagedMicrovmImageVersions", async () => {
      const versions: ManagedImageVersion[] = [];
      let token: string | undefined;
      do {
        const page = await this.#microvmsClient().send(
          new ListManagedMicrovmImageVersionsCommand({
            imageIdentifier: imageArn,
            ...(token === undefined ? {} : { nextToken: token }),
          }),
        );
        for (const item of page.items ?? []) {
          versions.push({
            imageVersion: String(item.imageVersion),
            createdAt: item.createdAt as Date,
          });
        }
        token = page.nextToken;
      } while (token !== undefined);
      return versions;
    });
  }

  async appliedQuotas(): Promise<AppliedQuota[]> {
    return operationCall("ListServiceQuotas", async () => {
      const { sdk, send } = await loadOptionalSdkClient<QuotasModule>(
        SERVICE_QUOTAS_PEER,
        "rayito doctor (cuotas)",
        (module) => module.ServiceQuotasClient,
        this.region,
        this.#credentials,
      );
      return listMicrovmQuotas((input) => send<QuotaPage>(new sdk.ListServiceQuotasCommand(input)));
    });
  }

  async headBucket(bucket: string, expectedOwner?: string): Promise<BucketFacts> {
    return operationCall("HeadBucket", async () => {
      const { sdk, send } = await loadOptionalSdkClient<S3Module>(
        S3_PEER,
        "rayito doctor (bucket de artefactos)",
        (module) => module.S3Client,
        this.region,
        this.#credentials,
      );
      const response = await send<{ BucketRegion?: string }>(
        new sdk.HeadBucketCommand({
          Bucket: bucket,
          ...(expectedOwner === undefined ? {} : { ExpectedBucketOwner: expectedOwner }),
        }),
      );
      return { region: response.BucketRegion ?? "" };
    });
  }

  async getImage(imageArn: string): Promise<ImageDetail | undefined> {
    try {
      const image = await operationCall("GetMicrovmImage", () =>
        this.#microvmsClient().send(new GetMicrovmImageCommand({ imageIdentifier: imageArn })),
      );
      return {
        state: String(image.state),
        latestActiveVersion: image.latestActiveImageVersion,
        latestFailedVersion: image.latestFailedImageVersion,
      };
    } catch (error) {
      if (error instanceof AwsOperationError && error.code === NOT_FOUND_CODE) {
        return undefined;
      }
      throw error;
    }
  }

  async getImageVersion(imageArn: string, imageVersion: string): Promise<ImageVersionDetail> {
    const detail = await operationCall("GetMicrovmImageVersion", () =>
      this.#microvmsClient().send(
        new GetMicrovmImageVersionCommand({ imageIdentifier: imageArn, imageVersion }),
      ),
    );
    return {
      state: String(detail.state),
      status: String(detail.status),
      stateReason: detail.stateReason,
    };
  }

  async latestBuild(imageArn: string, imageVersion: string): Promise<Record<string, unknown>> {
    const builds = await operationCall("ListMicrovmImageBuilds", () =>
      this.#microvmsClient().send(
        new ListMicrovmImageBuildsCommand({ imageIdentifier: imageArn, imageVersion }),
      ),
    );
    const first = builds.items?.[0];
    if (first === undefined) {
      return {};
    }
    const build = await operationCall("GetMicrovmImageBuild", () =>
      this.#microvmsClient().send(
        new GetMicrovmImageBuildCommand({
          imageIdentifier: imageArn,
          imageVersion,
          buildId: first.buildId,
        }),
      ),
    );
    const { $metadata: _metadata, ...fields } = build;
    return fields as Record<string, unknown>;
  }

  async inspectEfsNetwork(vpcId: string, subnetIds: string): Promise<EfsNetworkReport> {
    return operationCall("DescribeVpcs", () =>
      inspectNetwork(
        new Ec2NetworkInspector({ region: this.region, credentials: this.#credentials }),
        vpcId,
        subnetIds,
        EFS_VOLUMES_COMPONENT.cost,
      ),
    );
  }

  async launch(request: LaunchRequest): Promise<LaunchedSandbox> {
    const sandbox = await Sandbox.create({
      controlPlane: this.plane,
      template: request.template,
      templateVersion: request.templateVersion,
      timeoutMs: request.timeoutMs,
      idle: null,
      logging: "disabled",
      metadata: request.metadata,
    });
    return new SdkLaunchedSandbox(sandbox);
  }

  async probeHealth(
    info: SandboxInfo,
    mintedToken: string | undefined,
  ): Promise<AgentHealth | undefined> {
    if (info.state !== "RUNNING") {
      return undefined;
    }
    try {
      const response = await probeHealth(
        withPreminted(this.plane, mintedToken),
        info,
        resolveTransportSettings(undefined),
        HEALTH_PROBE_TIMEOUT_MS,
      );
      return {
        agentReady: response.agentReady,
        kernelReady: response.kernelReady,
        agentVersion: response.agentVersion,
        imdsBlocked: response.imdsBlocked,
        hookAnomalies: Number(response.hookAnomalies),
        resumeGeneration: Number(response.resumeGeneration),
        uptimeMs: Number(response.uptimeMs),
      };
    } catch (error) {
      throw metadataProbeFailure(info.sandboxId, error);
    }
  }
}
