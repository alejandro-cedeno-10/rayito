import { describe, get } from "../api.js";
import { formatDate, formatMemory, h, pill } from "../dom.js";
import { emptyState, pageHead } from "../states.js";
import { View } from "../view.js";

const IMAGE_TABLE_HEADERS = ["Imagen", "Estado", "Última activa", "Último fallo", ""];
const IMAGE_TABLE_COLUMNS = IMAGE_TABLE_HEADERS.length;

class RayitoImages extends View {
  #versions = new Map();

  get polls() {
    return false;
  }

  load() {
    return get("/images");
  }

  async #toggle(image) {
    if (this.#versions.has(image.name)) {
      this.#versions.delete(image.name);
    } else {
      try {
        this.#versions.set(image.name, (await get("/images/versions", { image: image.name })).versions);
      } catch (error) {
        this.#versions.set(image.name, describe(error));
      }
    }
    this.refresh();
  }

  #versionRows(image) {
    const versions = this.#versions.get(image.name);
    if (versions === undefined) return [];
    if (typeof versions === "string") {
      return [h("tr", {}, h("td", { colspan: IMAGE_TABLE_COLUMNS, class: "error", role: "alert" }, versions))];
    }
    return versions.map((version) => h("tr", {},
      h("td", {}),
      h("td", { class: "mono" }, `v${version.imageVersion}`),
      h("td", {}, pill(version.state, "neutral"), " ", h("span", { class: "muted" }, version.status)),
      h("td", { class: "num nowrap" }, formatMemory(version.memoryMib)),
      h("td", { class: "nowrap" }, formatDate(version.createdAt))));
  }

  render(data) {
    if (data.images.length === 0) return emptyState("No hay imágenes en esta región", "Las imágenes de MicroVM que crees aparecerán aquí.");
    const body = data.images.flatMap((image) => [
      h("tr", {},
        h("th", { scope: "row" }, image.name),
        h("td", {}, pill(image.state, "neutral")),
        h("td", {}, image.latestActiveVersion ? `v${image.latestActiveVersion}` : "n/d"),
        h("td", {}, image.latestFailedVersion ? `v${image.latestFailedVersion}` : "ninguna"),
        h("td", {}, h("button", { type: "button", class: "button small", "aria-expanded": String(this.#versions.has(image.name)), onclick: () => this.#toggle(image) }, "Versiones"))),
      ...this.#versionRows(image),
    ]);
    return [
      pageHead("Imágenes", "Imágenes de MicroVM de la cuenta en la región."),
      h("div", { class: "table-scroll" }, h("table", { class: "dense" },
        h("caption", { class: "visually-hidden" }, "Imágenes de la cuenta en la región."),
        h("thead", {}, h("tr", {}, ...IMAGE_TABLE_HEADERS.map((name) => h("th", { scope: "col" }, name)))),
        h("tbody", {}, ...body))),
    ];
  }
}

customElements.define("rayito-images", RayitoImages);
