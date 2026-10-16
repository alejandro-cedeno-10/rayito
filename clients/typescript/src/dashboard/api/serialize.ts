/**
 * Del dominio a los documentos JSON de `/api/v1` (claves en `camelCase`).
 * Funciones puras: ningún modelo del dominio sale por la API sin pasar por
 * aquí, así que un campo nuevo del dominio (un `envs`, un token) no se filtra
 * sin querer. Espejo de `rayito._dashboard._api._serialize`.
 */

import { type EventRecord, eventRecordType } from "../../lifecycle-events/domain.js";
import { QUOTA_CRITICAL_FRACTION, QUOTA_WARNING_FRACTION } from "../../limits.js";
import type {
  BilledCost,
  BulkPlan,
  CostEstimate,
  ImageSummary,
  ImageVersionSummary,
  QuotaReport,
  SandboxView,
} from "../domain.js";
import { isoUtc, type JsonObject } from "./contract.js";

export function sandboxJson(view: SandboxView): JsonObject {
  return {
    sandboxId: view.sandboxId,
    state: view.state,
    image: view.templateName,
    imageArn: view.template,
    imageVersion: view.templateVersion,
    startedAt: isoUtc(view.startedAt),
    memoryMib: view.memoryMib ?? null,
  };
}

export function imageJson(image: ImageSummary): JsonObject {
  return {
    name: image.name,
    imageArn: image.imageArn,
    state: image.state,
    latestActiveVersion: image.latestActiveVersion ?? null,
    latestFailedVersion: image.latestFailedVersion ?? null,
    createdAt: isoUtc(image.createdAt),
  };
}

export function imageVersionJson(version: ImageVersionSummary): JsonObject {
  return {
    imageVersion: version.imageVersion,
    state: version.state,
    status: version.status,
    baseImageVersion: version.baseImageVersion ?? null,
    artifact: version.artifact ?? null,
    memoryMib: version.memoryMib ?? null,
    createdAt: isoUtc(version.createdAt),
  };
}

export function quotasJson(report: QuotaReport): JsonObject {
  const memory = report.memory;
  return {
    region: report.region,
    entries: report.entries.map((entry) => ({
      code: entry.code,
      name: entry.name,
      kind: entry.kind,
      operation: entry.operation ?? null,
      default: entry.defaultValue,
      value: entry.value ?? null,
      adjustable: entry.adjustable,
      belowDefault: entry.belowDefault,
      increaseUrl: entry.increaseUrl,
    })),
    memory: {
      allocatedGb: memory.allocatedGb,
      quotaGb: memory.quotaGb ?? null,
      fraction: memory.fraction ?? null,
      level: memory.level,
      sandboxes: memory.sandboxes,
      unresolved: memory.unresolved,
    },
    tps: report.tps.map((limit) => ({
      operation: limit.operation,
      rate: limit.rate ?? null,
      burst: limit.burst ?? null,
    })),
    warnings: report.warnings.map((warning) => ({
      kind: warning.kind,
      code: warning.code,
      message: warning.message,
      increaseUrl: warning.increaseUrl,
    })),
    thresholds: { warning: QUOTA_WARNING_FRACTION, critical: QUOTA_CRITICAL_FRACTION },
  };
}

export function estimateJson(estimate: CostEstimate): JsonObject {
  return {
    label: estimate.label,
    pricingRegion: estimate.pricingRegion,
    basis: estimate.basis,
    totalUsd: estimate.totalUsd,
    items: estimate.items.map((item) => ({
      sandboxId: item.sandboxId,
      state: item.state,
      memoryMib: item.memoryMib ?? null,
      activeSeconds: item.activeSeconds ?? null,
      costUsd: item.costUsd ?? null,
    })),
  };
}

export function billedJson(billed: BilledCost): JsonObject {
  return {
    label: billed.label,
    start: billed.start,
    end: billed.end,
    region: billed.region ?? null,
    totalUsd: billed.totalUsd,
    partial: billed.partial,
    requests: billed.requests,
    fetchedAt: isoUtc(billed.fetchedAt),
    lines: billed.lines.map((line) => ({
      usageType: line.usageType,
      component: line.component,
      quantity: line.quantity,
      unit: line.unit,
      costUsd: line.costUsd,
    })),
  };
}

export function eventJson(event: EventRecord): JsonObject {
  return {
    eventId: event.eventId,
    sandboxId: event.sandboxId,
    type: eventRecordType(event),
    killReason: event.killReason ?? null,
    generation: event.generation,
    occurredAt: isoUtc(new Date(event.occurredAtMs)),
    imageArn: event.imageArn,
    imageVersion: event.imageVersion,
  };
}

export function planJson(plan: BulkPlan): JsonObject {
  return {
    action: plan.action,
    operation: plan.operation,
    count: plan.count,
    tps: plan.tps,
    estimatedSeconds: plan.estimatedSeconds,
  };
}
