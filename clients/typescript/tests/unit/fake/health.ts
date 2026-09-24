/**
 * `HealthService` como lo implementa `rayd`. `failNext` lanza, por orden, los
 * `ConnectError` encolados (las formas de reset con las que Connect-ES presenta
 * una sesión HTTP/2 caída); `unavailableCalls` responde `Unavailable` las
 * primeras N veces (502 del proxy mientras se restaura el snapshot);
 * `notReadyCalls` responde `agentReady=false` las siguientes N y
 * `kernelNotReadyCalls` `kernelReady=false`; `beforeRunCalls` responde las
 * siguientes N como `rayd` antes de `/run` (el proxy deja pasar `Health`
 * durante la restauración): agente y kernel del snapshot listos, sin
 * `sandboxId`. `Metrics` y `MetricsHistory` exigen `x-access-token`.
 */

import { create } from "@bufbuild/protobuf";
import { Code, ConnectError, type HandlerContext } from "@connectrpc/connect";
import {
  type HealthResponse,
  HealthResponseSchema,
  type MetricsHistoryRequest,
  type MetricsHistoryResponse,
  MetricsHistoryResponseSchema,
  type MetricsResponse,
  MetricsResponseSchema,
} from "../../../src/gen/rayito/v1/health_pb.js";
import type { LifecycleState } from "../../../src/gen/rayito/v1/lifecycle_pb.js";
import { EgressEnforcement } from "../../../src/gen/rayito/v1/network_pb.js";
import { assertProxyHeaders, type HeaderMap, headerMap, requireAccessToken } from "./common.js";

export const SANDBOX_ID = "microvm-00000000-0000-0000-0000-000000000001";
export const METRICS_TIMESTAMP_UNIX_MS = 1_789_000_000_123;

export class FakeHealth {
  readonly tokenSha256: string;
  sandboxId = SANDBOX_ID;
  readonly failNext: ConnectError[] = [];
  unavailableCalls = 0;
  notReadyCalls = 0;
  kernelNotReadyCalls = 0;
  beforeRunCalls = 0;
  readonly healthCalls: HeaderMap[] = [];
  readonly metricsCalls: HeaderMap[] = [];
  resumeGeneration = 0;
  clockOffsetMs = 0;
  kernelStateLost = false;
  uptimeMs = 12_345;
  egressEnforcement: EgressEnforcement = EgressEnforcement.NONE;
  /** `undefined` es un agente anterior a M9; `FakeLifecycleService.setTimeout` lo mueve. */
  lifecycle: LifecycleState | undefined = undefined;
  /** Vista del guest en `Health`; 0 como un agente anterior a M9. */
  cpuCount = 0;
  memoryTotalBytes = 0n;
  /** Los metadatos del `runHookPayload` que `Health` devuelve (M6). */
  metadata: Record<string, string> = {};
  memCacheBytes = 0n;
  /** Lo que `MetricsHistory` devuelve tal cual; `historyUnimplemented` imita a un `rayd` anterior a M9. */
  history: MetricsResponse[] = [];
  historyUnimplemented = false;
  readonly historyRequests: MetricsHistoryRequest[] = [];
  readonly historyCalls: HeaderMap[] = [];

  constructor(tokenSha256: string) {
    this.tokenSha256 = tokenSha256;
  }

  health(_request: unknown, context: HandlerContext): HealthResponse {
    const headers = headerMap(context);
    assertProxyHeaders(headers);
    this.healthCalls.push(headers);
    const scripted = this.failNext.shift();
    if (scripted !== undefined) {
      throw scripted;
    }
    if (this.unavailableCalls > 0) {
      this.unavailableCalls -= 1;
      throw new ConnectError("snapshot restoring", Code.Unavailable);
    }
    const ready = this.notReadyCalls <= 0;
    if (!ready) {
      this.notReadyCalls -= 1;
    }
    const kernelReady = ready && this.kernelNotReadyCalls <= 0;
    if (ready && !kernelReady) {
      this.kernelNotReadyCalls -= 1;
    }
    const beforeRun = kernelReady && this.beforeRunCalls > 0;
    if (beforeRun) {
      this.beforeRunCalls -= 1;
    }
    return create(HealthResponseSchema, {
      agentReady: ready,
      kernelReady,
      agentVersion: "test",
      uptimeMs: BigInt(this.uptimeMs),
      sandboxId: beforeRun ? "" : this.sandboxId,
      resumeGeneration: BigInt(this.resumeGeneration),
      clockOffsetMs: BigInt(this.clockOffsetMs),
      kernelStateLost: this.kernelStateLost,
      egressEnforcement: this.egressEnforcement,
      ...(this.lifecycle === undefined ? {} : { lifecycle: this.lifecycle }),
      cpuCount: this.cpuCount,
      memoryTotalBytes: this.memoryTotalBytes,
      metadata: this.metadata,
    });
  }

  /** Como `rayd`: la capa del token va antes del enrutado, así que un agente viejo con token válido da `Unimplemented`. */
  metricsHistory(request: MetricsHistoryRequest, context: HandlerContext): MetricsHistoryResponse {
    const headers = headerMap(context);
    assertProxyHeaders(headers);
    this.historyCalls.push(headers);
    requireAccessToken(headers, this.tokenSha256);
    if (this.historyUnimplemented) {
      throw new ConnectError("unknown method MetricsHistory", Code.Unimplemented);
    }
    this.historyRequests.push(request);
    return create(MetricsHistoryResponseSchema, {
      samples: this.history,
      oldestUnixMs: this.history[0]?.timestampUnixMs ?? 0n,
    });
  }

  metrics(_request: unknown, context: HandlerContext): MetricsResponse {
    const headers = headerMap(context);
    assertProxyHeaders(headers);
    this.metricsCalls.push(headers);
    requireAccessToken(headers, this.tokenSha256);
    return create(MetricsResponseSchema, {
      cpuUsedPct: 12.5,
      memUsedBytes: BigInt(512 * 1024 * 1024),
      memTotalBytes: BigInt(2 * 1024 * 1024 * 1024),
      diskUsedBytes: BigInt(1_000_000),
      diskTotalBytes: BigInt(8_000_000),
      cpuCount: 1,
      timestampUnixMs: BigInt(METRICS_TIMESTAMP_UNIX_MS),
      memCacheBytes: this.memCacheBytes,
    });
  }
}
