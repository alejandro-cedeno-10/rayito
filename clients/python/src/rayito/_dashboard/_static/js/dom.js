// Ayudantes de DOM y de formato. Todo el texto entra por `textContent`:
// ningún dato de la API se interpreta como HTML. Los estilos viven en hojas
// del propio origen (la CSP no admite `style=`): aquí sólo se ponen clases y
// atributos de presentación SVG.

const SVG_NS = "http://www.w3.org/2000/svg";

function assign(element, attrs) {
  for (const [name, value] of Object.entries(attrs)) {
    if (value === false || value === null || value === undefined) continue;
    if (name.startsWith("on") && typeof value === "function") {
      element.addEventListener(name.slice(2), value);
    } else if (name === "class") {
      element.setAttribute("class", value);
    } else if (value === true) {
      element.setAttribute(name, "");
    } else {
      element.setAttribute(name, String(value));
    }
  }
}

/**
 * Lo que una vista puede pintar: aplana y descarta `null`, `undefined` y
 * `false`, para que un bloque opcional (un aviso que no aplica) no salga
 * escrito como el texto «null».
 */
export function renderableNodes(content) {
  return [].concat(content).flat().filter((child) => child !== null && child !== undefined && child !== false);
}

function adopt(element, children) {
  element.append(...renderableNodes(children));
  return element;
}

export function h(tag, attrs = {}, ...children) {
  const element = document.createElement(tag);
  assign(element, attrs);
  return adopt(element, children);
}

/** Como `h`, para elementos SVG. */
export function s(tag, attrs = {}, ...children) {
  const element = document.createElementNS(SVG_NS, tag);
  assign(element, attrs);
  return adopt(element, children);
}

export const MS_PER_SECOND = 1000;
const SECONDS_PER_MINUTE = 60;
const SECONDS_PER_HOUR = 3600;
const SECONDS_PER_DAY = 86400;
const MIB_PER_GB = 1024;
const USD_MIN_FRACTION_DIGITS = 2;
const USD_MAX_FRACTION_DIGITS = 4;
const NUMBER_MAX_FRACTION_DIGITS = 2;
const SHORT_ID_HEAD = 12;
const SHORT_ID_TAIL = 4;
const COPIED_FEEDBACK_MS = 1500;
const ETA_FLOOR_SECONDS = 1;
const NOT_AVAILABLE = "n/d";

export function formatUsd(value) {
  if (value === null || value === undefined) return NOT_AVAILABLE;
  return new Intl.NumberFormat("es", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: USD_MIN_FRACTION_DIGITS,
    maximumFractionDigits: USD_MAX_FRACTION_DIGITS,
  }).format(value);
}

export function formatMemory(mib) {
  if (mib === null || mib === undefined) return NOT_AVAILABLE;
  return mib >= MIB_PER_GB ? `${mib / MIB_PER_GB} GB` : `${mib} MiB`;
}

export function formatPercent(fraction) {
  if (fraction === null || fraction === undefined) return NOT_AVAILABLE;
  return new Intl.NumberFormat("es", { style: "percent", maximumFractionDigits: 0 }).format(fraction);
}

export function formatNumber(value) {
  if (value === null || value === undefined) return NOT_AVAILABLE;
  return new Intl.NumberFormat("es", { maximumFractionDigits: NUMBER_MAX_FRACTION_DIGITS }).format(value);
}

function splitDuration(totalSeconds) {
  const seconds = Math.max(0, Math.round(totalSeconds));
  return {
    days: Math.floor(seconds / SECONDS_PER_DAY),
    hours: Math.floor((seconds % SECONDS_PER_DAY) / SECONDS_PER_HOUR),
    minutes: Math.floor((seconds % SECONDS_PER_HOUR) / SECONDS_PER_MINUTE),
    seconds: seconds % SECONDS_PER_MINUTE,
    total: seconds,
  };
}

/** Duración en prosa: «2 d 3 h», «4 h 12 min». */
export function formatDuration(totalSeconds) {
  const { days, hours, minutes, seconds } = splitDuration(totalSeconds);
  if (days > 0) return `${days} d ${hours} h`;
  if (hours > 0) return `${hours} h ${minutes} min`;
  if (minutes > 0) return `${minutes} min ${seconds} s`;
  return `${seconds} s`;
}

/** Lo que tarda una cola, en prosa: una cola de menos de un segundo no es «0 s». */
export function formatEta(totalSeconds) {
  return totalSeconds < ETA_FLOOR_SECONDS ? "menos de 1 s" : `unos ${formatDuration(totalSeconds)}`;
}

/** «1 sandbox» / «3 sandboxes»: la concordancia de número del panel. */
export function plural(count, singular, pluralForm) {
  return `${count} ${count === 1 ? singular : pluralForm}`;
}

