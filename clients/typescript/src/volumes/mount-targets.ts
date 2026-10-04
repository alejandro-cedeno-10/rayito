/**
 * La IP del mount target que `rayd` pasa a `mount -t efs` como
 * `mounttargetip` (`m15-efs-volumes`, ADR-018, experimental). Espejo de
 * `rayito._volumes._mount_targets`. Un `EfsVolume` sin `mountTargetIp` la
 * resuelve aquí, antes de `run-microvm`, con `DescribeMountTargets` y las
 * credenciales del llamante: así el montaje no depende de que el guest
 * resuelva el DNS privado de EFS (research doc R3) y un sistema de ficheros
 * sin ningún mount target `available` falla antes de pagar un MicroVM.
 */

import { VolumeError } from "../errors.js";
import { EfsVolume } from "./domain.js";
import {
  type DescribedMountTarget,
  type DescribedMountTargets,
  type EfsMountTargetsApi,
  translateError,
} from "./efs.js";

/** El único `LifeCycleState` con el que se puede montar (AWS_API_NOTES.md
 * §22: `creating`, `available`, `updating`, `deleting`, `deleted`, `error`). */
export const MOUNT_TARGET_AVAILABLE = "available";

function byAvailabilityZone(left: DescribedMountTarget, right: DescribedMountTarget): number {
  return (
    (left.AvailabilityZoneId ?? "").localeCompare(right.AvailabilityZoneId ?? "") ||
    (left.MountTargetId ?? "").localeCompare(right.MountTargetId ?? "")
  );
}

/**
 * La IP del mount target con el que montar: entre los `available` con
 * `IpAddress`, el de menor `AvailabilityZoneId` (y, a igualdad, menor
 * `MountTargetId`). EFS admite uno por AZ y el SDK no sabe en qué AZ corre
 * el MicroVM, así que elige de forma determinista "la primera `available`"
 * (research doc §4.5); cualquier mount target de la VPC es alcanzable desde
 * el conector. `VolumeError` si no hay ninguno. Espejo de
 * `choose_mount_target_ip`.
 */
export function chooseMountTargetIp(response: DescribedMountTargets): string {
  const usable = (response.MountTargets ?? [])
    .filter((target) => target.LifeCycleState === MOUNT_TARGET_AVAILABLE && target.IpAddress)
    .sort(byAvailabilityZone);
  const chosen = usable[0]?.IpAddress;
  if (chosen === undefined) {
    throw new VolumeError(
      "el sistema de ficheros no tiene ningún mount target 'available': despliega efs-volumes " +
        "(o espera a que termine) antes de montar un volumen",
    );
  }
  return chosen;
}

/** Lo que construye el cliente de EFS sólo si hace falta (`LazyAwsApi`). */
export interface MountTargetsApiSource {
  get(): Promise<EfsMountTargetsApi>;
}

/**
 * Resuelve la IP de mount target de cada sistema de ficheros con un solo
 * `DescribeMountTargets` por sistema de ficheros: `create()` construye uno
 * nuevo en cada llamada, así que la caché dura lo que esa creación (un
 * `reincarnate()` vuelve a preguntar). Espejo de `MountTargetResolver`.
 */
export class MountTargetResolver {
  readonly #source: MountTargetsApiSource;
  readonly #resolved = new Map<string, Promise<string>>();

  constructor(source: MountTargetsApiSource) {
    this.#source = source;
  }

  resolve(fileSystemId: string): Promise<string> {
    let pending = this.#resolved.get(fileSystemId);
    if (pending === undefined) {
      pending = this.#describe(fileSystemId);
      this.#resolved.set(fileSystemId, pending);
    }
    return pending;
  }

  async #describe(fileSystemId: string): Promise<string> {
    const api = await this.#source.get();
    let response: DescribedMountTargets;
    try {
      response = await api.describeMountTargets({ FileSystemId: fileSystemId });
    } catch (error) {
      throw translateError("describeMountTargets", error);
    }
    return chooseMountTargetIp(response);
  }
}

/** `true` si algún volumen necesita `DescribeMountTargets`. */
export function needsMountTargets(volumes: ReadonlyMap<string, EfsVolume>): boolean {
  return [...volumes.values()].some((volume) => volume.mountTargetIp === undefined);
}

/**
 * `volumes` con `mountTargetIp` en cada volumen que no la traía (los demás
 * tal cual): `EfsVolume` es inmutable, así que construye uno nuevo con los
 * mismos campos. Espejo de `resolve_mount_targets`.
 */
export async function resolveMountTargets(
  volumes: ReadonlyMap<string, EfsVolume>,
  resolver: MountTargetResolver,
): Promise<ReadonlyMap<string, EfsVolume>> {
  const resolved = new Map<string, EfsVolume>();
  for (const [path, volume] of volumes) {
    resolved.set(
      path,
      volume.mountTargetIp === undefined
        ? new EfsVolume({
            fileSystemId: volume.fileSystemId,
            accessPointId: volume.accessPointId,
            name: volume.name,
            region: volume.region,
            readOnly: volume.readOnly,
            mountTargetIp: await resolver.resolve(volume.fileSystemId),
          })
        : volume,
    );
  }
  return resolved;
}
