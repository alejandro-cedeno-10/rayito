/**
 * Adaptador puro `mounts` -> `ConfigureRequest.s3Mounts` (`m15-s3-mounts`):
 * sólo construye y lee mensajes de proto ya generados, nunca llama a
 * `@connectrpc/connect`. Espejo de `rayito._s3_mounts._section`.
 */

import { create } from "@bufbuild/protobuf";

import type { ConfigureSection } from "../configure/base.js";
import { MountError, UnimplementedError } from "../errors.js";
import type { ConfigureRequest, ConfigureStatusResponse } from "../gen/rayito/v1/configure_pb.js";
import { SectionCode } from "../gen/rayito/v1/configure_pb.js";
import {
  S3MountPhase,
  S3MountSchema,
  type S3MountsConfig,
  S3MountsConfigSchema,
  type S3MountsStatus,
  S3MountsStatusSchema,
} from "../gen/rayito/v1/s3_mounts_pb.js";
import { validateMountPaths } from "../mount-path.js";
import type { MountStatus, S3Mount, S3MountsOption } from "./domain.js";
import {
  MOUNT_ERROR_CLASSES,
  MOUNT_ERROR_HINTS,
  mountEntries,
  TIMEOUT_ERROR_CLASS,
  UNKNOWN_ERROR_CLASS,
} from "./domain.js";

export const SECTION_NAME = "s3_mounts";
/** Nombre del campo en `AgentFeatures` (camelCase, a diferencia de
 * `SECTION_NAME`: el generador de TypeScript renombra los campos del
 * proto, el de Python no). */
export const REQUIRED_FLAG = "s3Mounts";

/** Cuánto espera `create()` a que cada montaje pase de `"pending"` a
 * `"mounted"`/`"failed"`. `rayd` ya acota attach + arranque + primera
 * respuesta de cada montaje a 10 s (`MOUNT_READY_TIMEOUT`,
 * `crates/rayd/src/features/s3_mounts.rs`) y lo marca `failed`/`timeout` él
 * mismo; 5 s más cubren la planificación de su tarea y el último sondeo,
 * para que el SDK informe del veredicto del agente en vez de adivinarlo.
 * Espejo de `MOUNT_SETTLE_TIMEOUT_S` de Python. */
export const MOUNT_SETTLE_TIMEOUT_MS = 15_000;

const KNOWN_ERROR_CLASSES: ReadonlySet<string> = new Set(MOUNT_ERROR_CLASSES);

const PHASE_TO_STATE: Readonly<Record<S3MountPhase, MountStatus["state"]>> = {
  [S3MountPhase.UNSPECIFIED]: "pending",
  [S3MountPhase.PENDING]: "pending",
  [S3MountPhase.MOUNTED]: "mounted",
  [S3MountPhase.FAILED]: "failed",
};

/** Una sección de `mounts` lista para enviar en la siguiente `Configure`. */
export class S3MountsSection implements ConfigureSection {
  readonly section = SECTION_NAME;
  readonly requiredFlag = REQUIRED_FLAG;

  constructor(readonly mounts: ReadonlyMap<string, S3Mount>) {}

  fill(request: ConfigureRequest): void {
    request.s3Mounts = toProto(this.mounts);
  }

  readonly settleTimeoutMs = MOUNT_SETTLE_TIMEOUT_MS;

  checkResult(code: number, errorClass: string): void {
    checkSectionResult(code, errorClass);
  }

  checkStatus(status: ConfigureStatusResponse, final: boolean): boolean {
    const states = fromProtoStatus(status.s3Mounts ?? create(S3MountsStatusSchema, {}));
    return checkMountsSettled(states, this.mounts, final);
  }
}

/** `undefined` si `mounts` es `undefined`: ninguna sección, ninguna
 * llamada a `ConfigureSandbox`. Valida las rutas (`mount-path.ts`,
 * compartida con `volumes`) antes de construir nada. */
export function planS3Mounts(mounts: S3MountsOption | undefined): S3MountsSection | undefined {
  if (mounts === undefined) {
    return undefined;
  }
  const entries = mountEntries(mounts);
  validateMountPaths(entries.map(([path]) => path));
  return new S3MountsSection(new Map(entries));
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

/** No lanza nada si la sección se aplicó o sigue asentándose (`PENDING`:
 * `create()` sondea entonces `ConfigureStatus` hasta que cada montaje se
 * asiente, `checkMountsSettled`);
 * en otro caso, el error que explica por qué. `code` es el entero de
 * `SectionCode` tal cual lo manda `rayd`, comparado contra las constantes
 * generadas (nunca una cadena comparada a mano). */
export function checkSectionResult(code: SectionCode, errorClass: string): void {
  if (code === SectionCode.APPLIED || code === SectionCode.PENDING) {
    return;
  }
  if (code === SectionCode.UNSUPPORTED) {
    throw new UnimplementedError(
      "mounts",
      "necesita una imagen 0.6.0 o posterior con el agente de m15-s3-mounts",
    );
  }
  const mountCode = KNOWN_ERROR_CLASSES.has(errorClass) ? errorClass : UNKNOWN_ERROR_CLASS;
  throw new MountError(
    withHint(`mounts: la sección se rechazó (${SectionCode[code]}, ${mountCode})`, mountCode),
    { code: mountCode },
  );
}

/** `message` más `MOUNT_ERROR_HINTS[mountCode]` si esa clase lo tiene. */
function withHint(message: string, mountCode: string): string {
  const hint = MOUNT_ERROR_HINTS[mountCode];
  return hint === undefined ? message : `${message}: ${hint}`;
}

/**
 * `true` si cada ruta de `wanted` ya está `"mounted"`. El primer montaje
 * `"failed"` lanza `MountError` con su `lastErrorClass` (así `create()`
 * termina el sandbox en vez de devolver uno con el montaje roto); con
 * `final`, uno que siga `"pending"` (o que el agente no reporte) lanza
 * `MountError` con `code: "timeout"`.
 */
export function checkMountsSettled(
  states: ReadonlyMap<string, MountStatus>,
  wanted: ReadonlyMap<string, S3Mount>,
  final: boolean,
): boolean {
  let settled = true;
  for (const path of wanted.keys()) {
    const state = states.get(path);
    if (state?.state === "failed") {
      const code = state.lastErrorClass ?? UNKNOWN_ERROR_CLASS;
      throw new MountError(withHint(`mounts: ${path} no se pudo montar (${code})`, code), {
        code: KNOWN_ERROR_CLASSES.has(code) ? code : UNKNOWN_ERROR_CLASS,
      });
    }
    if (state?.state !== "mounted") {
      settled = false;
    }
  }
  if (!settled && final) {
    throw new MountError(
      `mounts: algún montaje sigue sin asentarse tras ${MOUNT_SETTLE_TIMEOUT_MS / 1000} s`,
      { code: TIMEOUT_ERROR_CLASS },
    );
  }
  return settled;
}
