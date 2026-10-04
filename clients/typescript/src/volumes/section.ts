/**
 * `volumes` en `Sandbox.create()` (`m15-efs-volumes`, ADR-018,
 * experimental). Espejo de `rayito._volumes._section`: `planVolumes` valida
 * la petición antes de cualquier llamada a AWS (forma, tope, rutas, variante
 * de imagen, execution role y el conector de `egress`) y `EfsVolumesSection`
 * es la sección `efs_volumes` del único `Configure` de `create()`. `rayd`
 * monta cada volumen con `amazon-efs-utils` dentro de esa llamada, sólo en
 * una imagen que lo trae (`rayito image publish --with-efs`,
 * `rayito-base-caps-efs`); en cualquier otra `Health.features.efsVolumes` es
 * `false` y `create()` termina el sandbox con `UnimplementedError`.
 *
 * Un MicroVM admite un solo conector de egress (`AWS_API_NOTES.md` §16
 * Q131): un volumen necesita el de tu VPC, así que no se combina con
 * `INTERNET_EGRESS`.
 */

import { create } from "@bufbuild/protobuf";

import type { AgentFeatures, CapabilityGate, SlowApplySection } from "../configure/base.js";
import { InvalidArgumentError, UnimplementedError, VolumeMountError } from "../errors.js";
import type { ConfigureRequest, ConfigureStatusResponse } from "../gen/rayito/v1/configure_pb.js";
import { SectionCode } from "../gen/rayito/v1/configure_pb.js";
import {
  EfsVolumeMountSchema,
  EfsVolumeState,
  type EfsVolumesConfig,
  EfsVolumesConfigSchema,
  type EfsVolumesStatus,
} from "../gen/rayito/v1/efs_volumes_pb.js";
import { EFS_VOLUMES_MAX_PER_SANDBOX } from "../limits.js";
import { hasInternetConnector } from "../models.js";
import { validateMountPaths } from "../mount-path.js";
import { EFS_CAPS_VARIANT, requireCapsFor } from "../role-policy.js";
import { EfsVolume, type MountState, type VolumeStatus } from "./domain.js";

export const SECTION_NAME = "efs_volumes";
/** Nombre del campo en `AgentFeatures` (camelCase, como lo genera
 * protobuf-es). */
export const REQUIRED_FLAG = "efsVolumes";
/** La página de la función, enlazada desde cada `UnimplementedError`. */
export const VOLUMES_DOC = "docs/site/docs/funciones-opcionales/volumenes-efs.md";
/** La página que explica cómo dar internet a un sandbox con volumen. */
export const VPC_GUIDE = "funciones-opcionales/volumenes-efs-vpc.md";
/** La imagen opcional con `amazon-efs-utils` que publica
 * `rayito image publish --with-efs --os-capabilities ALL`. */
export const EFS_IMAGE_NAME = `rayito-${EFS_CAPS_VARIANT}`;
/** Cómo tiene internet un sandbox con volumen: saliendo por la VPC (Q131). */
export const INTERNET_THROUGH_VPC =
  "para tener internet además del volumen, sal por tu VPC: una NAT o un transit gateway " +
  "en las subredes del conector y un conector cuyo grupo de seguridad permita esa salida " +
  `(el de efs-volumes sólo deja salir NFS); ver ${VPC_GUIDE}`;
/** Por qué una imagen no monta volúmenes: no trae `amazon-efs-utils` (o no
 * tiene `CAP_SYS_ADMIN`), así que `rayd` responde `UNSUPPORTED`. */
export const IMAGE_REASON =
  "esta imagen no trae amazon-efs-utils (o no corre con additionalOsCapabilities ALL): " +
  `usa la imagen opcional ${EFS_IMAGE_NAME}, publicada con ` +
  "`rayito image publish --with-efs --os-capabilities ALL`";

/** Cuánto puede tardar `rayd` en cada `mount -t efs` antes de darlo por
 * fallido (`MOUNT_HELPER_TIMEOUT` de `crates/rayd/src/adapters/efs_mount.rs`,
 * 15 s; un montaje medido tarda p50 313 ms, p95 589 ms, AWS_API_NOTES.md §16
 * Q128). Espejo de `MOUNT_HELPER_TIMEOUT_SECONDS` de Python. */
export const MOUNT_HELPER_TIMEOUT_MS = 15_000;
/** Margen sobre los montajes en serie para el *bind mount*, la respuesta y
 * la red hasta el agente. Espejo de `VOLUME_APPLY_MARGIN_SECONDS`. */
export const VOLUME_APPLY_MARGIN_MS = 5_000;
/** Plazo mínimo de la llamada a `Configure` con volúmenes: `rayd` monta
 * cada uno, en serie y antes de responder, así que el peor caso son
 * `EFS_VOLUMES_MAX_PER_SANDBOX` agotando su plazo (65 s). Espejo de
 * `VOLUME_APPLY_TIMEOUT_SECONDS`. */
