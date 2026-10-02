/**
 * Núcleo compartido de build de imágenes (`ImageBuildGateway`, arquitectura
 * §1(g) de la investigación). Espejo de `rayito._images`: el envío de
 * `create`/`update-microvm-image`, la puerta de tres estados (imagen
 * `CREATED|UPDATED`, versión `SUCCESSFUL`, estado `ACTIVE`), el reuso de
 * una versión ya construida con la misma configuración (con la
 * normalización Q52 de `baseImageVersion`) y la subida direccionada por
 * contenido del artefacto. El puerto es `ImageBuildClients`; el adaptador
 * de AWS vive en `templates/build.ts` (`AwsBuildClients`). Cada nombre de
 * parámetro AWS usado aquí aparece en `AWS_API_NOTES.md` §4/§27.
 */

/** Cadencia de sondeo del gate de tres estados (igual que `rayito image publish`). */
export const POLL_INTERVAL_MS = 10_000;
/** Plazo por defecto de un build antes de darlo por parado (no fallido). */
export const DEFAULT_BUILD_TIMEOUT_MS = 1_800_000;
/** `minimumMemoryInMiB` por defecto; `limits.ts` `SUPPORTED_MEMORY_MIB` fija el catálogo (RES-1). */
export const DEFAULT_MEMORY_MIB = 2048;
/** Prefijo del grupo de logs de cada imagen, `/rayito/<nombre>` (`AWS_API_NOTES.md` §27). */
export const LOG_GROUP_PREFIX = "/rayito";
/** Imagen base publicada por `rayito image publish` (variante `full`). */
export const DEFAULT_BASE_IMAGE_NAME = "rayito-base";
/** Error de `create`/`update-microvm-image` al pasar de 10 builds simultáneos (Q83). */
export const BUILD_QUOTA_ERROR_CODE = "ServiceQuotaExceededException";
export const ACTIVE_VERSION_STATUS = "ACTIVE";
export const SUCCESSFUL_VERSION_STATE = "SUCCESSFUL";
export const FAILED_VERSION_STATE = "FAILED";
const BASE_IMAGE_VERSION_KEY = "baseImageVersion";
/** Imagen lista para `run-microvm` tras un build. */
export const LAUNCHABLE_IMAGE_STATES: ReadonlySet<string> = new Set(["CREATED", "UPDATED"]);
/** Imagen que ya no va a terminar: sondearla hasta el timeout sería esperar en vano. */
export const FAILED_IMAGE_STATES: ReadonlySet<string> = new Set(["CREATE_FAILED", "UPDATE_FAILED"]);
const SETTLED_VERSION_STATES: ReadonlySet<string> = new Set([
  SUCCESSFUL_VERSION_STATE,
  FAILED_VERSION_STATE,
]);
/** Códigos de `HeadObject` para un objeto ausente: sin `s3:ListBucket`, S3
 * contesta 403 en vez de 404, y un permiso de verdad ausente aparece en el
 * `PutObject` que sigue. */
export const MISSING_OBJECT_CODES: ReadonlySet<string> = new Set([
  "404",
  "NoSuchKey",
  "NotFound",
  "403",
]);
/**
 * Claves de `CreateMicrovmImageRequest` (`AWS_API_NOTES.md` §4) que una
 * imagen compuesta hereda de la versión base: todas menos la identidad de
 * la imagen nueva y `codeArtifact`/`resources`/`logging`. Heredar
 * `additionalOsCapabilities` mantiene con capabilities una composición
 * sobre `rayito-base-caps`.
 */
export const INHERITED_CONFIGURATION_KEYS = [
  "baseImageArn",
  BASE_IMAGE_VERSION_KEY,
  "buildRoleArn",
  "cpuConfigurations",
  "environmentVariables",
  "additionalOsCapabilities",
  "hooks",
  "egressNetworkConnectors",
] as const;

