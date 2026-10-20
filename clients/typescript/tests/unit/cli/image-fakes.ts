/**
 * Dobles en memoria de los puertos de `rayito image publish` y `prune`: una
 * imagen con sus versiones, un S3 de claves y el gate de tres estados que se
 * asienta tras un número programado de lecturas. Registran cada llamada, como
 * los `Stubber` de los tests de Python.
 */

import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { writeImageZip } from "../../../src/images/artifact-node.js";
import type { Version } from "../../../src/images/prune-domain.js";
import type { PrunePorts } from "../../../src/images/prune-service.js";
import { PruneRefusal } from "../../../src/images/prune-service.js";
import type { PublishPorts, RecentLogs } from "../../../src/images/publish-service.js";
import { ACCOUNT_ID, REGION } from "./case-world.js";

export const BUILD_ROLE = `arn:aws:iam::${ACCOUNT_ID}:role/build`;
export const BUCKET = "artifacts";
export const IMAGE_ARN = `arn:aws:lambda:${REGION}:${ACCOUNT_ID}:microvm-image:rayito-base`;

export function imageArnOf(name: string): string {
  return `arn:aws:lambda:${REGION}:${ACCOUNT_ID}:microvm-image:${name}`;
}

type Item = Record<string, unknown>;

export interface ImageRecord {
  state: string;
  versions: Item[];
}

/** Lo que `list-microvm-image-versions` devuelve: nunca las variables de entorno (Q118) y los objetos con otro orden de claves. */
function echoedByList(item: Item): Item {
  const { environmentVariables: _hidden, hooks, ...rest } = item;
  const reordered =
    typeof hooks === "object" && hooks !== null
      ? Object.fromEntries(Object.entries(hooks).reverse())
      : hooks;
  return { ...rest, hooks: reordered };
}

/** Las versiones que `create`/`update` produce: lanzables por defecto, o fallidas con `failNextBuild`. */
export class FakePublishPorts implements PublishPorts {
  readonly region = REGION;
  readonly calls: string[] = [];
  readonly images = new Map<string, ImageRecord>();
  readonly objects = new Map<string, Uint8Array>();
  readonly requests: Array<{ readonly operation: string; readonly body: Item }> = [];
  stackRole: string | undefined = BUILD_ROLE;
  /** La versión más reciente de la base gestionada; `undefined` si no lista ninguna. */
  managedVersion: string | undefined = "1";
  recent: RecentLogs = { kind: "streams", streams: [] };
  /** Lecturas del gate (imagen más versión) que tardan en asentarse tras enviar un build. */
  pendingReads = 1;
  failNextBuild = false;
  /** Se sustituye por una versión que nunca se asienta (para el plazo). */
  neverSettle = false;
  #nextVersion = 1;
  #reads = new Map<string, number>();
  #outcomes = new Map<string, "success" | "failure">();

  async accountId(): Promise<string> {
    return ACCOUNT_ID;
  }

  async buildRoleArnOfStack(stackName: string): Promise<string | undefined> {
    this.calls.push(`buildRoleArnOfStack:${stackName}`);
    return this.stackRole;
  }

  async newestManagedVersion(arn: string): Promise<string | undefined> {
    this.calls.push(`newestManagedVersion:${arn}`);
    return this.managedVersion;
  }

  async getMicrovmImage(arn: string): Promise<{ state: string } | undefined> {
    this.calls.push("getMicrovmImage");
    const image = this.images.get(arn);
    if (image === undefined) {
      return undefined;
    }
    const version = image.versions.at(-1);
    const settled = version === undefined || this.#isSettled(arn, version);
    return { state: settled ? this.#settledImageState(arn, version) : "UPDATING" };
  }

  #outcomeOf(arn: string, version: Item | undefined): "success" | "failure" {
    return this.#outcomes.get(`${arn}@${String(version?.imageVersion)}`) ?? "success";
  }

