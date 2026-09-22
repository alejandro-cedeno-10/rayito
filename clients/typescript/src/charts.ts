/**
 * Gráficos extraídos de matplotlib por el kernel (`e2b/chart`), con la misma
 * forma que los de E2B: un `Chart` por tipo y `parseChart` para el documento
 * JSON que viaja en `ExecutionResult.chart`. Sin I/O.
 *
 * `parseChart` nunca falla por un documento incompleto: cada campo ausente
 * queda en `undefined` o `[]`, y un `type` desconocido (o un documento que no
 * es un objeto JSON) produce un `Chart` de tipo `unknown`.
 */

export const ChartType = {
  LINE: "line",
  SCATTER: "scatter",
  BAR: "bar",
  PIE: "pie",
  BOX_AND_WHISKER: "box_and_whisker",
  SUPERCHART: "superchart",
  UNKNOWN: "unknown",
} as const;
export type ChartType = (typeof ChartType)[keyof typeof ChartType];

export const ScaleType = {
  LINEAR: "linear",
  DATETIME: "datetime",
  CATEGORICAL: "categorical",
  LOG: "log",
  SYMLOG: "symlog",
  LOGIT: "logit",
  FUNCTION: "function",
  FUNCTIONLOG: "functionlog",
  ASINH: "asinh",
} as const;
export type ScaleType = (typeof ScaleType)[keyof typeof ScaleType];

export type Coordinate = number | string;
export type Point = readonly [Coordinate, Coordinate];

export interface Chart {
  readonly type: ChartType;
  readonly title: string | undefined;
  readonly elements: readonly unknown[];
}

export interface Chart2D extends Chart {
  readonly xLabel: string | undefined;
  readonly yLabel: string | undefined;
  readonly xUnit: string | undefined;
  readonly yUnit: string | undefined;
}

export interface PointData {
  readonly label: string | undefined;
  readonly points: readonly Point[];
}

export interface PointChart extends Chart2D {
  readonly xTicks: readonly Coordinate[];
  readonly xTickLabels: readonly string[];
  readonly xScale: ScaleType | undefined;
  readonly yTicks: readonly Coordinate[];
  readonly yTickLabels: readonly string[];
  readonly yScale: ScaleType | undefined;
  readonly elements: readonly PointData[];
}

export interface LineChart extends PointChart {
  readonly type: typeof ChartType.LINE;
}

export interface ScatterChart extends PointChart {
  readonly type: typeof ChartType.SCATTER;
}

export interface BarData {
  readonly label: string | undefined;
  readonly group: string | undefined;
  readonly value: Coordinate | undefined;
}

export interface BarChart extends Chart2D {
  readonly type: typeof ChartType.BAR;
  readonly elements: readonly BarData[];
}

export interface PieData {
  readonly label: string | undefined;
  readonly angle: number | undefined;
  readonly radius: number | undefined;
  readonly autopct: Coordinate | undefined;
}

export interface PieChart extends Chart {
  readonly type: typeof ChartType.PIE;
  readonly elements: readonly PieData[];
}

export interface BoxAndWhiskerData {
  readonly label: string | undefined;
  readonly min: number | undefined;
  readonly firstQuartile: number | undefined;
  readonly median: number | undefined;
  readonly thirdQuartile: number | undefined;
  readonly max: number | undefined;
  readonly outliers: readonly number[];
}

export interface BoxAndWhiskerChart extends Chart2D {
  readonly type: typeof ChartType.BOX_AND_WHISKER;
  readonly elements: readonly BoxAndWhiskerData[];
}

export interface SuperChart extends Chart {
  readonly type: typeof ChartType.SUPERCHART;
  readonly elements: readonly Chart[];
}

export type ChartDocument = Readonly<Record<string, unknown>>;

const CHART_TYPES: ReadonlySet<string> = new Set(Object.values(ChartType));
const SCALE_TYPES: ReadonlySet<string> = new Set(Object.values(ScaleType));

/**
 * Convierte el documento `e2b/chart` (JSON o un objeto ya parseado) en el
 * `Chart` de su tipo; nunca lanza por campos ausentes o de tipo inesperado.
 */
export function parseChart(document: string | ChartDocument): Chart {
  const data = asDocument(document);
  if (data === undefined) {
    return Object.freeze({ type: ChartType.UNKNOWN, title: undefined, elements: [] });
  }
  const parser = CHART_PARSERS[chartType(data.type)];
  if (parser === undefined) {
    return Object.freeze({
      type: ChartType.UNKNOWN,
      title: optionalString(data.title),
      elements: [],
    });
  }
  return parser(data);
}

function asDocument(document: unknown): ChartDocument | undefined {
  if (isPlainObject(document)) {
    return document;
  }
  if (typeof document !== "string") {
    return undefined;
  }
  try {
    const loaded: unknown = JSON.parse(document);
    return isPlainObject(loaded) ? loaded : undefined;
  } catch {
    return undefined;
  }
}

