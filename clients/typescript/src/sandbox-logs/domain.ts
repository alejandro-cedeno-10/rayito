/**
 * Logs de runtime de un sandbox en CloudWatch: el grupo por defecto, el
 * nombre esperado del stream y la lectura de `--since`. Dominio puro, espejo
 * de la parte sin I/O de `rayito._cloudwatch_logs`.
 *
 * El grupo es `/rayito/<nombre de la imagen>` (el que el SDK pasa con
 * `logging: "cloudwatch"`); el stream se llama
 * `YYYY/MM/DD[<imageVersion>]<microvmId>` (`AWS_API_NOTES.md` §14, medido).
 */

import { InvalidArgumentError } from "../errors.js";
import { LOG_GROUP_PREFIX } from "../images/gateway.js";
import { type SandboxInfo, templateNameFromArn } from "../models.js";
import { pythonStringRepr } from "../strings.js";

/** Eventos que `rayito sandbox logs` pide por defecto. */
export const DEFAULT_EVENT_LIMIT = 1000;
export const NO_LOGS_MESSAGE =
  "sin logs: el sandbox se lanzó con logging disabled, sin executionRoleArn, " +
  "o en otro grupo (--log-group)";
const STREAM_DAY_LENGTH = 10;
const RELATIVE_SINCE = /^(\d+)([smhd])$/;
const MS_PER_UNIT: Readonly<Record<string, number>> = {
  s: 1000,
  m: 60_000,
  h: 3_600_000,
  d: 86_400_000,
};
const ISO_WITH_ZONE = /(?:[zZ]|[+-]\d{2}:?\d{2})$/;
const MIDNIGHT = "T00:00:00";
const ISO_DATE_TIME = /^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?/;

/** Ni el stream esperado ni ninguno del sandbox existe en el grupo. */
export class LogsNotFoundError extends Error {
  readonly group: string;

  constructor(group: string) {
    super(`${NO_LOGS_MESSAGE}; grupo consultado: ${group}`);
    this.name = "LogsNotFoundError";
    this.group = group;
  }
}

export function defaultLogGroup(templateArn: string): string {
  return `${LOG_GROUP_PREFIX}/${templateNameFromArn(templateArn)}`;
}

function utcDay(moment: Date): string {
  return moment.toISOString().slice(0, STREAM_DAY_LENGTH).replaceAll("-", "/");
}

export function expectedStreamName(info: SandboxInfo): string {
  return `${utcDay(info.startedAt)}[${info.templateVersion}]${info.sandboxId}`;
}

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/**
 * `YYYY/MM/DD[<versión>]<id>` exacto, con un día no anterior al del arranque
 * del sandbox (UTC). No basta con que termine en `]<id>`: el rol de ejecución
 * puede crear streams en cualquier grupo `/rayito/*`, y código de otro
 * sandbox podría fabricar uno con ese sufijo.
 */
export function isFallbackStream(name: string, info: SandboxInfo): boolean {
  const pattern = new RegExp(
    `^(\\d{4}/\\d{2}/\\d{2})\\[${escapeRegExp(info.templateVersion)}\\]${escapeRegExp(info.sandboxId)}$`,
  );
  const match = pattern.exec(name);
  return match !== null && (match[1] ?? "") >= utcDay(info.startedAt);
}

function isoInstant(text: string): number | undefined {
  const match = ISO_DATE_TIME.exec(text);
  if (match === null) {
    return undefined;
  }
  const dated = text.replace(" ", "T");
  const timed = match[1] === undefined ? `${dated}${MIDNIGHT}` : dated;
  const zoned = ISO_WITH_ZONE.test(timed) ? timed : `${timed}Z`;
  const parsed = Date.parse(zoned);
  return Number.isNaN(parsed) ? undefined : parsed;
}

/**
 * `30m`, `2h`, `1d` (relativo a `now`) o una fecha ISO-8601; una fecha sin
 * zona se interpreta en UTC. Devuelve milisegundos desde la época.
 */
export function parseSince(text: string, now: Date): number {
  const trimmed = text.trim();
  const relative = RELATIVE_SINCE.exec(trimmed);
  if (relative !== null) {
    const unit = MS_PER_UNIT[relative[2] ?? ""] ?? 0;
    return now.getTime() - Number(relative[1]) * unit;
  }
  const absolute = isoInstant(trimmed);
  if (absolute === undefined) {
    throw new InvalidArgumentError(
      `--since no reconocido: ${pythonStringRepr(text)} (usa 30m, 2h, 1d o ISO-8601)`,
    );
  }
  return absolute;
}
