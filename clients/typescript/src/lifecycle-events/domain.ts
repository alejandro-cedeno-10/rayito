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

/**
 * `deploy({ reconcilerIntervalMinutes })`'s default, mirrored by
 * `infra/events-webhooks.yaml`'s own `ReconcilerIntervalMinutes` parameter
 * default — kept as one constant so the Python sync API, the async API,
 * the CLI and this TypeScript mirror can never drift from each other.
 */
export const DEFAULT_RECONCILER_INTERVAL_MINUTES = 5;

/**
 * `infra/events-webhooks.yaml`'s `ReconcilerIntervalMinutes` `MinValue`:
 * EventBridge Scheduler only accepts `rate(1 minute)` (singular) for 1, and
 * the template always renders `rate(N minutes)`, so 1 is excluded rather
 * than special-cased.
 */
export const MIN_RECONCILER_INTERVAL_MINUTES = 2;

/**
 * `getEvents({ limit })`'s default, also `MAX_GET_EVENTS_LIMIT` in
 * `service.ts` (a DynamoDB `Query`'s own practical page size for this
 * table, AWS_API_NOTES.md §25).
 */
export const DEFAULT_GET_EVENTS_LIMIT = 100;

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
