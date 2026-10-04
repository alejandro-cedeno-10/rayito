/**
 * `EfsVolumes` (`m15-efs-volumes`, experimental): la puesta en marcha rápida
 * y segura de `infra/efs-volumes.yaml` en una VPC **que ya existe**. Espejo
 * de `rayito._volumes._efs_volumes` (una sola clase async, como el resto del
 * SDK TS): fachada sobre `OptionalStacks` más la comprobación previa de
 * sólo lectura (`check`) y el borrado explícito del sistema de ficheros que
 * la pila siempre conserva. Construirla no llama a AWS.
 */

import { awsCode, type LazyAwsApi } from "../aws/optional-client.js";
import { InvalidArgumentError, VolumeError, VolumeNotFoundError } from "../errors.js";
import type { StackComponent, StackStatus } from "../stacks/model.js";
import { componentByName } from "../stacks/registry.js";
import { OptionalStacks } from "../stacks/service.js";
import { validateFileSystemId } from "./domain.js";
import { type Credentials, type EfsFileSystemApi, newLazyEfsApi, translateError } from "./efs.js";
import {
  type EfsNetworkReport,
  inspectNetwork,
  type NetworkInspector,
  validateAccessPointArns,
} from "./network.js";
import { VolumeStore } from "./store.js";
import { Ec2NetworkInspector } from "./vpc.js";

const COMPONENT: StackComponent = componentByName("efs-volumes") as StackComponent;

export const DEFAULT_EFS_VOLUMES_STACK_NAME = `rayito-${COMPONENT.name}`;
/** La salida de la pila con el sistema de ficheros (`infra/efs-volumes.yaml`). */
const FILE_SYSTEM_ID_OUTPUT = "FileSystemId";
/** Tras borrar la pila, `DescribeMountTargets` puede tardar algo en dejar de
 * listarlos (eventualmente consistente como `DescribeAccessPoints`:
 * AWS_API_NOTES.md §16 Q125); se sondea con margen antes de
 * `DeleteFileSystem`. Espejo de `MOUNT_TARGET_DRAIN_BUDGET_SECONDS`. */
export const MOUNT_TARGET_DRAIN_BUDGET_MS = 120_000;
export const MOUNT_TARGET_DRAIN_POLL_MS = 5_000;
/** `DescribeFileSystems`/`DescribeMountTargets`/`DeleteFileSystem` sobre uno
 * que ya no existe. */
const FILE_SYSTEM_NOT_FOUND = "FileSystemNotFound";
/** La etiqueta literal que `infra/efs-volumes.yaml` pone a su sistema de
 * ficheros (`FileSystemTags`): `deleteFileSystem` sólo borra uno que la lleve. */
const FILE_SYSTEM_TAG_KEY = "rayito";
const FILE_SYSTEM_TAG_VALUE = "efs-volumes";

export interface EfsVolumesOptions {
  readonly stackName?: string;
  readonly region?: string | undefined;
  readonly credentials?: Credentials | undefined;
  readonly stacks?: OptionalStacks;
  /** Un `NetworkInspector` propio (p. ej. en tests); por defecto, EC2. */
  readonly network?: NetworkInspector;
  /** Un cliente EFS propio con la forma de `EfsFileSystemApi`, p. ej. en tests. */
  readonly efsClient?: EfsFileSystemApi;
  /** Reloj en ms y espera del sondeo de mount targets: sólo para tests. */
  readonly now?: () => number;
  readonly sleep?: (ms: number) => Promise<void>;
}

export interface EfsVolumesDeployOptions {
  readonly vpcId: string;
  readonly subnetIds: readonly string[] | string;
  readonly allowWrite?: boolean;
  readonly accessPointArns?: readonly string[];
  /**
   * Access points que se quedan en sólo lectura aunque `allowWrite` sea
   * `true`: la política les deniega `ClientWrite`. Es lo que hace de sólo
   * lectura un volumen; la opción `ro` del montaje no basta
   * (`AWS_API_NOTES.md` §16 Q133).
   */
  readonly readOnlyAccessPointArns?: readonly string[];
  readonly connectorName?: string;
  readonly tags?: Readonly<Record<string, string>>;
  readonly wait?: boolean;
}

class FileSystemGone extends Error {}

