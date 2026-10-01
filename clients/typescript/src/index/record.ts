/**
 * Núcleo puro del índice de metadatos (M14), espejo de
 * `rayito/_index.py`: la fila inmutable de un sandbox (`IndexRecord`), su
 * codificación como ítem de DynamoDB y su TTL determinista. Sin I/O.
 *
 * La fila guarda sólo datos inmutables escritos una vez tras `run-microvm`:
 * nunca el access token (ni su hash), `envs`, referencias o valores de
 * secretos, el `runHookPayload` ni el JWE.
 */

import type { SandboxInfo } from "../models.js";
import { VERSION } from "../version.js";

export const DEFAULT_TTL_MARGIN_SECONDS = 3600;
export const SDK_TAG = `ts/${VERSION}`;
/** Los únicos atributos que escribe Rayito (el test de lista permitida los compara). */
export const RECORD_ATTRIBUTES: readonly string[] = Object.freeze([
  "pk",
  "image_arn",
  "image_version",
  "started_at_ms",
  "metadata",
  "sdk",
  "expires_at",
]);

/** Un valor de atributo de DynamoDB en el formato del SDK (`{ S }`, `{ N }`, `{ M }`). */
export type AttributeValue =
  | { readonly S: string }
  | { readonly N: string }
  | { readonly M: Readonly<Record<string, AttributeValue>> };

export type IndexItem = Readonly<Record<string, AttributeValue>>;

/**
 * La fila de un sandbox. `startedAtMs` es el `startedAt` de `run-microvm`;
 * `expiresAt` (segundos epoch, el atributo TTL de la tabla) es `startedAt +
 * maximumDurationInSeconds + margen`: el sandbox ya no puede existir después.
 */
export interface IndexRecord {
  readonly sandboxId: string;
  readonly imageArn: string;
  readonly imageVersion: string;
  readonly startedAtMs: number;
  readonly metadata: Readonly<Record<string, string>>;
  readonly sdk: string;
  readonly expiresAt: number;
}

/** La fila de un sandbox recién lanzado: sólo depende de `run-microvm`, los metadatos y el margen. */
export function recordFor(
  info: Pick<
    SandboxInfo,
    "sandboxId" | "template" | "templateVersion" | "startedAt" | "maximumDurationSeconds"
  >,
  metadata: Readonly<Record<string, string>> | undefined,
  marginSeconds: number = DEFAULT_TTL_MARGIN_SECONDS,
  sdk: string = SDK_TAG,
): IndexRecord {
  const startedAtMs = info.startedAt.getTime();
  return Object.freeze({
    sandboxId: info.sandboxId,
    imageArn: info.template,
    imageVersion: info.templateVersion,
    startedAtMs,
    metadata: Object.freeze({ ...(metadata ?? {}) }),
    sdk,
    expiresAt: Math.floor(startedAtMs / 1000) + info.maximumDurationSeconds + marginSeconds,
  });
}

/** El `Item` de `PutItem`. */
export function toItem(record: IndexRecord): IndexItem {
  return {
    pk: { S: record.sandboxId },
    image_arn: { S: record.imageArn },
    image_version: { S: record.imageVersion },
    started_at_ms: { N: String(record.startedAtMs) },
    metadata: {
      M: Object.fromEntries(
        Object.entries(record.metadata).map(([key, value]) => [key, { S: value }]),
      ),
    },
    sdk: { S: record.sdk },
    expires_at: { N: String(record.expiresAt) },
  };
}

function stringOf(value: unknown): string | undefined {
  const text = (value as { S?: unknown } | null | undefined)?.S;
  return typeof text === "string" ? text : undefined;
}

function integerOf(value: unknown): number | undefined {
  const text = (value as { N?: unknown } | null | undefined)?.N;
  if (typeof text !== "string" || !/^-?\d+$/.test(text)) {
    return undefined;
  }
  const parsed = Number(text);
  return Number.isSafeInteger(parsed) ? parsed : undefined;
}

function metadataOf(value: unknown): Record<string, string> | undefined {
  const map = (value as { M?: unknown } | null | undefined)?.M;
  if (map === null || typeof map !== "object") {
    return undefined;
  }
  const result: Record<string, string> = {};
  for (const [key, entry] of Object.entries(map as Record<string, unknown>)) {
    const text = stringOf(entry);
    if (text === undefined) {
      return undefined;
    }
    result[key] = text;
  }
  return result;
}

/** Una fila leída con `BatchGetItem`, o `undefined` si no tiene la forma que escribe Rayito. */
export function fromItem(item: Readonly<Record<string, unknown>>): IndexRecord | undefined {
  const sandboxId = stringOf(item.pk);
  const imageArn = stringOf(item.image_arn);
  const imageVersion = stringOf(item.image_version);
  const startedAtMs = integerOf(item.started_at_ms);
  const metadata = metadataOf(item.metadata);
  const sdk = stringOf(item.sdk);
  const expiresAt = integerOf(item.expires_at);
  if (
    sandboxId === undefined ||
    imageArn === undefined ||
    imageVersion === undefined ||
    startedAtMs === undefined ||
    metadata === undefined ||
    sdk === undefined ||
    expiresAt === undefined
  ) {
    return undefined;
  }
  return Object.freeze({
    sandboxId,
    imageArn,
    imageVersion,
    startedAtMs,
    metadata: Object.freeze(metadata),
    sdk,
    expiresAt,
  });
}
