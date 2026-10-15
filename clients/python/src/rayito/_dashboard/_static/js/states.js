// Estados vacíos, de error y de carga, iguales en todas las vistas.
import { h, icon } from "./dom.js";

export function emptyState(title, text, action = null) {
  return h("div", { class: "state empty" }, icon("inbox"), h("h2", { class: "state-title" }, title), text ? h("p", { class: "muted" }, text) : null, action);
}

export function errorState(message, retry = null) {
  return h("div", { class: "state error", role: "alert" }, icon("alert"), h("h2", { class: "state-title" }, "No se pudo cargar"), h("p", {}, message),
    retry ? h("button", { type: "button", class: "button", onclick: retry }, "Reintentar") : null);
}

/** Marcador de carga: bloques con brillo suave (sin animación si el usuario la evita). */
export function loadingState(rows = 3) {
  return h("div", { class: "loading", role: "status", "aria-label": "Cargando" },
    ...Array.from({ length: rows }, () => h("div", { class: "skeleton" })));
}

export function pageHead(title, subtitle, ...actions) {
  return h("header", { class: "page-head" },
    h("div", {}, h("h1", {}, title), subtitle ? h("p", { class: "muted" }, subtitle) : null),
    actions.length ? h("div", { class: "page-actions" }, ...actions) : null);
}
