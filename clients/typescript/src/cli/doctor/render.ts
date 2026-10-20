/**
 * La salida de `rayito doctor`: una línea por comprobación, la tabla de
 * compatibilidad y el resumen, o el documento JSON. Espejo de
 * `rayito.cli.doctor`.
 */

import { VERSION } from "../../version.js";
import type { CliConsole, FloatKeys } from "../console.js";
import { pythonDisplay } from "../format.js";
import { COMPATIBILITY } from "./compat.js";
import type { CheckResult, CheckStatus, DoctorContext } from "./model.js";

const STATUS_WIDTH = 4;
const NAME_WIDTH = 15;
const NOT_CHECKED = "no comprobado";
/** Tablas crudas que sólo van en `--json`; el resto de detalles de `WARN`/`FAIL` se indentan. */
const VERBOSE_DETAIL_KEYS: ReadonlySet<string> = new Set(["results", "quotas"]);
const COMPATIBILITY_COLUMNS = ["SDK", "rayd mínimo", "Estado", "Nota"];
const STATUS_ORDER: readonly CheckStatus[] = ["OK", "WARN", "FAIL", "SKIP"];
const SERIES_PARTS = 2;
const DETAIL_INDENT = "       ";

export const EXIT_CHECKS_FAILED = 1;

export function exitCodeFor(results: readonly CheckResult[]): number {
  return results.some((result) => result.status === "FAIL") ? EXIT_CHECKS_FAILED : 0;
}

function compatibilityStatus(results: readonly CheckResult[]): string {
  const found = results.find((result) => result.name === "compatibility");
  if (found === undefined || found.status === "SKIP") {
    return NOT_CHECKED;
  }
  return found.status;
}

function compatibilityTableRows(results: readonly CheckResult[]): string[][] {
  const sdkSeries = VERSION.split(".").slice(0, SERIES_PARTS).join(".");
  const status = compatibilityStatus(results);
  return COMPATIBILITY.map((row) => [
    row.sdkSeries,
    row.minAgentVersion,
    row.sdkSeries === sdkSeries ? status : "",
    row.note,
  ]);
}

/** Una línea por comprobación; los detalles de `WARN`/`FAIL` indentados salvo las tablas crudas. */
export function renderHuman(console: CliConsole, results: readonly CheckResult[]): void {
  for (const result of results) {
    console.echo(
      `${result.status.padEnd(STATUS_WIDTH)} ${result.name.padEnd(NAME_WIDTH)} ${result.summary}`,
    );
    if (result.status === "WARN" || result.status === "FAIL") {
      for (const [key, value] of Object.entries(result.details)) {
        if (!VERBOSE_DETAIL_KEYS.has(key)) {
          console.echo(`${DETAIL_INDENT}${key}: ${pythonDisplay(value, "list")}`);
        }
      }
    }
  }
  console.echo();
  console.table(COMPATIBILITY_COLUMNS, compatibilityTableRows(results));
  const counts = STATUS_ORDER.map(
    (status) => `${results.filter((result) => result.status === status).length} ${status}`,
  );
  console.echo();
  console.echo(`rayito doctor: ${counts.join(", ")}`);
}

/** Los valores de cuota y su mínimo son `float` en Python: `10` se escribe `10.0`. */
export const QUOTA_FLOAT_KEYS: FloatKeys = new Set(["value", "default"]);

export interface DoctorDocumentInput {
  readonly region: string;
  readonly context: DoctorContext;
  readonly results: readonly CheckResult[];
  readonly exitCode: number;
}

/** El documento de `rayito --json doctor`; `rayito setup` lo incrusta tal cual. */
export function doctorDocument(input: DoctorDocumentInput): Record<string, unknown> {
  return {
    rayito: VERSION,
    region: input.region,
    account: input.context.account ?? null,
    principal_kind: input.context.principalKind ?? null,
    checks: input.results.map((result) => ({ ...result })),
    compatibility: COMPATIBILITY.map((row) => ({
      sdk_series: row.sdkSeries,
      min_agent_version: row.minAgentVersion,
      note: row.note,
    })),
    launched_sandbox_id: input.context.launched?.sandboxId ?? null,
    exit_code: input.exitCode,
  };
}

export function renderJson(console: CliConsole, input: DoctorDocumentInput): void {
  console.emitJson(doctorDocument(input), QUOTA_FLOAT_KEYS);
}
