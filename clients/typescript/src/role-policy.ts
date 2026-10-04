/**
 * Puerta de política de roles para las funciones 0.6 que necesitan las
 * credenciales del execution role dentro del guest (M15 foundations):
 * s3-mounts, efs-volumes y rayd-otlp con `OtlpAuth.executionRole()`. Espejo
 * de `rayito._role_policy`.
 */

import { UnimplementedError } from "./errors.js";

const RAYITO_IMAGE_PATTERN = /^rayito-(?<variant>[a-z]+(?:-[a-z]+)*?)(?:-\d+[a-z]+)?$/;
export const CAPS_VARIANT = "base-caps";
/**
 * `rayito image publish --with-efs` (m15-efs-volumes): la imagen caps con la
 * capa de `amazon-efs-utils` (`rayito-base-caps-efs`); concede lo mismo que la
 * caps y además deja montar `volumes`.
 */
export const EFS_CAPS_VARIANT = `${CAPS_VARIANT}-efs`;
/** Las variantes que conceden el capability mask de ADR-012. */
export const CAPS_VARIANTS: ReadonlySet<string> = new Set([CAPS_VARIANT, EFS_CAPS_VARIANT]);

/**
 * `"rayito-base-caps-4gb"` -> `"base-caps"`; `"rayito-base"` -> `"base"`;
 * `undefined`, un ARN o un nombre fuera de la convención devuelven
 * `undefined` (variante desconocida: la decisión se difiere al agente).
 */
export function resolveImageVariant(template: string | undefined): string | undefined {
  if (!template || template.startsWith("arn:")) {
    return undefined;
  }
  const match = RAYITO_IMAGE_PATTERN.exec(template);
  return match?.groups?.variant;
}

/**
 * Lanza `UnimplementedError` si `imageVariant` se conoce y no es una variante
 * caps (`CAPS_VARIANTS`: la caps o la caps con `amazon-efs-utils`); no hace
 * nada cuando lo es o cuando es `undefined` (desconocida).
 */
export function requireCapsFor(feature: string, imageVariant: string | undefined): void {
  if (imageVariant === undefined || CAPS_VARIANTS.has(imageVariant)) {
    return;
  }
  throw new UnimplementedError(
    feature,
    `necesita una imagen de la variante '${CAPS_VARIANT}' (o derivada, por tamaño); ` +
      `la imagen pedida es la variante '${imageVariant}'`,
  );
}
