/**
 * `VolumeStore`: CRUD de volúmenes EFS (access points) sobre AWS, con las
 * credenciales del llamante (`m15-efs-volumes`, ADR-018, experimental).
 * Espejo de `rayito._volumes.VolumeStore`/`AsyncVolumeStore` (TS tiene una
 * sola clase, siempre async). El bloque "Coste y activación" está en el
 * TSDoc de la clase (lo que enseña el IDE al pasar el ratón), no aquí: el
 * de módulo tsdown lo descarta al compilar `dist/*.d.mts`.
 */

import type { LazyAwsApi } from "../aws/optional-client.js";
import { VolumeError, VolumeNotFoundError } from "../errors.js";
import { EfsVolume, validateFileSystemId, validateVolumeName } from "./domain.js";
import {
  ACCESS_POINT_ALREADY_EXISTS,
  type Credentials,
  createAccessPointParams,
  type EfsApi,
  LIST_VISIBILITY_BUDGET_MS,
  LIST_VISIBILITY_POLL_MS,
  newLazyEfsApi,
  translateError,
  volumeFromDescription,
} from "./efs.js";

export interface VolumeStoreOptions {
  readonly fileSystemId: string;
  /** Por defecto `AWS_REGION`/`AWS_DEFAULT_REGION`. */
  readonly region?: string | undefined;
  readonly credentials?: Credentials | undefined;
  /** Un cliente propio con la forma de `EfsApi` (el agregado del SDK v3), p. ej. en tests. */
  readonly client?: EfsApi | undefined;
  /** Reloj en ms y espera del reintento de `create` (Q125): sólo para tests. */
  readonly now?: (() => number) | undefined;
  readonly sleep?: ((ms: number) => Promise<void>) | undefined;
}

/**
 * CRUD de volúmenes EFS (access points) sobre AWS, con las credenciales del
 * llamante.
 *
 * Coste y activación
 * -------------------
 * Activa: `new VolumeStore({...})`; construirlo no llama a AWS ni carga
 *   `@aws-sdk/client-efs` (peer opcional): el cliente se crea en la primera
 *   llamada a un método. Experimental: lo que devuelve se monta con
 *   `Sandbox.create({ volumes: { ruta: vol } })` sobre la imagen opcional
 *   `rayito-base-caps-efs` (`rayito image publish --with-efs`); ese
 *   `create()` añade un `DescribeMountTargets` por sistema de ficheros si el
 *   volumen no trae `mountTargetIp`.
 * Recursos y llamadas AWS: ningún recurso nuevo (el sistema de ficheros lo
 *   crea `infra/efs-volumes.yaml`, por separado); `create` =
 *   `CreateAccessPointCommand`, `get`/`list` = `DescribeAccessPointsCommand`,
 *   `destroy` = `DeleteAccessPointCommand`.
 * Coste aproximado: los access points no tienen cargo propio listado; el
 *   sistema de ficheros se factura por `infra/efs-volumes.yaml`.
 * IAM: `elasticfilesystem:CreateAccessPoint`, `DescribeAccessPoints`,
 *   `DeleteAccessPoint` (y `DescribeMountTargets` para montar sin
 *   `mountTargetIp`) sobre el sistema de ficheros (credenciales del
 *   LLAMANTE, no del execution role del MicroVM).
 * Cómo apagarla: no instancies `VolumeStore`; borra con `destroy()` los
 *   volúmenes que ya no uses.
 * Ejemplo:
 *   const store = new VolumeStore({ fileSystemId: "fs-0123abcd", region: "us-east-1" });
 *   const vol = await store.create("datos-agente-7");
 *   await store.get("datos-agente-7");
 *   await store.list();
 *   await store.destroy("datos-agente-7");
 */
export class VolumeStore {
  readonly #fileSystemId: string;
  readonly #region: string | undefined;
  readonly #api: LazyAwsApi<EfsApi>;
  readonly #now: () => number;
  readonly #sleep: (ms: number) => Promise<void>;

  constructor(options: VolumeStoreOptions) {
    this.#fileSystemId = validateFileSystemId(options.fileSystemId);
    this.#region = options.region;
    this.#api = newLazyEfsApi(options.region, options.credentials, options.client);
    this.#now = options.now ?? (() => performance.now());
    this.#sleep = options.sleep ?? ((ms) => new Promise((resolve) => setTimeout(resolve, ms)));
  }

  get fileSystemId(): string {
    return this.#fileSystemId;
  }

  get region(): string | undefined {
    return this.#region;
  }

