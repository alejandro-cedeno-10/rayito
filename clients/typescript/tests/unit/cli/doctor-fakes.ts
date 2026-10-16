/**
 * Un `DoctorAws` en memoria: cada lectura de AWS es un valor (o un `Error`
 * que se lanza), y las llamadas quedan registradas. El plano de control es el
 * de los casos de la CLI, con la acuñación de tokens programable.
 */

import type { PortSpec } from "../../../src/aws/control-plane.js";
import type {
  AgentHealth,
  BucketFacts,
  CallerIdentity,
  DoctorAws,
  ImageDetail,
  ImageVersionDetail,
  LaunchedSandbox,
  LaunchRequest,
  ManagedImageVersion,
  PolicyDecision,
} from "../../../src/cli/doctor/ports.js";
import type { AppliedQuota } from "../../../src/dashboard/domain.js";
import { quotaDefault } from "../../../src/dashboard/domain.js";
import { baseImageArn } from "../../../src/images/managed.js";
import { QUOTA_NAMES } from "../../../src/limits.js";
import type { SandboxInfo } from "../../../src/models.js";
import { COMPONENT as EFS_VOLUMES_COMPONENT } from "../../../src/stacks/components/efs-volumes.js";
import { VERSION } from "../../../src/version.js";
import {
  type EfsNetworkReport,
  inspectNetwork,
  type NetworkInspector,
} from "../../../src/volumes/network.js";
import type { CaseWorld } from "./case-world.js";
import { ACCOUNT_ID, CasePlane, REGION } from "./case-world.js";

export const USER_ARN = `arn:aws:iam::${ACCOUNT_ID}:user/maintainer`;
export const ROLE_SESSION_ARN = `arn:aws:sts::${ACCOUNT_ID}:assumed-role/AWSReservedSSO_Admin_abc/user`;
export const ROLE_ARN = `arn:aws:iam::${ACCOUNT_ID}:role/aws-reserved/sso.amazonaws.com/AWSReservedSSO_Admin_abc`;
export const BUCKET = "artifacts";
export const JWE = "eyJhbGciOiJkaXIiLCJlbmMiOiJBMjU2R0NNIn0..fake.jwe";
const FIRST_MANAGED_VERSION = new Date("2026-09-01T00:00:00Z");
const SECOND_MANAGED_VERSION = new Date("2026-09-02T00:00:00Z");

export type Scripted<T> = T | Error;

function settle<T>(value: Scripted<T>): Promise<T> {
  return value instanceof Error ? Promise.reject(value) : Promise.resolve(value);
}

/** El plano de los casos con la acuñación de tokens registrada y programable. */
export class DoctorPlane extends CasePlane {
  readonly tokens: Array<{ sandboxId: string; ports: readonly PortSpec[] }> = [];
  tokenFailure: Error | undefined;

  constructor(world: CaseWorld) {
    super(world);
  }

  override async createAuthToken(sandboxId: string, ports: readonly PortSpec[]): Promise<string> {
    if (this.tokenFailure !== undefined) {
      throw this.tokenFailure;
    }
    this.tokens.push({ sandboxId, ports });
    return JWE;
  }
}

export function healthy(overrides: Partial<AgentHealth> = {}): AgentHealth {
  return {
    agentReady: true,
    kernelReady: true,
    agentVersion: VERSION,
    imdsBlocked: false,
    hookAnomalies: 0,
    resumeGeneration: 0,
    uptimeMs: 1234,
    ...overrides,
  };
}

export function defaultQuotas(overrides: Readonly<Record<string, number>> = {}): AppliedQuota[] {
  return Object.keys(QUOTA_NAMES).map((code) => ({
    code,
    name: `microvm ${code}`,
    value: overrides[code] ?? quotaDefault(code, REGION),
    adjustable: true,
  }));
}

export class FakeLaunched implements LaunchedSandbox {
  killed = 0;
  killFailure: Error | undefined;

  constructor(
    readonly sandboxId: string,
    readonly info: SandboxInfo,
  ) {}

  async kill(): Promise<void> {
    this.killed += 1;
    if (this.killFailure !== undefined) {
      throw this.killFailure;
    }
  }
}

