/**
 * Los documentos `--json` de `sandbox info`, `create --detach` y `metrics`
 * frente a los vectores que genera la CLI de Python
 * (`testdata/cli/document-vectors.json`, ADR-027).
 */

import { describe, expect, it } from "vitest";
import { infoDocument, launchDocument, metricsDocument } from "../../../src/cli/documents.js";
import {
  type SandboxInfo,
  type SandboxLifecycle,
  type SandboxMetrics,
  sandboxInfo,
} from "../../../src/models.js";
import { loadCliTestdata } from "./paths.js";

type Raw = Record<string, unknown>;

interface DocumentVectors {
  info: Array<{ name: string; info: Raw; expected: Raw; launch: Raw }>;
  metrics: Array<{ name: string; metrics: Raw; expected: Raw }>;
}

const vectors = loadCliTestdata<DocumentVectors>("document-vectors.json");
const PHASES: Record<string, SandboxLifecycle["phase"]> = {
  unmanaged: "unmanaged",
  active: "active",
  resume_grace: "resumeGrace",
  expired: "expired",
};
const MS_PER_SECOND = 1000;

function optionalDate(value: unknown): Date | undefined {
  return typeof value === "string" ? new Date(value) : undefined;
}

function lifecycleOf(raw: Raw | undefined): SandboxLifecycle | undefined {
  if (raw === undefined || raw === null) {
    return undefined;
  }
  return {
    phase: PHASES[String(raw.phase)] ?? "unmanaged",
    deadline: optionalDate(raw.deadline),
    cap: optionalDate(raw.cap),
    timeoutMs: Number(raw.timeout_seconds) * MS_PER_SECOND,
    onTimeout: (raw.on_timeout ?? undefined) as SandboxLifecycle["onTimeout"],
    autoResume: Boolean(raw.auto_resume),
    extensions: Number(raw.extensions),
  };
}

function idleOf(raw: Raw | undefined) {
  if (raw === undefined || raw === null) {
    return undefined;
  }
  return {
    maxIdleSeconds: Number(raw.max_idle_seconds),
    suspendedDurationSeconds: (raw.suspended_duration_seconds ?? undefined) as number | undefined,
    autoResume: Boolean(raw.auto_resume),
  };
}

function infoOf(raw: Raw): SandboxInfo {
  return sandboxInfo({
    sandboxId: String(raw.sandbox_id),
    state: String(raw.state),
    endpoint: String(raw.endpoint),
    template: String(raw.template),
    templateVersion: String(raw.template_version),
    startedAt: new Date(String(raw.started_at)),
    maximumDurationSeconds: Number(raw.maximum_duration_seconds),
    terminatedAt: optionalDate(raw.terminated_at),
    stateReason: raw.state_reason as string | undefined,
    idle: idleOf(raw.idle as Raw | undefined),
    executionRoleArn: raw.execution_role_arn as string | undefined,
    metadata: raw.metadata as Record<string, string> | undefined,
    ingress: raw.ingress as string[] | undefined,
    egress: raw.egress as string[] | undefined,
    lifecycle: lifecycleOf(raw.lifecycle as Raw | undefined),
    agentVersion: raw.agent_version as string | undefined,
    cpuCount: raw.cpu_count as number | undefined,
    memoryMb: raw.memory_mb as number | undefined,
    size: raw.size as string | undefined,
    baselineMemoryMib: raw.baseline_memory_mib as number | undefined,
    baselineCpu: raw.baseline_cpu as number | undefined,
  });
}

function metricsOf(raw: Raw): SandboxMetrics {
  return {
    cpuUsedPct: Number(raw.cpu_used_pct),
    memUsedBytes: Number(raw.mem_used_bytes),
    memTotalBytes: Number(raw.mem_total_bytes),
    diskUsedBytes: Number(raw.disk_used_bytes),
    diskTotalBytes: Number(raw.disk_total_bytes),
    cpuCount: Number(raw.cpu_count),
    timestamp: new Date(String(raw.timestamp)),
    memCacheBytes: Number(raw.mem_cache_bytes ?? 0),
  };
}

describe("documentos --json frente a los vectores de Python", () => {
  it.each(vectors.info)("sandbox info: $name", (vector) => {
    expect(infoDocument(infoOf(vector.info))).toEqual(vector.expected);
  });

  it.each(vectors.info)("el orden de las claves de sandbox info: $name", (vector) => {
    expect(Object.keys(infoDocument(infoOf(vector.info)))).toEqual(Object.keys(vector.expected));
  });

  it.each(vectors.info)("sandbox create --detach: $name", (vector) => {
    expect(launchDocument(infoOf(vector.info))).toEqual(vector.launch);
  });

  it.each(vectors.metrics)("sandbox metrics: $name", (vector) => {
    expect(metricsDocument(metricsOf(vector.metrics))).toEqual(vector.expected);
  });
});
