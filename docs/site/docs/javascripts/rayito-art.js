/*
 * Ilustración animada de la portada: carga el vídeo al entrar en pantalla, lo
 * reproduce sin sonido sólo si el visitante no pidió menos movimiento y ofrece
 * un botón de pausa. Los textos salen de los atributos data-* de cada página,
 * así que sirve igual para español e inglés.
 */
(() => {
  const VISIBLE_RATIO = 0.2;

  function mount() {
    document.querySelectorAll("[data-rayito-art]").forEach((box) => {
      if (box.dataset.ready) return;
      const video = box.querySelector("[data-rayito-video]");
      const button = box.querySelector("[data-rayito-toggle]");
      if (!video || !button) return;
      box.dataset.ready = "true";

      const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
      let loaded = false;

      const load = () => {
        if (loaded) return;
        loaded = true;
        video.querySelectorAll("source[data-src]").forEach((source) => {
          source.src = source.dataset.src;
        });
        video.load();
      };
      const play = () => {
        load();
        video.play().catch(() => {});
      };
      const sync = () => {
        const key = video.paused ? "play" : "pause";
        button.textContent = box.dataset[`${key}Label`] || button.textContent;
        button.setAttribute("aria-label", box.dataset[`${key}Aria`] || button.textContent);
      };

      button.addEventListener("click", () => (video.paused ? play() : video.pause()));
      video.addEventListener("play", () => {
        if (!loaded) play();
        sync();
      });
      video.addEventListener("pause", sync);
      video.addEventListener("pointerdown", load, { once: true });
      video.addEventListener("keydown", load, { once: true });
      reduced.addEventListener("change", () => {
        if (reduced.matches) video.pause();
      });

      if ("IntersectionObserver" in window) {
        const observer = new IntersectionObserver(
          (entries) => {
            if (!entries.some((entry) => entry.isIntersecting)) return;
            observer.disconnect();
            if (!reduced.matches) play();
          },
          { threshold: VISIBLE_RATIO },
        );
        observer.observe(box);
      }
      sync();
    });
  }

  if (typeof document$ !== "undefined") document$.subscribe(mount);
  else if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount, { once: true });
  else mount();
})();
