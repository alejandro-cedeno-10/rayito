// Cáscara de la SPA: barra lateral, navegación por hash, tema (oscuro por
// defecto, claro o el del sistema) y las vistas, que reciben /api/v1/meta al
// crearse. Los colores, radios y fuentes son tokens CSS (`css/tokens.css`);
// un tema de usuario los sobrescribe desde `/theme.css`.
import { describe, get, setAccount } from "./api.js";
import { detectMode, isSignedInMode, signOut } from "./auth.js";
import { h, icon } from "./dom.js";
import { errorState } from "./states.js";
import "./views/overview.js";
import "./views/sandboxes.js";
import "./views/quotas.js";
import "./views/cost.js";
import "./views/images.js";
import "./views/events.js";
import "./views/help.js";

const ROUTES = [
  { path: "overview", label: "Resumen", tag: "rayito-overview", icon: "overview" },
  { path: "sandboxes", label: "Sandboxes", tag: "rayito-sandboxes", icon: "sandboxes" },
  { path: "quotas", label: "Cuotas", tag: "rayito-quotas", icon: "quotas" },
  { path: "cost", label: "Coste", tag: "rayito-cost", icon: "cost" },
  { path: "images", label: "Imágenes", tag: "rayito-images", icon: "images" },
  { path: "events", label: "Eventos", tag: "rayito-events", icon: "events" },
  { path: "help", label: "Ayuda", tag: "rayito-help", icon: "help" },
];
const THEMES = [
  { value: "dark", label: "Oscuro" },
  { value: "light", label: "Claro" },
  { value: "auto", label: "Auto" },
];
const DEFAULT_THEME = "dark";
const THEME_KEY = "rayito-dashboard-theme";
const LOGO_URL = "/theme/logo";
const BRAND_NAME = "Rayito";

function storedTheme() {
  try {
    const value = localStorage.getItem(THEME_KEY);
    return THEMES.some((theme) => theme.value === value) ? value : DEFAULT_THEME;
  } catch {
    return DEFAULT_THEME;
  }
}

function applyTheme(theme) {
  if (theme === "auto") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", theme);
  try {
    localStorage.setItem(THEME_KEY, theme);
  } catch {
    // sin almacenamiento: el tema sólo dura esta sesión
  }
}

class RayitoApp extends HTMLElement {
  #meta = null;
  #accounts = [];
  #account = "";
  #theme = storedTheme();

  connectedCallback() {
    applyTheme(this.#theme);
    window.addEventListener("hashchange", () => this.#show());
    this.#start();
  }

  async #start() {
    try {
      await detectMode();
      this.#meta = await get("/meta");
      this.#accounts = await this.#loadAccounts();
    } catch (error) {
      this.replaceChildren(h("main", { id: "main", class: "boot-error" }, errorState(describe(error), () => location.reload())));
      return;
    }
    this.#paint();
  }

  /** Las cuentas adicionales (vista multicuenta); una pila antigua sin la ruta no tiene ninguna. */
  async #loadAccounts() {
    try {
      return (await get("/accounts", undefined, { scoped: false })).accounts;
    } catch {
      return [];
    }
  }

  async #chooseAccount(name) {
    const previous = this.#account;
    try {
      setAccount(name);
      this.#meta = await get("/meta");
      this.#account = name;
      this.#paint();
    } catch (error) {
      setAccount(previous);
      this.#paint();
      this.querySelector("main").replaceChildren(errorState(describe(error), () => this.#show()));
    }
  }

  #accountSwitch() {
    if (this.#accounts.length === 0) return null;
    return h("select", {
      "aria-label": "Cuenta",
      onchange: (event) => this.#chooseAccount(event.target.value),
    },
    h("option", { value: "", selected: this.#account === "" }, "Esta cuenta"),
    ...this.#accounts.map((account) => h("option", { value: account.name, selected: this.#account === account.name }, account.name)));
  }

  #route() {
    const path = location.hash.replace(/^#\/?/, "");
    return ROUTES.find((route) => route.path === path) ?? ROUTES[0];
  }

  #brand() {
    const mark = this.#meta.theme.logo
      ? h("img", { class: "brand-logo", src: LOGO_URL, alt: "" })
      : h("span", { class: "brand-mark", "aria-hidden": "true" }, icon("bolt"));
    const name = this.#meta.theme.name === "rayito" ? BRAND_NAME : this.#meta.theme.name;
    return h("div", { class: "brand" }, mark, h("span", { class: "brand-name" }, name));
  }

  #themeSwitch() {
    return h("div", { class: "segmented", role: "group", "aria-label": "Tema de color" },
      ...THEMES.map((theme) => h("button", {
        type: "button",
        "data-theme-value": theme.value,
        "aria-pressed": String(theme.value === this.#theme),
        onclick: () => this.#chooseTheme(theme.value),
      }, theme.label)));
  }

  #paint() {
    const mode = this.#meta.allowActions ? "Con acciones" : "Sólo lectura";
    this.replaceChildren(
      h("div", { class: "shell" },
        h("aside", { class: "sidebar" },
          this.#brand(),
          h("nav", { class: "nav", "aria-label": "Secciones" }, ...ROUTES.map((route) => this.#link(route))),
          h("div", { class: "sidebar-foot" },
            this.#themeSwitch(),
            isSignedInMode() ? h("button", { type: "button", class: "button small", onclick: signOut }, "Cerrar sesión") : null,
            h("p", { class: "muted version" }, `v${this.#meta.rayitoVersion}`))),
        h("div", { class: "content" },
          h("div", { class: "topbar" },
            this.#accountSwitch(),
            h("span", { class: "pill neutral" }, this.#meta.region),
            h("span", { class: this.#meta.allowActions ? "pill warning" : "pill neutral" }, mode)),
          h("main", { id: "main", tabindex: "-1" }))),
    );
    this.#show();
  }

  #link(route) {
    return h("a", { href: `#/${route.path}`, "data-route": route.path }, icon(route.icon), h("span", {}, route.label));
  }

  #show() {
    if (this.#meta === null) return;
    const route = this.#route();
    for (const link of this.querySelectorAll("nav a")) {
      if (link.dataset.route === route.path) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    }
    const view = document.createElement(route.tag);
    view.meta = this.#meta;
    const main = this.querySelector("main");
    main.replaceChildren(view);
    main.focus();
  }

  #chooseTheme(value) {
    this.#theme = value;
    applyTheme(value);
    for (const button of this.querySelectorAll("[data-theme-value]")) {
      button.setAttribute("aria-pressed", String(button.dataset.themeValue === value));
    }
  }
}

customElements.define("rayito-app", RayitoApp);
