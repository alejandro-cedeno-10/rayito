/**
 * Unidades de tiempo y de formato que comparten la CLI, el servidor MCP, el
 * panel y las imágenes. Una sola definición por valor: ningún módulo vuelve a
 * declarar `1000` ni `2`.
 */

/** Milisegundos en un segundo, para convertir marcas de `Date` y plazos. */
export const MS_PER_SECOND = 1000;
export const SECONDS_PER_MINUTE = 60;
export const SECONDS_PER_HOUR = 3600;
export const SECONDS_PER_DAY = 86_400;
/** Sangría de todo JSON que se imprime para una persona. */
export const JSON_INDENT = 2;
/** `timeoutMs: 0` en un comando o PTY: sin plazo en el servidor (el guest no corta). */
export const NO_SERVER_TIMEOUT_MS = 0;
