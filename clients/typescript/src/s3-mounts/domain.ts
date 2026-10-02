/**
 * Dominio puro de `mounts` (`m15-s3-mounts`, ADR-017). Espejo de
 * `rayito._s3_mounts._domain`. Nada aquí importa `@connectrpc/connect` ni
 * un cliente AWS; la traducción a `ConfigureRequest` vive en `section.ts`.
 */

import { InvalidArgumentError } from "../errors.js";

export type MountPhase = "pending" | "mounted" | "failed";

/** Cadenas cerradas; espejo exacto de `rayd_core::s3_mount::MountErrorClass`
 * y de `S3MountState.errorClass` en el proto. */
export const MOUNT_ERROR_CLASSES = [
  "network",
  "iam_denied",
  "not_found",
  "not_allowed",
  "invalid_path",
  "helper_missing",
  "timeout",
] as const;

/** Nunca confundir con una clase real del agente (nunca aparece en
 * `MOUNT_ERROR_CLASSES`): usada en vez de adivinar una clase real cuando el
 * agente manda una que este SDK no reconoce todavía — ocultar eso detrás
 * de, por ejemplo, `"network"` enmascararía una deriva de protocolo en vez
 * de hacerla visible. */
export const UNKNOWN_ERROR_CLASS = "unknown";

export interface S3MountOptions {
  readonly bucket: string;
  readonly prefix?: string;
  readonly readOnly?: boolean;
  readonly allowOverwrite?: boolean;
  readonly allowDelete?: boolean;
}

/**
 * Un bucket (o un prefijo suyo) montado en el guest por `rayd` vía
 * `mount-s3`/FUSE. Sólo tiene efecto sobre `rayito-base-caps` (o una
 * variante derivada por tamaño). `readOnly` es `true` por defecto
 * (mínimo privilegio): `allowOverwrite`/`allowDelete` sólo se aceptan con
 * `readOnly: false`.
 */
export class S3Mount {
  readonly bucket: string;
  readonly prefix: string;
  readonly readOnly: boolean;
  readonly allowOverwrite: boolean;
  readonly allowDelete: boolean;

  constructor(options: S3MountOptions) {
    if (!options.bucket) {
      throw new InvalidArgumentError("S3Mount.bucket no puede estar vacío");
    }
    const prefix = options.prefix ?? "";
    if (prefix.startsWith("/")) {
      throw new InvalidArgumentError(
        `S3Mount.prefix es relativo al bucket, sin '/' inicial: ${JSON.stringify(prefix)}`,
      );
    }
    const readOnly = options.readOnly ?? true;
    const allowOverwrite = options.allowOverwrite ?? false;
    const allowDelete = options.allowDelete ?? false;
    if (readOnly && (allowOverwrite || allowDelete)) {
      throw new InvalidArgumentError("S3Mount: allowOverwrite/allowDelete exigen readOnly: false");
    }
    this.bucket = options.bucket;
    this.prefix = prefix;
    this.readOnly = readOnly;
    this.allowOverwrite = allowOverwrite;
    this.allowDelete = allowDelete;
  }
}

/** Lo que `sbx.mounts[path]` expone. `lastErrorClass` es `undefined` salvo
 * cuando `state === "failed"`. */
export interface MountStatus {
  readonly state: MountPhase;
  readonly lastErrorClass?: string | undefined;
}
