/**
 * La puerta de `volumes` en `Sandbox.create()` (`m15-efs-volumes`,
 * ADR-018, experimental). Espejo de `rayito._volumes._section`: valida la
 * forma de la petición antes de cualquier llamada a AWS y luego lanza
 * `UnimplementedError`, porque ningún build 0.6 de `rayd` tiene todavía un
 * `VolumeMounter` real, pendiente de la campaña de medición EFS-1..EFS-20
 * (`docs/research/2026-10-efs-persistence.md`).
 */

import { InvalidArgumentError, UnimplementedError } from "../errors.js";
import { validateMountPaths } from "../mount-path.js";
import { requireCapsFor } from "../role-policy.js";
import { EfsVolume } from "./domain.js";

export const MEASUREMENT_DOC = "docs/research/2026-10-efs-persistence.md";

/**
 * Lo que comparten `volumes` y el `volumeMounts` del shim de E2B, sin I/O:
 * rutas válidas, variante caps y, después, siempre `UnimplementedError`
 * (ningún `VolumeMounter` real todavía). Espejo de
 * `rayito._volumes._section.require_volume_mounts`.
 */
export function requireVolumeMounts(
  paths: readonly string[],
  imageVariant: string | undefined,
  feature = "volumes",
): never {
  validateMountPaths(paths);
  requireCapsFor(feature, imageVariant);
  throw new UnimplementedError(
    feature,
    "es experimental: necesita una imagen rayito-base-caps con amazon-efs-utils y " +
      "executionRoleArn más un conector egress a infra/efs-volumes.yaml, pendiente de la " +
      `campaña de medición EFS-1..EFS-20 (${MEASUREMENT_DOC})`,
  );
}

/**
 * Valida `volumes` por completo y después lanza siempre
 * `UnimplementedError`. El orden importa (rutas y forma antes que caps)
 * para que el primer error que vea el llamante sea siempre el más
 * específico.
 */
export function requireVolumeSupport(
  volumes: Readonly<Record<string, unknown>>,
  imageVariant: string | undefined,
): void {
  const entries = Object.entries(volumes);
  if (entries.length === 0) {
    throw new InvalidArgumentError("volumes no admite un objeto vacío; omite la opción");
  }
  for (const [, value] of entries) {
    if (!(value instanceof EfsVolume)) {
      throw new InvalidArgumentError("volumes espera valores EfsVolume");
    }
  }
  requireVolumeMounts(Object.keys(volumes), imageVariant);
}
