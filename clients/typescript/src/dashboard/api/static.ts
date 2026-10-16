/**
 * La SPA empaquetada más el aspecto. La copia canónica de la SPA vive en el
 * paquete de Python (`rayito/_dashboard/_static/`, ADR-027, D4); el build de
 * TypeScript la copia a `dist/dashboard-static/` y `pack:check` comprueba que
 * es idéntica byte a byte. Sólo se sirve lo que hay bajo ese directorio y el
 * tema validado, listado al arrancar: ninguna ruta de la petición toca el
 * sistema de ficheros. Espejo de `rayito._dashboard._api._static`.
 */

import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { Appearance } from "../theme.js";

export const INDEX_PATH = "/" as const;
export const INDEX_FILE = "index.html" as const;
export const THEME_CSS_PATH = "/theme.css" as const;
export const LOGO_PATH = "/theme/logo" as const;
export const CSS_CONTENT_TYPE = "text/css; charset=utf-8" as const;
const CONTENT_TYPES: Readonly<Record<string, string>> = {
  ".html": "text/html; charset=utf-8",
  ".css": CSS_CONTENT_TYPE,
  ".js": "text/javascript; charset=utf-8",
  ".svg": "image/svg+xml",
};

export interface StaticFile {
  readonly data: Uint8Array;
  readonly contentType: string;
}

/** Lo que aporta el aspecto: `/theme.css`, que existe siempre (vacío sin tema de usuario) para que `index.html` lo enlace sin 404, y el logo si lo hay. */
export function appearanceFiles(
  appearance: Appearance = new Appearance(),
): Map<string, StaticFile> {
  const files = new Map<string, StaticFile>([
    [
      THEME_CSS_PATH,
      { data: new TextEncoder().encode(appearance.css), contentType: CSS_CONTENT_TYPE },
    ],
  ]);
  if (appearance.logo !== undefined) {
    files.set(LOGO_PATH, { data: appearance.logo.data, contentType: appearance.logo.contentType });
  }
  return files;
}

function extensionOf(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot < 0 ? "" : name.slice(dot);
}

function walk(directory: string, prefix: string, files: Map<string, StaticFile>): void {
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    const path = `${prefix}${entry.name}`;
    if (entry.isDirectory()) {
      walk(join(directory, entry.name), `${path}/`, files);
    } else if (entry.isFile()) {
      const contentType = CONTENT_TYPES[extensionOf(entry.name)];
      if (contentType !== undefined) {
        files.set(`/${path}`, { data: readFileSync(join(directory, entry.name)), contentType });
      }
    }
  }
}

/** La SPA de `directory` y el aspecto: ruta URL a (bytes, tipo). */
export function loadStaticFiles(
  directory: string,
  appearance: Appearance = new Appearance(),
): Map<string, StaticFile> {
  const files = new Map<string, StaticFile>();
  walk(directory, "", files);
  const index = files.get(`/${INDEX_FILE}`);
  if (index !== undefined) {
    files.set(INDEX_PATH, index);
  }
  for (const [path, file] of appearanceFiles(appearance)) {
    files.set(path, file);
  }
  return files;
}
