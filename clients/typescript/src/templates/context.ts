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
import { DockerIgnore } from "./dockerignore.js";
import type { CopyStep } from "./instructions.js";

export { DOCKERIGNORE_FILENAME, DockerIgnore } from "./dockerignore.js";

/**
 * Rutas que casi siempre llevan secretos o historia que no debería acabar en
 * una imagen (credenciales de `.env`, `.git` con remotos con token,
 * `.aws`/`.ssh`, claves privadas). Sólo avisan, no excluyen: el
 * `.dockerignore` del usuario manda. Mismo texto que `SENSITIVE_PATTERNS` de
 * `rayito._templates._context`.
 */
export const SENSITIVE_PATTERNS =
  "**/.env\n**/.env.*\n**/.git\n**/.aws\n**/.ssh\n**/*.pem\n**/*.key\n";
/** Cuántas rutas sensibles se nombran en el aviso (el resto sólo se cuenta). */
export const SENSITIVE_SAMPLE_SIZE = 3;
/** El `type` de `process.emitWarning` del aviso de rutas sensibles. */
export const SENSITIVE_CONTEXT_WARNING_TYPE = "RayitoContextWarning";
const SENSITIVE = DockerIgnore.fromText(SENSITIVE_PATTERNS);

/** Las rutas de `relpaths` que casan con `SENSITIVE_PATTERNS`. */
export function sensitivePaths(relpaths: readonly string[]): string[] {
  return relpaths.filter((relpath) => SENSITIVE.matches(relpath));
}

/** El texto del aviso, o `null` si ninguna ruta es sensible. Nunca lleva el
 * contenido de los ficheros. */
export function sensitiveContextWarning(relpaths: readonly string[]): string | null {
  const found = sensitivePaths(relpaths);
  if (found.length === 0) {
    return null;
  }
  return (
    `el contexto de build empaqueta ${found.length} fichero(s) que suelen llevar secretos ` +
    `(p. ej. ${found.slice(0, SENSITIVE_SAMPLE_SIZE).join(", ")}): exclúyelos en .dockerignore ` +
    "si no deben acabar en la imagen, donde cualquier código del sandbox puede leerlos"
  );
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
