/**
 * Los errores propios de los constructos CDK (agentcore-cdk, ADR-032): los
 * códigos y mensajes de `testdata/agentcore-cdk/contract.json`
 * (`errors.messages`), los mismos que `RayitoCdkException` de Python. Los
 * errores del plan de AgentCore salen como `AgentCoreError`, sin traducir.
 * Puro: ningún import de `aws-cdk-lib`.
 */

import {
  formatTemplate,
  type MessageFields,
  messageFields,
  type TemplateFields,
} from "../template-text.js";

export const RAYITO_CDK_ERROR_CODES = [
  "region_unresolved",
  "account_invalid",
  "image_name_invalid",
  "image_name_reserved",
  "artifact_not_found",
  "artifact_checksum_mismatch",
  "artifact_variant_mismatch",
  "artifact_efs_mismatch",
  "option_invalid",
  "release_download_failed",
] as const;

export type RayitoCdkErrorCode = (typeof RAYITO_CDK_ERROR_CODES)[number];

/** `contract.errors.messages`, en español y sin ARN, URL ni texto crudo de AWS. */
export const RAYITO_CDK_ERROR_MESSAGES: Readonly<Record<RayitoCdkErrorCode, string>> = {
  region_unresolved:
    "el constructo {construct} necesita una región concreta en el stack (env.region): con un stack sin entorno la región es un token y no se puede validar",
  account_invalid: "la cuenta {account} del stack no es un id de 12 dígitos",
  image_name_invalid:
    "el nombre de imagen {name} no vale: de 1 a {max} caracteres de letras, dígitos, - o _ (con el sufijo de tamaño incluido)",
  image_name_reserved:
    "el nombre de imagen {name} está reservado para rayito setup y rayito image publish ({prefix}*): elige otro",
  artifact_not_found: "no existe el artefacto {path}",
  artifact_checksum_mismatch: "el sha256 de {path} no coincide con el esperado",
  artifact_variant_mismatch: "el artefacto es de la variante {found} y se pidió {variant}",
  artifact_efs_mismatch: "el artefacto {state} el marcador de EFS y with_efs es {withEfs}",
  option_invalid: "{option}: {reason}",
  release_download_failed: "no se pudo descargar {asset} de la release {release}: {reason}",
};

/**
 * Un constructo de `rayito/cdk` no se puede sintetizar con estas props o
 * este entorno. Se lanza en el constructor, antes de añadir ningún hijo.
 * `fields` son los valores del mensaje, como en Python.
 */
export class RayitoCdkError extends Error {
  readonly code: RayitoCdkErrorCode;
  readonly fields: MessageFields;

  constructor(
    code: RayitoCdkErrorCode,
    fields: TemplateFields = {},
    options: { cause?: unknown } = {},
  ) {
    super(
      formatTemplate(RAYITO_CDK_ERROR_MESSAGES[code], fields),
      options.cause === undefined ? undefined : { cause: options.cause },
    );
    Object.setPrototypeOf(this, new.target.prototype);
    this.name = new.target.name;
    this.code = code;
    this.fields = messageFields(fields);
  }
}
