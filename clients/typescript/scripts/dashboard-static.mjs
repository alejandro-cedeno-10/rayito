import { readdirSync, readFileSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const PACKAGE_DIR = resolve(dirname(fileURLToPath(import.meta.url)), "..");

/**
 * La copia canónica de la SPA del panel (ADR-027, D4): vive en el paquete de
 * Python, donde la necesitan el wheel y la Lambda de la pila `dashboard`. El
 * paquete npm lleva una copia en `dist/dashboard-static/`, hecha en cada build
 * y nunca versionada.
 */
export const SOURCE_DIR = resolve(
  PACKAGE_DIR,
  "..",
  "python",
  "src",
  "rayito",
  "_dashboard",
  "_static",
);
export const DIST_STATIC_DIR = join(PACKAGE_DIR, "dist", "dashboard-static");

/** Las rutas relativas (con `/`) de todos los ficheros bajo `directory`, ordenadas. */
export function listFiles(directory) {
  return readdirSync(directory, { recursive: true, withFileTypes: true })
    .filter((entry) => entry.isFile())
    .map((entry) => relative(directory, join(entry.parentPath, entry.name)).split("\\").join("/"))
    .sort();
}

/**
 * Lo que separa `copy` de `source`: ficheros que faltan, que sobran o que
 * difieren byte a byte. Una lista vacía es que son idénticos.
 */
export function compareDirectories(source, copy) {
  const expected = listFiles(source);
  const actual = listFiles(copy);
  const missing = expected.filter((path) => !actual.includes(path)).map((path) => `falta ${path}`);
  const extra = actual.filter((path) => !expected.includes(path)).map((path) => `sobra ${path}`);
  const differing = expected
    .filter((path) => actual.includes(path))
    .filter((path) => !readFileSync(join(source, path)).equals(readFileSync(join(copy, path))))
    .map((path) => `difiere ${path}`);
  return [...missing, ...extra, ...differing];
}
