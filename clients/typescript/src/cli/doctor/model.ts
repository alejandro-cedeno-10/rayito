/**
 * El modelo de `rayito doctor`: el resultado de cada comprobación y el
 * contexto que las comprobaciones se pasan unas a otras. Espejo de
 * `rayito.cli._checks`. Ningún campo guarda un JWE, un access token ni un
 * `runHookPayload`.
 */

import type { SandboxInfo } from "../../models.js";
import type { AgentHealth, LaunchedSandbox } from "./ports.js";

export type CheckStatus = "OK" | "WARN" | "FAIL" | "SKIP";

export interface CheckResult {
  readonly name: string;
  readonly status: CheckStatus;
  readonly summary: string;
  readonly details: Record<string, unknown>;
}

/**
 * Lo que las comprobaciones se pasan entre sí. `mintedToken` sólo alimenta la
 * sonda de `Health`: nunca se imprime ni entra en `details`.
 */
export interface DoctorContext {
  template: string;
  templateVersion: string | undefined;
  bucket: string | undefined;
  launch: boolean;
  /** `--efs-vpc-id`: activa la comprobación previa de `EfsVolumes` (sólo lectura). */
  efsVpcId: string | undefined;
  efsSubnetIds: string | undefined;
  account: string | undefined;
  principalArn: string | undefined;
  principalKind: string | undefined;
  templateArn: string | undefined;
  resolvedVersion: string | undefined;
  launched: LaunchedSandbox | undefined;
  targetSandbox: SandboxInfo | undefined;
  mintedToken: string | undefined;
  health: AgentHealth | undefined;
}

export interface DoctorRequest {
  readonly template: string;
  readonly templateVersion?: string | undefined;
  readonly bucket?: string | undefined;
  readonly launch?: boolean | undefined;
  readonly efsVpcId?: string | undefined;
  readonly efsSubnetIds?: string | undefined;
}

export function newDoctorContext(request: DoctorRequest): DoctorContext {
  return {
    template: request.template,
    templateVersion: request.templateVersion,
    bucket: request.bucket,
    launch: request.launch ?? false,
    efsVpcId: request.efsVpcId,
    efsSubnetIds: request.efsSubnetIds,
    account: undefined,
    principalArn: undefined,
    principalKind: undefined,
    templateArn: undefined,
    resolvedVersion: undefined,
    launched: undefined,
    targetSandbox: undefined,
    mintedToken: undefined,
    health: undefined,
  };
}