/**
 * Volúmenes EFS en tu propia VPC: comprueba, despliega, consulta y borra el
 * componente `efs-volumes`. Construirlo no llama a AWS ni carga ningún peer.
 *
 * Coste y activación
 * -------------------
 * Activa: una llamada explícita a `deploy({ vpcId, subnetIds })` (o
 *   `rayito stack deploy efs-volumes --param VpcId=... --param SubnetIds=...`).
 *   `check()` es de sólo lectura y no crea nada. Experimental:
 *   `Sandbox.create({ volumes })` monta sobre la imagen opcional
 *   `rayito-base-caps-efs` (`rayito image publish --with-efs`), con el
 *   execution role y este conector como único `egress`: no puede usar
 *   además `INTERNET_EGRESS`.
 * Recursos y llamadas AWS: `check()` = `DescribeVpcs`, `DescribeVpcAttribute`,
 *   `DescribeSubnets`, `DescribeRouteTables` (peer `@aws-sdk/client-ec2`).
 *   `deploy()` crea, sólo dentro de la VPC dada y etiquetado: un sistema de
 *   ficheros EFS cifrado (clave KMS gestionada por AWS), un mount target por
 *   subred, dos grupos de seguridad nuevos (el de los mount targets sólo
 *   admite TCP 2049 desde el del conector), el `AWS::Lambda::NetworkConnector`
 *   de salida a la VPC para MicroVMs, su rol de operador y la política
 *   `RayitoEfsVolumeClient`. Nunca modifica la VPC, sus subredes, tablas de
 *   rutas, NACLs ni grupos existentes.
 * Coste aproximado: $0 con el sistema de ficheros vacío; $0,30/GB-mes
 *   (Standard) y $0,016/GB-mes tras 30 días (IA); Elastic Throughput
 *   $0,03/GB leído y $0,06/GB escrito; sin cargo listado por mount targets,
 *   access points ni ENIs del conector (us-east-1, 2026-09-11).
 * IAM: el llamante necesita `cloudformation:*Stack*` sobre la pila, crear
 *   los recursos de arriba (`CAPABILITY_IAM`), los `ec2:Describe*` de
 *   `check()` y, para borrar el sistema de ficheros,
 *   `elasticfilesystem:DescribeFileSystems`/`DescribeMountTargets`/
 *   `DescribeAccessPoints`/`DeleteAccessPoint`/`DeleteFileSystem`; el
 *   execution role del MicroVM, la política `CallerPolicyArn`
 *   de la salida (`ClientMount`/`ClientWrite` sólo sobre este sistema de
 *   ficheros y sus access points).
 * Cómo apagarla: `destroy()` borra la pila y conserva el sistema de ficheros
 *   con sus datos; `destroy({ deleteFileSystem: true })` borra además sus
 *   access points y el propio sistema de ficheros: todo lo que `deploy()` creó.
 *   Tras `rayito stack destroy efs-volumes` (que siempre lo conserva),
 *   `deleteFileSystem("fs-…")` borra el que quedó.
 * Ejemplo:
 *   const efs = new EfsVolumes({ region: "us-east-1" });
 *   const network = { vpcId: "vpc-0123456789abcdef0", subnetIds: ["subnet-0123456789abcdef0"] };
 *   const report = await efs.check(network);
 *   await efs.deploy(network);
 *   const store = await efs.volumeStore();
 *   await efs.destroy({ deleteFileSystem: true });
 */
export class EfsVolumes {
  readonly #stackName: string;
  readonly #region: string | undefined;
  readonly #credentials: Credentials | undefined;
  readonly #stacks: OptionalStacks;
  readonly #network: NetworkInspector;
  readonly #efs: LazyAwsApi<EfsFileSystemApi>;
  readonly #now: () => number;
  readonly #sleep: (ms: number) => Promise<void>;

