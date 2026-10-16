/**
 * `rayito image prune` (dominio puro): qué versiones de una imagen se
 * conservan y cuáles se borran. Espejo de `rayito.cli._prune`;
 * `testdata/images/prune-vectors.json` fija los mismos planes y la misma
 * tabla en los dos lenguajes. Cada publicación deja una versión que cuesta
 * una semana de almacenamiento de snapshot, y borrarlas en bloque falla con
 * `ConflictException` mientras la imagen está `UPDATING` o `DELETING`.
 *
 * El plan conserva las `keep` versiones lanzables más nuevas, toda versión
 * en la que corre un MicroVM vivo y toda versión con un build o un borrado en
 * vuelo; el resto se borra de la más antigua a la más nueva.
 */

import { ISO_SEPARATOR, isoUtc, pythonDatetimeText } from "../time-text.js";
import { MS_PER_SECOND } from "../units.js";
import { ACTIVE_VERSION_STATUS, SUCCESSFUL_VERSION_STATE } from "./gateway.js";
import { ImageSettingsError } from "./publish-domain.js";

export const DEFAULT_PRUNE_IMAGE_NAME = "rayito-base";
export const DEFAULT_KEEP = 5;
export const DEFAULT_WAIT_TIMEOUT_MS = 600_000;
export const POLL_INTERVAL_MS = 5_000;
/** Esperas entre intentos de un borrado rechazado; su longitud es el máximo de intentos. */
export const RETRY_BACKOFF_MS: readonly number[] = [5_000, 10_000, 20_000, 40_000, 80_000];
export const IN_FLIGHT_STATES: ReadonlySet<string> = new Set([
  "PENDING",
  "IN_PROGRESS",
  "DELETING",
  "DELETED",
]);
export const BUSY_IMAGE_STATES: ReadonlySet<string> = new Set(["UPDATING", "DELETING"]);
export const GONE_VERSION_STATE = "DELETED";
export const CONFLICT_CODE = "ConflictException";
export const NOT_FOUND_CODE = "ResourceNotFoundException";
export const THROTTLED_CODE = "ThrottlingException";
export const REFUSED_ACTIVE_CODES: ReadonlySet<string> = new Set([
  "ValidationException",
  CONFLICT_CODE,
]);
export const TABLE_HEADER: readonly string[] = [
  "version",
  "state",
  "status",
  "createdAt",
  "action",
];

export interface Version {
  readonly imageVersion: string;
  readonly state: string;
  readonly status: string;
  readonly createdAt: Date;
}

export function isLaunchable(version: Version): boolean {
  return version.state === SUCCESSFUL_VERSION_STATE && version.status === ACTIVE_VERSION_STATUS;
}

export function isInFlight(version: Version): boolean {
  return IN_FLIGHT_STATES.has(version.state);
}

export interface Plan {
  readonly keep: ReadonlyArray<readonly [Version, string]>;
  readonly delete: readonly Version[];
}

export type PlanRow = readonly [string, string, string, string, string];

export interface PruneSettings {
  readonly imageName: string;
  readonly keep: number;
  readonly dryRun: boolean;
  readonly waitTimeoutMs: number;
}

/** `--keep` por debajo de uno borraría hasta la última versión lanzable. */
export function validatePruneSettings(settings: PruneSettings): PruneSettings {
  if (settings.keep < 1) {
    throw new ImageSettingsError("--keep debe ser al menos 1", "--keep");
  }
  return settings;
}

/**
 * `versions` va de la más nueva a la más antigua: las primeras `keep`
 * lanzables se conservan, igual que lo vivo y lo que está en vuelo; el resto
 * se borra de la más antigua a la más nueva.
 */
export function buildPlan(
  versions: readonly Version[],
  live: ReadonlySet<string>,
  keep: number,
): Plan {
  const kept: Array<readonly [Version, string]> = [];
  const candidates: Version[] = [];
  let launchableKept = 0;
  for (const version of versions) {
    if (isLaunchable(version) && launchableKept < keep) {
      launchableKept += 1;
      kept.push([version, "newest"]);
    } else if (live.has(version.imageVersion)) {
      kept.push([version, "live microvm"]);
    } else if (isInFlight(version)) {
      kept.push([version, version.state.toLowerCase()]);
    } else {
      candidates.push(version);
    }
  }
  candidates.sort((left, right) => left.createdAt.getTime() - right.createdAt.getTime());
  return { keep: kept, delete: candidates };
}

/** Una fila por versión, la más nueva primero. */
export function planRows(plan: Plan): PlanRow[] {
  const describe = (version: Version, action: string): PlanRow => [
    version.imageVersion,
    version.state,
    version.status,
    pythonDatetimeText(version.createdAt, ISO_SEPARATOR),
    action,
  ];
  const rows = [
    ...plan.keep.map(([version, why]) => describe(version, `keep (${why})`)),
    ...plan.delete.map((version) => describe(version, "delete")),
  ];
  return rows.sort((left, right) => (left[3] < right[3] ? 1 : left[3] > right[3] ? -1 : 0));
}

/** La tabla del plan con las columnas alineadas a la izquierda, como `format_table` de Python. */
export function formatTable(plan: Plan): string[] {
  const rows: Array<readonly string[]> = [TABLE_HEADER, ...planRows(plan)];
  const widths = TABLE_HEADER.map((_, column) =>
    Math.max(...rows.map((row) => (row[column] ?? "").length)),
  );
  return rows.map((row) => row.map((cell, column) => cell.padEnd(widths[column] ?? 0)).join("  "));
}

export type DeleteOutcome = "pending" | "deleted" | "timeout" | "refused" | "failed";

/** Lo que pasó al borrar una versión: intentos, conflictos, esperas y el último error. */
export interface DeleteReport {
  imageVersion: string;
  outcome: DeleteOutcome;
  attempts: number;
  conflicts: number;
  deactivated: boolean;
  waitedMs: number;
  lastError: string | undefined;
}

export function newDeleteReport(imageVersion: string): DeleteReport {
  return {
    imageVersion,
    outcome: "pending",
    attempts: 0,
    conflicts: 0,
    deactivated: false,
    waitedMs: 0,
    lastError: undefined,
  };
}

const WAITED_DECIMALS = 10;

export function reportDocument(report: DeleteReport): Record<string, unknown> {
  return {
    imageVersion: report.imageVersion,
    outcome: report.outcome,
    attempts: report.attempts,
    conflicts: report.conflicts,
    deactivated: report.deactivated,
    waitedSeconds:
      Math.round((report.waitedMs / MS_PER_SECOND) * WAITED_DECIMALS) / WAITED_DECIMALS,
    lastError: report.lastError ?? null,
  };
}

/** El documento final de una poda: qué se conservó, qué se planeó, qué se borró y cada informe. */
export function pruneSummary(input: {
  readonly arn: string;
  readonly plan: Plan;
  readonly reports: readonly DeleteReport[];
  readonly dryRun: boolean;
  readonly finishedAt: Date;
}): Record<string, unknown> {
  const finished = isoUtc(input.finishedAt);
  return {
    imageArn: input.arn,
    dryRun: input.dryRun,
    kept: input.plan.keep.map(([version]) => version.imageVersion),
    planned: input.plan.delete.map((version) => version.imageVersion),
    deleted: input.reports
      .filter((report) => report.outcome === "deleted")
      .map((report) => report.imageVersion),
    reports: input.reports.map(reportDocument),
    finishedAt: finished,
  };
}
