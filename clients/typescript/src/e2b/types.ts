/**
 * Los tipos con forma de E2B JS 2.51 que el shim acepta y devuelve. Los
 * nativos equivalentes viven en `models.ts`; aquí sólo cambian los nombres y
 * las unidades que E2B fija (`memoryMB`, `endAt`, `state: "running"`).
 */

import type { EgressProxyInput, NetworkSelector } from "../models.js";
import type { LoggingOption, PortLike } from "../sandbox/launch.js";
import type { ListOrder } from "../sandbox/listing.js";
import type { ConnectionOpts } from "./connection.js";

export type SandboxState = "running" | "paused";

export type SandboxOnResume = "restore" | "reboot";

export type SandboxOnTimeout =
  | "pause"
  | "kill"
  | { readonly action: "pause"; readonly keepMemory?: boolean | undefined }
  | { readonly action: "kill" };

export interface SandboxLifecycle {
  readonly onTimeout: SandboxOnTimeout;
  readonly autoResume?: boolean | undefined;
}

export interface SandboxInfoLifecycle {
  readonly onTimeout: "pause" | "kill";
  readonly autoResume: boolean;
}

export type SandboxNetworkSelector = NetworkSelector;

/**
 * `network` de `create`: `allowOut`, `denyOut` y `egressProxy` se aplican en
 * el guest; `allowPublicTraffic: false` es el comportamiento permanente y
 * `httpsPorts: []` se acepta. `rules`, `maskRequestHost`,
 * `allowPublicTraffic: true` y un `httpsPorts` no vacío (medición QE2
 * pendiente) son `UnimplementedError`.
 */
export interface SandboxNetworkOpts {
  readonly allowOut?: SandboxNetworkSelector | undefined;
  readonly denyOut?: SandboxNetworkSelector | undefined;
  readonly rules?: unknown;
  readonly egressProxy?: EgressProxyInput | undefined;
  readonly allowPublicTraffic?: boolean | undefined;
  readonly maskRequestHost?: string | undefined;
  readonly httpsPorts?: readonly number[] | undefined;
}

export interface SandboxNetworkUpdate {
  readonly allowOut?: SandboxNetworkSelector | undefined;
  readonly denyOut?: SandboxNetworkSelector | undefined;
  readonly rules?: unknown;
  readonly egressProxy?: EgressProxyInput | undefined;
  readonly allowInternetAccess?: boolean | undefined;
}

export interface SandboxNetworkInfo {
  readonly allowOut: readonly string[];
  readonly denyOut: readonly string[];
}

export interface SandboxOpts extends ConnectionOpts {
  readonly template?: string | undefined;
  readonly metadata?: Readonly<Record<string, string>> | undefined;
  readonly envs?: Readonly<Record<string, string>> | undefined;
  /** El plazo lógico del sandbox (300 000 por defecto, como E2B). */
  readonly timeoutMs?: number | undefined;
  readonly secure?: boolean | undefined;
  readonly allowInternetAccess?: boolean | undefined;
  readonly mcp?: unknown;
  readonly network?: SandboxNetworkOpts | undefined;
  readonly iam?: unknown;
  readonly volumeMounts?: unknown;
  readonly lifecycle?: SandboxLifecycle | undefined;
  /** El tope de la plataforma; por defecto `max(3 600 000, min(timeoutMs + 60 000, 28 800 000))`. */
  readonly maxLifetimeMs?: number | undefined;
  readonly templateVersion?: string | undefined;
  readonly executionRoleArn?: string | undefined;
  readonly allowedPorts?: readonly PortLike[] | undefined;
  readonly ingress?: readonly string[] | undefined;
  readonly logging?: LoggingOption | undefined;
  readonly readyTimeoutMs?: number | undefined;
  readonly reconnectTimeoutMs?: number | undefined;
  readonly keepOnFailure?: boolean | undefined;
}

export interface SandboxConnectOpts extends ConnectionOpts {
  readonly timeoutMs?: number | undefined;
  readonly onResume?: SandboxOnResume | undefined;
  readonly readyTimeoutMs?: number | undefined;
  readonly reconnectTimeoutMs?: number | undefined;
}

export interface SandboxPauseOpts extends ConnectionOpts {
  readonly keepMemory?: boolean | undefined;
}

export interface SandboxMetricsOpts extends ConnectionOpts {
  readonly start?: Date | undefined;
  readonly end?: Date | undefined;
}

export interface SandboxListOpts extends ConnectionOpts {
  readonly query?:
    | {
        readonly metadata?: Readonly<Record<string, string>> | undefined;
        readonly state?: readonly SandboxState[] | undefined;
        readonly startedAfter?: Date | undefined;
        readonly template?: string | undefined;
      }
    | undefined;
  readonly order?: ListOrder | undefined;
  readonly limit?: number | undefined;
  readonly nextToken?: string | undefined;
}

export interface SandboxUrlOpts {
  readonly user?: string | undefined;
  readonly useSignatureExpiration?: number | undefined;
}

/** `SandboxInfo` de E2B 2.51; lo que Rayito no sabe queda `undefined` y `volumeMounts` siempre vacío. */
export interface SandboxInfo {
  readonly sandboxId: string;
  readonly templateId: string;
  readonly name: string | undefined;
  readonly metadata: Readonly<Record<string, string>>;
  readonly startedAt: Date;
  readonly endAt: Date | undefined;
  readonly state: SandboxState;
  readonly cpuCount: number | undefined;
  readonly memoryMB: number | undefined;
  readonly envdVersion: string | undefined;
  readonly allowInternetAccess: boolean | undefined;
  readonly network: SandboxNetworkInfo | undefined;
  readonly lifecycle: SandboxInfoLifecycle | undefined;
  readonly volumeMounts: readonly { readonly name: string; readonly path: string }[];
  readonly sandboxDomain: string | undefined;
}

export interface SandboxMetrics {
  readonly timestamp: Date;
  readonly cpuUsedPct: number;
  readonly cpuCount: number;
  readonly memUsed: number;
  readonly memTotal: number;
  readonly memCache: number;
  readonly diskUsed: number;
  readonly diskTotal: number;
}
