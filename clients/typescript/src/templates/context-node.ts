/**
 * Adaptador de E/S local de `ContextHasher` (m15-templates): lee del disco
 * los ficheros que un `CopyStep` nombra, filtrados por `.dockerignore`.
 * Nunca de red: el zip base viene de S3 en `build.ts`, no de aquí.
 *
 * Las reglas (contención, enlaces, `.dockerignore`, orden) se escriben una
 * sola vez como pasos (`steps.ts`) y corren con `fs/promises`
 * (`collectContextFiles`, el SDK) o con las llamadas síncronas de `node:fs`
 * (`collectContextFilesSync`, los constructos CDK, ADR-032 D7).
 */

import {
  closeSync,
  constants,
  type Dirent,
  openSync,
  readdirSync,
  readFileSync,
  realpathSync,
  statSync,
} from "node:fs";
import { type FileHandle, open, readdir, readFile, realpath, stat } from "node:fs/promises";
import { isAbsolute, join, relative, sep } from "node:path";
import { BuildError } from "../errors.js";
import { runSteps, runStepsAsync, type Steps } from "../steps.js";
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

/** Una lectura del disco que piden los pasos de la recogida del contexto. */
type ContextRead =
  | { readonly kind: "realpath"; readonly path: string }
  | { readonly kind: "readText"; readonly path: string }
  | { readonly kind: "isDirectory"; readonly path: string }
  | { readonly kind: "listDirectory"; readonly path: string }
  | { readonly kind: "readNoFollow"; readonly path: string };

/** Una entrada de directorio: un enlace simbólico no es ni fichero ni directorio. */
interface DirectoryEntry {
  readonly name: string;
  readonly directory: boolean;
  readonly file: boolean;
}

function* ask<T>(request: ContextRead): Steps<ContextRead, T> {
  return (yield request) as T;
}

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

function byName(left: DirectoryEntry, right: DirectoryEntry): number {
  return left.name < right.name ? -1 : left.name > right.name ? 1 : 0;
}

/**
 * Los ficheros regulares bajo `root`. Un `Dirent` de enlace simbólico no es
 * ni `isFile()` ni `isDirectory()`, así que nunca se sigue un enlace (ni a
 * fichero ni a directorio), como Docker y `_walk_regular_files` del SDK de
 * Python: un `config -> ~/.aws/credentials` dentro del contexto no se lee.
 */
