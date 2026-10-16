/**
 * La imagen base gestionada por AWS sobre la que se compone `rayito-base` y
 * la clave bajo la que `rayito image publish` guarda el artefacto de código.
 * Las comparten `rayito doctor` y `rayito image publish`/`prune`.
 */

/** Nombre de la imagen base gestionada de Lambda MicroVMs (`AWS_API_NOTES.md` §4). */
export const MANAGED_BASE_IMAGE_NAME = "al2023-1";

/**
 * Prefijo de las claves S3 de los artefactos de `rayito image publish`; los
 * permisos `rayito/images/*` de `infra/iam.yaml` y `infra/templates.yaml` lo
 * cubren.
 */
export const IMAGE_ARTIFACT_KEY_PREFIX = "rayito/images";

/** El ARN de la imagen base gestionada en `region` (la cuenta es `aws`). */
export function baseImageArn(region: string): string {
  return `arn:aws:lambda:${region}:aws:microvm-image:${MANAGED_BASE_IMAGE_NAME}`;
}
