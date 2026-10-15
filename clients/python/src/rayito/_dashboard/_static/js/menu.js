// Menú contextual de una fila: un único `div[role=menu]` fijo sobre la página,
// para que ni el desplazamiento de la tabla ni su cabecera fija lo recorten.
// Teclado: flechas, Inicio, Fin, Escape (devuelve el foco) y Tab (cierra).
import { h } from "./dom.js";

const MENU_GAP_PX = 4;
const VIEWPORT_MARGIN_PX = 8;

let current = null;

export function closeMenu({ restoreFocus = false } = {}) {
  if (current === null) return;
  const { element, anchor, cleanup } = current;
  current = null;
  cleanup();
  element.remove();
  anchor.setAttribute("aria-expanded", "false");
  if (restoreFocus && anchor.isConnected) anchor.focus();
}

function place(element, anchor) {
  const box = anchor.getBoundingClientRect();
  const width = element.offsetWidth;
  const height = element.offsetHeight;
  const left = Math.max(VIEWPORT_MARGIN_PX, Math.min(box.right - width, window.innerWidth - width - VIEWPORT_MARGIN_PX));
  const below = box.bottom + MENU_GAP_PX;
  const top = below + height > window.innerHeight - VIEWPORT_MARGIN_PX ? Math.max(VIEWPORT_MARGIN_PX, box.top - height - MENU_GAP_PX) : below;
  element.style.left = `${left}px`;
  element.style.top = `${top}px`;
}

/**
 * Abre el menú junto a `anchor`. `items`: `{ label, onSelect, danger? }`.
 */
export function openMenu(anchor, items, label) {
  closeMenu();
  const buttons = items.map((item) =>
    h("button", { type: "button", role: "menuitem", class: item.danger ? "menu-item danger" : "menu-item", tabindex: "-1", onclick: () => {
      closeMenu({ restoreFocus: true });
      item.onSelect();
    } }, item.label));
  const element = h("div", { class: "menu", role: "menu", "aria-label": label }, ...buttons);
  document.body.append(element);
  anchor.setAttribute("aria-expanded", "true");
  place(element, anchor);

  const focusAt = (index) => buttons[(index + buttons.length) % buttons.length].focus();
  const onKey = (event) => {
    const index = buttons.indexOf(document.activeElement);
    const moves = { ArrowDown: () => focusAt(index + 1), ArrowUp: () => focusAt(index - 1), Home: () => focusAt(0), End: () => focusAt(-1) };
    if (event.key === "Escape") {
      event.preventDefault();
      closeMenu({ restoreFocus: true });
    } else if (event.key === "Tab") {
      closeMenu();
    } else if (moves[event.key]) {
      event.preventDefault();
      moves[event.key]();
    }
  };
  const onPointer = (event) => {
    if (!element.contains(event.target) && !anchor.contains(event.target)) closeMenu();
  };
  const onDismiss = () => closeMenu();
  element.addEventListener("keydown", onKey);
  document.addEventListener("pointerdown", onPointer, true);
  window.addEventListener("resize", onDismiss);
  window.addEventListener("scroll", onDismiss, true);
  current = {
    element,
    anchor,
    cleanup: () => {
      document.removeEventListener("pointerdown", onPointer, true);
      window.removeEventListener("resize", onDismiss);
      window.removeEventListener("scroll", onDismiss, true);
    },
  };
  focusAt(0);
}
