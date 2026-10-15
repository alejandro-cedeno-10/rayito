/**
 * Puerta de política de roles para las funciones 0.6 que necesitan las
 * credenciales del execution role dentro del guest (M15 foundations):
 * s3-mounts, efs-volumes y rayd-otlp con `OtlpAuth.executionRole()`. Espejo
 * de `rayito._role_policy`: por nombre sólo se reconoce el patrón oficial
 * `rayito-<variante>[-<talla>]` (variante en `OFFICIAL_IMAGE_VARIANTS`, talla en
 * `SIZE_NAMES`); una imagen propia o un ARN se difiere a `Health.features`.
 */

import { UnimplementedError } from "./errors.js";
import { OFFICIAL_IMAGE_VARIANTS } from "./limits.js";
import { SIZE_NAMES } from "./sizing/sizing.js";

const IMAGE_NAME_PREFIX = "rayito-";
const ARN_PREFIX = "arn:";
export const CAPS_VARIANT = "base-caps";
/**
 * `rayito image publish --with-efs` (m15-efs-volumes): la imagen caps con la
 * capa de `amazon-efs-utils` (`rayito-base-caps-efs`); concede lo mismo que la
 * caps y además deja montar `volumes`.
 */
export const EFS_CAPS_VARIANT = `${CAPS_VARIANT}-efs`;
/** Las variantes que conceden el capability mask de ADR-012. */
export const CAPS_VARIANTS: ReadonlySet<string> = new Set([CAPS_VARIANT, EFS_CAPS_VARIANT]);
/** Las variantes oficiales (catálogo cerrado de `limits.json`). */
const KNOWN_VARIANTS: ReadonlySet<string> = new Set(OFFICIAL_IMAGE_VARIANTS);

/**
 * `"rayito-base-caps-4gb"` -> `"base-caps"`; `"rayito-base"` -> `"base"`.
 * `undefined`, un ARN y cualquier nombre fuera del patrón oficial (también
 * `rayito-mybuild`: una imagen propia) devuelven `undefined`, variante
 * desconocida: la decisión se difiere al agente.
 */
export function resolveImageVariant(template: string | undefined): string | undefined {
  if (!template || template.startsWith(ARN_PREFIX) || !template.startsWith(IMAGE_NAME_PREFIX)) {
    return undefined;
  }
  const candidate = template.slice(IMAGE_NAME_PREFIX.length);
  if (KNOWN_VARIANTS.has(candidate)) {
    return candidate;
  }
  for (const size of SIZE_NAMES) {
    const suffix = `-${size}`;
    if (candidate.endsWith(suffix)) {
      const unsized = candidate.slice(0, -suffix.length);
      if (KNOWN_VARIANTS.has(unsized)) {
        return unsized;
      }
    }
  }
  return undefined;
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

/**
 * Como `requireCapsFor`, pero para `volumes`: con la variante conocida exige
 * `EFS_CAPS_VARIANT` (la caps sin `amazon-efs-utils` no puede montar EFS y
 * fallaría tras pagar el lanzamiento). Una variante desconocida no se rechaza
 * aquí: `Health.features.efs_volumes` decide tras `/run`.
 */
export function requireEfsFor(feature: string, imageVariant: string | undefined): void {
  if (imageVariant === undefined || imageVariant === EFS_CAPS_VARIANT) {
    return;
  }
  throw new UnimplementedError(
    feature,
    `necesita una imagen de la variante '${EFS_CAPS_VARIANT}' (o derivada, por tamaño); ` +
      `la imagen pedida es la variante '${imageVariant}'`,
  );
}
