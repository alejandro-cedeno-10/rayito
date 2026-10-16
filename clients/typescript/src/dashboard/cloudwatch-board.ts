/**
 * Generador del dashboard de CloudWatch (ADR-026, fase 3): el JSON de un
 * `DashboardBody` con widgets de Logs Insights sobre los grupos `/rayito/*` y,
 * si se piden, los de las métricas OTLP de `rayd` y el uso de la cuota de
 * memoria que publica la pila `quota-alerts`. Espejo de
 * `rayito._dashboard._cloudwatch_board`; `testdata/dashboard/
 * cloudwatch-board-cases.json` fija los mismos bytes en los dos lenguajes.
 *
 * Es dominio puro: no importa el SDK de AWS ni hace ninguna llamada. Los
 * widgets no llevan `x`/`y`: CloudWatch los coloca en la siguiente posición
 * libre, así que los opcionales no dejan huecos cuando faltan.
 */

import { InvalidArgumentError } from "../errors.js";
import { LOG_GROUP_PREFIX } from "../images/gateway.js";
import {
  QUOTA_CRITICAL_FRACTION,
  QUOTA_METRIC_NAME,
  QUOTA_METRIC_NAMESPACE,
  QUOTA_WARNING_FRACTION,
} from "../limits.js";
import { JSON_INDENT } from "../units.js";

export type Widget = Readonly<Record<string, unknown>>;

export interface DashboardBoard {
  readonly start: string;
  readonly widgets: readonly Widget[];
}

