import { get } from "../api.js";
import { formatDate, h, shortId } from "../dom.js";
import { emptyState, pageHead } from "../states.js";
import { View } from "../view.js";

class RayitoEvents extends View {
  #sandboxId = "";

  get polls() {
    return false;
  }

  load() {
    if (!this.meta.eventsConfigured) return Promise.resolve({ configured: false, events: [] });
    return get("/events", { sandboxId: this.#sandboxId });
  }

  render(data) {
    if (!data.configured) {
      return [
        pageHead("Eventos", "Historial de ciclo de vida de los sandboxes."),
        emptyState("Sin historial de eventos", null),
        h("p", { class: "muted center" }, "Despliega la pila events-webhooks y arranca el panel con ", h("code", {}, "--events-stack <nombre>"), "."),
      ];
    }
    const filter = h("input", { type: "search", id: "event-filter", class: "search", "aria-label": "Filtrar eventos por ID de sandbox", value: this.#sandboxId, placeholder: "Filtrar por ID de sandbox" });
    const form = h("form", { class: "toolbar", onsubmit: (event) => {
      event.preventDefault();
      this.#sandboxId = filter.value.trim();
      this.refresh();
    } }, filter, h("button", { type: "submit", class: "button primary" }, "Filtrar"));
    return [pageHead("Eventos", "Historial de ciclo de vida de los sandboxes.", h("button", { type: "button", class: "button", onclick: () => this.refresh() }, "Actualizar")), form, this.#table(data.events)];
  }

  #table(events) {
    if (events.length === 0) return emptyState("Sin eventos para este filtro", "Prueba con otro ID de sandbox o quita el filtro.");
    const head = ["Cuándo", "Evento", "Sandbox", "Motivo"].map((name) => h("th", { scope: "col" }, name));
    const rows = events.map((event) =>
      h("tr", {},
        h("td", { class: "nowrap" }, formatDate(event.occurredAt)),
        h("td", {}, event.type),
        h("td", { class: "mono", title: event.sandboxId }, shortId(event.sandboxId)),
        h("td", {}, event.killReason ?? "")),
    );
    return h("div", { class: "table-scroll" },
      h("table", { class: "dense" }, h("caption", { class: "visually-hidden" }, "Del más reciente al más antiguo."), h("thead", {}, h("tr", {}, ...head)), h("tbody", {}, ...rows)));
  }
}

customElements.define("rayito-events", RayitoEvents);