/** Puerto: lo que la puerta necesita de Lambda MicroVMs y S3. */
export interface ImageBuildClients {
  getMicrovmImage(arn: string): Promise<{ state: string } | undefined>;
  getMicrovmImageVersion(
    arn: string,
    version: string,
  ): Promise<Record<string, unknown> | undefined>;
  listMicrovmImageVersions(arn: string): Promise<Array<Record<string, unknown>>>;
  createMicrovmImage(
    name: string,
    request: Record<string, unknown>,
  ): Promise<{ imageArn: string; imageVersion: string }>;
  updateMicrovmImage(
    arn: string,
    request: Record<string, unknown>,
  ): Promise<{ imageArn: string; imageVersion: string }>;
  headObject(bucket: string, key: string): Promise<boolean>;
  putObject(bucket: string, key: string, body: Uint8Array): Promise<void>;
}

/** Los tres estados independientes que deben pasar antes de `run-microvm`. */
export class VersionGate {
  constructor(
    readonly imageState: string,
    readonly versionState: string,
    readonly versionStatus: string,
    readonly stateReason: string | undefined,
  ) {}

  get launchable(): boolean {
    return (
      LAUNCHABLE_IMAGE_STATES.has(this.imageState) &&
      this.versionState === SUCCESSFUL_VERSION_STATE &&
      this.versionStatus === ACTIVE_VERSION_STATUS
    );
  }

  /** Terminó de resolverse, bien o mal (misma regla que `rayito._images.VersionGate.settled`). */
  get settled(): boolean {
    const versionDone = SETTLED_VERSION_STATES.has(this.versionState);
    const imageDone =
      LAUNCHABLE_IMAGE_STATES.has(this.imageState) || FAILED_IMAGE_STATES.has(this.imageState);
    return (versionDone && imageDone) || this.versionState === FAILED_VERSION_STATE;
  }
}

/** ARN de una imagen propia de la cuenta; un `name` que ya es ARN se devuelve tal cual. */
export function accountImageArn(region: string, accountId: string, name: string): string {
  if (name.startsWith("arn:")) {
    return name;
  }
  return `arn:aws:lambda:${region}:${accountId}:microvm-image:${name}`;
}

/** `1` y `1.0` nombran la misma versión gestionada (Q52); el resto sólo coincide exacto. */
export function baseImageVersionMatches(echoed: unknown, desired: unknown): boolean {
  if (typeof echoed !== "string" || typeof desired !== "string") {
    return deepEqual(echoed, desired);
  }
  if (echoed === desired) {
    return true;
  }
  const a = Number(echoed);
  const b = Number(desired);
  return echoed.trim() !== "" && desired.trim() !== "" && Number.isFinite(a) && a === b;
}

