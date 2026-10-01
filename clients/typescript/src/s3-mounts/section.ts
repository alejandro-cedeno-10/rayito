/**
 * Adaptador puro `mounts` -> `ConfigureRequest.s3Mounts` (`m15-s3-mounts`):
 * sólo construye y lee mensajes de proto ya generados, nunca llama a
 * `@connectrpc/connect`. Espejo de `rayito._s3_mounts._section`.
 */

import { create } from "@bufbuild/protobuf";

import { MountError, UnimplementedError } from "../errors.js";
import type { ConfigureRequest } from "../gen/rayito/v1/configure_pb.js";
import {
  S3MountPhase,
  S3MountSchema,
  type S3MountsConfig,
  S3MountsConfigSchema,
  type S3MountsStatus,
} from "../gen/rayito/v1/s3_mounts_pb.js";
import { validateMountPaths } from "../mount-path.js";

import type { MountStatus, S3Mount } from "./domain.js";

export const SECTION_NAME = "s3_mounts";
export const REQUIRED_FLAG = "s3_mounts";

const KNOWN_ERROR_CLASSES = new Set([
  "network",
  "iam_denied",
  "not_found",
  "not_allowed",
  "helper_missing",
  "timeout",
]);

const PHASE_TO_STATE: Readonly<Record<S3MountPhase, MountStatus["state"]>> = {
  [S3MountPhase.UNSPECIFIED]: "pending",
  [S3MountPhase.PENDING]: "pending",
  [S3MountPhase.MOUNTED]: "mounted",
  [S3MountPhase.FAILED]: "failed",
};

/** Una sección de `mounts` lista para enviar en la siguiente `Configure`. */
export class S3MountsSection {
  readonly section = SECTION_NAME;
  readonly requiredFlag = REQUIRED_FLAG;

  constructor(readonly mounts: ReadonlyMap<string, S3Mount>) {}

  fill(request: ConfigureRequest): void {
    request.s3Mounts = toProto(this.mounts);
  }
}

/** `undefined` si `mounts` es `undefined`: ninguna sección, ninguna
 * llamada a `ConfigureSandbox`. Valida las rutas (`mount-path.ts`,
 * compartida con `volumes`) antes de construir nada. */
export function planS3Mounts(
  mounts: ReadonlyMap<string, S3Mount> | undefined,
): S3MountsSection | undefined {
  if (mounts === undefined) {
    return undefined;
  }
  validateMountPaths([...mounts.keys()]);
  return new S3MountsSection(new Map(mounts));
}

export function toProto(mounts: ReadonlyMap<string, S3Mount>): S3MountsConfig {
  return create(S3MountsConfigSchema, {
    mounts: [...mounts.entries()].map(([mountPath, mount]) =>
      create(S3MountSchema, {
        mountPath,
        bucket: mount.bucket,
        prefix: mount.prefix,
        readOnly: mount.readOnly,
        allowOverwrite: mount.allowOverwrite,
        allowDelete: mount.allowDelete,
      }),
    ),
  });
}

export function fromProtoStatus(status: S3MountsStatus): ReadonlyMap<string, MountStatus> {
  const result = new Map<string, MountStatus>();
  for (const state of status.mounts) {
    result.set(state.mountPath, {
      state: PHASE_TO_STATE[state.phase] ?? "pending",
      lastErrorClass: state.errorClass === "" ? undefined : state.errorClass,
    });
  }
  return result;
}

/** No lanza nada si la sección se aplicó o sigue asentándose (`PENDING`);
 * en otro caso, el error que explica por qué. */
export function checkSectionResult(codeName: string, errorClass: string): void {
  if (codeName === "SECTION_CODE_APPLIED" || codeName === "SECTION_CODE_PENDING") {
    return;
  }
  if (codeName === "SECTION_CODE_UNSUPPORTED") {
    throw new UnimplementedError(
      "mounts",
      "necesita una imagen 0.6.0 o posterior con el agente de m15-s3-mounts",
    );
  }
  throw new MountError(`mounts: la sección se rechazó (${codeName})`, {
    code: KNOWN_ERROR_CLASSES.has(errorClass) ? errorClass : "network",
  });
}
