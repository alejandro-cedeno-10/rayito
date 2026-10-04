/**
 * `getSignature` de E2B: lanza `UnimplementedError` con su motivo.
 * `Template` vive en `./template.js` (m15-templates: ya construye de verdad)
 * y `Volume` en `./volume.js` (m15-efs-volumes, experimental: CRUD real una
 * vez `new E2B({ volumeStore })` lo configura).
 */

import { unimplemented } from "./unimplemented.js";

/** La firma de URLs de envd de E2B: no autentica en el proxy de AWS. */
export function getSignature(..._args: unknown[]): never {
  throw unimplemented("getSignature");
}
