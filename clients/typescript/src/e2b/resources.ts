/**
 * `Template`, `Volume`, `Secret` y `getSignature` de E2B: cada estático que
 * E2B define lanza `UnimplementedError` con su motivo (nunca un
 * `TypeError` por método inexistente). Lanzan en el acto, también los que en
 * E2B son asíncronos: un `await Template.build()` lo recibe igual.
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

/** El almacén de secretos de E2B: necesita un plano de control y un inyector de egress. */
export class Secret {
  private constructor() {
    throw unimplemented("Secret");
  }

  static create(..._args: unknown[]): never {
    throw unimplemented("Secret");
  }

  static update(..._args: unknown[]): never {
    throw unimplemented("Secret");
  }

  static getInfo(..._args: unknown[]): never {
    throw unimplemented("Secret");
  }

  static list(..._args: unknown[]): never {
    throw unimplemented("Secret");
  }

  static exists(..._args: unknown[]): never {
    throw unimplemented("Secret");
  }

  static destroy(..._args: unknown[]): never {
    throw unimplemented("Secret");
  }

  static fill(..._args: unknown[]): never {
    throw unimplemented("Secret");
  }

  static iamToken(..._args: unknown[]): never {
    throw unimplemented("Secret");
  }
}

/** La firma de URLs de envd de E2B: no autentica en el proxy de AWS. */
export function getSignature(..._args: unknown[]): never {
  throw unimplemented("getSignature");
}
