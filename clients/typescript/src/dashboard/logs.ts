/**
 * El adaptador de logs del panel: lee el final del stream de CloudWatch de
 * un sandbox con las mismas funciones que `rayito sandbox logs`. Sólo
 * lecturas (`GetMicrovm`, `DescribeLogStreams`, `GetLogEvents`) y sólo
 * cuando la SPA pide los logs de un sandbox. CloudWatch no publica una tarifa
 * propia para esas dos llamadas (https://aws.amazon.com/cloudwatch/pricing/,
 * consultado 2026-10-13): sólo cobra ingesta, almacenamiento y Logs Insights,
 * y el panel no hace ninguna consulta de Logs Insights. Muestra las últimas
 * líneas, que son las que importan al operar. Espejo de
 * `rayito._dashboard._logs_aws`.
 */

import type { ControlPlane } from "../aws/control-plane.js";
import { DASHBOARD_LOG_LINE_MAX_CHARS } from "../limits.js";
import { defaultLogGroup, expectedStreamName, LogsNotFoundError } from "../sandbox-logs/domain.js";
import { findStreams, type LogsApi, tailEvents } from "../sandbox-logs/service.js";
import { isoUtc, type LogLine, type LogPage, type LogsSource } from "./api/contract.js";
import { dashboardCall } from "./aws.js";

const EMPTY_PAGE: LogPage = { lines: [], truncated: false, fallback: false };
const TRAILING_NEWLINES = /\n+$/;

export class CloudWatchLogsSource implements LogsSource {
  readonly #logs: LogsApi;
  readonly #plane: Pick<ControlPlane, "getMicrovm">;
  readonly #logGroup: string | undefined;

  constructor(
    logs: LogsApi,
    controlPlane: Pick<ControlPlane, "getMicrovm">,
    options: { readonly logGroup?: string | undefined } = {},
  ) {
    this.#logs = logs;
    this.#plane = controlPlane;
    this.#logGroup = options.logGroup;
  }

  /**
   * Las últimas `limit` líneas en orden cronológico; un error de CloudWatch
   * sale como el error del SDK que corresponde a su código (sin el mensaje de
   * AWS), nunca como el error crudo del cliente.
   */
  read(sandboxId: string, options: { readonly limit: number }): Promise<LogPage> {
    return dashboardCall("logs", () => this.#read(sandboxId, options.limit));
  }

  async #read(sandboxId: string, limit: number): Promise<LogPage> {
    const info = await this.#plane.getMicrovm(sandboxId);
    const group = this.#logGroup ?? defaultLogGroup(info.template);
    let streams: string[];
    try {
      streams = await findStreams(this.#logs, group, info);
    } catch (error) {
      if (error instanceof LogsNotFoundError) {
        return EMPTY_PAGE;
      }
      throw error;
    }
    const fallback = !(streams.length === 1 && streams[0] === expectedStreamName(info));
    const lines: LogLine[] = [];
    for (const stream of streams) {
      const events = await tailEvents(this.#logs, {
        group,
        stream,
        limit: limit + 1 - lines.length,
      });
      lines.unshift(
        ...events.map((event) => ({
          timestamp: isoUtc(new Date(event.timestamp)),
          message: trimLine(event.message),
          stream,
        })),
      );
      if (lines.length > limit) {
        break;
      }
    }
    return { lines: lines.slice(-limit), truncated: lines.length > limit, fallback };
  }
}

function trimLine(message: string): string {
  return [...message.replace(TRAILING_NEWLINES, "")]
    .slice(0, DASHBOARD_LOG_LINE_MAX_CHARS)
    .join("");
}
