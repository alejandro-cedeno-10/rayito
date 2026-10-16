/**
 * Puertos de `rayito doctor`: lo que las diez comprobaciones leen de AWS y
 * del agente. `aws.ts` los cumple con el SDK de AWS v3 y los tests con dobles
 * en memoria. El adaptador traduce cada fallo técnico a `AwsOperationError`,
 * así que ningún tipo de smithy cruza hacia las comprobaciones.
 */

import type { ControlPlane } from "../../aws/control-plane.js";
import type { AppliedQuota } from "../../dashboard/domain.js";
import type { SandboxInfo } from "../../models.js";
import type { EfsNetworkReport } from "../../volumes/network.js";

/** Un fallo de una llamada de AWS: la operación rechazada, su código y el mensaje ya redactado. */
export class AwsOperationError extends Error {
  readonly operation: string;
  readonly code: string;

  constructor(operation: string, code: string, message: string) {
    super(message);
    this.name = "AwsOperationError";
    this.operation = operation;
    this.code = code;
  }
}

/** Falta un peer opcional: la comprobación que lo necesita no es un fallo de la cuenta. */
export class MissingPeerError extends Error {
  constructor(message: string, cause: unknown) {
    super(message, { cause });
    this.name = "MissingPeerError";
  }
}

export interface CallerIdentity {
  readonly account: string;
  readonly arn: string;
}

export interface ManagedImageVersion {
  readonly imageVersion: string;
  readonly createdAt: Date;
}

export interface ImageDetail {
  readonly state: string;
  readonly latestActiveVersion: string | undefined;
  readonly latestFailedVersion: string | undefined;
}

export interface ImageVersionDetail {
  readonly state: string;
  readonly status: string;
  readonly stateReason: string | undefined;
}

/** El veredicto del simulador de IAM sobre una acción y un recurso. */
export interface PolicyDecision {
  readonly action: string;
  readonly resource: string;
  readonly decision: string;
  readonly allowedByOrganizations: boolean | undefined;
}

export interface BucketFacts {
  /** `x-amz-bucket-region`; vacío si `HeadBucket` no la trajo. */
  readonly region: string;
}

/** Lo que las comprobaciones 8 a 10 leen del `Health` de `rayd`. */
export interface AgentHealth {
  readonly agentReady: boolean;
  readonly kernelReady: boolean;
  readonly agentVersion: string;
  readonly imdsBlocked: boolean;
  readonly hookAnomalies: number;
  readonly resumeGeneration: number;
  readonly uptimeMs: number;
}

export interface LaunchRequest {
  readonly template: string;
  readonly templateVersion: string | undefined;
  readonly timeoutMs: number;
  readonly metadata: Readonly<Record<string, string>>;
}

/** El sandbox efímero de `--launch`: se mata siempre al terminar. */
export interface LaunchedSandbox {
  readonly sandboxId: string;
  readonly info: SandboxInfo;
  kill(): Promise<void>;
}

export interface DoctorAws {
  readonly region: string;
  /** Resolución de nombres, listados y tokens del SDK (ya traduce sus errores). */
  readonly plane: ControlPlane;
  callerIdentity(): Promise<CallerIdentity>;
  /** `iam:GetRole`: el ARN con path del rol de una sesión asumida. */
  roleArn(roleName: string): Promise<string>;
  simulatePrincipalPolicy(
    sourceArn: string,
    actions: readonly string[],
    resources: readonly string[],
  ): Promise<PolicyDecision[]>;
  listManagedImageArns(): Promise<string[]>;
  listManagedImageVersions(imageArn: string): Promise<ManagedImageVersion[]>;
  appliedQuotas(): Promise<AppliedQuota[]>;
  headBucket(bucket: string): Promise<BucketFacts>;
  /** `undefined` si la imagen no existe; cualquier otro fallo sube como `AwsOperationError`. */
  getImage(imageArn: string): Promise<ImageDetail | undefined>;
  getImageVersion(imageArn: string, imageVersion: string): Promise<ImageVersionDetail>;
  latestBuild(imageArn: string, imageVersion: string): Promise<Record<string, unknown>>;
  inspectEfsNetwork(vpcId: string, subnetIds: string): Promise<EfsNetworkReport>;
  launch(request: LaunchRequest): Promise<LaunchedSandbox>;
  /** Un `Health` anónimo; `undefined` si el sandbox no está `RUNNING`. */
  probeHealth(info: SandboxInfo, mintedToken: string | undefined): Promise<AgentHealth | undefined>;
}
