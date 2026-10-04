/**
 * Adaptador de E/S local de `ContextHasher` (m15-templates): lee del disco
 * los ficheros que un `CopyStep` nombra, filtrados por `.dockerignore`.
 * Nunca de red: el zip base viene de S3 en `build.ts`, no de aquí.
 */

import { readFile, realpath, stat } from "node:fs/promises";
import { isAbsolute, join, relative, sep } from "node:path";
import { BuildError } from "../errors.js";
import { DOCKERIGNORE_FILENAME, DockerIgnore } from "./context.js";
import type { CopyStep } from "./instructions.js";

/**
 * Rechaza un `CopyStep.src` que resuelve fuera de `root` (un `../..`, un
 * enlace simbólico que escapa, o una ruta absoluta ajena): sin esto,
 * `collectContextFiles` leería ficheros fuera del contexto declarado y los
 * guardaría con un nombre de entrada de zip estilo `../..` (zip-slip) en el
 * artefacto subido. Espejo de `rayito._templates._context._ensure_contained`:
 * sólo mira el `src` de primer nivel (que pasa por `realpath`, así que un
 * `src` que es un enlace que apunta fuera también se rechaza); los enlaces
 * de dentro de un directorio copiado los omite `listFilesRecursively`.
 */
function ensureContained(root: string, resolvedSource: string, src: string): void {
  const relpath = relative(root, resolvedSource);
  if (relpath === ".." || relpath.startsWith(`..${sep}`) || isAbsolute(relpath)) {
    throw new BuildError(
      `la ruta de contexto ${JSON.stringify(src)} sale del contexto de build (${root})`,
      {
        reason: "context_path_outside",
      },
    );
  }
}

/**
 * Los ficheros regulares bajo `root`. Un `Dirent` de enlace simbólico no es
 * ni `isFile()` ni `isDirectory()`, así que nunca se sigue un enlace (ni a
 * fichero ni a directorio), como Docker y `_walk_regular_files` del SDK de
 * Python: un `config -> ~/.aws/credentials` dentro del contexto no se lee.
 */
async function listFilesRecursively(root: string): Promise<string[]> {
  const { readdir } = await import("node:fs/promises");
  const entries = await readdir(root, { withFileTypes: true });
  const files: string[] = [];
  for (const entry of entries.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0))) {
    const full = join(root, entry.name);
    if (entry.isDirectory()) {
      files.push(...(await listFilesRecursively(full)));
    } else if (entry.isFile()) {
      files.push(full);
    }
  }
  return files;
}

/** `[rutaRelativaAlContexto, contenido]`, ordenado y sin duplicados, para
 * cada `CopyStep.src` bajo `contextDir`. */
export async function collectContextFiles(
  contextDir: string,
  copies: readonly CopyStep[],
): Promise<Array<[string, Uint8Array]>> {
  // Resolved once, like `_context.py`'s `collect_context_files`: every
  // `CopyStep.src` and every file found under it is checked against this
  // same real path, so a symlink that escapes `contextDir` is caught too.
  const resolvedRoot = await realpath(contextDir);
  let ignore: DockerIgnore;
  try {
    ignore = DockerIgnore.fromText(
      await readFile(join(resolvedRoot, DOCKERIGNORE_FILENAME), "utf8"),
    );
  } catch {
    ignore = DockerIgnore.empty();
  }
  const seen = new Map<string, Uint8Array>();
  for (const step of copies) {
    const source = join(resolvedRoot, step.src);
    let resolvedSource: string;
    let isDirectory: boolean;
    try {
      resolvedSource = await realpath(source);
      isDirectory = (await stat(resolvedSource)).isDirectory();
    } catch {
      throw new BuildError(`la ruta de contexto no existe: ${relative(resolvedRoot, source)}`, {
        reason: "context_path_missing",
      });
    }
    ensureContained(resolvedRoot, resolvedSource, step.src);
    const files = isDirectory ? await listFilesRecursively(resolvedSource) : [resolvedSource];
    for (const file of files) {
      const relpath = relative(resolvedRoot, file).split("\\").join("/");
      if (ignore.matches(relpath)) {
        continue;
      }
      seen.set(relpath, await readFile(file));
    }
  }
  return [...seen.entries()].sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
}