/** Prefijo por defecto de los grupos (`/rayito/<imagen>`), con la barra final para no abarcar `/rayito-otro`. */
export const DEFAULT_LOG_GROUP_PREFIX = `${LOG_GROUP_PREFIX}/`;
/** El intervalo del dashboard al abrirlo (ISO 8601) y el cubo de las series. */
export const DASHBOARD_START = "-PT6H";
const LOG_BIN = "5m";
const GRID_COLUMNS = 24;
const HALF_WIDTH = GRID_COLUMNS / 2;
const HEADER_HEIGHT = 3;
const CHART_HEIGHT = 6;
/** Filas de las tablas «más ruidosos» y «errores recientes». */
const TOP_ROWS = 20;
const RECENT_ERROR_ROWS = 50;
/** Lo que cuenta como error en una línea de log, sin distinguir mayúsculas. */
const ERROR_PATTERN = "/(?i)error|exception|traceback|fatal|panic/";
/** Período de la serie de uso de cuota: la comprobación corre cada hora por defecto. */
const QUOTA_PERIOD_SECONDS = 3600;
const PERCENT_AXIS_MAX = 1;
const CHART_VIEW = "line";
const OTLP_CPU_USED_PCT = "rayito.sandbox.cpu.used_pct";
const OTLP_MEMORY_USED_BYTES = "rayito.sandbox.memory.used_bytes";
const OTLP_MEMORY_TOTAL_BYTES = "rayito.sandbox.memory.total_bytes";
const OTLP_DISK_USED_BYTES = "rayito.sandbox.disk.used_bytes";
const OTLP_DISK_TOTAL_BYTES = "rayito.sandbox.disk.total_bytes";
const LOG_GROUP_NAME_CHARACTER = /^[A-Za-z0-9/_.#-]+$/;
const HEADER_MARKDOWN = (scope: string): string =>
  `# Rayito\nLogs de los sandboxes (Logs Insights sobre \`${scope}\`). Cada widget de log ` +
  "ejecuta su consulta al abrir o refrescar el dashboard y CloudWatch factura " +
  "los datos escaneados: usa un intervalo corto.";

/** El prefijo entra tal cual en una consulta entre comillas simples: sólo caracteres de un nombre de grupo, empezando por `/`. */
export function validateLogGroupPrefix(prefix: string): string {
  if (!prefix.startsWith("/") || !LOG_GROUP_NAME_CHARACTER.test(prefix)) {
    throw new InvalidArgumentError(
      "prefijo de grupo de logs inválido: empieza por «/» y sólo lleva letras, " +
        "números y «_ . # - /»",
    );
  }
  return prefix;
}

/** Un nombre de grupo explícito, con la misma gramática que el prefijo. */
export function validateLogGroup(name: string): string {
  if (!LOG_GROUP_NAME_CHARACTER.test(name)) {
    throw new InvalidArgumentError(
      "nombre de grupo de logs inválido: sólo letras, números y «_ . # - /»",
    );
  }
  return name;
}

/** La cláusula `SOURCE` de las consultas: los grupos nombrados o, sin ninguno, todos los que empiezan por `prefix`. */
function logSource(prefix: string, logGroups: readonly string[]): string {
  if (logGroups.length > 0) {
    return logGroups.map((name) => `SOURCE '${name}'`).join(" | ");
  }
  return `SOURCE logGroups(namePrefix: ['${prefix}'])`;
}

function query(source: string, ...clauses: string[]): string {
  return `${source} | ${clauses.join("\n| ")}`;
}

function logWidget(
  region: string,
  title: string,
  logQuery: string,
  view: string,
  width: number,
): Widget {
  return {
    type: "log",
    width,
    height: CHART_HEIGHT,
    properties: { region, title, query: logQuery, view },
  };
}

/** El encabezado y los cinco widgets de Logs Insights (siempre presentes). */
function logWidgets(region: string, source: string, scope: string): Widget[] {
  const errors = `filter @message like ${ERROR_PATTERN}`;
  return [
    {
      type: "text",
      width: GRID_COLUMNS,
      height: HEADER_HEIGHT,
      properties: { markdown: HEADER_MARKDOWN(scope) },
    },
    logWidget(
      region,
      "Líneas de log por intervalo",
      query(source, `stats count(*) as lines by bin(${LOG_BIN})`),
      "timeSeries",
      HALF_WIDTH,
    ),
    logWidget(
      region,
      "Errores por intervalo",
      query(source, errors, `stats count(*) as errors by bin(${LOG_BIN})`),
      "timeSeries",
      HALF_WIDTH,
    ),
    logWidget(
      region,
      "Líneas por grupo de logs",
      query(source, "stats count(*) as lines by @log", "sort lines desc", `limit ${TOP_ROWS}`),
      "table",
      HALF_WIDTH,
    ),
    logWidget(
      region,
      "Streams más ruidosos",
      query(
        source,
        "stats count(*) as lines by @logStream",
        "sort lines desc",
        `limit ${TOP_ROWS}`,
      ),
      "table",
      HALF_WIDTH,
    ),
    logWidget(
      region,
      "Errores recientes",
      query(
        source,
        errors,
        "fields @timestamp, @logStream, @message",
        "sort @timestamp desc",
        `limit ${RECENT_ERROR_ROWS}`,
      ),
      "table",
      GRID_COLUMNS,
    ),
  ];
}

function promql(id: string, promqlQuery: string, label: string): Record<string, string> {
  return { id, type: "cloudwatch-metrics", language: "PromQL", query: promqlQuery, label };
}

function chart(
  region: string,
  title: string,
  description: string,
  queries: Array<Record<string, string>>,
): Widget {
  return {
    type: "chart",
    width: HALF_WIDTH,
    height: CHART_HEIGHT,
    properties: {
      view: CHART_VIEW,
      title,
      description,
      region,
      data: { queries },
    },
  };
}

function gauge(metric: string): string {
  return `avg({"${metric}"})`;
}

/** CPU, memoria y disco de los sandboxes que exportan por OTLP (`telemetry`), con PromQL (medias entre sandboxes). */
function otlpWidgets(region: string): Widget[] {
  const note = "Métricas OTLP de rayd (telemetry=); media entre sandboxes.";
  return [
    chart(region, "CPU en uso (%)", note, [promql("cpu", gauge(OTLP_CPU_USED_PCT), "CPU %")]),
    chart(region, "Memoria (bytes)", note, [
      promql("used", gauge(OTLP_MEMORY_USED_BYTES), "en uso"),
      promql("total", gauge(OTLP_MEMORY_TOTAL_BYTES), "total"),
    ]),
    chart(region, "Disco (bytes)", note, [
      promql("used", gauge(OTLP_DISK_USED_BYTES), "en uso"),
      promql("total", gauge(OTLP_DISK_TOTAL_BYTES), "total"),
    ]),
  ];
}

/** La memoria asignada contra la cuota (la métrica que publica `quota-alerts`) con los umbrales de aviso y crítico como líneas. */
function quotaWidgets(region: string): Widget[] {
  return [
    {
      type: "metric",
      width: HALF_WIDTH,
      height: CHART_HEIGHT,
      properties: {
        region,
        title: "Memoria asignada / cuota",
        view: "timeSeries",
        metrics: [[QUOTA_METRIC_NAMESPACE, QUOTA_METRIC_NAME, "Region", region]],
        stat: "Maximum",
        period: QUOTA_PERIOD_SECONDS,
        yAxis: { left: { min: 0, max: PERCENT_AXIS_MAX } },
        annotations: {
          horizontal: [
            { label: "Aviso", value: QUOTA_WARNING_FRACTION },
            { label: "Crítico", value: QUOTA_CRITICAL_FRACTION },
          ],
        },
      },
    },
  ];
}

export interface BoardOptions {
  readonly region: string;
  readonly logGroupPrefix?: string | undefined;
  readonly logGroups?: readonly string[] | undefined;
  readonly includeOtlp?: boolean | undefined;
  readonly includeQuota?: boolean | undefined;
}

/** El `DashboardBody`. Valida el prefijo y los nombres antes de construir ninguna consulta. */
export function buildBoard(options: BoardOptions): DashboardBoard {
  const prefix = validateLogGroupPrefix(options.logGroupPrefix ?? DEFAULT_LOG_GROUP_PREFIX);
  const groups = (options.logGroups ?? []).map(validateLogGroup);
  const scope = groups.length > 0 ? groups.join(", ") : `${prefix}*`;
  const widgets = logWidgets(options.region, logSource(prefix, groups), scope);
  if (options.includeOtlp === true) {
    widgets.push(...otlpWidgets(options.region));
  }
  if (options.includeQuota === true) {
    widgets.push(...quotaWidgets(options.region));
  }
  return { start: DASHBOARD_START, widgets };
}

/** El JSON del dashboard, con la sangría que usa `rayito dashboard cloudwatch`. */
export function renderBoard(board: DashboardBoard, indented = false): string {
  return JSON.stringify(board, null, indented ? JSON_INDENT : undefined);
}
