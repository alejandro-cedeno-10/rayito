import { describe, get } from "../api.js";
import { formatCompactDuration, formatDate, formatHours, formatMemory, formatNumber, formatUsd, h, pill, plural, shortId } from "../dom.js";
import { rememberBilled } from "../state.js";
import { pageHead } from "../states.js";
import { View } from "../view.js";

function estimatedCard(estimated) {
  const rows = estimated.items.filter((item) => item.costUsd !== null);
  return h("section", { class: "card estimated", "aria-labelledby": "estimated-title" },
    h("h2", { id: "estimated-title", class: "stat-label" }, "Estimado por sandbox ", pill("ESTIMADO", "warning")),
    h("p", { class: "stat-value" }, formatUsd(estimated.totalUsd), " ", h("span", { class: "stat-unit" }, `precios de ${estimated.pricingRegion}`)),
    h("p", { class: "muted" }, estimated.basis),
    rows.length === 0
      ? h("p", { class: "muted" }, "Ningún sandbox en ejecución con memoria conocida.")
      : h("div", { class: "table-scroll" }, h("table", { class: "dense" },
          h("caption", { class: "visually-hidden" }, "Cómputo estimado de los sandboxes en ejecución."),
          h("thead", {}, h("tr", {}, h("th", { scope: "col" }, "Sandbox"), h("th", { scope: "col", class: "num" }, "Memoria"), h("th", { scope: "col", class: "num" }, "Activo"), h("th", { scope: "col", class: "num" }, "Estimado"))),
          h("tbody", {}, ...rows.map((item) => h("tr", {},
            h("th", { scope: "row", class: "mono", title: item.sandboxId }, shortId(item.sandboxId)),
            h("td", { class: "num nowrap" }, formatMemory(item.memoryMib)),
            h("td", { class: "num nowrap" }, formatCompactDuration(item.activeSeconds)),
            h("td", { class: "num nowrap" }, formatUsd(item.costUsd))))))),
  );
}

function billedBody(billed, costExplorer) {
  const lag = formatHours(costExplorer.cacheTtlSeconds);
  return [
    h("p", { class: "stat-value" }, formatUsd(billed.totalUsd), " ", h("span", { class: "stat-unit" }, `del ${billed.start} al ${billed.end} (excluido)`)),
    billed.partial ? h("p", { class: "notice", role: "note" }, `Cost Explorer tiene unas ${lag} de retraso: algún periodo todavía es una estimación de AWS.`) : null,
    h("p", { class: "muted" }, `Leído ${formatDate(billed.fetchedAt)} · ${plural(billed.requests, "consulta", "consultas")} de pago. La caché local lo conserva ${lag}.`),
    h("div", { class: "table-scroll" }, h("table", { class: "dense" },
      h("caption", { class: "visually-hidden" }, "Coste facturado por tipo de uso."),
      h("thead", {}, h("tr", {}, h("th", { scope: "col" }, "Uso"), h("th", { scope: "col", class: "num" }, "Cantidad"), h("th", { scope: "col", class: "num" }, "Coste"))),
      h("tbody", {}, ...billed.lines.map((line) => h("tr", {},
        h("th", { scope: "row" }, `${line.component} `, h("span", { class: "muted mono" }, line.usageType)),
        h("td", { class: "num nowrap" }, `${formatNumber(line.quantity)} ${line.unit}`),
        h("td", { class: "num nowrap" }, formatUsd(line.costUsd))))))),
  ];
}

class RayitoCost extends View {
  #billed = null;
  #error = null;
  #busy = false;

  get polls() {
    return false;
  }

  load() {
    return get("/cost");
  }

  async #loadBilled(refresh) {
    this.#busy = true;
    this.#error = null;
    this.refresh();
    try {
      this.#billed = (await get("/cost", { billed: true, refresh }, { csrf: true })).billed;
      rememberBilled(this.#billed);
    } catch (error) {
      this.#error = describe(error);
    }
    this.#busy = false;
    this.refresh();
  }

  #billedCard(costExplorer) {
    const title = h("h2", { id: "billed-title", class: "stat-label" }, "Facturado (Cost Explorer) ", pill("FACTURADO", "info"));
    if (!costExplorer.enabled) {
      return h("section", { class: "card billed", "aria-labelledby": "billed-title" }, title,
        h("p", {}, "Desactivado por defecto: cada consulta a Cost Explorer se factura. Arranca el panel con ", h("code", {}, "--cost-explorer"), " para activarlo."));
    }
    const price = formatUsd(costExplorer.requestPriceUsd);
    return h("section", { class: "card billed", "aria-labelledby": "billed-title" }, title,
      h("div", { class: "toolbar" },
        h("button", { type: "button", class: "button primary", disabled: this.#busy, onclick: () => this.#loadBilled(false) }, `Consultar coste facturado (${price} por consulta)`),
        this.#billed ? h("button", { type: "button", class: "button", disabled: this.#busy, onclick: () => this.#loadBilled(true) }, `Ignorar la caché (${price} por consulta)`) : null),
      this.#error ? h("p", { class: "error", role: "alert" }, this.#error) : null,
      this.#billed ? billedBody(this.#billed, costExplorer) : h("p", { class: "muted" }, "Aún no se ha consultado."));
  }

  render(data) {
    return [
      pageHead("Coste", "Son dos cosas distintas: lo facturado lo calcula AWS (con retraso); lo estimado lo calcula el panel y no sustituye a la factura."),
      h("div", { class: "cards" }, estimatedCard(data.estimated), this.#billedCard(data.costExplorer)),
    ];
  }
}

customElements.define("rayito-cost", RayitoCost);
