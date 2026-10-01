/**
 * Adaptador de E/S local de `ContextHasher` (m15-templates): lee del disco
 * los ficheros que un `CopyStep` nombra, filtrados por `.dockerignore`.
 * Nunca de red: el zip base viene de S3 en `build.ts`, no de aquí.
 */

import { readFile, stat } from "node:fs/promises";
import { join, relative } from "node:path";
import { BuildError } from "../errors.js";
import { DOCKERIGNORE_FILENAME, DockerIgnore } from "./context.js";
import type { CopyStep } from "./instructions.js";

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
  let ignore: DockerIgnore;
  try {
    ignore = DockerIgnore.fromText(await readFile(join(contextDir, DOCKERIGNORE_FILENAME), "utf8"));
  } catch {
    ignore = DockerIgnore.empty();
  }
  const seen = new Map<string, Uint8Array>();
  for (const step of copies) {
    const source = join(contextDir, step.src);
    let isDirectory: boolean;
    try {
      isDirectory = (await stat(source)).isDirectory();
    } catch {
      throw new BuildError(`la ruta de contexto no existe: ${relative(contextDir, source)}`, {
        reason: "context_path_missing",
      });
    }
    const files = isDirectory ? await listFilesRecursively(source) : [source];
    for (const file of files) {
      const relpath = relative(contextDir, file).split("\\").join("/");
      if (ignore.matches(relpath)) {
        continue;
      }
      seen.set(relpath, await readFile(file));
    }
  }
  return [...seen.entries()].sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
}
