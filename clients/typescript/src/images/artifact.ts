/**
 * El artefacto de la imagen (dominio puro): qué ficheros del directorio de la
 * imagen viajan en el zip, qué entradas sintéticas añade cada variante y cómo
 * se reconoce la variante de un zip ya construido. Espejo de
 * `rayito.cli._artifact`; `testdata/images/artifact-cases.json` fija las
 * entradas, su orden y su modo en los dos lenguajes.
 *
 * Los dos adaptadores de ficheros (recorrer el árbol y escribir el zip) viven
 * en `artifact-node.ts`.
 */

export const IMAGE_VARIANTS = ["full", "slim", "poly"] as const;
export type ImageVariant = (typeof IMAGE_VARIANTS)[number];

export const EXCLUDED_SUFFIXES: readonly string[] = [".zip", ".pyc"];
export const EXCLUDED_DIRECTORIES: ReadonlySet<string> = new Set([
  "__pycache__",
  "tests",
  ".venv",
  ".pytest_cache",
  ".ruff_cache",
  ".mypy_cache",
]);
export const EXCLUDED_FILES: ReadonlySet<string> = new Set([
  "uv.lock",
  ".gitignore",
  "requirements.in",
]);
/** Los marcadores sintéticos: `rayd` lee su contenido en el arranque de la imagen. */
export const WARMUP_MARKER_ENTRY = "kernel-sidecar/ipython/startup/warmup_variant";
export const KERNELS_MARKER_ENTRY = "kernel-sidecar/kernels_variant";
export const EFS_MARKER_ENTRY = "kernel-sidecar/efs_variant";
const SLIM_MARKER_CONTENT = "slim\n";
const POLY_MARKER_CONTENT = "poly\n";
const EFS_MARKER_CONTENT = "efs\n";
const MARKER_ENTRIES: Readonly<Record<string, string>> = {
  slim: WARMUP_MARKER_ENTRY,
  poly: KERNELS_MARKER_ENTRY,
};
const MARKER_CONTENTS: Readonly<Record<string, string>> = {
  slim: SLIM_MARKER_CONTENT,
  poly: POLY_MARKER_CONTENT,
};
export const RAYD_ENTRY = "rayd";
export const NOTICES_DIRECTORY = "licenses";
export const RAYD_NOTICE_FILES: readonly string[] = [
  "LICENSE",
  "NOTICE",
  "THIRD_PARTY_LICENSES.md",
];
export const REQUIREMENTS_FILES: readonly string[] = ["requirements.txt", "requirements-poly.txt"];
export const DOCKERFILE_ENTRY = "Dockerfile";
export const SIDECAR_DIRECTORY = "kernel-sidecar";
/** Modos Unix del zip: `rayd` ejecutable y todo lo demás de sólo lectura para el resto. */
export const EXECUTABLE_MODE = 0o755;
export const REGULAR_MODE = 0o644;
const PATH_SEPARATOR = "/";

/** El zip no se puede armar: falta el Dockerfile, un aviso de licencia o hay un enlace simbólico. */
export class ImageArtifactError extends Error {
  readonly kind: "missingDockerfile" | "missingNotice" | "symlink" | "missingRequirements";

  constructor(kind: ImageArtifactError["kind"], message: string) {
    super(message);
    this.name = "ImageArtifactError";
    this.kind = kind;
  }
}

/** `true` si el fichero de ruta relativa `relative` (con `/`) no viaja en el zip. */
export function isExcluded(relative: string): boolean {
  const parts = relative.split(PATH_SEPARATOR);
  const name = parts.at(-1) ?? "";
  if (EXCLUDED_SUFFIXES.some((suffix) => name.endsWith(suffix)) || EXCLUDED_FILES.has(name)) {
    return true;
  }
  return parts.slice(0, -1).some((part) => EXCLUDED_DIRECTORIES.has(part));
}

/** Un directorio excluido en sí mismo: no se recorre ni se rechaza. */
export function isExcludedDirectoryName(name: string): boolean {
  return EXCLUDED_DIRECTORIES.has(name);
}

/** El orden de `sorted(Path)` de Python: componente a componente, no por la cadena entera. */
export function comparePaths(left: string, right: string): number {
  const leftParts = left.split(PATH_SEPARATOR);
  const rightParts = right.split(PATH_SEPARATOR);
  const shared = Math.min(leftParts.length, rightParts.length);
  for (let index = 0; index < shared; index += 1) {
    const a = leftParts[index] ?? "";
    const b = rightParts[index] ?? "";
    if (a !== b) {
      return a < b ? -1 : 1;
    }
  }
  return leftParts.length - rightParts.length;
}

/** Las entradas (nombre → contenido) que un zip de `variant` lleva además de los ficheros del árbol. */
export function syntheticEntries(variant: string, withEfs: boolean): Array<[string, string]> {
  const entries: Array<[string, string]> = [];
  const marker = MARKER_ENTRIES[variant];
  if (marker !== undefined) {
    entries.push([marker, MARKER_CONTENTS[variant] ?? ""]);
  }
  if (withEfs) {
    entries.push([EFS_MARKER_ENTRY, EFS_MARKER_CONTENT]);
  }
  return entries;
}

/** El modo de la entrada de un fichero del árbol: sólo `rayd` es ejecutable. */
export function modeOf(fileName: string): number {
  return fileName === RAYD_ENTRY ? EXECUTABLE_MODE : REGULAR_MODE;
}

/**
 * La variante para la que se armó un zip: `poly` o `slim` si lleva su
 * marcador con ese contenido, `full` si no lleva ninguno. Los dos a la vez
 * nombran un artefacto que ningún script produce y se rechazan.
 */
export function markerVariantOf(entries: ReadonlyMap<string, Uint8Array>): ImageVariant {
  const found = IMAGE_VARIANTS.filter((variant) => {
    const entry = MARKER_ENTRIES[variant];
    const content = entry === undefined ? undefined : entries.get(entry);
    return content !== undefined && new TextDecoder().decode(content).trim() === variant;
  });
  if (found.length > 1) {
    throw new RangeError("el artefacto lleva a la vez el marcador slim y el poly");
  }
  return found[0] ?? "full";
}

/** `true` si el zip lleva el marcador de `amazon-efs-utils` con su contenido exacto. */
export function hasEfsMarker(entries: ReadonlyMap<string, Uint8Array>): boolean {
  const content = entries.get(EFS_MARKER_ENTRY);
  return content !== undefined && new TextDecoder().decode(content) === EFS_MARKER_CONTENT;
}
