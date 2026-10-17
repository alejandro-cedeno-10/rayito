/**
 * Adaptador de disco del puerto `ProjectTree` (ADR-029): lista y lee los
 * ficheros de un directorio local para la detección. Espejo de
 * `rayito._agent._detect_fs`. Recorre por anchura (los manifiestos de la raíz
 * siempre caben), no entra en las carpetas excluidas (`.git`, `node_modules`,
 * `.venv`...) ni sigue enlaces simbólicos, y nunca lee fuera del directorio
 * del proyecto. No decide qué es un secreto: el dominio descarta `.env` antes
 * de leer nada.
 */

import {
  closeSync,
  type Dirent,
  fstatSync,
  lstatSync,
  openSync,
  readdirSync,
  readSync,
  realpathSync,
} from "node:fs";
import { join, relative, resolve, sep } from "node:path";
import { AGENT_SOURCE_DEFAULT_EXCLUDES } from "../limits.js";
import { globMatch, PATH_SEPARATOR, type ProjectTree } from "./detect-scan.js";

const NUL = 0;

function excludedDirectory(name: string): boolean {
  return AGENT_SOURCE_DEFAULT_EXCLUDES.some((pattern) => globMatch(pattern, name));
}

function entriesOf(folder: string): Dirent[] {
  try {
    return readdirSync(folder, { withFileTypes: true }).sort((left, right) =>
      left.name < right.name ? -1 : left.name > right.name ? 1 : 0,
    );
  } catch {
    return [];
  }
}

/** Un directorio del disco visto como `ProjectTree`. */
export class LocalProjectTree implements ProjectTree {
  readonly #root: string;

  constructor(root: string) {
    this.#root = realpathSync(resolve(root));
  }

  /** Rutas relativas con `/`, por anchura y en orden, como mucho `maxFiles`,
   * sin bajar más de `maxDepth` carpetas. */
  listFiles(maxDepth: number, maxFiles: number): string[] {
    const found: string[] = [];
    const pending: Array<[string, number]> = [[this.#root, 0]];
    for (
      let next = pending.shift();
      next !== undefined && found.length < maxFiles;
      next = pending.shift()
    ) {
      const [folder, depth] = next;
      for (const entry of entriesOf(folder)) {
        if (entry.isSymbolicLink()) {
          continue;
        }
        if (entry.isDirectory()) {
          if (depth < maxDepth && !excludedDirectory(entry.name)) {
            pending.push([join(folder, entry.name), depth + 1]);
          }
        } else if (entry.isFile() && found.length < maxFiles) {
          found.push(
            relative(this.#root, join(folder, entry.name)).split(sep).join(PATH_SEPARATOR),
          );
        }
      }
    }
    return found;
  }

  /** El texto de `path` (como mucho `maxBytes`), o `undefined` si no existe,
   * sale del proyecto, es un enlace o es binario. */
  readText(path: string, maxBytes: number): string | undefined {
    const target = join(this.#root, path);
    try {
      if (lstatSync(target).isSymbolicLink()) {
        return undefined;
      }
      const real = realpathSync(target);
      if (real !== this.#root && !real.startsWith(this.#root + sep)) {
        return undefined;
      }
      const descriptor = openSync(real, "r");
      try {
        const size = Math.min(maxBytes, fstatSync(descriptor).size);
        const buffer = Buffer.alloc(size);
        const read = readSync(descriptor, buffer, 0, size, 0);
        const data = buffer.subarray(0, read);
        return data.includes(NUL) ? undefined : data.toString("utf8");
      } finally {
        closeSync(descriptor);
      }
    } catch {
      return undefined;
    }
  }
}