  constructor(options: EfsVolumesOptions = {}) {
    this.#stackName = options.stackName ?? DEFAULT_EFS_VOLUMES_STACK_NAME;
    this.#region = options.region;
    this.#credentials = options.credentials;
    this.#stacks =
      options.stacks ??
      new OptionalStacks({ region: options.region, credentials: options.credentials });
    this.#network =
      options.network ??
      new Ec2NetworkInspector({ region: options.region, credentials: options.credentials });
    this.#efs = newLazyEfsApi<EfsFileSystemApi>(
      options.region,
      options.credentials,
      options.efsClient,
    );
    this.#now = options.now ?? (() => performance.now());
    this.#sleep = options.sleep ?? ((ms) => new Promise((resolve) => setTimeout(resolve, ms)));
  }

  get stackName(): string {
    return this.#stackName;
  }

  /**
   * Valida, sin crear nada, que la VPC y las subredes sirven (existen, las
   * subredes son de la VPC y están en AZs distintas, les quedan IPs libres,
   * DNS de la VPC); avisa de una sola AZ y de cómo saldría a internet un
   * sandbox. Incluye lo que `deploy()` crearía y su coste.
   */
  async check(input: {
    readonly vpcId: string;
    readonly subnetIds: readonly string[] | string;
  }): Promise<EfsNetworkReport> {
    return inspectNetwork(this.#network, input.vpcId, input.subnetIds, COMPONENT.cost);
  }

  /**
   * Corre `check()` y, sólo si ningún hallazgo es `FAIL`, despliega (o
   * actualiza) la pila. `accessPointArns` acota `RayitoEfsVolumeClient` a
   * esos access points; `allowWrite: false` sólo concede `ClientMount`;
   * `readOnlyAccessPointArns` les deniega `ClientWrite` a esos access points
   * aunque `allowWrite` sea `true`. Ver "Coste y activación" de la clase.
   */
  async deploy(options: EfsVolumesDeployOptions): Promise<StackStatus> {
    const arns = validateAccessPointArns(options.accessPointArns ?? []);
    const readOnlyArns = validateAccessPointArns(
      options.readOnlyAccessPointArns ?? [],
      "readOnlyAccessPointArns",
    );
    const report = await this.check({ vpcId: options.vpcId, subnetIds: options.subnetIds });
    if (!report.ok) {
      const reasons = report.findings
        .filter((finding) => finding.level === "FAIL")
        .map((finding) => finding.message)
        .join("; ");
      throw new InvalidArgumentError(
        `la VPC no sirve para efs-volumes (no se creó nada): ${reasons}`,
      );
    }
    const parameters: Record<string, string> = {
      VpcId: report.vpcId,
      SubnetIds: report.subnetIds.join(","),
      AllowWrite: options.allowWrite === false ? "false" : "true",
      AccessPointArns: arns.join(","),
      ReadOnlyAccessPointArns: readOnlyArns.join(","),
    };
    if (options.connectorName !== undefined) {
      parameters.ConnectorName = options.connectorName;
    }
    return this.#stacks.deploy(COMPONENT, {
      stackName: this.#stackName,
      parameters,
      tags: options.tags ?? {},
      ...(options.wait === undefined ? {} : { wait: options.wait }),
    });
  }

  async status(): Promise<StackStatus | undefined> {
    return this.#stacks.status(COMPONENT, { stackName: this.#stackName });
  }

  /** Un `VolumeStore` sobre el sistema de ficheros de la pila desplegada;
   * `VolumeError` si la pila no existe. */
  async volumeStore(): Promise<VolumeStore> {
    const fileSystemId = await this.#fileSystemIdOrUndefined();
    if (fileSystemId === undefined) {
      throw new VolumeError(
        `la pila ${JSON.stringify(this.#stackName)} no existe o no tiene FileSystemId: ` +
          "despliégala con EfsVolumes.deploy(...)",
      );
    }
    return new VolumeStore({
      fileSystemId,
      region: this.#region,
      credentials: this.#credentials,
    });
  }

  /**
   * Borra la pila. El sistema de ficheros se conserva siempre
   * (`DeletionPolicy: Retain`) salvo con `deleteFileSystem: true`, que
   * después borra sus access points y el propio sistema de ficheros (y con
   * él los datos): eso exige esperar al borrado de la pila (`wait` no
   * puede ser `false`).
   */
  async destroy(
    options: { readonly deleteFileSystem?: boolean; readonly wait?: boolean } = {},
  ): Promise<void> {
    if (options.deleteFileSystem === true && options.wait === false) {
      throw new InvalidArgumentError(
        "destroy({ deleteFileSystem: true }) necesita esperar: el sistema de ficheros " +
          "sólo se puede borrar cuando la pila ya borró sus mount targets",
      );
    }
    const fileSystemId =
      options.deleteFileSystem === true ? await this.#fileSystemIdOrUndefined() : undefined;
    await this.#stacks.destroy(COMPONENT, {
      stackName: this.#stackName,
      ...(options.wait === undefined ? {} : { wait: options.wait }),
    });
    if (fileSystemId !== undefined) {
      await this.deleteFileSystem(fileSystemId);
    }
  }

  /**
   * Borra un sistema de ficheros que esta pila creó y conservó (p. ej. tras
   * `rayito stack destroy efs-volumes`): espera a que no le quede ningún
   * mount target, borra sus access points y después el sistema de ficheros,
   * con todos sus datos. Se niega (`VolumeError`) si no lleva la etiqueta
   * `rayito=efs-volumes` que le pone `infra/efs-volumes.yaml`: nunca borra
   * uno ajeno. Uno que ya no existe es un no-op.
   */
  async deleteFileSystem(fileSystemId: string): Promise<void> {
    validateFileSystemId(fileSystemId);
    try {
      await this.#requireStackFileSystem(fileSystemId);
      await this.#waitWithoutMountTargets(fileSystemId);
      await this.#deleteAccessPoints(fileSystemId);
      await this.#call("deleteFileSystem", { FileSystemId: fileSystemId });
    } catch (error) {
      if (!(error instanceof FileSystemGone)) {
        throw error;
      }
    }
  }

  async #fileSystemIdOrUndefined(): Promise<string | undefined> {
    const status = await this.status();
    const value = status?.state === undefined ? undefined : status.outputs[FILE_SYSTEM_ID_OUTPUT];
    return value === undefined ? undefined : validateFileSystemId(value);
  }

  async #call<K extends keyof EfsFileSystemApi>(
    operation: K,
    input: Parameters<EfsFileSystemApi[K]>[0],
  ): Promise<Awaited<ReturnType<EfsFileSystemApi[K]>>> {
    const api = await this.#efs.get();
    const method = api[operation] as (arg: typeof input) => ReturnType<EfsFileSystemApi[K]>;
    try {
      return await method.call(api, input);
    } catch (error) {
      if (awsCode(error) === FILE_SYSTEM_NOT_FOUND) {
        throw new FileSystemGone();
      }
      throw translateError(operation, error);
    }
  }

  async #requireStackFileSystem(fileSystemId: string): Promise<void> {
    const described = await this.#call("describeFileSystems", { FileSystemId: fileSystemId });
    const fileSystem = described.FileSystems?.[0];
    if (fileSystem === undefined) {
      throw new FileSystemGone();
    }
    const tag = (fileSystem.Tags ?? []).find((candidate) => candidate.Key === FILE_SYSTEM_TAG_KEY);
    if (tag?.Value !== FILE_SYSTEM_TAG_VALUE) {
      throw new VolumeError(
        "ese sistema de ficheros no lleva la etiqueta de efs-volumes " +
          `(${FILE_SYSTEM_TAG_KEY}=${FILE_SYSTEM_TAG_VALUE}): no se borra`,
      );
    }
  }

  async #waitWithoutMountTargets(fileSystemId: string): Promise<void> {
    const deadline = this.#now() + MOUNT_TARGET_DRAIN_BUDGET_MS;
    while (
      (
        (await this.#call("describeMountTargets", { FileSystemId: fileSystemId })).MountTargets ??
        []
      ).length > 0
    ) {
      if (this.#now() >= deadline) {
        throw new VolumeError(
          "el sistema de ficheros sigue con mount targets tras borrar la pila; repite " +
            "destroy({ deleteFileSystem: true }) en unos minutos",
        );
      }
      await this.#sleep(MOUNT_TARGET_DRAIN_POLL_MS);
    }
  }

  async #deleteAccessPoints(fileSystemId: string): Promise<void> {
    let nextToken: string | undefined;
    do {
      const page = await this.#call("describeAccessPoints", {
        FileSystemId: fileSystemId,
        ...(nextToken === undefined ? {} : { NextToken: nextToken }),
      });
      for (const accessPoint of page.AccessPoints ?? []) {
        try {
          await this.#call("deleteAccessPoint", { AccessPointId: accessPoint.AccessPointId ?? "" });
        } catch (error) {
          // Un `VolumeNotFoundError` es el listado mostrando uno ya borrado (Q125).
          if (!(error instanceof VolumeNotFoundError)) {
            throw error;
          }
        }
      }
      nextToken = page.NextToken;
    } while (nextToken);
  }
}
