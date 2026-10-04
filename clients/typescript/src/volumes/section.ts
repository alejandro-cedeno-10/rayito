/**
 * La puerta de `volumes` en `Sandbox.create()` (`m15-efs-volumes`,
 * ADR-018, experimental). Espejo de `rayito._volumes._section`: valida la
 * petición antes de cualquier llamada a AWS (rutas, tipos, variante de
 * imagen y el conector de `egress`) y luego lanza `UnimplementedError`.
 * `rayd` ya monta EFS, pero sólo en una imagen con `amazon-efs-utils`, que
 * ninguna imagen publicada de Rayito trae todavía, y `create()` aún no manda
 * la sección `efs_volumes`. Un MicroVM admite un solo conector de egress
 * (`AWS_API_NOTES.md` §16 Q131): un volumen necesita el de tu VPC, así que no
 * se combina con `INTERNET_EGRESS`.
 */

import { InvalidArgumentError, UnimplementedError } from "../errors.js";
import { hasInternetConnector } from "../models.js";
import { validateMountPaths } from "../mount-path.js";
import { requireCapsFor } from "../role-policy.js";
import { EfsVolume } from "./domain.js";

export const MEASUREMENT_DOC = "docs/research/2026-10-efs-persistence.md";
/** La página que explica cómo dar internet a un sandbox con volumen. */
export const VPC_GUIDE = "funciones-opcionales/volumenes-efs-vpc.md";
/** Cómo tiene internet un sandbox con volumen: saliendo por la VPC (Q131). */
export const INTERNET_THROUGH_VPC =
  "para tener internet además del volumen, sal por tu VPC: una NAT o un transit gateway " +
  "en las subredes del conector y un conector cuyo grupo de seguridad permita esa salida " +
  `(el de efs-volumes sólo deja salir NFS); ver ${VPC_GUIDE}`;
export const UNIMPLEMENTED_REASON =
  "es experimental: rayd sólo monta en una imagen rayito-base-caps con amazon-efs-utils, que " +
  "ninguna imagen publicada trae todavía, y create() aún no manda la sección efs_volumes " +
  `(${MEASUREMENT_DOC})`;
/** Por qué `volumeMounts` del shim de E2B nunca monta tal cual. */
export const SHIM_REASON =
  "el shim de E2B siempre lanza con INTERNET_EGRESS y un MicroVM sólo admite un conector de " +
  "egress, que un volumen necesita para llegar a tu VPC: usa rayito Sandbox.create({ volumes, " +
  `egress: [<ConnectorArn>] }); además, ${UNIMPLEMENTED_REASON}`;

/**
 * Sin I/O: `egress` tiene que ser exactamente un conector propio (el
 * `ConnectorArn` de `infra/efs-volumes.yaml` o uno de tu VPC que llegue al
 * mount target). `InvalidArgumentError` si falta (heredaría
 * `INTERNET_EGRESS` de la imagen), si incluye `INTERNET_EGRESS` o si trae
 * más de uno. Espejo de `require_volume_connector`.
 */
export function requireVolumeConnector(
  egress: readonly string[] | undefined,
  feature = "volumes",
): void {
  const connectors = egress ?? [];
  if (connectors.length === 0) {
    throw new InvalidArgumentError(
      `${feature} necesita egress: [<ConnectorArn de efs-volumes>]: sin él el sandbox hereda ` +
        `INTERNET_EGRESS de la imagen y no llega al mount target; ${INTERNET_THROUGH_VPC}`,
    );
  }
  if (hasInternetConnector(connectors)) {
    throw new InvalidArgumentError(
      `${feature} no se combina con INTERNET_EGRESS: un MicroVM admite un solo conector de ` +
        `egress y el volumen necesita el de tu VPC; ${INTERNET_THROUGH_VPC}`,
    );
  }
  if (connectors.length > 1) {
    throw new InvalidArgumentError(
      `${feature} admite un solo conector en egress (un MicroVM sólo acepta uno); ` +
        INTERNET_THROUGH_VPC,
    );
  }
}

/**
 * La puerta del `volumeMounts` del shim de E2B, sin I/O: rutas válidas,
 * variante caps y, después, siempre `UnimplementedError` con `reason`.
 * Espejo de `rayito._volumes._section.require_volume_mounts`.
 */
export function requireVolumeMounts(
  paths: readonly string[],
  imageVariant: string | undefined,
  feature = "volumeMounts",
  reason = SHIM_REASON,
): never {
  validateMountPaths(paths);
  requireCapsFor(feature, imageVariant);
  throw new UnimplementedError(feature, reason);
}

/**
 * Valida `volumes` por completo y después lanza siempre
 * `UnimplementedError`. El orden importa (forma, rutas, caps y conector
 * antes que la función pendiente) para que el primer error que vea el
 * llamante sea siempre el que puede corregir.
 */
export function requireVolumeSupport(
  volumes: Readonly<Record<string, unknown>>,
  imageVariant: string | undefined,
  egress?: readonly string[],
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
  validateMountPaths(Object.keys(volumes));
  requireCapsFor("volumes", imageVariant);
  requireVolumeConnector(egress);
  throw new UnimplementedError("volumes", UNIMPLEMENTED_REASON);
}
