/*
 * Ilustración "Cómo funciona": mejora progresiva. La figura se dibuja y se anima
 * sola con CSS; este script sólo ahorra trabajo (pausa fuera de pantalla y con
 * la pestaña oculta) y añade el botón de pausa que exige WCAG 2.2.2 para un
 * movimiento que dura más de 5 s. La etiqueta del botón es fija (la pone el
 * HTML, en cada idioma) y su estado se anuncia con aria-pressed.
 */
(() => {
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");

  function mount() {
    document.querySelectorAll("[data-rx-art]").forEach((figure) => {
      if (figure.dataset.ready) return;
      figure.dataset.ready = "true";
      const button = figure.querySelector("[data-rx-art-toggle]");

      figure.dataset.offscreen = "";
      if ("IntersectionObserver" in window) {
        new IntersectionObserver(
          ([entry]) => {
            if (entry.isIntersecting) delete figure.dataset.offscreen;
            else figure.dataset.offscreen = "";
          },
          { rootMargin: "100px" },
        ).observe(figure);
      } else {
        delete figure.dataset.offscreen;
      }

      if (!button) return;

      const sync = () => {
        button.hidden = reduced.matches;
      };
      button.addEventListener("click", () => {
        if ("userPaused" in figure.dataset) delete figure.dataset.userPaused;
        else figure.dataset.userPaused = "";
        button.setAttribute("aria-pressed", String("userPaused" in figure.dataset));
      });
      reduced.addEventListener("change", sync);
      sync();
    });
  }

  document.addEventListener("visibilitychange", () => {
    document.querySelectorAll("[data-rx-art]").forEach((figure) => {
      if (document.visibilityState === "hidden") figure.dataset.hidden = "";
      else delete figure.dataset.hidden;
    });
  });

  if (typeof document$ !== "undefined") document$.subscribe(mount);
  else if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount, { once: true });
  else mount();
})();