function* listFilesRecursively(root: string): Steps<ContextRead, string[]> {
  const entries = yield* ask<DirectoryEntry[]>({ kind: "listDirectory", path: root });
  const files: string[] = [];
  for (const entry of [...entries].sort(byName)) {
    const full = join(root, entry.name);
    if (entry.directory) {
      files.push(...(yield* listFilesRecursively(full)));
    } else if (entry.file) {
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
function* readContained(
  root: string,
  file: string,
  relpath: string,
): Steps<ContextRead, Uint8Array> {
  if (isOutside(root, yield* ask<string>({ kind: "realpath", path: file }))) {
    throw changedDuringRead(relpath);
  }
  try {
    return yield* ask<Uint8Array>({ kind: "readNoFollow", path: file });
  } catch (error) {
    throw isSymlinkLoop(error) ? changedDuringRead(relpath, error) : error;
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

function* ignorePatterns(resolvedRoot: string): Steps<ContextRead, string> {
  try {
    return yield* ask<string>({
      kind: "readText",
      path: join(resolvedRoot, DOCKERIGNORE_FILENAME),
    });
  } catch {
    return "";
  }
}

function* resolveSource(
  resolvedRoot: string,
  source: string,
): Steps<ContextRead, { readonly resolved: string; readonly directory: boolean }> {
  try {
    const resolved = yield* ask<string>({ kind: "realpath", path: source });
    const directory = yield* ask<boolean>({ kind: "isDirectory", path: resolved });
    return { resolved, directory };
  } catch {
    throw new BuildError(`la ruta de contexto no existe: ${relative(resolvedRoot, source)}`, {
      reason: "context_path_missing",
    });
  }
}

/** Los pasos de `collectContextFiles`: el contrato es el de esa función. */
function* contextFiles(
  contextDir: string,
  copies: readonly CopyStep[],
  extraIgnore: readonly string[],
): Steps<ContextRead, Array<[string, Uint8Array]>> {
  // Resolved once, like `_context.py`'s `collect_context_files`: every
  // `CopyStep.src` and every file found under it is checked against this
  // same real path, so a symlink that escapes `contextDir` is caught too.
  const resolvedRoot = yield* ask<string>({ kind: "realpath", path: contextDir });
  const patterns = yield* ignorePatterns(resolvedRoot);
  const ignore = DockerIgnore.fromText([patterns, ...extraIgnore].join("\n"));
  const seen = new Map<string, Uint8Array>();
  for (const step of copies) {
    const source = join(resolvedRoot, step.src);
    const { resolved, directory } = yield* resolveSource(resolvedRoot, source);
    ensureContained(resolvedRoot, resolved, step.src);
    const files = directory ? yield* listFilesRecursively(resolved) : [resolved];
    for (const file of files) {
      const relpath = relative(resolvedRoot, file).split("\\").join("/");
      if (ignore.matches(relpath)) {
        continue;
      }
      seen.set(relpath, yield* readContained(resolvedRoot, file, relpath));
    }
  }
  const warning = sensitiveContextWarning([...seen.keys()]);
  if (warning !== null) {
    process.emitWarning(warning, { type: SENSITIVE_CONTEXT_WARNING_TYPE });
  }
  return [...seen.entries()].sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
}

function entryOf(dirent: Dirent): DirectoryEntry {
  return { name: dirent.name, directory: dirent.isDirectory(), file: dirent.isFile() };
}

async function readNoFollow(path: string): Promise<Uint8Array> {
  const handle: FileHandle = await open(path, OPEN_NO_FOLLOW);
  try {
    return await handle.readFile();
  } finally {
    await handle.close();
  }
}

function readNoFollowSync(path: string): Uint8Array {
  const descriptor = openSync(path, OPEN_NO_FOLLOW);
  try {
    return readFileSync(descriptor);
  } finally {
    closeSync(descriptor);
  }
}

async function answer(request: ContextRead): Promise<unknown> {
  switch (request.kind) {
    case "realpath":
      return realpath(request.path);
    case "readText":
      return readFile(request.path, "utf8");
    case "isDirectory":
      return (await stat(request.path)).isDirectory();
    case "listDirectory":
      return (await readdir(request.path, { withFileTypes: true })).map(entryOf);
    case "readNoFollow":
      return readNoFollow(request.path);
  }
}

function answerSync(request: ContextRead): unknown {
  switch (request.kind) {
    case "realpath":
      return realpathSync(request.path);
    case "readText":
      return readFileSync(request.path, "utf8");
    case "isDirectory":
      return statSync(request.path).isDirectory();
    case "listDirectory":
      return readdirSync(request.path, { withFileTypes: true }).map(entryOf);
    case "readNoFollow":
      return readNoFollowSync(request.path);
  }
}

/** `[rutaRelativaAlContexto, contenido]`, ordenado y sin duplicados, para
 * cada `CopyStep.src` bajo `contextDir`. `extraIgnore` son patrones de
 * `.dockerignore` que se suman a los del fichero y filtran antes de leer. */
export async function collectContextFiles(
  contextDir: string,
  copies: readonly CopyStep[],
  extraIgnore: readonly string[] = [],
): Promise<Array<[string, Uint8Array]>> {
  return runStepsAsync(contextFiles(contextDir, copies, extraIgnore), answer);
}

/** `collectContextFiles` con lecturas síncronas: las mismas reglas y el mismo resultado. */
export function collectContextFilesSync(
  contextDir: string,
  copies: readonly CopyStep[],
  extraIgnore: readonly string[] = [],
): Array<[string, Uint8Array]> {
  return runSteps(contextFiles(contextDir, copies, extraIgnore), answerSync);
}
