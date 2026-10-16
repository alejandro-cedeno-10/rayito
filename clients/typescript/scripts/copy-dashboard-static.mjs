import { cpSync, rmSync } from "node:fs";
import { compareDirectories, DIST_STATIC_DIR, SOURCE_DIR } from "./dashboard-static.mjs";

/**
 * Copia la SPA canónica del panel (`SOURCE_DIR`) a `dist/dashboard-static/`
 * tras `tsdown` (que limpia `dist/`). No hay copia en git: cada build la
 * rehace, y la comprobación final falla si la copia no es idéntica.
 */
rmSync(DIST_STATIC_DIR, { recursive: true, force: true });
cpSync(SOURCE_DIR, DIST_STATIC_DIR, { recursive: true });
const problems = compareDirectories(SOURCE_DIR, DIST_STATIC_DIR);
if (problems.length > 0) {
  throw new Error(`la copia de la SPA no es idéntica a la fuente: ${problems.join(", ")}`);
}
