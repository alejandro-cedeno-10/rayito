import { ApiFailure, describe, get, post } from "../api.js";
import { copyButton, countSandboxes, formatCompactDuration, formatDate, formatEta, formatMemory, formatRelative, h, icon, MS_PER_SECOND, pill, renderableNodes, shortId } from "../dom.js";
import { closeMenu, openMenu } from "../menu.js";
import { emptyState, pageHead } from "../states.js";
import { View } from "../view.js";

const ACTIONS = {
  kill: { label: "Matar", verb: "matar", danger: true, note: "Matar un sandbox es irreversible." },
  suspend: { label: "Suspender", verb: "suspender", danger: false, note: "Suspender guarda un snapshot del sandbox, que tiene coste de almacenamiento." },
  resume: { label: "Reanudar", verb: "reanudar", danger: false, note: "Reanudar vuelve a facturar cómputo." },
};
const ACTIONS_BY_STATE = { RUNNING: ["suspend", "kill"], PENDING: ["kill"], SUSPENDED: ["resume", "kill"], SUSPENDING: ["kill"] };
const STATES = {
  RUNNING: { label: "En ejecución", tone: "ok" },
  PENDING: { label: "Pendiente", tone: "warning" },
  SUSPENDING: { label: "Suspendiendo", tone: "warning" },
  SUSPENDED: { label: "Suspendido", tone: "info" },
};
const ID_PREVIEW = 10;
const LOG_FALLBACK_NOTICE = "Aviso: no existía el stream esperado; estas líneas vienen de streams hallados por nombre, y el rol de ejecución de otro sandbox pudo escribirlas.";

function stateLabel(state) {
  const known = STATES[state];
  return pill(known ? known.label : state, known ? known.tone : "neutral");
}

class RayitoSandboxes extends View {
  #selection = new Set();
  #stateFilter = "";
  #textFilter = "";
  #sandboxes = [];
  #tableArea = h("div", { class: "table-region" });
  #jobArea = h("div", { role: "status", "aria-live": "polite" });
  #dialog = h("dialog", { "aria-labelledby": "dialog-title" });
  #bulkBar = h("div", { class: "bulk-bar", role: "region", "aria-label": "Acciones sobre la selección", hidden: true });
  #filterBar = h("div", { class: "toolbar" });
  #jobTimer = null;

  get target() {
    return this.#tableArea;
  }

