/**
 * Regla de rutas de montaje compartida por `mounts` (s3-mounts) y `volumes`
 * (efs-volumes) (M15 foundations). Espejo de `rayito._mount_path`. Valida
 * las claves antes de lanzar nada, puramente sobre la cadena.
 *
 * Reglas: absoluta, canónica (sin `.`, `..` ni `//`, sin `/` final), bajo
 * `/mnt/` o `/home/user/`, sin solapar entre sí y como mucho `MAX_MOUNTS`
 * por sandbox.
 */

import { InvalidArgumentError } from "./errors.js";

/** Un sandbox tiene como mucho 4 puntos de montaje entre `mounts` y
 * `volumes` juntos. */
export const MAX_MOUNTS = 4;
const ALLOWED_ROOTS = ["/mnt/", "/home/user/"];

function normalize(path: string): string {
  const segments: string[] = [];
  for (const segment of path.split("/")) {
    if (segment === "" || segment === ".") {
      continue;
    }
    if (segment === "..") {
      segments.pop();
      continue;
    }
    segments.push(segment);
  }
  return `/${segments.join("/")}`;
}

function validateOne(path: string): void {
  if (!path.startsWith("/")) {
    throw new InvalidArgumentError(`la ruta de montaje debe ser absoluta: ${JSON.stringify(path)}`);
  }
  if (normalize(path) !== path) {
    throw new InvalidArgumentError(
      `la ruta de montaje debe ser canónica (sin ".", ".." ni "//" ni "/" final): ${JSON.stringify(path)}`,
    );
  }
  const underRoot = ALLOWED_ROOTS.some(
    (root) => path.startsWith(root) && path !== root.slice(0, -1),
  );
  if (!underRoot) {
    throw new InvalidArgumentError(
      `la ruta de montaje debe estar bajo ${ALLOWED_ROOTS.join(" o ")}: ${JSON.stringify(path)}`,
    );
  }
}

function overlaps(first: string, second: string): boolean {
  const firstDir = `${first}/`;
  const secondDir = `${second}/`;
  return first === second || firstDir.startsWith(secondDir) || secondDir.startsWith(firstDir);
}

/** Valida cada ruta y que, juntas, no se solapen ni superen `MAX_MOUNTS`. */
export function validateMountPaths(paths: readonly string[]): readonly string[] {
  if (paths.length > MAX_MOUNTS) {
    throw new InvalidArgumentError(
      `como mucho ${MAX_MOUNTS} montajes por sandbox (mounts + volumes), se pidieron ${paths.length}`,
    );
  }
  const validated: string[] = [];
  for (const path of paths) {
    validateOne(path);
    for (const other of validated) {
      if (overlaps(path, other)) {
        throw new InvalidArgumentError(
          `las rutas de montaje no pueden solaparse: ${JSON.stringify(path)} y ${JSON.stringify(other)}`,
        );
      }
    }
    validated.push(path);
  }
  return paths;
}