  toJSON(): Record<string, unknown> {
    return { fileSystemId: this.#fileSystemId, region: this.#region };
  }

  /**
   * `CreateAccessPointCommand` con uid/gid 1000:1000 y la ruta raíz
   * `/rayito-volumes/<name>`. Idempotente: llamarla dos veces con el mismo
   * nombre no crea dos access points (`ClientToken` es un hash de
   * `fileSystemId`+nombre). AWS no devuelve el access point ya creado para
   * un `ClientToken` repetido: responde `AccessPointAlreadyExists`, que
   * esta función atrapa para devolver `get(name)` en su lugar, reintentado
   * durante `LIST_VISIBILITY_BUDGET_MS` porque el listado de EFS tarda en
   * mostrar un access point recién creado (Q125).
   */
  async create(name: string): Promise<EfsVolume> {
    validateVolumeName(name);
    const params = createAccessPointParams(this.#fileSystemId, name);
    try {
      const described = await this.#call("createAccessPoint", params);
      return new EfsVolume({
        fileSystemId: this.#fileSystemId,
        accessPointId: described.AccessPointId ?? "",
        name,
        region: this.#region,
      });
    } catch (error) {
      if (error instanceof VolumeError && error.awsCode === ACCESS_POINT_ALREADY_EXISTS) {
        return this.#getOnceListed(name);
      }
      throw error;
    }
  }

  /**
   * `DescribeAccessPointsCommand` filtrado por la etiqueta
   * `rayito:volume=<name>`. Lanza `VolumeNotFoundError` si no existe
   * ninguno con ese nombre.
   */
  async get(name: string): Promise<EfsVolume> {
    validateVolumeName(name);
    for (const volume of await this.list()) {
      if (volume.name === name) {
        return volume;
      }
    }
    throw new VolumeNotFoundError(`no existe un volumen llamado ${JSON.stringify(name)}`);
  }

  /** Todos los access points del sistema de ficheros con la etiqueta de
   * volumen de Rayito, paginando hasta agotar `NextToken`. Eventualmente
   * consistente, como el propio `DescribeAccessPoints` (Q125): un volumen
   * recién creado puede tardar unos segundos en aparecer y uno recién
   * borrado seguir apareciendo (también en `get`). */
  async list(): Promise<EfsVolume[]> {
    const volumes: EfsVolume[] = [];
    let nextToken: string | undefined;
    for (;;) {
      const response = await this.#call("describeAccessPoints", {
        FileSystemId: this.#fileSystemId,
        ...(nextToken === undefined ? {} : { NextToken: nextToken }),
      });
      for (const described of response.AccessPoints ?? []) {
        const volume = volumeFromDescription(described, this.#region);
        if (volume.name !== undefined) {
          volumes.push(volume);
        }
      }
      nextToken = response.NextToken;
      if (nextToken === undefined) {
        break;
      }
    }
    return volumes;
  }

  /**
   * `DeleteAccessPointCommand`; los ficheros bajo su directorio raíz no se
   * borran. `true` si existía y se borró; `false` si no existía ningún
   * volumen con ese nombre.
   */
  async destroy(name: string): Promise<boolean> {
    let volume: EfsVolume;
    try {
      volume = await this.get(name);
    } catch (error) {
      if (error instanceof VolumeNotFoundError) {
        return false;
      }
      throw error;
    }
    try {
      await this.#call("deleteAccessPoint", { AccessPointId: volume.accessPointId });
    } catch (error) {
      if (error instanceof VolumeNotFoundError) {
        // El listado aún mostraba un access point ya borrado (Q125).
        return false;
      }
      throw error;
    }
    return true;
  }

  /** `get(name)` de un access point que EFS acaba de confirmar que existe
   * (`AccessPointAlreadyExists`) pero que su listado todavía puede no
   * mostrar: reintenta hasta `LIST_VISIBILITY_BUDGET_MS`. */
  async #getOnceListed(name: string): Promise<EfsVolume> {
    const deadline = this.#now() + LIST_VISIBILITY_BUDGET_MS;
    for (;;) {
      try {
        return await this.get(name);
      } catch (error) {
        if (!(error instanceof VolumeNotFoundError) || this.#now() >= deadline) {
          throw error;
        }
      }
      await this.#sleep(LIST_VISIBILITY_POLL_MS);
    }
  }

  async #call<K extends keyof EfsApi>(
    operation: K,
    input: Parameters<EfsApi[K]>[0],
  ): Promise<Awaited<ReturnType<EfsApi[K]>>> {
    const api = await this.#api.get();
    try {
      // biome-ignore lint/suspicious/noExplicitAny: shape varies per operation; narrowed by the caller.
      return (await (api[operation] as any)(input)) as Awaited<ReturnType<EfsApi[K]>>;
    } catch (error) {
      throw translateError(operation, error);
    }
  }
}