export const VOLUME_APPLY_TIMEOUT_MS =
  EFS_VOLUMES_MAX_PER_SANDBOX * MOUNT_HELPER_TIMEOUT_MS + VOLUME_APPLY_MARGIN_MS;
/** Si un agente futuro deja la sección `PENDING`, lo que `create()` espera a
 * que cada volumen llegue a `mounted` (el mismo peor caso). Espejo de
 * `VOLUME_SETTLE_TIMEOUT_SECONDS`. */
export const VOLUME_SETTLE_TIMEOUT_MS = VOLUME_APPLY_TIMEOUT_MS;

/** Los `code` posibles de `VolumeMountError`: los `last_error_class` de
 * `FAILED` que documenta `efs_volumes.proto`; cualquier otro llega como
 * `UNKNOWN_VOLUME_ERROR_CODE`. */
export const VOLUME_MOUNT_ERROR_CLASSES = [
  "network",
  "iam_denied",
  "not_found",
  "tls",
  "helper_missing",
  "timeout",
  "invalid_path",
] as const;
export const TIMEOUT_VOLUME_ERROR_CODE = "timeout";
export const UNKNOWN_VOLUME_ERROR_CODE = "unknown";
const KNOWN_ERROR_CODES: ReadonlySet<string> = new Set(VOLUME_MOUNT_ERROR_CLASSES);

function knownCode(errorClass: string | undefined): string {
  return errorClass !== undefined && KNOWN_ERROR_CODES.has(errorClass)
    ? errorClass
    : UNKNOWN_VOLUME_ERROR_CODE;
}

/**
 * Sin I/O: `egress` tiene que ser exactamente un conector propio (el
 * `ConnectorArn` de `infra/efs-volumes.yaml` o uno de tu VPC que llegue al
 * mount target). `InvalidArgumentError` si falta (heredaría
 * `INTERNET_EGRESS` de la imagen), si incluye `INTERNET_EGRESS` o si trae
 * más de uno. Espejo de `require_volume_connector`.
 */
export function requireVolumeConnector(
  egress: readonly string[] | undefined,
  feature = "volumes",
): void {
  const connectors = egress ?? [];
  if (connectors.length === 0) {
    throw new InvalidArgumentError(
      `${feature} necesita egress: [<ConnectorArn de efs-volumes>]: sin él el sandbox hereda ` +
        `INTERNET_EGRESS de la imagen y no llega al mount target; ${INTERNET_THROUGH_VPC}`,
    );
  }
  if (hasInternetConnector(connectors)) {
    throw new InvalidArgumentError(
      `${feature} no se combina con INTERNET_EGRESS: un MicroVM admite un solo conector de ` +
        `egress y el volumen necesita el de tu VPC; ${INTERNET_THROUGH_VPC}`,
    );
  }
  if (connectors.length > 1) {
    throw new InvalidArgumentError(
      `${feature} admite un solo conector en egress (un MicroVM sólo acepta uno); ` +
        INTERNET_THROUGH_VPC,
    );
  }
}

/** Lo que `planVolumes` necesita de `create()` además de `volumes`. */
export interface PlanVolumesOptions {
  readonly imageVariant?: string | undefined;
  readonly egress?: readonly string[] | undefined;
  readonly executionRoleArn?: string | undefined;
}

/** `volumes` ya validado, a la espera de resolver las IPs de mount target
 * (`prepareFeatures`). */
export interface VolumesRequest {
  readonly volumes: ReadonlyMap<string, EfsVolume>;
}

/**
 * Valida `volumes` por completo, sin I/O. El orden importa (forma, tope,
 * rutas, caps, conector y execution role) para que el primer error que vea
 * el llamante sea siempre el que puede corregir. Espejo de `plan_volumes`.
 */
export function planVolumes(
  volumes: Readonly<Record<string, unknown>>,
  options: PlanVolumesOptions = {},
): VolumesRequest {
  const entries = Object.entries(volumes);
  if (entries.length === 0) {
    throw new InvalidArgumentError("volumes no admite un objeto vacío; omite la opción");
  }
  if (entries.length > EFS_VOLUMES_MAX_PER_SANDBOX) {
    throw new InvalidArgumentError(
      `volumes admite como mucho ${EFS_VOLUMES_MAX_PER_SANDBOX} volúmenes por sandbox`,
    );
  }
  const validated = new Map<string, EfsVolume>();
  for (const [path, value] of entries) {
    if (!(value instanceof EfsVolume)) {
      throw new InvalidArgumentError("volumes espera valores EfsVolume");
    }
    validated.set(path, value);
  }
  validateMountPaths([...validated.keys()]);
  requireCapsFor("volumes", options.imageVariant);
  requireVolumeConnector(options.egress);
  if (!options.executionRoleArn) {
    throw new InvalidArgumentError(
      "volumes necesita executionRoleArn: efs-utils firma el túnel TLS con las credenciales " +
        "del execution role (IMDS)",
    );
  }
  return { volumes: validated };
}

