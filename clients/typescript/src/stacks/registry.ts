/**
 * Catálogo estático de componentes `OptionalStack` (M15 foundations,
 * ADR-016). Espejo de `rayito._stacks._registry`. Foundations escribe este
 * array una sola vez; cada feature sustituye sólo el `COMPONENT` de su
 * propio módulo en `components/`, nunca este array.
 */

import { COMPONENT as customDomain } from "./components/custom-domain.js";
import { COMPONENT as efsVolumes } from "./components/efs-volumes.js";
import { COMPONENT as eventsWebhooks } from "./components/events-webhooks.js";
import { COMPONENT as metadataIndex } from "./components/metadata-index.js";
import { COMPONENT as otlpExport } from "./components/otlp-export.js";
import { COMPONENT as s3Mounts } from "./components/s3-mounts.js";
import { COMPONENT as secretsAccess } from "./components/secrets-access.js";
import { COMPONENT as sizesGuard } from "./components/sizes-guard.js";
import { COMPONENT as templates } from "./components/templates.js";
import type { StackComponent } from "./model.js";

export const COMPONENTS: readonly StackComponent[] = [
  metadataIndex,
  secretsAccess,
  efsVolumes,
  s3Mounts,
  sizesGuard,
  eventsWebhooks,
  otlpExport,
  templates,
  customDomain,
];

export function componentByName(name: string): StackComponent | undefined {
  return COMPONENTS.find((component) => component.name === name);
}
