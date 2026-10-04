/**
 * Lectura de las plantillas y artefactos empaquetados con el SDK (M15
 * foundations, ADR-016). Espejo de `rayito._stacks._packaging`.
 * `scripts/gen_stack_assets.py` renderiza `templates/<component>.gen.ts`
 * de forma determinista desde `infra/<component>.yaml`; en tiempo de
 * ejecución este módulo sólo las lee. Cada feature añade la importación de
 * su propio `<component>.gen.ts` a este mapa en su propio cambio.
 */

import type { StackComponent } from "./model.js";
import * as efsVolumes from "./templates/efs-volumes.gen.js";
import * as eventsWebhooks from "./templates/events-webhooks.gen.js";
import * as metadataIndex from "./templates/metadata-index.gen.js";
import * as otlpExport from "./templates/otlp-export.gen.js";
import * as s3Mounts from "./templates/s3-mounts.gen.js";
import * as secretsAccess from "./templates/secrets-access.gen.js";
import * as sizesGuard from "./templates/sizes-guard.gen.js";
import * as templates from "./templates/templates.gen.js";

interface GeneratedAsset {
  readonly TEMPLATE_BODY: string;
  readonly ARTIFACT_BASE64: string | undefined;
}

const ASSETS: Readonly<Record<string, GeneratedAsset>> = {
  "efs-volumes": efsVolumes,
  "events-webhooks": eventsWebhooks,
  "metadata-index": metadataIndex,
  "otlp-export": otlpExport,
  "s3-mounts": s3Mounts,
  "secrets-access": secretsAccess,
  "sizes-guard": sizesGuard,
  templates,
};

function assetFor(component: StackComponent): GeneratedAsset {
  const asset = ASSETS[component.name];
  if (asset === undefined) {
    throw new Error(
      `sin plantilla empaquetada para el componente ${JSON.stringify(component.name)}`,
    );
  }
  return asset;
}

export async function loadTemplate(component: StackComponent): Promise<string> {
  return assetFor(component).TEMPLATE_BODY;
}

/**
 * Dentro de `rayito/`, el espacio de nombres que las plantillas de IAM ya
 * protegen (sólo el publicador escribe ahí, el execution role tiene un
 * `Deny` explícito y el builder de templates sólo lee); nunca la raíz del
 * bucket. Espejo de `STACK_ARTIFACT_PREFIX` de `_packaging.py`.
 */
export const STACK_ARTIFACT_PREFIX = "rayito/stacks";

/**
 * La clave S3 determinista de un artefacto,
 * `rayito/stacks/<componente>/<sha256>.zip`: subir el mismo contenido dos
 * veces es un no-op (`putArtifact` compara el contenido ya subido antes).
 */
export async function artifactKey(component: StackComponent, data: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", data);
  const hex = [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
  return `${STACK_ARTIFACT_PREFIX}/${component.name}/${hex}.zip`;
}

export async function loadArtifact(component: StackComponent): Promise<Uint8Array> {
  const base64 = assetFor(component).ARTIFACT_BASE64;
  if (base64 === undefined) {
    throw new Error(`el componente ${JSON.stringify(component.name)} no tiene artefacto Lambda`);
  }
  return Buffer.from(base64, "base64");
}
