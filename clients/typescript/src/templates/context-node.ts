/**
 * Adaptador de E/S local de `ContextHasher` (m15-templates): lee del disco
 * los ficheros que un `CopyStep` nombra, filtrados por `.dockerignore`.
 * Nunca de red: el zip base viene de S3 en `build.ts`, no de aquí.
 */

import { constants } from "node:fs";
import { type FileHandle, open, readFile, realpath, stat } from "node:fs/promises";
import { isAbsolute, join, relative, sep } from "node:path";
import { BuildError } from "../errors.js";
import {
  DOCKERIGNORE_FILENAME,
  DockerIgnore,
  SENSITIVE_CONTEXT_WARNING_TYPE,
  sensitiveContextWarning,
} from "./context.js";
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
const CONTEXT_PATH_OUTSIDE = "context_path_outside";

function isOutside(root: string, resolved: string): boolean {
  const relpath = relative(root, resolved);
  return relpath === ".." || relpath.startsWith(`..${sep}`) || isAbsolute(relpath);
}

function ensureContained(root: string, resolvedSource: string, src: string): void {
  if (isOutside(root, resolvedSource)) {
    throw new BuildError(
      `la ruta de contexto ${JSON.stringify(src)} sale del contexto de build (${root})`,
      {
        reason: CONTEXT_PATH_OUTSIDE,
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

/** `O_NOFOLLOW` no existe en Windows; ahí basta con no seguir `Dirent`s de enlace. */
const OPEN_NO_FOLLOW = constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0);

/**
 * Lee `file` sin seguir un enlace en el último componente (`O_NOFOLLOW`) y
 * sólo si sigue resolviendo dentro de `root`: defensa en profundidad por si
 * el árbol cambia entre el recorrido y la lectura. Espejo de
 * `_read_contained` del SDK de Python.
 */
async function readContained(root: string, file: string, relpath: string): Promise<Uint8Array> {
  if (isOutside(root, await realpath(file))) {
    throw changedDuringRead(relpath);
  }
  let handle: FileHandle;
  try {
    handle = await open(file, OPEN_NO_FOLLOW);
  } catch (error) {
    throw isSymlinkLoop(error) ? changedDuringRead(relpath, error) : error;
  }
  try {
    return await handle.readFile();
  } finally {
    await handle.close();
  }
}

/** `open` con `O_NOFOLLOW` sobre un enlace falla con `ELOOP`. */
const SYMLINK_LOOP_CODE = "ELOOP";

function isSymlinkLoop(error: unknown): boolean {
  return (error as NodeJS.ErrnoException | undefined)?.code === SYMLINK_LOOP_CODE;
}

function changedDuringRead(relpath: string, cause?: unknown): BuildError {
  return new BuildError(
    `la ruta de contexto ${JSON.stringify(relpath)} cambió o sale del contexto de build durante la lectura`,
    cause === undefined
      ? { reason: CONTEXT_PATH_OUTSIDE }
      : { reason: CONTEXT_PATH_OUTSIDE, cause },
  );
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
      seen.set(relpath, await readContained(resolvedRoot, file, relpath));
    }
  }
  const warning = sensitiveContextWarning([...seen.keys()]);
  if (warning !== null) {
    process.emitWarning(warning, { type: SENSITIVE_CONTEXT_WARNING_TYPE });
  }
  return [...seen.entries()].sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
}
