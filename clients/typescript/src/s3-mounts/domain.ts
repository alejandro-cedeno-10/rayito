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

/** Lo que el mensaje de `MountError` añade para las clases que se arreglan
 * fuera del código que llama a `create()`. Nunca nombra el bucket (tampoco
 * lo hace `rayd`, que sólo manda la clase). Mismo texto que
 * `MOUNT_ERROR_HINTS` en `_domain.py`. */
export const MOUNT_ERROR_HINTS: Readonly<Partial<Record<string, string>>> = {
  not_allowed:
    "el bucket no está en el allowlist de la imagen (RAYITO_ALLOWED_MOUNT_BUCKETS, " +
    "vacío o ausente deniega todos): publica la imagen con " +
    "`--env RAYITO_ALLOWED_MOUNT_BUCKETS=<bucket>` " +
    "(`make image-publish-caps MOUNT_BUCKETS=<bucket>`)",
};

/** Nunca confundir con una clase real del agente (nunca aparece en
 * `MOUNT_ERROR_CLASSES`): usada en vez de adivinar una clase real cuando el
 * agente manda una que este SDK no reconoce todavía — ocultar eso detrás
 * de, por ejemplo, `"network"` enmascararía una deriva de protocolo en vez
 * de hacerla visible. */
export const UNKNOWN_ERROR_CLASS = "unknown";

/** La clase que `create()` informa cuando un montaje sigue `"pending"` al
 * agotar su espera (`section.ts`'s `MOUNT_SETTLE_TIMEOUT_MS`); la misma
 * cadena que el agente usa para su propio límite. */
export const TIMEOUT_ERROR_CLASS = "timeout";

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
 *
 * Coste y activación
 * -------------------
 * Activa: `Sandbox.create({ mounts: { "/mnt/data": new S3Mount({...}) } })`;
 *   construir un `S3Mount` no llama a AWS.
 * Recursos y llamadas AWS: ningún recurso nuevo; `mount-s3` hace las
 *   peticiones S3 normales (`GetObject`/`ListObjectsV2`, y
 *   `PutObject`/`DeleteObject` con `readOnly: false`) con el execution role
 *   del sandbox.
 * Coste aproximado: $0 propio de Rayito; las peticiones y el almacenamiento
 *   normales de S3 del bucket (us-east-1, consultado 2026-10-01); la pila
 *   `s3-mounts` es $0 (sólo IAM).
 * IAM: `RayitoS3MountAccess` (`rayito stack deploy s3-mounts`) en el
 *   execution role, y el bucket en `RAYITO_ALLOWED_MOUNT_BUCKETS` de la
 *   imagen.
 * Cómo apagarla: no pases `mounts`; `rayito stack destroy s3-mounts` quita
 *   la política (no borra objetos ni el bucket).
 * Ejemplo:
 *   await Sandbox.create({
 *     template: "rayito-base-caps", executionRoleArn,
 *     mounts: { "/mnt/data": new S3Mount({ bucket: "mi-bucket", prefix: "team7/" }) },
 *   });
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

/**
 * `mounts` de `Sandbox.create()`: un objeto literal
 * (`{ "/mnt/data": new S3Mount(...) }`, igual que `volumes`) o un `Map`.
 */
export type S3MountsOption = Readonly<Record<string, S3Mount>> | ReadonlyMap<string, S3Mount>;

/** Las entradas de `mounts` en orden de inserción, sea cual sea su forma. */
export function mountEntries(mounts: S3MountsOption): [string, S3Mount][] {
  return mounts instanceof Map ? [...mounts.entries()] : Object.entries(mounts);
}

/** Lo que `sbx.mounts[path]` expone. `lastErrorClass` es `undefined` salvo
 * cuando `state === "failed"`. */
export interface MountStatus {
  readonly state: MountPhase;
  readonly lastErrorClass?: string | undefined;
}
