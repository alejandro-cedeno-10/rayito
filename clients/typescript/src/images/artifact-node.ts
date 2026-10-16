/**
 * Los adaptadores de ficheros del artefacto de la imagen: recorre el árbol
 * con las exclusiones de `artifact.ts`, escribe el zip determinista y lee la
 * variante de un zip ya construido. Espejo de `write_zip`, `copy_sidecar` y
 * `artifact_sha256` de `rayito.cli._artifact`.
 */

import { createHash } from "node:crypto";
import {
  copyFileSync,
  existsSync,
  lstatSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { dirname, join, relative, sep } from "node:path";
import { readZipEntries, writeZipEntries } from "../templates/zip-node.js";
import {
  comparePaths,
  DOCKERFILE_ENTRY,
  hasEfsMarker,
  ImageArtifactError,
  isExcluded,
  isExcludedDirectoryName,
  markerVariantOf,
  modeOf,
  NOTICES_DIRECTORY,
  RAYD_ENTRY,
  RAYD_NOTICE_FILES,
  REQUIREMENTS_FILES,
  syntheticEntries,
} from "./artifact.js";
import type { ArtifactFiles } from "./publish-service.js";

const POSIX_SEPARATOR = "/";

function posixRelative(root: string, path: string): string {
  return relative(root, path).split(sep).join(POSIX_SEPARATOR);
}

/**
 * Un enlace simbólico llevaría los bytes de su destino, que puede estar en
 * cualquier sitio de la máquina que construye (un fichero de credenciales del
 * home), a cada imagen de sandbox. Ni el árbol de la imagen ni el sidecar
 * tienen ninguno: el recorrido lo rechaza en vez de seguirlo.
 */
function refuseSymlink(root: string, path: string): void {
  if (lstatSync(path).isSymbolicLink()) {
    throw new ImageArtifactError(
      "symlink",
      `${posixRelative(root, path)} es un enlace simbólico: el artefacto de la imagen sólo lleva ficheros normales`,
    );
  }
}

function walk(root: string, directory: string, found: string[]): void {
  for (const entry of readdirSync(directory)) {
    const path = join(directory, entry);
    const relativePath = posixRelative(root, path);
    const stat = lstatSync(path);
    const excludedDirectory =
      isExcludedDirectoryName(entry) && (stat.isSymbolicLink() || stat.isDirectory());
    if (isExcluded(relativePath) || excludedDirectory) {
      continue;
    }
    refuseSymlink(root, path);
    if (stat.isDirectory()) {
      walk(root, path, found);
    } else if (stat.isFile()) {
      found.push(relativePath);
    }
  }
}

/** Los ficheros de `root` que viajan en el zip, en el orden de `sorted(Path)` de Python. */
export function shippedFiles(root: string): string[] {
  const found: string[] = [];
  if (!existsSync(root)) {
    return found;
  }
  walk(root, root, found);
  return found.sort(comparePaths);
}

/** Un árbol que lleva `rayd` lleva a su lado los tres avisos de licencia; sin `rayd` no se distribuye ningún binario. */
function requireRaydNotices(imageDir: string): void {
  if (!existsSync(join(imageDir, RAYD_ENTRY))) {
    return;
  }
  for (const name of RAYD_NOTICE_FILES) {
    if (!existsSync(join(imageDir, NOTICES_DIRECTORY, name))) {
      throw new ImageArtifactError(
        "missingNotice",
        `falta ${NOTICES_DIRECTORY}/${name}: un artefacto que lleva rayd lleva sus avisos de ` +
          "licencia (make image-licenses)",
      );
    }
  }
}

function imageFiles(imageDir: string): string[] {
  const files = shippedFiles(imageDir);
  if (!existsSync(join(imageDir, DOCKERFILE_ENTRY))) {
    throw new ImageArtifactError(
      "missingDockerfile",
      `falta ${DOCKERFILE_ENTRY} en la raíz de la imagen`,
    );
  }
  requireRaydNotices(imageDir);
  return files;
}

/** El zip determinista de `imageDir`: los ficheros del árbol y después los marcadores de la variante; devuelve cuántas entradas lleva. */
export function writeImageZip(
  imageDir: string,
  destination: string,
  variant: string,
  options: { readonly withEfs: boolean },
): number {
  const files = imageFiles(imageDir);
  const markers = syntheticEntries(variant, options.withEfs);
  const entries = [
    ...files.map((name) => ({
      name,
      content: new Uint8Array(readFileSync(join(imageDir, name))),
      mode: modeOf(name.split(POSIX_SEPARATOR).at(-1) ?? name),
    })),
    ...markers.map(([name, content]) => ({
      name,
      content: new TextEncoder().encode(content),
      mode: modeOf(name),
    })),
  ];
  writeFileSync(destination, writeZipEntries(entries, { compress: true, unixCreator: true }));
  return entries.length;
}

/** La variante y el marcador de EFS de un zip ya escrito. */
export function inspectArtifact(artifact: string): {
  readonly variant: ReturnType<typeof markerVariantOf>;
  readonly withEfs: boolean;
} {
  const entries = readZipEntries(new Uint8Array(readFileSync(artifact)));
  return { variant: markerVariantOf(entries), withEfs: hasEfsMarker(entries) };
}

export function artifactSha256(path: string): string {
  return createHash("sha256").update(readFileSync(path)).digest("hex");
}

/** Reemplaza `destination` entero por los ficheros de `source` que el zip enviaría (las mismas exclusiones). */
function copyTree(source: string, destination: string): number {
  rmSync(destination, { recursive: true, force: true });
  const files = shippedFiles(source);
  for (const name of files) {
    const target = join(destination, ...name.split(POSIX_SEPARATOR));
    mkdirSync(dirname(target), { recursive: true });
    copyFileSync(join(source, name), target);
  }
  return files.length;
}

/** Los dos ficheros de pines viajan (`requirements.txt` en toda imagen y `requirements-poly.txt` que sólo lee la capa `poly`); la copia falla si falta alguno. */
export function copySidecar(source: string, destination: string): number {
  for (const requirements of REQUIREMENTS_FILES) {
    const path = join(source, requirements);
    if (!existsSync(path) || !statSync(path).isFile()) {
      throw new ImageArtifactError("missingRequirements", `falta ${requirements} en el sidecar`);
    }
  }
  return copyTree(source, destination);
}

/** El puerto de lectura del artefacto de `publish`, sobre el disco. */
export const fileArtifacts: ArtifactFiles = {
  exists: (path) => existsSync(path) && statSync(path).isFile(),
  read: (path) => new Uint8Array(readFileSync(path)),
  inspect: inspectArtifact,
};
