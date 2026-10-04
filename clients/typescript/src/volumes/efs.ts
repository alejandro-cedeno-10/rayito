/**
 * Partes del adaptador de EFS compartidas por `store.ts` (`m15-efs-volumes`,
 * ADR-018, experimental). Espejo de `rayito._volumes._base`: cómo se
 * construyen los parámetros de `CreateAccessPointCommand`, cómo se parsea
 * un `AccessPointDescription` de vuelta a `EfsVolume`, y la tabla de
 * errores de EFS (AWS_API_NOTES.md §22).
 */

import { createHash } from "node:crypto";
import type { AwsClientSettings } from "../aws/control-plane.js";
import { awsCode, LazyAwsApi, loadOptionalSdkClient } from "../aws/optional-client.js";
import { sanitizeAwsError } from "../aws/sanitize.js";
import { RateLimitError, VolumeError, VolumeNotFoundError } from "../errors.js";
import {
  EfsVolume,
  ROOT_DIRECTORY_PERMISSIONS,
  ROOT_DIRECTORY_PREFIX,
  VOLUME_POSIX_GID,
  VOLUME_POSIX_UID,
  VOLUME_TAG_KEY,
} from "./domain.js";

export const EFS_PEER = "@aws-sdk/client-efs";
/** `ClientToken` acepta hasta 64 caracteres ASCII (modelo `efs`). */
const CLIENT_TOKEN_LENGTH = 64;
/** `ClientToken` repetido (mismo `FileSystemId`+nombre): EFS responde 409
 * `AccessPointAlreadyExists`, nunca el access point ya creado
 * (`VolumeStore.create` lo atrapa y hace `get(name)` en su lugar). */
export const ACCESS_POINT_ALREADY_EXISTS = "AccessPointAlreadyExists";
/** `DescribeAccessPoints({FileSystemId})` es eventualmente consistente
 * (AWS_API_NOTES.md §16 Q125, medido 2026-10-02 en tres ciclos
 * crear/borrar): un access point recién creado tardó hasta 11 s en aparecer
 * en el listado y uno recién borrado siguió listado como `available` hasta
 * 8 s. `create()` de un nombre que ya existe sólo puede resolver su access
 * point por ese listado, así que reintenta `get` durante este presupuesto
 * (casi 3 veces el peor caso medido). Espejo de
 * `LIST_VISIBILITY_BUDGET_SECONDS` de `rayito._volumes._base`. */
export const LIST_VISIBILITY_BUDGET_MS = 30_000;
/** Pausa entre dos `DescribeAccessPoints` de ese reintento: el listado se
 * puso al día en saltos de 1-10 s (Q125). */
export const LIST_VISIBILITY_POLL_MS = 1_000;

export type Credentials = AwsClientSettings["credentials"];

export interface DescribedAccessPoint {
  readonly AccessPointId?: string | undefined;
  readonly FileSystemId?: string | undefined;
  readonly Tags?: Array<{ Key?: string | undefined; Value?: string | undefined }> | undefined;
}

/** Lo que Rayito usa de EFS (y sólo esto: AWS_API_NOTES.md §22). */
export interface EfsApi {
  createAccessPoint(input: {
    ClientToken: string;
    FileSystemId: string;
    PosixUser: { Uid: number; Gid: number };
    RootDirectory: {
      Path: string;
      CreationInfo: { OwnerUid: number; OwnerGid: number; Permissions: string };
    };
    Tags: Array<{ Key: string; Value: string }>;
  }): Promise<DescribedAccessPoint>;
  describeAccessPoints(input: { FileSystemId: string; NextToken?: string }): Promise<{
    AccessPoints?: DescribedAccessPoint[] | undefined;
    NextToken?: string | undefined;
  }>;
  deleteAccessPoint(input: { AccessPointId: string }): Promise<unknown>;
}

interface EfsModule {
  readonly EFSClient: new (config: object) => { send(command: unknown): Promise<unknown> };
  readonly CreateAccessPointCommand: new (input: object) => unknown;
  readonly DescribeAccessPointsCommand: new (input: object) => unknown;
  readonly DeleteAccessPointCommand: new (input: object) => unknown;
}

