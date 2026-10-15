// Gráficas de línea en SVG en línea, sin librerías ni compilación. Los colores
// salen de los tokens `--chart-N` por clase CSS: un tema de usuario las cambia.
import { h, s } from "./dom.js";

const CHART_WIDTH = 320;
const CHART_HEIGHT = 96;
const CHART_PAD_X = 4;
const CHART_PAD_Y = 8;
const GRID_LINES = 3;
const POINT_RADIUS = 3;
const MIN_SAMPLES_FOR_LINE = 2;
const DECIMALS = 2;

function scaleX(index, count) {
  const inner = CHART_WIDTH - 2 * CHART_PAD_X;
  return CHART_PAD_X + (count === 1 ? inner : (inner * index) / (count - 1));
}

function scaleY(value, max) {
  const inner = CHART_HEIGHT - 2 * CHART_PAD_Y;
  return CHART_HEIGHT - CHART_PAD_Y - (max === 0 ? 0 : (inner * value) / max);
}

function points(samples, key, max) {
  return samples.map((sample, index) => [scaleX(index, samples.length), scaleY(sample[key] ?? 0, max)]);
}

function path(coordinates) {
  return coordinates.map(([x, y], index) => `${index === 0 ? "M" : "L"}${x.toFixed(DECIMALS)} ${y.toFixed(DECIMALS)}`).join(" ");
}

function grid() {
  const lines = [];
  for (let step = 0; step < GRID_LINES; step += 1) {
    const y = CHART_PAD_Y + ((CHART_HEIGHT - 2 * CHART_PAD_Y) * step) / (GRID_LINES - 1);
    lines.push(s("line", { class: "chart-grid", x1: 0, x2: CHART_WIDTH, y1: y, y2: y }));
  }
  return lines;
}

/**
 * Una gráfica con una o más series.
 * @param {{ samples: object[], series: { key: string, label: string }[], summary: string, empty: string }} options
 */
export function lineChart({ samples, series, summary, empty }) {
  if (samples.length === 0) return h("p", { class: "muted chart-empty" }, empty);
  const max = Math.max(...samples.flatMap((sample) => series.map((item) => sample[item.key] ?? 0)));
  const layers = series.map((item, index) => {
    const coordinates = points(samples, item.key, max);
    const last = coordinates[coordinates.length - 1];
    const line = samples.length >= MIN_SAMPLES_FOR_LINE ? s("path", { class: `chart-line series-${index + 1}`, d: path(coordinates) }) : null;
    const area = samples.length >= MIN_SAMPLES_FOR_LINE && index === 0
      ? s("path", { class: "chart-area series-1", d: `${path(coordinates)} L${last[0]} ${CHART_HEIGHT - CHART_PAD_Y} L${coordinates[0][0]} ${CHART_HEIGHT - CHART_PAD_Y} Z` })
      : null;
    return [area, line, s("circle", { class: `chart-point series-${index + 1}`, cx: last[0], cy: last[1], r: POINT_RADIUS })];
  });
  return s("svg", { class: "chart", viewBox: `0 0 ${CHART_WIDTH} ${CHART_HEIGHT}`, role: "img", "aria-label": summary, preserveAspectRatio: "none" },
    s("title", {}, summary), ...grid(), ...layers);
}

export function legend(series) {
  return h("ul", { class: "legend", "aria-hidden": "true" }, ...series.map((item, index) =>
    h("li", {}, h("span", { class: `swatch series-${index + 1}` }), item.label)));
}

const BAR_WIDTH = 100;
const BAR_HEIGHT = 6;
const BAR_RADIUS = 3;
const PERCENT = 100;

/** Barra de uso de 0 a 1; el tono (`ok`, `warning`, `critical`) lo pone el nivel que calcula la API. */
export function usageBar(fraction, level) {
  const filled = Math.min(1, Math.max(0, fraction ?? 0)) * BAR_WIDTH;
  return s("svg", { class: `meter ${level}`, viewBox: `0 0 ${BAR_WIDTH} ${BAR_HEIGHT}`, preserveAspectRatio: "none", role: "img", "aria-label": `Uso: ${Math.round((fraction ?? 0) * PERCENT)} %` },
    s("rect", { class: "meter-track", width: BAR_WIDTH, height: BAR_HEIGHT, rx: BAR_RADIUS }),
    s("rect", { class: "meter-fill", width: filled, height: BAR_HEIGHT, rx: BAR_RADIUS }));
}
