/**
 * `Volume` de E2B JS (`m15-efs-volumes`, ADR-018, experimental): CRUD real
 * sobre un `VolumeStore` que `new E2B({ volumeStore })` liga a
 * `client.Volume`. Espejo de `rayito/e2b/_volume.py`'s `Volume`. Sin
 * `volumeStore`, cada método lanza `unimplemented("Volume")` en el acto
 * (nunca dentro de la promesa que devuelve, igual que el resto de
 * `resources.ts`).
 *
 * Un solo identificador: `volumeId` es el **nombre lógico** del volumen, el
 * mismo que reciben `connect`/`getInfo`/`destroy` (`VolumeStore` indexa por
 * nombre), así que `Volume.destroy(vol.volumeId)` borra el volumen que
 * `Volume.create` devolvió. El `AccessPointId` de AWS va aparte, en
 * `accessPointId`.
 */

import { InvalidArgumentError } from "../errors.js";
import { resolveImageVariant } from "../role-policy.js";
import { type EfsVolume, validateVolumeName } from "../volumes/domain.js";
import { requireVolumeMounts } from "../volumes/section.js";
import type { VolumeStore } from "../volumes/store.js";
import { unimplemented } from "./unimplemented.js";

/** La clave de la tabla D14 de toda operación de contenido de un `Volume`. */
export const VOLUME_CONTENT_FEATURE = "volume.content";

function toVolume(VolumeClass: typeof Volume, efsVolume: EfsVolume): Volume {
  // `VolumeStore.create`/`get` siempre fijan el nombre; `list` filtra los
  // access points sin él antes de llegar aquí.
  if (efsVolume.name === undefined) {
    throw new InvalidArgumentError("el access point no tiene nombre de volumen Rayito");
  }
  return new VolumeClass(efsVolume.name, efsVolume.accessPointId);
}

/** `boundStore`, o lanza `unimplemented("Volume")`: la única forma en que
 * esta clase lee el store ligado, para no repetir el guard en cada método. */
function requireStore(ctor: typeof Volume): VolumeStore {
  if (ctor.boundStore === undefined) {
    throw unimplemented("Volume");
  }
  return ctor.boundStore;
}

export class Volume {
  /** La opción que fija `new E2B({ volumeStore }).Volume`. @internal */
  static readonly boundStore: VolumeStore | undefined = undefined;

  /** El nombre lógico: el identificador de `connect`/`getInfo`/`destroy`. */
  readonly volumeId: string;
  /** El `AccessPointId` de AWS (`fsap-...`), informativo. */
  readonly accessPointId: string | undefined;

  constructor(volumeId: string, accessPointId?: string) {
    this.volumeId = volumeId;
    this.accessPointId = accessPointId;
  }

  /** E2B expone `name` además de `volumeId`; en Rayito son el mismo. */
  get name(): string {
    return this.volumeId;
  }

  static create(this: typeof Volume, name: string): Promise<Volume> {
    // biome-ignore lint/complexity/noThisInStatic: `this` es la subclase ligada de `new E2B(...).Volume`
    return requireStore(this)
      .create(name)
      .then((created) => toVolume(this, created));
  }

  static connect(this: typeof Volume, volumeId: string): Promise<Volume> {
    // biome-ignore lint/complexity/noThisInStatic: `this` es la subclase ligada de `new E2B(...).Volume`
    return requireStore(this)
      .get(volumeId)
      .then((found) => toVolume(this, found));
  }

  static getInfo(this: typeof Volume, volumeId: string): Promise<Volume> {
    // biome-ignore lint/complexity/noThisInStatic: `this` es la subclase ligada de `new E2B(...).Volume`
    return this.connect(volumeId);
  }

  static list(this: typeof Volume): Promise<Volume[]> {
    // biome-ignore lint/complexity/noThisInStatic: `this` es la subclase ligada de `new E2B(...).Volume`
    return requireStore(this)
      .list()
      .then((volumes) => volumes.filter((v) => v.name !== undefined).map((v) => toVolume(this, v)));
  }

  static destroy(this: typeof Volume, volumeId: string): Promise<boolean> {
    // biome-ignore lint/complexity/noThisInStatic: `this` es la subclase ligada de `new E2B(...).Volume`
    return requireStore(this).destroy(volumeId);
  }

  readFile(..._args: unknown[]): never {
    throw unimplemented(VOLUME_CONTENT_FEATURE);
  }

  writeFile(..._args: unknown[]): never {
    throw unimplemented(VOLUME_CONTENT_FEATURE);
  }

  makeDir(..._args: unknown[]): never {
    throw unimplemented(VOLUME_CONTENT_FEATURE);
  }

  list(..._args: unknown[]): never {
    throw unimplemented(VOLUME_CONTENT_FEATURE);
  }

  remove(..._args: unknown[]): never {
    throw unimplemented(VOLUME_CONTENT_FEATURE);
  }

  updateMetadata(..._args: unknown[]): never {
    throw unimplemented(VOLUME_CONTENT_FEATURE);
  }
}

export function bindVolume(store: VolumeStore): typeof Volume {
  return class BoundVolume extends Volume {
    static override readonly boundStore: VolumeStore = store;
  };
}

/**
 * `Sandbox.create({ volumeMounts: {path: Volume|nombre} })`, sin I/O: la
 * misma puerta que `volumes` (`requireVolumeMounts`). Sin `volumeStore` en
 * el cliente lanza `unimplemented("Volume")`, el mismo guard que
 * `client.Volume`. Valida la forma y nunca resuelve un nombre: mientras no
 * haya montaje real, `requireVolumeMounts` siempre termina en
 * `UnimplementedError`, así que un `DescribeAccessPoints` sólo costaría una
 * llamada sin cambiar el resultado. Espejo de
 * `rayito.e2b._volume.require_volume_mount_support`.
 */
export function requireVolumeMountSupport(
  volumeMounts: unknown,
  store: VolumeStore | undefined,
  template: string | undefined,
): never {
  if (store === undefined) {
    throw unimplemented("Volume");
  }
  if (typeof volumeMounts !== "object" || volumeMounts === null || Array.isArray(volumeMounts)) {
    throw new InvalidArgumentError("volumeMounts espera un objeto {ruta: Volume|nombre}");
  }
  const entries = Object.entries(volumeMounts);
  if (entries.length === 0) {
    throw new InvalidArgumentError("volumeMounts no admite un objeto vacío; omite la opción");
  }
  for (const [, value] of entries) {
    if (typeof value === "string") {
      validateVolumeName(value);
    } else if (!(value instanceof Volume)) {
      throw new InvalidArgumentError(
        `volumeMounts espera un Volume o un nombre de texto, se recibió ${typeof value}`,
      );
    }
  }
  return requireVolumeMounts(
    entries.map(([path]) => path),
    resolveImageVariant(template),
    "volumeMounts",
  );
}