/** El adaptador real: carga el peer opcional en el primer uso. */
export async function efsApi(region: string, credentials: Credentials): Promise<EfsApi> {
  const { sdk, send } = await loadOptionalSdkClient<EfsModule>(
    EFS_PEER,
    "EFS (VolumeStore / volumes, experimental)",
    (module) => module.EFSClient,
    region,
    credentials,
  );
  return {
    createAccessPoint: (input) => send(new sdk.CreateAccessPointCommand(input)),
    describeAccessPoints: (input) => send(new sdk.DescribeAccessPointsCommand(input)),
    deleteAccessPoint: (input) => send(new sdk.DeleteAccessPointCommand(input)),
  };
}

export function newLazyEfsApi(
  region: string | undefined,
  credentials: Credentials,
  preset: EfsApi | undefined,
): LazyAwsApi<EfsApi> {
  return new LazyAwsApi(
    region,
    "falta la región: pasa `region` o define AWS_REGION",
    (resolvedRegion) => efsApi(resolvedRegion, credentials),
    preset,
  );
}

const IAM_ACTIONS: Readonly<Record<keyof EfsApi, string>> = Object.freeze({
  createAccessPoint: "elasticfilesystem:CreateAccessPoint",
  describeAccessPoints: "elasticfilesystem:DescribeAccessPoints",
  deleteAccessPoint: "elasticfilesystem:DeleteAccessPoint",
});

/** Hash estable de `fileSystemId`+nombre lógico: scoped al sistema de
 * ficheros (research doc §4.5) para que el mismo nombre en dos sistemas de
 * ficheros del mismo llamante nunca comparta token. */
export function clientToken(fileSystemId: string, name: string): string {
  return createHash("sha256")
    .update(`${fileSystemId}:${name}`, "utf8")
    .digest("hex")
    .slice(0, CLIENT_TOKEN_LENGTH);
}

export function rootDirectoryPath(name: string): string {
  return `${ROOT_DIRECTORY_PREFIX}/${name}`;
}

export function createAccessPointParams(
  fileSystemId: string,
  name: string,
): Parameters<EfsApi["createAccessPoint"]>[0] {
  return {
    ClientToken: clientToken(fileSystemId, name),
    FileSystemId: fileSystemId,
    PosixUser: { Uid: VOLUME_POSIX_UID, Gid: VOLUME_POSIX_GID },
    RootDirectory: {
      Path: rootDirectoryPath(name),
      CreationInfo: {
        OwnerUid: VOLUME_POSIX_UID,
        OwnerGid: VOLUME_POSIX_GID,
        Permissions: ROOT_DIRECTORY_PERMISSIONS,
      },
    },
    Tags: [{ Key: VOLUME_TAG_KEY, Value: name }],
  };
}

export function volumeNameFromTags(tags: DescribedAccessPoint["Tags"]): string | undefined {
  for (const tag of tags ?? []) {
    if (tag.Key === VOLUME_TAG_KEY && tag.Value) {
      return tag.Value;
    }
  }
  return undefined;
}

export function volumeFromDescription(
  described: DescribedAccessPoint,
  region: string | undefined,
): EfsVolume {
  return new EfsVolume({
    fileSystemId: described.FileSystemId ?? "",
    accessPointId: described.AccessPointId ?? "",
    name: volumeNameFromTags(described.Tags),
    region,
  });
}

/** El error del SDK de AWS como error propio de Rayito, sin el mensaje de AWS. */
export function translateError(operation: keyof EfsApi, error: unknown): Error {
  const code = awsCode(error);
  const cause = sanitizeAwsError(error, { includeMessage: false });
  const options = { awsCode: code, cause };
  switch (code) {
    case "AccessPointNotFound":
    case "FileSystemNotFound":
      return new VolumeNotFoundError("el access point o el sistema de ficheros no existe", options);
    case "ThrottlingException":
      return new RateLimitError(`EFS limitó la tasa de ${IAM_ACTIONS[operation]}`, options);
    case "AccessDeniedException":
      return new VolumeError(
        `sin permiso IAM ${IAM_ACTIONS[operation]} sobre el sistema de ficheros (credenciales del llamante)`,
        options,
      );
    case ACCESS_POINT_ALREADY_EXISTS:
      return new VolumeError("ya existe un access point con ese nombre", options);
    case "AccessPointLimitExceeded":
      return new VolumeError(
        "se alcanzó el límite de access points del sistema de ficheros",
        options,
      );
    case "IncorrectFileSystemLifeCycleState":
      return new VolumeError("el sistema de ficheros no está en estado 'available'", options);
    default:
      return new VolumeError(
        `EFS falló en ${IAM_ACTIONS[operation]} (${code ?? "error"})`,
        options,
      );
  }
}
