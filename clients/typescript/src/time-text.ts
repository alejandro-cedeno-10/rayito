/**
 * Texto de un instante UTC tal como lo escribe Python, para que la CLI de
 * TypeScript y la de Python impriman las mismas fechas (vectores compartidos
 * en `testdata/cli/format-vectors.json`). Una sola implementación: la usan la
 * CLI, las imágenes y la descripción de cada versión.
 */

/** `YYYY-MM-DDTHH:MM:SS`: lo que `toISOString()` trae antes de los milisegundos. */
const ISO_WITHOUT_MILLIS_LENGTH = 19;
const DATE_TIME_SEPARATOR_INDEX = 10;
const MILLIS_DIGITS = 3;
const MICROS_PER_MILLI = 1000;
const MICROS_DIGITS = 6;
const UTC_OFFSET = "+00:00";
/** Separador de `str(datetime)`. */
export const PYTHON_STR_SEPARATOR = " ";
/** Separador de `datetime.isoformat()`. */
export const ISO_SEPARATOR = "T";

export function padNumber(value: number, width: number): string {
  return String(value).padStart(width, "0");
}

/** `2026-09-15T14:39:02Z`, o `null` si no hay instante. */
export function isoUtc(moment: Date): string;
export function isoUtc(moment: Date | undefined): string | null;
export function isoUtc(moment: Date | undefined): string | null {
  if (moment === undefined) {
    return null;
  }
  return `${moment.toISOString().slice(0, ISO_WITHOUT_MILLIS_LENGTH)}Z`;
}

/**
 * `str(datetime)` de Python para un instante UTC con espacio entre fecha y
 * hora (`2026-09-15 14:39:02+00:00`), o `datetime.isoformat()` con `"T"`. La
 * fracción va en microsegundos sólo si no es cero.
 */
export function pythonDatetimeText(moment: Date, separator = PYTHON_STR_SEPARATOR): string {
  const base = moment.toISOString().slice(0, ISO_WITHOUT_MILLIS_LENGTH);
  const date = base.slice(0, DATE_TIME_SEPARATOR_INDEX);
  const time = base.slice(DATE_TIME_SEPARATOR_INDEX + 1);
  const micros = moment.getUTCMilliseconds() * MICROS_PER_MILLI;
  const fraction = micros === 0 ? "" : `.${padNumber(micros, MICROS_DIGITS)}`;
  return `${date}${separator}${time}${fraction}${UTC_OFFSET}`;
}

/** `2026-09-15T14:39:02.123Z`: el instante de un evento de CloudWatch. */
export function eventTimestamp(epochMs: number): string {
  const moment = new Date(epochMs);
  const millis = padNumber(moment.getUTCMilliseconds(), MILLIS_DIGITS);
  return `${moment.toISOString().slice(0, ISO_WITHOUT_MILLIS_LENGTH)}.${millis}Z`;
}