  connectedCallback() {
    this.replaceChildren(
      pageHead("Sandboxes", "MicroVMs de la región con su estado, memoria y antigüedad.", h("button", { type: "button", class: "button", onclick: () => this.refresh() }, icon("refresh"), "Actualizar")),
      this.#filterBar,
      this.#bulkBar,
      this.#jobArea,
      this.#tableArea,
      this.#dialog,
    );
    this.#buildFilters();
    this.#buildBulkBar();
    super.connectedCallback();
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    closeMenu();
    if (this.#jobTimer !== null) clearTimeout(this.#jobTimer);
  }

  async load() {
    this.#sandboxes = (await get("/sandboxes")).sandboxes;
    const present = new Set(this.#sandboxes.map((sandbox) => sandbox.sandboxId));
    for (const id of this.#selection) if (!present.has(id)) this.#selection.delete(id);
    this.#updateBulkBar();
    return this.#sandboxes;
  }

  #buildFilters() {
    const state = h("select", { id: "state-filter", "aria-label": "Filtrar por estado", onchange: (event) => { this.#stateFilter = event.target.value; this.#paint(); } },
      h("option", { value: "" }, "Todos los estados"),
      ...Object.entries(STATES).map(([name, known]) => h("option", { value: name }, known.label)));
    const text = h("input", { id: "text-filter", type: "search", class: "search", "aria-label": "Buscar sandboxes por ID o imagen", placeholder: "Buscar por ID o imagen", autocomplete: "off", oninput: (event) => { this.#textFilter = event.target.value.trim().toLowerCase(); this.#paint(); } });
    this.#filterBar.replaceChildren(text, state);
  }

  #buildBulkBar() {
    if (!this.meta.allowActions) return;
    this.#bulkBar.replaceChildren(
      h("span", { id: "selection-count", class: "bulk-count", "aria-live": "polite" }),
      h("span", { class: "spacer" }),
      ...Object.entries(ACTIONS).map(([name, action]) =>
        h("button", { type: "button", "data-action": name, class: action.danger ? "button danger" : "button", onclick: () => this.#confirm(name, [...this.#selection]) }, action.label)),
      h("button", { type: "button", class: "button ghost", onclick: () => { this.#selection.clear(); this.#paint(); this.#updateBulkBar(); } }, "Limpiar selección"),
    );
  }

  #updateBulkBar() {
    const total = this.#selection.size;
    this.#bulkBar.hidden = total === 0 || !this.meta.allowActions;
    const label = this.#bulkBar.querySelector("#selection-count");
    if (label) label.textContent = `${total} seleccionado(s)`;
  }

  #visible() {
    return this.#sandboxes.filter((sandbox) =>
      (!this.#stateFilter || sandbox.state === this.#stateFilter) &&
      (!this.#textFilter || `${sandbox.sandboxId} ${sandbox.image}`.toLowerCase().includes(this.#textFilter)));
  }

  #paint() {
    this.#tableArea.replaceChildren(...renderableNodes(this.render(this.#sandboxes)));
  }

  #clearFilters() {
    this.#stateFilter = "";
    this.#textFilter = "";
    this.#filterBar.querySelector("#text-filter").value = "";
    this.#filterBar.querySelector("#state-filter").value = "";
    this.#paint();
  }

  #rowMenu(row) {
    const items = [{ label: "Copiar ID", onSelect: () => navigator.clipboard?.writeText(row.sandboxId).catch(() => {}) }];
    if (this.meta.logsAvailable) items.push({ label: "Ver logs", onSelect: () => this.#showLogs(row.sandboxId) });
    if (this.meta.allowActions) {
      for (const name of ACTIONS_BY_STATE[row.state] ?? []) {
        items.push({ label: ACTIONS[name].label, danger: ACTIONS[name].danger, onSelect: () => this.#confirm(name, [row.sandboxId]) });
      }
    }
    const button = h("button", { type: "button", class: "icon-button", "aria-haspopup": "menu", "aria-expanded": "false", "aria-label": `Acciones de ${row.sandboxId}`, title: "Acciones" }, icon("more"));
    button.addEventListener("click", () => openMenu(button, items, `Acciones de ${row.sandboxId}`));
    return button;
  }

  render() {
    closeMenu();
    if (this.#sandboxes.length === 0) {
      return emptyState("Aún no hay sandboxes en esta región", "Cuando crees uno aparecerá aquí con su estado y su memoria.");
    }
    const rows = this.#visible();
    if (rows.length === 0) {
      return emptyState("Ningún sandbox coincide", "Prueba con otro texto o quita el filtro de estado.",
        h("button", { type: "button", class: "button", onclick: () => this.#clearFilters() }, "Quitar filtros"));
    }
    const selectable = this.meta.allowActions;
    const now = Date.now();
    const all = h("input", { type: "checkbox", "aria-label": "Seleccionar todos los visibles", checked: rows.every((row) => this.#selection.has(row.sandboxId)), onchange: (event) => {
      for (const row of rows) event.target.checked ? this.#selection.add(row.sandboxId) : this.#selection.delete(row.sandboxId);
      this.#paint();
      this.#updateBulkBar();
    } });
    return h("div", { class: "table-scroll" }, h("table", { class: "dense" },
      h("caption", { class: "visually-hidden" }, `${countSandboxes(rows.length)} en la región.`),
      h("thead", {}, h("tr", {},
        selectable ? h("th", { scope: "col", class: "check" }, all) : null,
        h("th", { scope: "col" }, "ID"),
        h("th", { scope: "col" }, "Imagen"),
        h("th", { scope: "col" }, "Estado"),
        h("th", { scope: "col", class: "num" }, "Memoria"),
        h("th", { scope: "col" }, "Iniciado"),
        h("th", { scope: "col", class: "actions-col" }, h("span", { class: "visually-hidden" }, "Acciones")))),
      h("tbody", {}, ...rows.map((row) => h("tr", { class: this.#selection.has(row.sandboxId) ? "selected" : "" },
        selectable ? h("td", { class: "check" }, h("input", { type: "checkbox", "aria-label": `Seleccionar ${row.sandboxId}`, checked: this.#selection.has(row.sandboxId), onchange: (event) => {
          event.target.checked ? this.#selection.add(row.sandboxId) : this.#selection.delete(row.sandboxId);
          event.target.closest("tr").classList.toggle("selected", event.target.checked);
          this.#updateBulkBar();
        } })) : null,
        h("th", { scope: "row", class: "id-cell" }, h("span", { class: "mono", title: row.sandboxId }, shortId(row.sandboxId)), copyButton(row.sandboxId, `Copiar el ID ${row.sandboxId}`)),
        h("td", { class: "nowrap" }, row.image, " ", h("span", { class: "muted mono" }, `v${row.imageVersion}`)),
        h("td", {}, stateLabel(row.state)),
        h("td", { class: "num nowrap" }, formatMemory(row.memoryMib)),
        h("td", { class: "nowrap" }, h("span", { title: `${formatDate(row.startedAt)} · activo ${formatCompactDuration((now - new Date(row.startedAt).getTime()) / MS_PER_SECOND)}` }, formatRelative(row.startedAt, now))),
        h("td", { class: "actions-col" }, this.#rowMenu(row)))))));
  }

  async #showLogs(sandboxId) {
    const body = h("pre", { class: "logs", tabindex: "0", "aria-label": "Logs del sandbox" }, "Cargando…");
    const notice = h("p", { class: "notice", role: "status", hidden: true }, LOG_FALLBACK_NOTICE);
    this.#openDialog(`Logs de ${sandboxId}`, [notice, body], [h("button", { type: "button", class: "button", autofocus: true, onclick: () => this.#dialog.close() }, "Cerrar")]);
    try {
      const page = await get("/logs", { sandboxId });
      notice.hidden = !page.fallback;
      body.textContent = page.lines.length === 0
        ? "Sin logs (el sandbox no tiene executionRoleArn o aún no escribió)."
        : (page.truncated ? "… (hay líneas anteriores)\n" : "") + page.lines.map((line) => `${line.timestamp} ${line.message}`).join("\n");
    } catch (error) {
      body.textContent = describe(error);
    }
  }

  #openDialog(title, content, buttons) {
    this.#dialog.replaceChildren(
      h("h2", { id: "dialog-title" }, title),
      ...content,
      h("div", { class: "actions" }, ...buttons),
    );
    if (!this.#dialog.open) this.#dialog.showModal();
  }

  async #confirm(name, ids) {
    const action = ACTIONS[name];
    let plan;
    try {
      plan = (await get("/actions/plan", { action: name, count: ids.length })).plan;
    } catch (error) {
      this.#jobArea.replaceChildren(h("p", { class: "error", role: "alert" }, describe(error)));
      return;
    }
    const preview = ids.slice(0, ID_PREVIEW).map((id) => h("li", { class: "mono" }, id));
    const rest = ids.length - preview.length;
    const confirm = h("button", { type: "button", class: action.danger ? "button danger" : "button primary", onclick: () => this.#run(name, ids) }, `${action.label} ${ids.length}`);
    this.#openDialog(`¿${action.label} ${countSandboxes(ids.length)}?`, [
      h("p", {}, action.note),
      h("p", {}, `La cola respeta el límite de ${plan.tps} llamadas por segundo de ${plan.operation}: tardará ${formatEta(plan.estimatedSeconds)}.`),
      h("ul", {}, ...preview, rest > 0 ? h("li", {}, `y ${rest} más`) : null),
    ], [h("button", { type: "button", class: "button", autofocus: true, onclick: () => this.#dialog.close() }, "Cancelar"), confirm]);
  }

  async #run(name, ids) {
    this.#dialog.close();
    try {
      const { job } = await post("/actions", { action: name, sandboxIds: ids, confirm: true });
      this.#followJob(job);
    } catch (error) {
      const text = error instanceof ApiFailure && error.code === "actions_disabled" ? "Las acciones están desactivadas." : describe(error);
      this.#jobArea.replaceChildren(h("p", { class: "error", role: "alert" }, text));
    }
  }

  #followJob(job) {
    const action = ACTIONS[job.action];
    const progress = h("progress", { max: job.total, value: job.completed, "aria-label": `Progreso de ${action.verb}` });
    const failed = job.items.filter((item) => item.outcome === "failed");
    this.#jobArea.replaceChildren(
      h("div", { class: "card job" },
        h("p", {}, `${action.label}: ${job.completed} de ${job.total} (${job.status === "finished" ? "terminado" : "en curso"}).`),
        progress,
        job.status === "finished"
          ? h("p", {}, `Hechos ${job.counts.done}, sin cambios ${job.counts.unchanged}, fallidos ${job.counts.failed}.`, failed.length ? ` Códigos: ${[...new Set(failed.map((item) => item.code))].join(", ")}.` : "")
          : null),
    );
    if (job.status === "finished") {
      for (const item of job.items) if (item.outcome !== "failed") this.#selection.delete(item.sandboxId);
      this.refresh();
      return;
    }
    this.#jobTimer = setTimeout(async () => {
      try {
        this.#followJob((await get(`/actions/${encodeURIComponent(job.jobId)}`)).job);
      } catch (error) {
        this.#jobArea.replaceChildren(h("p", { class: "error", role: "alert" }, describe(error)));
      }
    }, this.meta.jobPollIntervalSeconds * MS_PER_SECOND);
  }
}

customElements.define("rayito-sandboxes", RayitoSandboxes);
