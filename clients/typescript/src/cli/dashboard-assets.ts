/**
 * Dónde está la SPA del panel. La copia canónica vive en el paquete de Python
 * (`rayito/_dashboard/_static/`, ADR-027, D4); el build de TypeScript la
 * copia a `dist/dashboard-static/` (`scripts/copy-dashboard-static.mjs`) y
 * `pack:check` comprueba que es idéntica. En el árbol de fuentes, sin
 * construir, se sirve la copia canónica.
 */

import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";

/** Junto a `dist/cli.mjs` y a sus fragmentos compartidos en el paquete publicado. */
const PACKAGED_DIRECTORY = "./dashboard-static";
/** Desde `src/cli/`, la copia canónica del paquete de Python. */
const SOURCE_TREE_DIRECTORY = "../../../python/src/rayito/_dashboard/_static";
const MISSING_SPA_MESSAGE = "no se encuentra la SPA del panel en este paquete";

export function dashboardStaticDirectory(): string {
  for (const relative of [PACKAGED_DIRECTORY, SOURCE_TREE_DIRECTORY]) {
    const directory = fileURLToPath(new URL(relative, import.meta.url));
    if (existsSync(directory)) {
      return directory;
    }
  }
  throw new Error(MISSING_SPA_MESSAGE);
}