export function toProto(volumes: ReadonlyMap<string, EfsVolume>): EfsVolumesConfig {
  return create(EfsVolumesConfigSchema, {
    mounts: [...volumes.entries()].map(([mountPath, volume]) =>
      create(EfsVolumeMountSchema, {
        mountPath,
        fileSystemId: volume.fileSystemId,
        accessPointId: volume.accessPointId,
        readOnly: volume.readOnly,
        mountTargetIp: volume.mountTargetIp ?? "",
      }),
    ),
  });
}

const STATE_NAMES: Readonly<Record<EfsVolumeState, MountState>> = {
  [EfsVolumeState.UNSPECIFIED]: "requested",
  [EfsVolumeState.REQUESTED]: "requested",
  [EfsVolumeState.MOUNTING]: "mounting",
  [EfsVolumeState.MOUNTED]: "mounted",
  [EfsVolumeState.DEGRADED]: "degraded",
  [EfsVolumeState.REMOUNTING]: "remounting",
  [EfsVolumeState.UNMOUNTED]: "unmounted",
  [EfsVolumeState.FAILED]: "failed",
};

/** El estado de cada ruta de `status` (`sbx.volumes()`); un estado que este
 * SDK no conoce se lee como `"requested"`. Espejo de `from_proto_status`. */
export function fromProtoStatus(
  status: EfsVolumesStatus | undefined,
): ReadonlyMap<string, VolumeStatus> {
  const result = new Map<string, VolumeStatus>();
  for (const state of status?.volumes ?? []) {
    result.set(state.mountPath, {
      state: STATE_NAMES[state.state] ?? "requested",
      lastErrorClass: state.lastErrorClass === "" ? undefined : state.lastErrorClass,
    });
  }
  return result;
}

/**
 * La sección `efs_volumes` del `Configure` de `create()`, con cada IP de
 * mount target ya resuelta. `rayd` monta dentro de la llamada (de ahí
 * `applyTimeoutMs`) y responde `APPLIED` con todo montado o `FAILED` con la
 * clase del primer fallo; `create()` termina entonces el sandbox (salvo
 * `keepOnFailure`). Espejo de `EfsVolumesSection`.
 */
export class EfsVolumesSection implements SlowApplySection, CapabilityGate {
  readonly section = SECTION_NAME;
  readonly requiredFlag = REQUIRED_FLAG;
  readonly settleTimeoutMs = VOLUME_SETTLE_TIMEOUT_MS;
  readonly applyTimeoutMs = VOLUME_APPLY_TIMEOUT_MS;

  constructor(readonly volumes: ReadonlyMap<string, EfsVolume>) {}

  fill(request: ConfigureRequest): void {
    request.efsVolumes = toProto(this.volumes);
  }

  requireSupport(features: AgentFeatures): void {
    if (!features.efsVolumes) {
      throw new UnimplementedError("volumes", IMAGE_REASON, VOLUMES_DOC);
    }
  }

  checkResult(code: number, errorClass: string): void {
    if (code === SectionCode.APPLIED || code === SectionCode.PENDING) {
      return;
    }
    if (code === SectionCode.UNSUPPORTED) {
      throw new UnimplementedError("volumes", IMAGE_REASON, VOLUMES_DOC);
    }
    const label = errorClass === "" ? undefined : errorClass;
    throw new VolumeMountError(`volumes: no se pudo montar (${label ?? SectionCode[code]})`, {
      code: knownCode(label),
    });
  }

  checkStatus(status: ConfigureStatusResponse, final: boolean): boolean {
    const states = new Map(
      (status.efsVolumes?.volumes ?? []).map((state) => [state.mountPath, state] as const),
    );
    let settled = true;
    for (const path of this.volumes.keys()) {
      const state = states.get(path);
      if (state?.state === EfsVolumeState.FAILED) {
        const errorClass = state.lastErrorClass === "" ? undefined : state.lastErrorClass;
        throw new VolumeMountError(
          `volumes: ${path} no se pudo montar (${errorClass ?? UNKNOWN_VOLUME_ERROR_CODE})`,
          { code: knownCode(errorClass) },
        );
      }
      if (state?.state !== EfsVolumeState.MOUNTED) {
        settled = false;
      }
    }
    if (!settled && final) {
      throw new VolumeMountError(
        `volumes: algún volumen sigue sin montar tras ${VOLUME_SETTLE_TIMEOUT_MS / 1000} s`,
        { code: TIMEOUT_VOLUME_ERROR_CODE },
      );
    }
    return settled;
  }
}