function deepEqual(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

export function configurationMatches(
  version: Record<string, unknown>,
  desired: Record<string, unknown>,
): boolean {
  return Object.entries(desired).every(([key, value]) =>
    key === BASE_IMAGE_VERSION_KEY
      ? baseImageVersionMatches(version[key], value)
      : deepEqual(version[key], value),
  );
}

function createdAtMs(item: Record<string, unknown>): number {
  return new Date(item["createdAt"] as string | number | Date).getTime();
}

/** La `imageVersion` más reciente por `createdAt`, o `undefined` si no hay ninguna. */
export function newestVersion(items: ReadonlyArray<Record<string, unknown>>): string | undefined {
  if (items.length === 0) {
    return undefined;
  }
  const latest = items.reduce((best, item) =>
    createdAtMs(best) > createdAtMs(item) ? best : item,
  );
  return String(latest["imageVersion"]);
}

export function isActiveSuccessful(item: Record<string, unknown>): boolean {
  return item["state"] === SUCCESSFUL_VERSION_STATE && item["status"] === ACTIVE_VERSION_STATUS;
}

/** La versión lanzable más reciente con exactamente `desired`; reusarla evita un build. */
export async function findReusableVersion(
  clients: ImageBuildClients,
  arn: string,
  desired: Record<string, unknown>,
): Promise<string | undefined> {
  const versions = await clients.listMicrovmImageVersions(arn);
  return newestVersion(
    versions.filter((item) => isActiveSuccessful(item) && configurationMatches(item, desired)),
  );
}

/** Las claves de `INHERITED_CONFIGURATION_KEYS` que la versión base declara. */
export function inheritedConfiguration(
  baseVersion: Record<string, unknown>,
): Record<string, unknown> {
  const configuration: Record<string, unknown> = {};
  for (const key of INHERITED_CONFIGURATION_KEYS) {
    const value = baseVersion[key];
    if (value !== undefined && value !== null) {
      configuration[key] =
        key === BASE_IMAGE_VERSION_KEY ? requestableBaseImageVersion(value) : value;
    }
  }
  return configuration;
}

/** La grafía de `baseImageVersion` que aceptan `create`/`update-microvm-image`:
 * la versión gestionada mayor (`1`), no la normalizada (`1.0`) que devuelve
 * `get-microvm-image-version` (Q52). Reenviar el eco tal cual da
 * `ValidationException` "Expected a single major version number" (Q115).
 * Una grafía no numérica o con parte fraccionaria se deja intacta: la
 * validación es de AWS. */
export function requestableBaseImageVersion(echoed: unknown): string {
  const text = String(echoed);
  const number = Number(text);
  if (text.trim() === "" || !Number.isInteger(number)) {
    return text;
  }
  return String(number);
}

export interface SubmittedBuild {
  readonly arn: string;
  readonly version: string;
  readonly created: boolean;
}

/** `update-microvm-image` si la imagen existe, si no `create-microvm-image`;
 * los errores del SDK (incluido `BUILD_QUOTA_ERROR_CODE`) suben tal cual. */
export async function submitImageBuild(
  clients: ImageBuildClients,
  name: string,
  arn: string,
  request: Record<string, unknown>,
): Promise<SubmittedBuild> {
  const existing = await clients.getMicrovmImage(arn);
  const response =
    existing === undefined
      ? await clients.createMicrovmImage(name, request)
      : await clients.updateMicrovmImage(arn, request);
  return {
    arn: response.imageArn,
    version: response.imageVersion,
    created: existing === undefined,
  };
}

/** Sube `payload` salvo que la clave (direccionada por contenido) ya exista. */
export async function uploadIfAbsent(
  clients: ImageBuildClients,
  bucket: string,
  key: string,
  payload: Uint8Array,
): Promise<boolean> {
  if (await clients.headObject(bucket, key)) {
    return false;
  }
  await clients.putObject(bucket, key, payload);
  return true;
}

export async function readGate(
  clients: ImageBuildClients,
  arn: string,
  version: string,
): Promise<VersionGate> {
  const image = await clients.getMicrovmImage(arn);
  const detail = await clients.getMicrovmImageVersion(arn, version);
  return new VersionGate(
    image?.state ?? "",
    String(detail?.["state"] ?? ""),
    String(detail?.["status"] ?? ""),
    detail?.["stateReason"] as string | undefined,
  );
}

export type Sleeper = (ms: number) => Promise<void>;

const defaultSleep: Sleeper = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** Sondea hasta `settled` o hasta agotar `timeoutMs`; el llamante decide qué
 * error lanzar en cada caso (un timeout no es un build fallido). */
export async function waitForGate(
  clients: ImageBuildClients,
  arn: string,
  version: string,
  timeoutMs: number,
  sleep: Sleeper = defaultSleep,
): Promise<{ gate: VersionGate; timedOut: boolean }> {
  const started = Date.now();
  for (;;) {
    const gate = await readGate(clients, arn, version);
    if (gate.settled) {
      return { gate, timedOut: false };
    }
    if (Date.now() - started >= timeoutMs) {
      return { gate, timedOut: true };
    }
    await sleep(POLL_INTERVAL_MS);
  }
}
