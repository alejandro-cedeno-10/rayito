// Base de las vistas: carga al conectarse, refresca cada `pollIntervalSeconds`
// mientras la pestaña está visible y pinta con `render(data)`. Un refresco
// pedido mientras otro carga no se pierde: se repite al terminar.
import { describe } from "./api.js";
import { MS_PER_SECOND, renderableNodes } from "./dom.js";
import { errorState, loadingState } from "./states.js";

export class View extends HTMLElement {
  #timer = null;
  #loading = false;
  #pending = false;
  #painted = false;

  /** @type {object | null} lo inyecta rayito-app con /api/v1/meta */
  meta = null;

  connectedCallback() {
    this.refresh();
    const seconds = this.meta?.pollIntervalSeconds;
    if (seconds && this.polls) {
      this.#timer = setInterval(() => {
        if (!document.hidden) this.refresh();
      }, seconds * MS_PER_SECOND);
    }
  }

  disconnectedCallback() {
    if (this.#timer !== null) clearInterval(this.#timer);
    this.#timer = null;
  }

  get polls() {
    return true;
  }

  /** Dónde se pinta: la propia vista, o una región si la vista tiene controles que no deben recrearse. */
  get target() {
    return this;
  }

  async refresh() {
    if (this.#loading) {
      this.#pending = true;
      return;
    }
    this.#loading = true;
    if (!this.#painted) this.target.replaceChildren(loadingState());
    try {
      const data = await this.load();
      this.target.replaceChildren(...renderableNodes(this.render(data)));
      this.#painted = true;
    } catch (error) {
      this.target.replaceChildren(errorState(describe(error), () => this.refresh()));
    } finally {
      this.#loading = false;
      if (this.#pending) {
        this.#pending = false;
        this.refresh();
      }
    }
  }
}