  #settledImageState(arn: string, version: Item | undefined): string {
    return this.#outcomeOf(arn, version) === "failure" ? "UPDATE_FAILED" : "UPDATED";
  }

  #isSettled(arn: string, version: Item): boolean {
    if (this.neverSettle) {
      return false;
    }
    const reads = this.#reads.get(`${arn}@${String(version.imageVersion)}`) ?? 0;
    return reads >= this.pendingReads;
  }

  async getMicrovmImageVersion(arn: string, version: string): Promise<Item | undefined> {
    this.calls.push("getMicrovmImageVersion");
    const image = this.images.get(arn);
    const found = image?.versions.find((item) => item.imageVersion === version);
    if (found === undefined) {
      return undefined;
    }
    const key = `${arn}@${version}`;
    this.#reads.set(key, (this.#reads.get(key) ?? 0) + 1);
    if (!this.#isSettled(arn, found)) {
      return { ...found, state: "IN_PROGRESS", status: "INACTIVE" };
    }
    const failed = this.#outcomeOf(arn, found) === "failure";
    return {
      ...found,
      state: failed ? "FAILED" : "SUCCESSFUL",
      status: failed ? "INACTIVE" : "ACTIVE",
      stateReason: failed ? "boom" : "ok",
    };
  }

  async listMicrovmImageVersions(arn: string): Promise<Item[]> {
    this.calls.push("listMicrovmImageVersions");
    return (this.images.get(arn)?.versions ?? []).map(echoedByList);
  }

  #record(
    operation: string,
    arn: string,
    request: Item,
  ): { imageArn: string; imageVersion: string } {
    this.requests.push({ operation, body: request });
    const imageVersion = `${this.#nextVersion}.0`;
    this.#nextVersion += 1;
    const baseImageVersion = String(Number(request.baseImageVersion));
    const item: Item = {
      ...request,
      imageVersion,
      baseImageVersion: `${baseImageVersion}.0`,
      state: "SUCCESSFUL",
      status: "ACTIVE",
      createdAt: new Date(Date.UTC(2026, 8, 1, this.#nextVersion)),
    };
    const image = this.images.get(arn) ?? { state: "CREATED", versions: [] };
    image.versions.push(item);
    this.images.set(arn, image);
    if (this.failNextBuild) {
      this.#outcomes.set(`${arn}@${imageVersion}`, "failure");
      this.failNextBuild = false;
    }
    return { imageArn: arn, imageVersion };
  }

  async createMicrovmImage(
    name: string,
    request: Item,
  ): Promise<{ imageArn: string; imageVersion: string }> {
    this.calls.push("createMicrovmImage");
    return this.#record("create", imageArnOf(name), request);
  }

  async updateMicrovmImage(
    arn: string,
    request: Item,
  ): Promise<{ imageArn: string; imageVersion: string }> {
    this.calls.push("updateMicrovmImage");
    return this.#record("update", arn, request);
  }

  /** El `owner` de cada `headObject` y `putObject`, en orden. */
  readonly objectOwners: string[] = [];

  async headObject(bucket: string, key: string, owner: string): Promise<boolean> {
    this.calls.push(`headObject:${bucket}/${key}`);
    this.objectOwners.push(owner);
    return this.objects.has(`${bucket}/${key}`);
  }

  async putObject(bucket: string, key: string, body: Uint8Array, owner: string): Promise<void> {
    this.calls.push(`putObject:${bucket}/${key}`);
    this.objectOwners.push(owner);
    this.objects.set(`${bucket}/${key}`, body);
  }

  async latestBuild(): Promise<Item> {
    this.calls.push("latestBuild");
    return {
      buildState: "SUCCESSFUL",
      chipsetGeneration: "3",
      snapshotBuild: { memorySnapshotSizeInBytes: 900 },
      stateReason: null,
    };
  }

  async recentLogs(): Promise<RecentLogs> {
    this.calls.push("recentLogs");
    return this.recent;
  }
}

export interface FakeVersion {
  readonly imageVersion: string;
  readonly state?: string;
  readonly status?: string;
  readonly createdAt?: Date;
}

const T0 = Date.UTC(2026, 8, 1);
const MS_PER_HOUR = 3_600_000;

export function versionOf(number: number, state = "SUCCESSFUL", status = "ACTIVE"): Version {
  return {
    imageVersion: `${number}.0`,
    state,
    status,
    createdAt: new Date(T0 + number * MS_PER_HOUR),
  };
}

/** Un `PrunePorts` con un borrado que cambia el estado de la imagen y de la versión como lo hace AWS. */
export class FakePrunePorts implements PrunePorts {
  readonly region = REGION;
  readonly calls: string[] = [];
  versions: Version[];
  live = new Set<string>();
  imageStates: string[] = ["UPDATED"];
  /** Códigos con los que rechazar, en orden, los primeros borrados (los siguientes tienen éxito). */
  refusals: Array<{ code: string; message: string }> = [];
  /** `true`: la versión tarda en pasar a `DELETED`; el estado que ve `versionState` mientras tanto. */
  versionStatesAfterDelete: Array<string | undefined> = [undefined];
  #deleted = new Set<string>();
  #stateReads = 0;
  #imageReads = 0;

  constructor(versions: Version[]) {
    this.versions = versions;
  }

  async accountId(): Promise<string> {
    return ACCOUNT_ID;
  }

  async listVersions(): Promise<Version[]> {
    this.calls.push("listVersions");
    return [...this.versions].sort((a, b) => b.createdAt.getTime() - a.createdAt.getTime());
  }

  async liveVersions(): Promise<Set<string>> {
    this.calls.push("liveVersions");
    return this.live;
  }

  async imageState(): Promise<string> {
    this.calls.push("imageState");
    const state =
      this.imageStates[Math.min(this.#imageReads, this.imageStates.length - 1)] ?? "UPDATED";
    this.#imageReads += 1;
    return state;
  }

  async versionState(_arn: string, imageVersion: string): Promise<string | undefined> {
    this.calls.push(`versionState:${imageVersion}`);
    if (!this.#deleted.has(imageVersion)) {
      return this.versions.find((item) => item.imageVersion === imageVersion)?.state;
    }
    const state =
      this.versionStatesAfterDelete[
        Math.min(this.#stateReads, this.versionStatesAfterDelete.length - 1)
      ];
    this.#stateReads += 1;
    return state;
  }

  async deleteVersion(_arn: string, imageVersion: string): Promise<void> {
    this.calls.push(`deleteVersion:${imageVersion}`);
    const refusal = this.refusals.shift();
    if (refusal !== undefined) {
      throw new PruneRefusal(refusal.code, refusal.message);
    }
    this.#deleted.add(imageVersion);
    this.#stateReads = 0;
  }

  async deactivateVersion(_arn: string, imageVersion: string): Promise<void> {
    this.calls.push(`deactivateVersion:${imageVersion}`);
    this.versions = this.versions.map((item) =>
      item.imageVersion === imageVersion ? { ...item, status: "INACTIVE" } : item,
    );
  }
}

/** Un directorio de imagen y su zip en un directorio temporal que `dispose` borra. */
export class ImageWorkspace {
  readonly root = mkdtempSync(join(tmpdir(), "rayito-image-"));
  readonly imageDir = join(this.root, "image");

  constructor() {
    this.write("Dockerfile", "FROM scratch\n");
    this.write("rayd", "ELF\n");
    for (const notice of ["LICENSE", "NOTICE", "THIRD_PARTY_LICENSES.md"]) {
      this.write(`licenses/${notice}`, `${notice}\n`);
    }
  }

  write(relative: string, text: string): string {
    const path = join(this.imageDir, relative);
    mkdirSync(dirname(path), { recursive: true });
    writeFileSync(path, text, "utf8");
    return path;
  }

  zip(variant = "full", withEfs = false, name = "rayito-image.zip"): string {
    const destination = join(this.root, name);
    writeImageZip(this.imageDir, destination, variant, { withEfs });
    return destination;
  }

  dispose(): void {
    rmSync(this.root, { recursive: true, force: true });
  }
}