export class FakeDoctorAws implements DoctorAws {
  region = REGION;
  readonly plane: DoctorPlane;
  readonly calls: string[] = [];
  readonly probes: Array<{ sandboxId: string; token: string | undefined }> = [];
  readonly launches: LaunchRequest[] = [];
  identity: Scripted<CallerIdentity> = { account: ACCOUNT_ID, arn: USER_ARN };
  roleArns: Record<string, Scripted<string>> = {};
  decisions: Record<string, string> = {};
  scpDenied: ReadonlySet<string> = new Set();
  simulationFailure: Error | undefined;
  managedArns: Scripted<string[]> = [baseImageArn(REGION)];
  managedVersions: ManagedImageVersion[] = [
    { imageVersion: "0", createdAt: FIRST_MANAGED_VERSION },
    { imageVersion: "1", createdAt: SECOND_MANAGED_VERSION },
  ];
  quotas: Scripted<AppliedQuota[]> = defaultQuotas();
  bucketFacts: Scripted<BucketFacts> = { region: REGION };
  image: Scripted<ImageDetail | undefined> = {
    state: "UPDATED",
    latestActiveVersion: "3.0",
    latestFailedVersion: undefined,
  };
  imageVersion: Scripted<ImageVersionDetail> = {
    state: "SUCCESSFUL",
    status: "ACTIVE",
    stateReason: "ok",
  };
  build: Record<string, unknown> = {
    chipsetGeneration: "3",
    snapshotBuild: { memorySnapshotSizeInBytes: 900 },
  };
  inspector: NetworkInspector | undefined;
  health: Scripted<AgentHealth | undefined> = healthy();
  launchResult: Scripted<FakeLaunched> | undefined;
  launched: FakeLaunched[] = [];

  constructor(world: CaseWorld = {}) {
    this.plane = new DoctorPlane(world);
  }

  async callerIdentity(): Promise<CallerIdentity> {
    this.calls.push("callerIdentity");
    return settle(this.identity);
  }

  async roleArn(roleName: string): Promise<string> {
    this.calls.push(`roleArn:${roleName}`);
    return settle(this.roleArns[roleName] ?? ROLE_ARN);
  }

  async simulatePrincipalPolicy(
    sourceArn: string,
    actions: readonly string[],
    resources: readonly string[],
  ): Promise<PolicyDecision[]> {
    this.calls.push(`simulate:${sourceArn}:${resources.join(",")}`);
    if (this.simulationFailure !== undefined) {
      throw this.simulationFailure;
    }
    return actions.map((action) => ({
      action,
      resource: resources[0] ?? "*",
      decision: this.decisions[action] ?? "allowed",
      allowedByOrganizations: this.scpDenied.has(action) ? false : undefined,
    }));
  }

  async listManagedImageArns(): Promise<string[]> {
    return settle(this.managedArns);
  }

  async listManagedImageVersions(): Promise<ManagedImageVersion[]> {
    return this.managedVersions;
  }

  async appliedQuotas(): Promise<AppliedQuota[]> {
    return settle(this.quotas);
  }

  async headBucket(bucket: string): Promise<BucketFacts> {
    this.calls.push(`headBucket:${bucket}`);
    return settle(this.bucketFacts);
  }

  async getImage(): Promise<ImageDetail | undefined> {
    return settle(this.image);
  }

  async getImageVersion(): Promise<ImageVersionDetail> {
    return settle(this.imageVersion);
  }

  async latestBuild(): Promise<Record<string, unknown>> {
    return this.build;
  }

  async inspectEfsNetwork(vpcId: string, subnetIds: string): Promise<EfsNetworkReport> {
    this.calls.push("inspectEfsNetwork");
    if (this.inspector === undefined) {
      throw new Error("sin inspector de red en este caso");
    }
    return inspectNetwork(this.inspector, vpcId, subnetIds, EFS_VOLUMES_COMPONENT.cost);
  }

  async launch(request: LaunchRequest): Promise<LaunchedSandbox> {
    this.launches.push(request);
    if (this.launchResult === undefined) {
      throw new Error("sin sandbox de --launch en este caso");
    }
    const launched = await settle(this.launchResult);
    this.launched.push(launched);
    return launched;
  }

  async probeHealth(
    info: SandboxInfo,
    mintedToken: string | undefined,
  ): Promise<AgentHealth | undefined> {
    this.probes.push({ sandboxId: info.sandboxId, token: mintedToken });
    return settle(this.health);
  }
}
