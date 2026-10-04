/**
 * Dominio puro de `volumes` (`m15-efs-volumes`, ADR-018, experimental).
 * Espejo de `rayito._volumes._domain`. Sin SDK de AWS: las llamadas a EFS
 * viven en `efs.ts`/`store.ts`.
 */

import { InvalidArgumentError } from "../errors.js";

/** `fs-[0-9a-f]{8,40}` (research doc R3; AWS_API_NOTES.md §22). */
const FILE_SYSTEM_ID_PATTERN = /^fs-[0-9a-f]{8,40}$/;
/** `fsap-[0-9a-f]{8,40}` (research doc R3; AWS_API_NOTES.md §22). */
const ACCESS_POINT_ID_PATTERN = /^fsap-[0-9a-f]{8,40}$/;
/** Letras, números y guiones (research doc §2, "Volume.create(name)"), como
 * mucho lo que cabe en un componente de `RootDirectory.Path` (100 caracteres,
 * modelo `efs`). */
const VOLUME_NAME_PATTERN = /^[A-Za-z0-9-]{1,100}$/;

/** Prefijo bajo el que vive todo volumen (research doc §4.5):
 * `RootDirectory.Path = \`${ROOT_DIRECTORY_PREFIX}/${name}\`` . */
export const ROOT_DIRECTORY_PREFIX = "/rayito-volumes";
/** Usuario POSIX que el access point fuerza para todo acceso (research doc
 * §4.1 regla 5: root squash, `ClientRootAccess` nunca se concede). */
export const VOLUME_POSIX_UID = 1000;
export const VOLUME_POSIX_GID = 1000;
/** Permisos octales de `CreationInfo` para un directorio raíz nuevo. */
export const ROOT_DIRECTORY_PERMISSIONS = "0750";
/** Etiqueta que identifica el access point como un volumen de Rayito. */
export const VOLUME_TAG_KEY = "rayito:volume";

export function validateVolumeName(name: string): string {
  if (!VOLUME_NAME_PATTERN.test(name)) {
    throw new InvalidArgumentError(
      "el nombre de un volumen EFS es letras, números y guiones, de 1 a 100 caracteres",
    );
  }
  return name;
}

/** Nunca repite `value` en el mensaje (§6 "los errores nunca repiten …
 * identificadores de sistema de ficheros"): sólo la forma esperada. */
export function validateFileSystemId(value: string): string {
  if (!FILE_SYSTEM_ID_PATTERN.test(value)) {
    throw new InvalidArgumentError("fileSystemId inválido: se esperaba 'fs-' y 8-40 hex");
  }
  return value;
}

/** Nunca repite `value` en el mensaje (§6, igual que `validateFileSystemId`). */
export function validateAccessPointId(value: string): string {
  if (!ACCESS_POINT_ID_PATTERN.test(value)) {
    throw new InvalidArgumentError("accessPointId inválido: se esperaba 'fsap-' y 8-40 hex");
  }
  return value;
}

/** Mirrors `rayd_core::volume::MountState`; ningún build 0.6 de `rayd`
 * reporta otra cosa que no sea ausente (`UnavailableEfsMounter`). */
export type MountState =
  | "requested"
  | "mounting"
  | "mounted"
  | "degraded"
  | "remounting"
  | "unmounted"
  | "failed";

export interface EfsVolumeOptions {
  readonly fileSystemId: string;
  readonly accessPointId: string;
  readonly name?: string | undefined;
  readonly region?: string | undefined;
  readonly readOnly?: boolean;
  readonly mountTargetIp?: string | undefined;
}

/**
 * Un volumen EFS: el access point que un sandbox puede montar bajo
 * `volumes: {path: new EfsVolume({...})}`. Validado en construcción, sin
 * ninguna llamada a AWS.
 */
export class EfsVolume {
  readonly fileSystemId: string;
  readonly accessPointId: string;
  readonly name: string | undefined;
  readonly region: string | undefined;
  readonly readOnly: boolean;
  readonly mountTargetIp: string | undefined;

  constructor(options: EfsVolumeOptions) {
    this.fileSystemId = validateFileSystemId(options.fileSystemId);
    this.accessPointId = validateAccessPointId(options.accessPointId);
    this.name = options.name === undefined ? undefined : validateVolumeName(options.name);
    this.region = options.region;
    this.readOnly = options.readOnly ?? false;
    this.mountTargetIp = options.mountTargetIp;
  }
}

/**
 * Lo que `sbx.volumes[path]` reporta tras `/run`; en 0.6 ningún build de
 * `rayd` llega a `"mounted"` (ver `requireVolumeSupport` en `section.ts`).
 */
export interface VolumeStatus {
  readonly state: MountState;
  readonly lastErrorClass: string | undefined;
}
