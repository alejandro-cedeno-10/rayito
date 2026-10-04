/**
 * `ContextHasher` (m15-templates). Espejo de `rayito._templates._context`:
 * qué ficheros locales entran en el contexto de build, filtrados por
 * `.dockerignore`, y un hash determinista sobre su contenido. La E/S de
 * fichero es local (nunca de red): vive en `context-node.ts`, detrás de un
 * puerto `ContextReader` para que este módulo siga siendo puro.
 *
 * Divergencia documentada: el hash es propio y determinista, no se ha
 * verificado bit a bit contra el `filesHash` real de E2B.
 */

import { createHash } from "node:crypto";
import type { CopyStep } from "./instructions.js";

export const DOCKERIGNORE_FILENAME = ".dockerignore";

export class DockerIgnore {
  readonly #patterns: ReadonlyArray<readonly [pattern: string, negated: boolean]>;

  private constructor(patterns: ReadonlyArray<readonly [string, boolean]>) {
    this.#patterns = patterns;
  }

  static fromText(text: string): DockerIgnore {
    const patterns: Array<[string, boolean]> = [];
    for (const rawLine of text.split("\n")) {
      const line = rawLine.trim();
      if (!line || line.startsWith("#")) {
        continue;
      }
      const negated = line.startsWith("!");
      const pattern = (negated ? line.slice(1) : line).trim().replace(/^\/+|\/+$/g, "");
      patterns.push([pattern, negated]);
    }
    return new DockerIgnore(patterns);
  }

  static empty(): DockerIgnore {
    return new DockerIgnore([]);
  }

  matches(relpath: string): boolean {
    let excluded = false;
    for (const [pattern, negated] of this.#patterns) {
      if (globMatch(pattern, relpath) || globMatch(`${pattern}/*`, relpath)) {
        excluded = !negated;
      }
    }
    return excluded;
  }
}

/** Glob simple (`*` cruza `/`, como `fnmatch` de Python), suficiente para un
 * `.dockerignore` de contexto de template. */
function globMatch(pattern: string, value: string): boolean {
  const escaped = pattern
    .replace(/[.+^${}()|[\]\\]/g, "\\$&")
    .replace(/\*/g, ".*")
    .replace(/\?/g, ".");
  return new RegExp(`^${escaped}$`).test(value);
}

/** sha256 hexadecimal sobre `entries`: el mismo conjunto de ficheros con el
 * mismo contenido siempre da el mismo hash, sin importar el orden. */
export function filesHash(entries: ReadonlyArray<readonly [string, Uint8Array]>): string {
  const sorted = [...entries].sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  const digest = createHash("sha256");
  for (const [relpath, content] of sorted) {
    digest.update(relpath, "utf8");
    digest.update(Buffer.from([0]));
    digest.update(createHash("sha256").update(content).digest());
  }
  return digest.digest("hex");
}

/** El `CopyStep` de `spec.steps`, en orden (filtro usado por
 * `context-node.ts` al leer el contexto local). */
export function copySteps(steps: ReadonlyArray<{ readonly kind: string }>): CopyStep[] {
  return steps.filter((step): step is CopyStep => step.kind === "copy");
}
