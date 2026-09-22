/**
 * `HealthService` como lo implementa `rayd`. `failNext` lanza, por orden, los
 * `ConnectError` encolados (las formas de reset con las que Connect-ES presenta
 * una sesión HTTP/2 caída); `unavailableCalls` responde `Unavailable` las
 * primeras N veces (502 del proxy mientras se restaura el snapshot);
 * `notReadyCalls` responde `agentReady=false` las siguientes N y
 * `kernelNotReadyCalls` `kernelReady=false`. `Metrics` exige `x-access-token`.
 */

import { create } from "@bufbuild/protobuf";
import { Code, ConnectError, type HandlerContext } from "@connectrpc/connect";
import {
  type HealthResponse,
  HealthResponseSchema,
  type MetricsResponse,
  MetricsResponseSchema,
} from "../../../src/gen/rayito/v1/health_pb.js";
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
  readonly healthCalls: HeaderMap[] = [];
  readonly metricsCalls: HeaderMap[] = [];
  resumeGeneration = 0;
  clockOffsetMs = 0;
  kernelStateLost = false;
  uptimeMs = 12_345;

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
    return create(HealthResponseSchema, {
      agentReady: ready,
      kernelReady,
      agentVersion: "test",
      uptimeMs: BigInt(this.uptimeMs),
      sandboxId: this.sandboxId,
      resumeGeneration: BigInt(this.resumeGeneration),
      clockOffsetMs: BigInt(this.clockOffsetMs),
      kernelStateLost: this.kernelStateLost,
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
    });
  }
}