export function countSandboxes(count) {
  return plural(count, "sandbox", "sandboxes");
}

/** Duración para celdas de tabla: «2d 3h», «4h 12m», «12m 5s», «45s». Sin espacios
 * dentro de cada unidad, para que la celda no se parta en varias líneas. */
export function formatCompactDuration(totalSeconds) {
  const { days, hours, minutes, seconds } = splitDuration(totalSeconds);
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m ${seconds}s`;
  return `${seconds}s`;
}

export function formatHours(totalSeconds) {
  return `${formatNumber(totalSeconds / SECONDS_PER_HOUR)} h`;
}

export function formatDate(iso) {
  const moment = new Date(iso);
  return Number.isNaN(moment.getTime()) ? NOT_AVAILABLE : moment.toLocaleString("es");
}

const RELATIVE_UNITS = [
  { unit: "day", seconds: SECONDS_PER_DAY },
  { unit: "hour", seconds: SECONDS_PER_HOUR },
  { unit: "minute", seconds: SECONDS_PER_MINUTE },
  { unit: "second", seconds: 1 },
];

/** «hace 5 min» respecto de ahora. */
export function formatRelative(iso, now = Date.now()) {
  const moment = new Date(iso).getTime();
  if (Number.isNaN(moment)) return NOT_AVAILABLE;
  const elapsed = Math.max(0, (now - moment) / MS_PER_SECOND);
  const { unit, seconds } = RELATIVE_UNITS.find((entry) => elapsed >= entry.seconds) ?? RELATIVE_UNITS[RELATIVE_UNITS.length - 1];
  return new Intl.RelativeTimeFormat("es", { numeric: "auto", style: "short" }).format(-Math.floor(elapsed / seconds), unit);
}

/** El ID abreviado para tablas; el completo va en `title` y en el portapapeles. */
export function shortId(id) {
  const head = SHORT_ID_HEAD;
  const tail = SHORT_ID_TAIL;
  return id.length > head + tail + 1 ? `${id.slice(0, head)}…${id.slice(-tail)}` : id;
}

const LEVEL_LABELS = { ok: "OK", warning: "Aviso", critical: "Crítico" };

export function pill(text, tone = "neutral") {
  return h("span", { class: `pill ${tone}` }, text);
}

export function levelPill(level) {
  return pill(LEVEL_LABELS[level] ?? level, level);
}

export function safeLink(href, text) {
  if (typeof href !== "string" || !href.startsWith("https://")) return h("span", {}, text);
  return h("a", { href, target: "_blank", rel: "noopener noreferrer" }, text);
}

/** Botón con el texto del botón y de la acción, para quien usa lector de pantalla. */
export function copyButton(text, label) {
  const button = h("button", { type: "button", class: "icon-button", "aria-label": label, title: label });
  button.append(icon("copy"));
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      return;
    }
    button.replaceChildren(icon("check"));
    button.classList.add("done");
    setTimeout(() => {
      button.replaceChildren(icon("copy"));
      button.classList.remove("done");
    }, COPIED_FEEDBACK_MS);
  });
  return button;
}

const ICON_VIEWBOX = "0 0 24 24";
const ICON_PATHS = {
  overview: "M3 13h8V3H3zM13 21h8v-8h-8zM13 3v6h8V3zM3 21h8v-6H3z",
  sandboxes: "M21 8l-9-5-9 5v8l9 5 9-5zM3 8l9 5 9-5M12 13v8",
  quotas: "M3.5 17a9 9 0 1 1 17 0M12 14l4-4",
  cost: "M12 3v18M16 7.5c-1-1-2.5-1.5-4-1.5-2.2 0-4 1-4 2.8 0 4 8 2 8 6 0 1.8-1.8 2.7-4 2.7-1.6 0-3.2-.6-4.2-1.7",
  images: "M12 3l9 5-9 5-9-5zM3 13l9 5 9-5M3 17.5l9 5 9-5",
  events: "M3 12h4l3-8 4 16 3-8h4",
  copy: "M9 9h11v11H9zM5 15V5h10",
  check: "M5 13l4 4L19 7",
  more: "M5 12h.01M12 12h.01M19 12h.01",
  refresh: "M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7",
  alert: "M12 9v4M12 17h.01M10.3 3.9L2.4 18a2 2 0 0 0 1.7 3h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z",
  inbox: "M22 12h-6l-2 3h-4l-2-3H2M5.5 5h13L22 12v6a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2v-6z",
  bolt: "M13 2L4 14h7l-1 8 9-12h-7z",
  help: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.7.4-1 .9-1 1.7M12 17h.01",
};

export function icon(name) {
  return s("svg", { class: "icon", viewBox: ICON_VIEWBOX, "aria-hidden": "true", focusable: "false" }, s("path", { d: ICON_PATHS[name] ?? "" }));
}
