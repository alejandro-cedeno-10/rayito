/**
 * Los documentos que `--json` imprime, construidos como los de la CLI de
 * Python: mismas claves, mismo orden y los instantes como `str(datetime)` o
 * ISO UTC según el campo (`testdata/cli/document-vectors.json`).
 */

import type { SandboxInfo, SandboxListItem, SandboxMetrics } from "../models.js";
import { MS_PER_SECOND } from "../units.js";
import { age, isoUtc, pythonDatetimeText } from "./format.js";

const UPPERCASE_LETTER = /[A-Z]/g;

/** `resumeGrace` es `resume_grace` en el documento, como en la CLI de Python. */
function snakeCase(name: string): string {
  return name.replace(UPPERCASE_LETTER, (letter) => `_${letter.toLowerCase()}`);
}

export function listRow(item: SandboxListItem, now: Date): Record<string, unknown> {
  return {
    sandbox_id: item.sandboxId,
    state: item.state,
    template: item.templateName,
    template_arn: item.template,
    template_version: item.templateVersion,
    started_at: isoUtc(item.startedAt),
    age: age(item.startedAt, now),
    ...(item.metadata === undefined ? {} : { metadata: { ...item.metadata } }),
  };
}

function idleDocument(info: SandboxInfo): Record<string, unknown> | null {
  if (info.idle === undefined) {
    return null;
  }
  return {
    max_idle_seconds: info.idle.maxIdleSeconds,
    suspended_duration_seconds: info.idle.suspendedDurationSeconds ?? null,
    auto_resume: info.idle.autoResume,
  };
}

function lifecycleDocument(info: SandboxInfo): Record<string, unknown> | null {
  const lifecycle = info.lifecycle;
  if (lifecycle === undefined) {
    return null;
  }
  return {
    phase: snakeCase(lifecycle.phase),
    deadline: lifecycle.deadline === undefined ? null : pythonDatetimeText(lifecycle.deadline),
    cap: lifecycle.cap === undefined ? null : pythonDatetimeText(lifecycle.cap),
    timeout_seconds: lifecycle.timeoutMs / MS_PER_SECOND,
    on_timeout: lifecycle.onTimeout ?? null,
    auto_resume: lifecycle.autoResume,
    extensions: lifecycle.extensions,
  };
}

/** `dataclasses.asdict(info)` más las propiedades derivadas, con los instantes en ISO UTC. */
export function infoDocument(info: SandboxInfo): Record<string, unknown> {
  return {
    sandbox_id: info.sandboxId,
    state: info.state,
    endpoint: info.endpoint,
    template: info.template,
    template_version: info.templateVersion,
    started_at: isoUtc(info.startedAt),
    maximum_duration_seconds: info.maximumDurationSeconds,
    terminated_at: isoUtc(info.terminatedAt),
    state_reason: info.stateReason ?? null,
    idle: idleDocument(info),
    execution_role_arn: info.executionRoleArn ?? null,
    metadata: info.metadata === undefined ? null : { ...info.metadata },
    ingress: [...info.ingress],
    egress: [...info.egress],
    lifecycle: lifecycleDocument(info),
    agent_version: info.agentVersion ?? null,
    cpu_count: info.cpuCount ?? null,
    memory_mb: info.memoryMb ?? null,
    size: info.size ?? null,
    baseline_memory_mib: info.baselineMemoryMib ?? null,
    baseline_cpu: info.baselineCpu ?? null,
    template_name: info.templateName,
    endpoint_url: info.endpointUrl,
    expires_at: isoUtc(info.expiresAt),
  };
}

export function launchDocument(info: SandboxInfo): Record<string, unknown> {
  return {
    sandbox_id: info.sandboxId,
    endpoint: info.endpoint,
    template: info.templateName,
    template_version: info.templateVersion,
    expires_at: isoUtc(info.expiresAt),
  };
}

export function metricsDocument(metrics: SandboxMetrics): Record<string, unknown> {
  return {
    timestamp: isoUtc(metrics.timestamp),
    cpu_used_pct: metrics.cpuUsedPct,
    cpu_count: metrics.cpuCount,
    mem_used: metrics.memUsedBytes,
    mem_total: metrics.memTotalBytes,
    disk_used: metrics.diskUsedBytes,
    disk_total: metrics.diskTotalBytes,
    ...(metrics.memCacheBytes ? { mem_cache: metrics.memCacheBytes } : {}),
  };
}
