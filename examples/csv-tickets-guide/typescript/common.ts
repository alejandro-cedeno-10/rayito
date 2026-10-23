/**
 * Valores compartidos por los ejemplos de TypeScript. Ningún secreto vive aquí.
 * Las columnas del CSV (`categoria`, `horas`) son datos del caso y se quedan en español.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import type { Execution, Sandbox } from "rayito";

export const ROOT = fileURLToPath(new URL("..", import.meta.url));
export const TEMPLATE = process.env.RAYITO_TEMPLATE ?? "rayito-base";
export const METADATA = { app: process.env.RAYITO_APP_TAG ?? "tickets-demo" };
export const SANDBOX_CSV = "/home/user/tickets.csv";
/** Plazo de cada celda del camino feliz; el CSV del caso tiene siete filas. */
export const CELL_TIMEOUT_MS = 60_000;

export interface CategorySummary {
  tickets: number;
  mean_hours: number;
}
export type Summary = Record<string, CategorySummary>;
export interface Contract {
  total: number;
  categories: Summary;
  mean_tolerance: number;
}

/** El CSV del caso, leído de `data/tickets.csv`. */
export const localCsv = (): string => readFileSync(`${ROOT}data/tickets.csv`, "utf8");

/** El resultado esperado, leído de `contract/expected.json`. */
export const expectedContract = (): Contract =>
  JSON.parse(readFileSync(`${ROOT}contract/expected.json`, "utf8")) as Contract;

/** Las diferencias frente a `contract/expected.json` (vacío si cumple). */
export function compare(actual: Summary): string[] {
  const contract = expectedContract();
  const errors: string[] = [];
  const categories = Object.keys(contract.categories).sort();
  if (Object.keys(actual).sort().join() !== categories.join()) {
    errors.push(`categorías ${Object.keys(actual)}`);
  }
  for (const category of categories) {
    const got = actual[category];
    const want = contract.categories[category];
    if (!got || !want) continue;
    if (got.tickets !== want.tickets) {
      errors.push(`${category}: ${got.tickets} tickets, se esperaban ${want.tickets}`);
    }
    if (Math.abs(got.mean_hours - want.mean_hours) > contract.mean_tolerance) {
      errors.push(`${category}: media ${got.mean_hours.toFixed(4)} h, se esperaba ${want.mean_hours} h`);
    }
  }
  const total = Object.values(actual).reduce((sum, summary) => sum + summary.tickets, 0);
  if (total !== contract.total) errors.push(`total ${total}, se esperaba ${contract.total}`);
  return errors;
}

/** Una celda que tenía que funcionar devolvió `execution.error`. */
export class CellFailedError extends Error {
  override name = "CellFailedError";
}

/**
 * Ejecuta una celda que tiene que funcionar: si `execution.error` existe, lanza
 * `CellFailedError`. Un error del código del sandbox no lanza en el SDK: llega como dato.
 */
export async function runCell(
  sbx: Sandbox,
  code: string,
  step: string,
  options: { timeoutMs?: number } = {},
): Promise<Execution> {
  const execution = await sbx.runCode(code, { timeoutMs: options.timeoutMs ?? CELL_TIMEOUT_MS });
  if (execution.error) {
    throw new CellFailedError(`falló el paso «${step}»: ${execution.error.name}: ${execution.error.value}`);
  }
  return execution;
}
