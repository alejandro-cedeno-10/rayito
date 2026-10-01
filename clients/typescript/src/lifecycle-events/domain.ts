/**
 * Dominio puro de `m15-events-webhooks`: espejo de
 * `rayito._lifecycle_events._domain` (Python) y de
 * `infra/lambdas/events_webhooks/domain/event.py`.
 */

export const EVENT_TYPE_PREFIX = "sandbox.lifecycle.";

export const EVENT_KINDS = ["created", "paused", "resumed", "killed"] as const;
export type EventKind = (typeof EVENT_KINDS)[number];

export const KILL_REASONS = ["request", "timeout", "unknown"] as const;
export type KillReason = (typeof KILL_REASONS)[number];

export const DEFAULT_STACK_NAME = "rayito-events-webhooks";

export function eventType(kind: EventKind): string {
  return `${EVENT_TYPE_PREFIX}${kind}`;
}

/** Una fila de `getEvents()`. */
export interface EventRecord {
  readonly eventId: string;
  readonly sandboxId: string;
  readonly kind: EventKind;
  readonly killReason: KillReason | undefined;
  readonly generation: number;
  readonly occurredAtMs: number;
  readonly imageArn: string;
  readonly imageVersion: string;
}

export function eventRecordType(record: EventRecord): string {
  return eventType(record.kind);
}

export function sandboxExecutionId(record: EventRecord): string {
  return `${record.sandboxId}#${record.generation}`;
}

/** Lo que `registerWebhook`/`listWebhooks` exponen; nunca el secreto. */
export interface WebhookInfo {
  readonly webhookId: string;
  readonly url: string;
  readonly types: readonly string[];
}
