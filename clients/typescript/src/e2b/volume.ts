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
import { EfsVolume, validateVolumeName } from "../volumes/domain.js";
import { INTERNET_THROUGH_VPC } from "../volumes/section.js";
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
 * Lo que `new E2B({ volumeStore, volumeConnectorArn })` liga a
 * `client.Sandbox` para `volumeMounts`: el store que resuelve cada nombre y
 * el único conector de egress con el que se lanza un sandbox con volumen.
 */
export interface ShimVolumeConfig {
  readonly store?: VolumeStore | undefined;
  readonly connectorArn?: string | undefined;
}

/** `volumeMounts` ya validado: el store y el conector del cliente y cada
 * ruta con su `Volume` o nombre. */
export interface PlannedVolumeMounts {
  readonly store: VolumeStore;
  readonly connectorArn: string;
  readonly entries: readonly (readonly [string, Volume | string])[];
}

/**
 * `Sandbox.create({ volumeMounts: {path: Volume|nombre} })`, sin I/O. Sin
 * `volumeStore` en el cliente lanza `unimplemented("Volume")`, el mismo
 * guard que `client.Volume`; valida la forma (objeto no vacío; cada valor un
 * `Volume` o un nombre válido), exige `volumeConnectorArn` (un MicroVM sólo
 * admite un conector de egress y el volumen necesita el de tu VPC, así que
 * el shim no lanza con `INTERNET_EGRESS`) y rechaza `allowInternetAccess:
 * true`. El resto (rutas, variante de imagen, execution role) lo valida la
 * puerta nativa de `volumes`. Espejo de
 * `rayito.e2b._volume.plan_volume_mounts`.
 */
export function planVolumeMounts(
  volumeMounts: unknown,
  config: ShimVolumeConfig | undefined,
  allowInternetAccess: boolean | undefined,
): PlannedVolumeMounts {
  const store = config?.store;
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
  const planned: [string, Volume | string][] = [];
  for (const [path, value] of entries) {
    if (typeof value === "string") {
      planned.push([path, validateVolumeName(value)]);
    } else if (value instanceof Volume) {
      planned.push([path, value]);
    } else {
      throw new InvalidArgumentError(
        `volumeMounts espera un Volume o un nombre de texto, se recibió ${typeof value}`,
      );
    }
  }
  const connectorArn = config?.connectorArn;
  if (connectorArn === undefined) {
    throw new InvalidArgumentError(
      "volumeMounts necesita new E2B({ volumeConnectorArn: <ConnectorArn de efs-volumes> }): " +
        `el sandbox sólo puede salir por ese conector; ${INTERNET_THROUGH_VPC}`,
    );
  }
  if (allowInternetAccess === true) {
    throw new InvalidArgumentError(
      "volumeMounts no se combina con allowInternetAccess: true: un MicroVM admite un solo " +
        `conector de egress y el volumen necesita el de tu VPC; ${INTERNET_THROUGH_VPC}`,
    );
  }
  return { store, connectorArn, entries: planned };
}

/**
 * El `volumes` nativo de `planned`: un `Volume` se monta por su
 * `accessPointId` sobre el sistema de ficheros del store (sin llamar a
 * AWS); un nombre se resuelve con `store.get(nombre)`
 * (`DescribeAccessPoints`). Espejo de `resolve_volume_mounts`.
 */
export async function resolveVolumeMounts(
  planned: PlannedVolumeMounts,
): Promise<Record<string, EfsVolume>> {
  const volumes: Record<string, EfsVolume> = {};
  for (const [path, value] of planned.entries) {
    volumes[path] =
      typeof value === "string" ? await planned.store.get(value) : volumeOf(planned.store, value);
  }
  return volumes;
}

function volumeOf(store: VolumeStore, volume: Volume): EfsVolume {
  if (volume.accessPointId === undefined) {
    throw new InvalidArgumentError(
      "volumeMounts: el Volume no trae accessPointId; pásalo por nombre o usa Volume.connect",
    );
  }
  return new EfsVolume({
    fileSystemId: store.fileSystemId,
    accessPointId: volume.accessPointId,
    name: volume.volumeId,
    region: store.region,
  });
}
