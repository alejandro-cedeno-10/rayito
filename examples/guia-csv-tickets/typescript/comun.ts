// Valores compartidos por los ejemplos de TypeScript. Ningún secreto vive aquí.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { type Execution, Sandbox } from "rayito";

export const ROOT = fileURLToPath(new URL("..", import.meta.url));
export const TEMPLATE = process.env.RAYITO_TEMPLATE ?? "rayito-base";
export const METADATA = { app: process.env.RAYITO_APP_TAG ?? "tickets-demo" };
export const CSV_SANDBOX = "/home/user/tickets.csv";
export const csvLocal = (): string => readFileSync(`${ROOT}datos/tickets.csv`, "utf8");

type Resumen = Record<string, { tickets: number; media_horas: number }>;
interface Esperado {
  total: number;
  categorias: Resumen;
  tolerancia_media: number;
}
export const esperado = (): Esperado =>
  JSON.parse(readFileSync(`${ROOT}contrato/esperado.json`, "utf8")) as Esperado;

/** Las diferencias frente a contrato/esperado.json (vacío si cumple). */
export function comparar(obtenido: Resumen): string[] {
  const ref = esperado();
  const errores: string[] = [];
  const cats = Object.keys(ref.categorias).sort();
  if (Object.keys(obtenido).sort().join() !== cats.join()) errores.push(`categorías ${Object.keys(obtenido)}`);
  for (const cat of cats) {
    const got = obtenido[cat];
    const want = ref.categorias[cat]!;
    if (!got) continue;
    if (got.tickets !== want.tickets) errores.push(`${cat}: ${got.tickets} tickets, se esperaban ${want.tickets}`);
    if (Math.abs(got.media_horas - want.media_horas) > ref.tolerancia_media)
      errores.push(`${cat}: media ${got.media_horas.toFixed(4)} h, se esperaba ${want.media_horas} h`);
  }
  const total = Object.values(obtenido).reduce((n, v) => n + v.tickets, 0);
  if (total !== ref.total) errores.push(`total ${total}, se esperaba ${ref.total}`);
  return errores;
}

export class CeldaFallida extends Error {}

/** Ejecuta una celda que tiene que funcionar: si `execution.error` existe, se detiene. */
export async function runCell(sbx: Sandbox, code: string, paso: string, timeoutMs = 60_000): Promise<Execution> {
  const execution = await sbx.runCode(code, { timeoutMs });
  if (execution.error) {
    throw new CeldaFallida(`falló el paso «${paso}»: ${execution.error.name}: ${execution.error.value}`);
  }
  return execution;
}
