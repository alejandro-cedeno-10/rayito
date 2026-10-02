/**
 * Derivación de `k_sbx` (M15, m15-events-webhooks): espejo exacto de
 * `rayd_core::lifecycle_events::DOMAIN_SEPARATOR` y de
 * `rayito._lifecycle_events._keys` (Python). El SDK es el único lado que ve
 * `stackKey`; `rayd` sólo recibe `k_sbx`, ya derivado, vía `ConfigureSandbox`.
 * Vectores compartidos en `testdata/lifecycle-events/mac-vectors.json`.
 */

import { createHmac } from "node:crypto";

export const DOMAIN_SEPARATOR = "rayito.events.v1|";

export function deriveSandboxKey(stackKey: Uint8Array, sandboxId: string): Buffer {
  return createHmac("sha256", stackKey).update(`${DOMAIN_SEPARATOR}${sandboxId}`, "utf8").digest();
}
