import { get } from "../api.js";
import { usageBar } from "../charts.js";
import { formatNumber, formatPercent, h, levelPill, safeLink } from "../dom.js";
import { pageHead } from "../states.js";
import { View } from "../view.js";

const RATE_KIND = "rate";
const BURST_KIND = "burst";
const MEMORY_KIND = "memory-gb";

function byName(left, right) {
  return left.localeCompare(right, "es");
}

function memoryCard(report) {
  const { memory } = report;
  const entry = report.entries.find((item) => item.kind === MEMORY_KIND);
  const title = h("h2", { id: "memory-title", class: "stat-label" }, "Memoria asignada de la región");
  if (memory.quotaGb === null) {
    return h("section", { class: "card", "aria-labelledby": "memory-title" }, title,
      h("p", { class: "muted" }, "Service Quotas no lista la cuota de memoria de esta región."));
  }
  return h("section", { class: "card", "aria-labelledby": "memory-title" },
    title,
    h("p", { class: "stat-value" }, `${formatNumber(memory.allocatedGb)} `, h("span", { class: "stat-unit" }, `de ${formatNumber(memory.quotaGb)} GB · ${formatPercent(memory.fraction)} `), levelPill(memory.level)),
    usageBar(memory.fraction, memory.level),
    h("p", { class: "muted" }, `${memory.sandboxes} sandboxes cuentan (en ejecución y suspendidos).`,
      memory.unresolved > 0 ? ` No se pudo leer la memoria de ${memory.unresolved}: el uso real puede ser mayor.` : ""),
    entry ? h("p", {}, safeLink(entry.increaseUrl, "Pedir un aumento de cuota")) : null,
  );
}

function warningList(report) {
  if (report.warnings.length === 0) return null;
  return h("ul", { class: "alerts", "aria-label": "Avisos de cuota" },
    ...report.warnings.map((warning) =>
      h("li", {}, warning.message, " ", safeLink(warning.increaseUrl, "Pedir un aumento"))));
}

/** Una fila por operación con su tasa y su ráfaga: el límite que aplica la cola de acciones masivas. */
function rateRows(report) {
  const limits = new Map(report.tps.map((limit) => [limit.operation, limit]));
  const quotas = new Map();
  for (const entry of report.entries) {
    if (entry.kind !== RATE_KIND && entry.kind !== BURST_KIND) continue;
    quotas.set(entry.operation, { ...quotas.get(entry.operation), [entry.kind]: entry });
  }
  return [...new Set([...limits.keys(), ...quotas.keys()])].sort(byName).map((operation) => ({
    operation,
    limit: limits.get(operation),
    quota: quotas.get(operation) ?? {},
  }));
}

function applied(entry, effective) {
  const value = effective ?? entry?.value ?? entry?.default;
  const isDefault = entry !== undefined && (entry.value === null || entry.value === entry.default);
  return h("td", { class: "num nowrap" }, formatNumber(value), isDefault ? h("span", { class: "muted" }, " (def.)") : null);
}

function ratesTable(report) {
  const rows = rateRows(report);
  return h("div", { class: "table-scroll" }, h("table", { class: "dense" },
    h("caption", { class: "visually-hidden" }, "Límites de tasa por operación; las acciones masivas los respetan."),
    h("thead", {}, h("tr", {}, h("th", { scope: "col" }, "Operación"), h("th", { scope: "col", class: "num" }, "Tasa (por s)"), h("th", { scope: "col", class: "num" }, "Ráfaga"), h("th", { scope: "col" }, "Ajustable"), h("th", { scope: "col" }, "Aumento"))),
    h("tbody", {}, ...rows.map(({ operation, limit, quota }) => {
      const reference = quota[RATE_KIND] ?? quota[BURST_KIND];
      return h("tr", {},
        h("th", { scope: "row", class: "mono" }, operation),
        applied(quota[RATE_KIND], limit?.rate),
        applied(quota[BURST_KIND], limit?.burst),
        h("td", {}, reference ? (reference.adjustable ? "Sí" : "No") : "n/d"),
        h("td", {}, reference ? safeLink(reference.increaseUrl, "Solicitar") : ""));
    }))));
}

function capacityTable(report) {
  const rows = report.entries.filter((entry) => entry.kind !== RATE_KIND && entry.kind !== BURST_KIND).sort((left, right) => byName(left.name, right.name));
  if (rows.length === 0) return h("p", { class: "muted" }, "Sin cuotas de capacidad en esta región.");
  return h("div", { class: "table-scroll" }, h("table", { class: "dense" },
    h("caption", { class: "visually-hidden" }, "Cuotas de capacidad de Lambda MicroVMs en la región."),
    h("thead", {}, h("tr", {}, h("th", { scope: "col" }, "Cuota"), h("th", { scope: "col", class: "num" }, "Aplicada"), h("th", { scope: "col", class: "num" }, "Por defecto"), h("th", { scope: "col" }, "Ajustable"), h("th", { scope: "col" }, "Aumento"))),
    h("tbody", {}, ...rows.map((entry) => h("tr", {},
      h("th", { scope: "row" }, entry.name),
      h("td", { class: "num nowrap" }, entry.value === null ? "no listada" : formatNumber(entry.value)),
      h("td", { class: "num nowrap" }, formatNumber(entry.default)),
      h("td", {}, entry.adjustable ? "Sí" : "No"),
      h("td", {}, safeLink(entry.increaseUrl, "Solicitar")))))));
}

class RayitoQuotas extends View {
  load() {
    return get("/quotas");
  }

  render(report) {
    return [
      pageHead("Cuotas", `Límites de Lambda MicroVMs en ${report.region}.`),
      warningList(report),
      memoryCard(report),
      h("h2", { class: "section-title" }, "Tasas por operación"),
      ratesTable(report),
      h("h2", { class: "section-title" }, "Capacidad"),
      capacityTable(report),
    ];
  }
}

customElements.define("rayito-quotas", RayitoQuotas);
