/**
 * `Volume` de E2B JS (`m15-efs-volumes`, ADR-018, experimental): CRUD real
 * sobre un `VolumeStore` que `new E2B({ volumeStore })` liga a
 * `client.Volume`. Espejo de `rayito/e2b/_volume.py`'s `Volume`. Sin
 * `volumeStore`, cada método lanza `unimplemented("Volume")` en el acto
 * (nunca dentro de la promesa que devuelve, igual que el resto de
 * `resources.ts`).
 *
 * El identificador que usa Rayito aquí (`volumeId`, y el parámetro de
 * `connect`/`getInfo`/`destroy`) es el **nombre lógico** del volumen, no el
 * `AccessPointId` de AWS: `VolumeStore` indexa por nombre.
 */

import { InvalidArgumentError } from "../errors.js";
import { EfsVolume } from "../volumes/domain.js";
import type { VolumeStore } from "../volumes/store.js";
import { unimplemented } from "./unimplemented.js";

function toVolume(VolumeClass: typeof Volume, efsVolume: EfsVolume): Volume {
  return new VolumeClass(efsVolume.accessPointId, efsVolume.name);
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

  readonly volumeId: string;
  readonly name: string | undefined;

  constructor(volumeId: string, name?: string) {
    this.volumeId = volumeId;
    this.name = name;
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
    throw unimplemented("volume.readFile");
  }

  writeFile(..._args: unknown[]): never {
    throw unimplemented("volume.readFile");
  }

  makeDir(..._args: unknown[]): never {
    throw unimplemented("volume.readFile");
  }

  list(..._args: unknown[]): never {
    throw unimplemented("volume.readFile");
  }

  remove(..._args: unknown[]): never {
    throw unimplemented("volume.readFile");
  }

  updateMetadata(..._args: unknown[]): never {
    throw unimplemented("volume.readFile");
  }
}

export function bindVolume(store: VolumeStore): typeof Volume {
  return class BoundVolume extends Volume {
    static override readonly boundStore: VolumeStore = store;
  };
}

/**
 * `Sandbox.create({ volumeMounts: {path: Volume|string} })` → `volumes:
 * {path: EfsVolume}` (research doc §4.5 "Volume | name"): a bound `Volume`
 * already carries its `volumeId`/`name`, no AWS call; a plain string name
 * is looked up with `store.get(name)` (one `DescribeAccessPointsCommand`,
 * the same `Volume.connect` would make). Called from `Sandbox.createFor`,
 * never from the I/O-free `compat.ts` table, precisely because the
 * plain-string case does call AWS.
 */
export async function resolveVolumeMounts(
  volumeMounts: Readonly<Record<string, unknown>>,
  store: VolumeStore,
): Promise<Record<string, EfsVolume>> {
  const resolved: Record<string, EfsVolume> = {};
  for (const [path, value] of Object.entries(volumeMounts)) {
    if (value instanceof Volume) {
      resolved[path] = new EfsVolume({
        fileSystemId: store.fileSystemId,
        accessPointId: value.volumeId,
        name: value.name,
        region: store.region,
      });
    } else if (typeof value === "string") {
      resolved[path] = await store.get(value);
    } else {
      throw new InvalidArgumentError(
        `volumeMounts espera un Volume o un nombre de texto, se recibió ${typeof value}`,
      );
    }
  }
  return resolved;
}
