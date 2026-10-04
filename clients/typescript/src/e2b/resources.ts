/**
 * `Volume` y `getSignature` de E2B: cada estático que E2B define lanza
 * `UnimplementedError` con su motivo (nunca un `TypeError` por método
 * inexistente). `Template` vive en `./template.js` (m15-templates: ya
 * construye de verdad).
 */

import { unimplemented } from "./unimplemented.js";

/** Volúmenes compartidos de E2B: fuera de alcance (SPEC.md §4). */
export class Volume {
  private constructor() {
    throw unimplemented("Volume");
  }

  static create(..._args: unknown[]): never {
    throw unimplemented("Volume");
  }

  static connect(..._args: unknown[]): never {
    throw unimplemented("Volume");
  }

  static destroy(..._args: unknown[]): never {
    throw unimplemented("Volume");
  }

  static list(..._args: unknown[]): never {
    throw unimplemented("Volume");
  }

  static getInfo(..._args: unknown[]): never {
    throw unimplemented("Volume");
  }
}

/** La firma de URLs de envd de E2B: no autentica en el proxy de AWS. */
export function getSignature(..._args: unknown[]): never {
  throw unimplemented("getSignature");
}