function isPlainObject(value: unknown): value is ChartDocument {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function chartType(value: unknown): ChartType {
  const text = String(value);
  return CHART_TYPES.has(text) ? (text as ChartType) : ChartType.UNKNOWN;
}

function scaleType(value: unknown): ScaleType | undefined {
  if (value === null || value === undefined) {
    return undefined;
  }
  const text = String(value);
  return SCALE_TYPES.has(text) ? (text as ScaleType) : undefined;
}

function optionalString(value: unknown): string | undefined {
  return value === null || value === undefined ? undefined : String(value);
}

function optionalNumber(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function coordinate(value: unknown): Coordinate | undefined {
  if (typeof value === "boolean") {
    return undefined;
  }
  if (typeof value === "number") {
    return value;
  }
  return value === null || value === undefined ? undefined : String(value);
}

function coordinates(value: unknown): Coordinate[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.map(coordinate).filter((item): item is Coordinate => item !== undefined);
}

function numbers(value: unknown): number[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.map(optionalNumber).filter((item): item is number => item !== undefined);
}

function strings(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.map((item) => String(item));
}

function items(value: unknown): ChartDocument[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter(isPlainObject);
}

function point(value: unknown): Point | undefined {
  if (!Array.isArray(value) || value.length !== 2) {
    return undefined;
  }
  const x = coordinate(value[0]);
  const y = coordinate(value[1]);
  if (x === undefined || y === undefined) {
    return undefined;
  }
  return [x, y];
}

function points(value: unknown): Point[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.map(point).filter((item): item is Point => item !== undefined);
}

function pointData(data: ChartDocument): PointData {
  return Object.freeze({ label: optionalString(data.label), points: points(data.points) });
}

function barData(data: ChartDocument): BarData {
  return Object.freeze({
    label: optionalString(data.label),
    group: optionalString(data.group),
    value: coordinate(data.value),
  });
}

function pieData(data: ChartDocument): PieData {
  return Object.freeze({
    label: optionalString(data.label),
    angle: optionalNumber(data.angle),
    radius: optionalNumber(data.radius),
    autopct: coordinate(data.autopct),
  });
}

function boxData(data: ChartDocument): BoxAndWhiskerData {
  return Object.freeze({
    label: optionalString(data.label),
    min: optionalNumber(data.min),
    firstQuartile: optionalNumber(data.first_quartile),
    median: optionalNumber(data.median),
    thirdQuartile: optionalNumber(data.third_quartile),
    max: optionalNumber(data.max),
    outliers: numbers(data.outliers),
  });
}

function chart2dFields(data: ChartDocument): Omit<Chart2D, "type" | "elements"> {
  return {
    title: optionalString(data.title),
    xLabel: optionalString(data.x_label),
    yLabel: optionalString(data.y_label),
    xUnit: optionalString(data.x_unit),
    yUnit: optionalString(data.y_unit),
  };
}

function pointChartFields(data: ChartDocument): Omit<PointChart, "type"> {
  return {
    ...chart2dFields(data),
    xTicks: coordinates(data.x_ticks),
    xTickLabels: strings(data.x_tick_labels),
    xScale: scaleType(data.x_scale),
    yTicks: coordinates(data.y_ticks),
    yTickLabels: strings(data.y_tick_labels),
    yScale: scaleType(data.y_scale),
    elements: items(data.elements).map(pointData),
  };
}

function parseLine(data: ChartDocument): LineChart {
  return Object.freeze({ type: ChartType.LINE, ...pointChartFields(data) });
}

function parseScatter(data: ChartDocument): ScatterChart {
  return Object.freeze({ type: ChartType.SCATTER, ...pointChartFields(data) });
}

function parseBar(data: ChartDocument): BarChart {
  return Object.freeze({
    type: ChartType.BAR,
    ...chart2dFields(data),
    elements: items(data.elements).map(barData),
  });
}

function parsePie(data: ChartDocument): PieChart {
  return Object.freeze({
    type: ChartType.PIE,
    title: optionalString(data.title),
    elements: items(data.elements).map(pieData),
  });
}

function parseBox(data: ChartDocument): BoxAndWhiskerChart {
  return Object.freeze({
    type: ChartType.BOX_AND_WHISKER,
    ...chart2dFields(data),
    elements: items(data.elements).map(boxData),
  });
}

function parseSuper(data: ChartDocument): SuperChart {
  return Object.freeze({
    type: ChartType.SUPERCHART,
    title: optionalString(data.title),
    elements: items(data.elements).map(parseChart),
  });
}

const CHART_PARSERS: Partial<Record<ChartType, (data: ChartDocument) => Chart>> = {
  [ChartType.LINE]: parseLine,
  [ChartType.SCATTER]: parseScatter,
  [ChartType.BAR]: parseBar,
  [ChartType.PIE]: parsePie,
  [ChartType.BOX_AND_WHISKER]: parseBox,
  [ChartType.SUPERCHART]: parseSuper,
};
