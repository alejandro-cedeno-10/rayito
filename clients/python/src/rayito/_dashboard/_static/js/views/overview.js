import { describe, get } from "../api.js";
import { formatCompactDuration, formatNumber, formatPercent, formatUsd, h, levelPill, MS_PER_SECOND, safeLink } from "../dom.js";
import { legend, lineChart, usageBar } from "../charts.js";
import { history, lastBilled, recordSample } from "../state.js";
import { pageHead } from "../states.js";
import { View } from "../view.js";

const ACTIVE_STATES = ["RUNNING"];
const PENDING_STATES = ["PENDING"];
const SUSPENDED_STATES = ["SUSPENDED", "SUSPENDING"];
const SOURCES = ["sandboxes", "quotas", "cost"];

function count(sandboxes, states) {
  return sandboxes.filter((sandbox) => states.includes(sandbox.state)).length;
}

function card(title, body, { wide = false } = {}) {
  return h("section", { class: wide ? "card stat wide" : "card stat", "aria-label": title },
    h("h2", { class: "stat-label" }, title), ...body);
}

function unavailable(title, result) {
  return card(title, [h("p", { class: "stat-value muted" }, "n/d"), h("p", { class: "stat-sub error" }, describe(result.reason))]);
}

function activeCard(result) {
  if (result.status === "rejected") return unavailable("Sandboxes activos", result);
  const { sandboxes } = result.value;
  const pending = count(sandboxes, PENDING_STATES);
  return card("Sandboxes activos", [
    h("p", { class: "stat-value" }, formatNumber(count(sandboxes, ACTIVE_STATES))),
    h("p", { class: "stat-sub muted" }, pending > 0 ? `${pending} arrancando · ${sandboxes.length} en total` : `${sandboxes.length} en total en la región`),
  ]);
}

function suspendedCard(result) {
  if (result.status === "rejected") return unavailable("Suspendidos", result);
  return card("Suspendidos", [
    h("p", { class: "stat-value" }, formatNumber(count(result.value.sandboxes, SUSPENDED_STATES))),
    h("p", { class: "stat-sub muted" }, "Conservan su snapshot, que tiene coste de almacenamiento."),
  ]);
}

function memoryCard(result) {
  if (result.status === "rejected") return unavailable("Memoria de la cuota", result);
  const { memory } = result.value;
  if (memory.quotaGb === null) {
    return card("Memoria de la cuota", [h("p", { class: "stat-value" }, `${formatNumber(memory.allocatedGb)} GB`), h("p", { class: "stat-sub muted" }, "Service Quotas no lista la cuota de memoria de esta región.")]);
  }
  return card("Memoria de la cuota", [
    h("p", { class: "stat-value" }, `${formatNumber(memory.allocatedGb)} `, h("span", { class: "stat-unit" }, `de ${formatNumber(memory.quotaGb)} GB`)),
    usageBar(memory.fraction, memory.level),
    h("p", { class: "stat-sub" }, `${formatPercent(memory.fraction)} usado `, levelPill(memory.level)),
  ]);
}

function estimatedCard(result) {
  if (result.status === "rejected") return unavailable("Coste estimado", result);
  const { estimated } = result.value;
  return card("Coste estimado", [
    h("p", { class: "stat-value" }, formatUsd(estimated.totalUsd)),
    h("p", { class: "stat-sub muted" }, "Sandboxes en ejecución desde su arranque. No sustituye a la factura."),
  ]);
}

function billedCard(result) {
  const costExplorer = result.status === "fulfilled" ? result.value.costExplorer : null;
  const billed = lastBilled();
  if (costExplorer !== null && !costExplorer.enabled) {
    return card("Coste facturado", [h("p", { class: "stat-value stat-value-text muted" }, "Desactivado"), h("p", { class: "stat-sub muted" }, "Cada consulta a Cost Explorer se factura: arranca el panel con ", h("code", {}, "--cost-explorer"), ".")]);
  }
  return card("Coste facturado", [
    h("p", { class: billed ? "stat-value" : "stat-value stat-value-text muted" }, billed ? formatUsd(billed.totalUsd) : "Bajo demanda"),
    h("p", { class: "stat-sub muted" }, billed ? `Del ${billed.start} al ${billed.end} (excluido). ` : "Se consulta a mano porque cuesta dinero. ", h("a", { href: "#/cost" }, "Ir a Coste")),
  ]);
}

function warnings(result) {
  if (result.status !== "fulfilled" || result.value.warnings.length === 0) return null;
  return h("ul", { class: "alerts", "aria-label": "Avisos de cuota" }, ...result.value.warnings.map((warning) =>
    h("li", {}, warning.message, " ", safeLink(warning.increaseUrl, "Pedir un aumento"))));
}

function windowLabel(samples) {
  if (samples.length < 2) return "ahora";
  return `hace ${formatCompactDuration((Date.now() - samples[0].at) / MS_PER_SECOND)}`;
}

function chartCard(title, series, samples, summary, format) {
  const chart = lineChart({ samples, series, summary, empty: "Recogiendo muestras…" });
  return h("section", { class: "card chart-card", "aria-label": title },
    h("div", { class: "chart-head" }, h("h2", { class: "stat-label" }, title), legend(series)),
    chart,
    h("div", { class: "chart-axis muted" }, h("span", {}, windowLabel(samples)), h("span", {}, format(samples[samples.length - 1]))));
}

class RayitoOverview extends View {
  async load() {
    const settled = await Promise.allSettled([get("/sandboxes"), get("/quotas"), get("/cost")]);
    const results = Object.fromEntries(SOURCES.map((name, index) => [name, settled[index]]));
    if (results.sandboxes.status === "fulfilled" && results.cost.status === "fulfilled") {
      const list = results.sandboxes.value.sandboxes;
      recordSample({ at: Date.now(), running: count(list, ACTIVE_STATES), suspended: count(list, SUSPENDED_STATES), costUsd: results.cost.value.estimated.totalUsd });
    }
    return results;
  }

  render({ sandboxes, quotas, cost }) {
    const samples = history();
    const last = samples[samples.length - 1];
    return [
      pageHead("Resumen", `Región ${this.meta.region}. Se actualiza cada ${this.meta.pollIntervalSeconds} s.`,
        h("button", { type: "button", class: "button", onclick: () => this.refresh() }, "Actualizar")),
      warnings(quotas),
      h("div", { class: "stats" }, activeCard(sandboxes), suspendedCard(sandboxes), memoryCard(quotas), estimatedCard(cost), billedCard(cost)),
      h("div", { class: "charts" },
        chartCard("Sandboxes en el tiempo", [{ key: "running", label: "Activos" }, { key: "suspended", label: "Suspendidos" }], samples,
          last ? `Sandboxes activos: ${last.running}; suspendidos: ${last.suspended}.` : "Sin muestras.",
          (sample) => (sample ? `${sample.running} activos · ${sample.suspended} susp.` : "")),
        chartCard("Coste estimado en el tiempo", [{ key: "costUsd", label: "Estimado" }], samples,
          last ? `Coste estimado actual: ${formatUsd(last.costUsd)}.` : "Sin muestras.",
          (sample) => (sample ? formatUsd(sample.costUsd) : ""))),
      h("p", { class: "muted footnote" }, "Las gráficas muestran lo observado desde que abriste el panel; no se guarda nada en el navegador."),
    ];
  }
}

customElements.define("rayito-overview", RayitoOverview);
