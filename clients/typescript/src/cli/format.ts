/**
 * Formato de los valores que la CLI imprime. Cada función reproduce lo que
 * `rayito.cli._console` y `str()` de Python producen, para que las dos CLI
 * emitan los mismos documentos y las mismas tablas (vectores compartidos en
 * `testdata/cli/format-vectors.json`).
 */

import { pythonStringRepr } from "../strings.js";
import { padNumber, pythonDatetimeText } from "../time-text.js";
import { MS_PER_SECOND, SECONDS_PER_HOUR, SECONDS_PER_MINUTE } from "../units.js";

export { eventTimestamp, isoUtc, pythonDatetimeText } from "../time-text.js";

const TIME_FIELD_DIGITS = 2;

/** `1h23m`, `12m07s`, `45s`: la edad de un sandbox desde `startedAt`. */
export function age(startedAt: Date, now: Date): string {
  const seconds = Math.max(0, Math.trunc((now.getTime() - startedAt.getTime()) / MS_PER_SECOND));
  const hours = Math.trunc(seconds / SECONDS_PER_HOUR);
  const minutes = Math.trunc((seconds % SECONDS_PER_HOUR) / SECONDS_PER_MINUTE);
  const rest = seconds % SECONDS_PER_MINUTE;
  if (hours > 0) {
    return `${hours}h${padNumber(minutes, TIME_FIELD_DIGITS)}m`;
  }
  if (minutes > 0) {
    return `${minutes}m${padNumber(rest, TIME_FIELD_DIGITS)}s`;
  }
  return `${rest}s`;
}

const NONE = "None";
const FIXED_FRACTION_DIGITS = 1;

/** `str(float)` de Python para los valores de uso diario: `0` es `0.0` y `12.5` sigue siendo `12.5`. */
export function pythonFloatText(value: number): string {
  return Number.isInteger(value) ? value.toFixed(FIXED_FRACTION_DIGITS) : String(value);
}

/** Cómo imprime Python una secuencia: `dataclasses.asdict` conserva las tuplas; un `dict` de `details` lleva listas. */
export type SequenceStyle = "tuple" | "list";

function pythonSequence(items: readonly string[], style: SequenceStyle): string {
  if (style === "list") {
    return `[${items.join(", ")}]`;
  }
  return items.length === 1 ? `(${items[0]},)` : `(${items.join(", ")})`;
}

function pythonNested(value: unknown, style: SequenceStyle): string {
  if (value === null || value === undefined) {
    return NONE;
  }
  if (typeof value === "boolean") {
    return value ? "True" : "False";
  }
  if (typeof value === "string") {
    return pythonStringRepr(value);
  }
  if (Array.isArray(value)) {
    return pythonSequence(
      value.map((item) => pythonNested(item, style)),
      style,
    );
  }
  if (typeof value === "object") {
    const pairs = Object.entries(value).map(
      ([key, item]) => `${pythonStringRepr(key)}: ${pythonNested(item, style)}`,
    );
    return `{${pairs.join(", ")}}`;
  }
  return String(value);
}

/**
 * Cómo imprime `f"{key}: {value}"` de Python un valor del documento de `sandbox
 * info` en modo humano: `None`, `True`/`False`, los textos tal cual y, anidados,
 * su `repr` (los mapas como `{'a': 'b'}` y las listas como tuplas, que es lo
 * que `dataclasses.asdict` conserva en `ingress`/`egress`).
 */
export function pythonDisplay(value: unknown, style: SequenceStyle = "tuple"): string {
  return typeof value === "string" ? value : pythonNested(value, style);
}

/** El texto de una celda de tabla: vacío para ausente, `True`/`False` para booleanos. */
export function cellText(value: unknown): string {
  if (value === null || value === undefined) {
    return "";
  }
  if (typeof value === "boolean") {
    return value ? "True" : "False";
  }
  if (value instanceof Date) {
    return pythonDatetimeText(value);
  }
  return String(value);
}
