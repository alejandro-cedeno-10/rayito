/**
 * `Template` y `getSignature` de E2B: cada estático que E2B define lanza
 * `UnimplementedError` con su motivo (nunca un `TypeError` por método
 * inexistente). Lanzan en el acto, también los que en E2B son asíncronos:
 * un `await Template.build()` lo recibe igual. `Volume` vive en
 * `volume.ts` (m15-efs-volumes, experimental): tiene una implementación
 * real una vez `new E2B({ volumeStore })` lo configura.
 */

import { unimplemented } from "./unimplemented.js";

/** Plantillas declarativas de E2B: fuera de alcance (SPEC.md §4). */
export class Template {
  private constructor() {
    throw unimplemented("Template");
  }

  static build(..._args: unknown[]): never {
    throw unimplemented("Template");
  }

  static buildInBackground(..._args: unknown[]): never {
    throw unimplemented("Template");
  }

  static getBuildStatus(..._args: unknown[]): never {
    throw unimplemented("Template");
  }

  static exists(..._args: unknown[]): never {
    throw unimplemented("Template");
  }

  static aliasExists(..._args: unknown[]): never {
    throw unimplemented("Template");
  }

  static assignTags(..._args: unknown[]): never {
    throw unimplemented("Template");
  }

  static removeTags(..._args: unknown[]): never {
    throw unimplemented("Template");
  }

  static getTags(..._args: unknown[]): never {
    throw unimplemented("Template");
  }

  static toJSON(..._args: unknown[]): never {
    throw unimplemented("Template");
  }

  static toDockerfile(..._args: unknown[]): never {
    throw unimplemented("Template");
  }
}

// `Volume` moved to `volume.ts` (m15-efs-volumes, experimental): it has a
// real implementation once `new E2B({ volumeStore })` configures one.

/** La firma de URLs de envd de E2B: no autentica en el proxy de AWS. */
export function getSignature(..._args: unknown[]): never {
  throw unimplemented("getSignature");
}
