// Manual de ayuda del panel: los pasos cortos para sacarle partido y el
// enlace a la guía completa. Se pinta sin llamar a la API ni a ningún sitio
// externo: sólo usa lo que ya sabe `/api/v1/meta` (dónde corre, modo, Cost
// Explorer, eventos), así que sirve también sin red.
import { h } from "../dom.js";
import { statusRows } from "../help-hints.js";
import { pageHead } from "../states.js";
import { View } from "../view.js";

export const DOCS_GUIDE_URL = "https://alejandro-cedeno-10.github.io/rayito/funciones-opcionales/panel-de-gestion/#recorrido";
const DOCS_LINK_REL = "noopener noreferrer";

const SECTIONS = [
  { route: "overview", title: "Resumen", text: "sandboxes activos y suspendidos, la memoria contra la cuota de la región y el coste estimado." },
  { route: "sandboxes", title: "Sandboxes", text: "una fila por MicroVM. Filtra por estado, busca por ID o imagen, abre los logs y, con acciones, mata, suspende o reanuda." },
  { route: "quotas", title: "Cuotas", text: "la memoria asignada, los límites de tasa por operación y el enlace para pedir un aumento." },
  { route: "cost", title: "Coste", text: "el estimado lo calcula el panel; el facturado viene de Cost Explorer y se consulta con un botón." },
  { route: "images", title: "Imágenes", text: "las imágenes de la cuenta, su estado y sus versiones." },
  { route: "events", title: "Eventos", text: "el historial de ciclo de vida de la pila events-webhooks." },
];

function statusRow(row) {
  const hint = row.enabled ? null : [" · ", row.hintIsFlag ? ["arranca con ", h("code", {}, row.hint)] : row.hint];
  return h("li", {}, `${row.label}: `, h("strong", {}, row.enabled ? "activado" : "apagado"), hint);
}

function status(meta) {
  return h("ul", { class: "help-status" }, ...statusRows(meta).map(statusRow));
}

class RayitoHelp extends View {
  get polls() {
    return false;
  }

  load() {
    return Promise.resolve(this.meta);
  }

  render(meta) {
    return [
      pageHead("Ayuda", "Cómo usar el panel, en pocos pasos."),
      h("section", { class: "card", "aria-labelledby": "help-sections" },
        h("h2", { id: "help-sections", class: "stat-label" }, "Qué hay en cada sección"),
        h("ol", { class: "help-steps" }, ...SECTIONS.map((section) => h("li", {},
          h("a", { href: `#/${section.route}` }, section.title), ": ", section.text)))),
      h("section", { class: "card", "aria-labelledby": "help-modes" },
        h("h2", { id: "help-modes", class: "stat-label" }, "Qué está activado ahora"),
        status(meta),
        h("p", { class: "muted" }, "Sin acciones el panel sólo lee. Ninguna acción se lanza sin confirmación, y reanudar vuelve a facturar cómputo.")),
      h("section", { class: "card", "aria-labelledby": "help-more" },
        h("h2", { id: "help-more", class: "stat-label" }, "Varias cuentas y métricas"),
        h("p", {}, "Si arrancaste el panel local con ", h("code", {}, "--account"), ", el selector de la esquina superior cambia de cuenta. Las cuentas adicionales son siempre de sólo lectura: las acciones se piden en la principal."),
        h("p", {}, "La CPU, la memoria y el disco de los sandboxes que exportan OTLP se ven en el dashboard de CloudWatch: ", h("code", {}, "rayito dashboard cloudwatch --otlp"), ".")),
      h("section", { class: "card", "aria-labelledby": "help-docs" },
        h("h2", { id: "help-docs", class: "stat-label" }, "Guía completa con capturas"),
        h("p", {}, "Cada paso, las varias cuentas, las gráficas OTLP, el tema propio, la pila en tu cuenta y las alertas, con imágenes."),
        h("p", {}, h("a", { class: "button primary", href: DOCS_GUIDE_URL, target: "_blank", rel: DOCS_LINK_REL }, "Abrir la guía paso a paso"))),
    ];
  }
}

customElements.define("rayito-help", RayitoHelp);
