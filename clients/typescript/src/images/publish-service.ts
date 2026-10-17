/**
 * `rayito image publish` (aplicación): sube el artefacto por contenido, crea o
 * actualiza la imagen, sondea el gate de tres estados (imagen `CREATED|UPDATED`,
 * versión `SUCCESSFUL`, estado `ACTIVE`) y arma el resumen; con `--sizes`
 * publica además una imagen por tamaño desde el mismo artefacto. Espejo de
 * `rayito.cli._publish`.
 *
 * Una versión cuyo artefacto y configuración ya coinciden se reutiliza en vez
 * de reconstruirse (cada versión cuesta una semana de almacenamiento de
 * snapshot); `force` construye igualmente. Si un build falla se imprimen el
 * `stateReason` de la versión y del build y los eventos más nuevos del grupo de
 * logs de la imagen. El servicio nunca imprime el documento final: devuelve
 * resúmenes y quien llama (la CLI) decide cómo informarlos.
 */

import { createHash } from "node:crypto";
import { MAX_CONCURRENT_IMAGE_BUILDS } from "../limits.js";
import type { SizeName } from "../sizing/sizing.js";
import { pythonStringRepr } from "../strings.js";
import { isoUtc } from "../time-text.js";
import { MS_PER_SECOND } from "../units.js";
import {
  accountImageArn,
  findReusableVersion,
  type ImageBuildClients,
  submitImageBuild,
  uploadIfAbsent,
  type VersionGate,
  waitForGate,
} from "./gateway.js";
import { baseImageArn } from "./managed.js";
import {
  aggregateSummary,
  artifactKey,
  BUILD_ROLE_OUTPUT_KEY,
  desiredConfiguration,
  logGroupOf,
  type PublishSettings,
  type PublishSummary,
  publishSummary,
  requireEfsCombination,
  sizedSettings,
  type UploadedArtifact,
} from "./publish-domain.js";

const RECENT_LOG_STREAMS = 3;
const RECENT_LOG_EVENTS = 200;

/** Un evento de CloudWatch Logs del build: sólo salida de Dockerfile, nunca contenido de un sandbox. */
export interface BuildLogEvent {
  readonly timestamp: number;
  readonly message: string;
}

export interface BuildLogStream {
  readonly name: string;
  readonly events: readonly BuildLogEvent[];
}

/** Los logs recientes de un grupo, o el código de AWS por el que no se pudieron leer. */
export type RecentLogs =
  | { readonly kind: "streams"; readonly streams: readonly BuildLogStream[] }
  | { readonly kind: "unavailable"; readonly code: string };

/** Lo que la publicación necesita de AWS, además del puerto de construcción de imágenes. */
export interface PublishPorts extends ImageBuildClients {
  readonly region: string;
  accountId(): Promise<string>;
  /** La salida `BuildRoleArn` de la pila `stackName`; `undefined` si la pila no la tiene. */
  buildRoleArnOfStack(stackName: string): Promise<string | undefined>;
  latestBuild(arn: string, version: string): Promise<Record<string, unknown>>;
  /** La versión más reciente de la imagen base gestionada `arn` (`list-managed-microvm-image-versions`); `undefined` si no lista ninguna. */
  newestManagedVersion(arn: string): Promise<string | undefined>;
  recentLogs(group: string, streams: number, events: number): Promise<RecentLogs>;
}

/** Lo que la publicación lee del artefacto en disco. */
export interface ArtifactFiles {
  exists(path: string): boolean;
  read(path: string): Uint8Array;
  inspect(path: string): { readonly variant: string; readonly withEfs: boolean };
}

export interface PublishDeps {
  readonly ports: PublishPorts;
  readonly artifacts: ArtifactFiles;
  readonly sleep: (ms: number) => Promise<void>;
  /** Reloj monótono en milisegundos. */
  readonly now: () => number;
  /** Una línea de progreso. */
  readonly emit: (message: string) => void;
  readonly utcNow: () => Date;
}

/** La publicación no puede seguir: artefacto ausente o de otra variante, o sin rol de build (salida 1). */
export class ImagePublishError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ImagePublishError";
  }
}

/**
 * El marcador dentro del zip decide qué es la imagen; la opción sólo la
 * nombra. Un desacuerdo publicaría un build `full` con el nombre de `slim` o
 * `poly` (o al revés), así que se corta antes de la primera llamada a AWS.
 */
export function requireMatchingVariant(settings: PublishSettings, artifacts: ArtifactFiles): void {
  if (!artifacts.exists(settings.artifact)) {
    throw new ImagePublishError(
      `${settings.artifact} no existe: construye el zip con \`rayito image zip\` primero`,
    );
  }
  const found = artifacts.inspect(settings.artifact);
  if (found.variant !== settings.variant) {
    throw new ImagePublishError(
      `${settings.artifact} es un artefacto ${found.variant} pero se pasó --variant ` +
        `${settings.variant}: constrúyelo con \`rayito image zip --variant ${settings.variant}\``,
    );
  }
  if (found.withEfs && !settings.withEfs) {
    throw new ImagePublishError(
      `${settings.artifact} lleva el marcador de amazon-efs-utils: publícalo con --with-efs (o ` +
        "reconstrúyelo sin `rayito image zip --with-efs`)",
    );
  }
  if (settings.withEfs && !found.withEfs) {
    throw new ImagePublishError(
      `se pasó --with-efs pero ${settings.artifact} no lleva el marcador de amazon-efs-utils: ` +
        "constrúyelo con `rayito image zip --with-efs`",
    );
  }
  if (settings.withEfs) {
    requireEfsCombination(settings.variant, settings.osCapabilities);
  }
}

async function imageArnOf(ports: PublishPorts, imageName: string): Promise<string> {
  return accountImageArn(ports.region, await ports.accountId(), imageName);
}

async function buildRoleArnOf(ports: PublishPorts, settings: PublishSettings): Promise<string> {
  if (settings.buildRoleArn) {
    return settings.buildRoleArn;
  }
  const fromStack = await ports.buildRoleArnOfStack(settings.stackName);
  if (fromStack === undefined) {
    throw new ImagePublishError(
      `la pila ${settings.stackName} no tiene la salida ${BUILD_ROLE_OUTPUT_KEY}: pasa --build-role-arn`,
    );
  }
  return fromStack;
}

async function uploadArtifact(
  deps: PublishDeps,
  settings: PublishSettings,
): Promise<UploadedArtifact> {
  const payload = deps.artifacts.read(settings.artifact);
  const digest = createHash("sha256").update(payload).digest("hex");
  const key = artifactKey(digest, settings.artifactRunId);
  const uri = `s3://${settings.bucket}/${key}`;
  const uploaded = await uploadIfAbsent(deps.ports, settings.bucket, key, payload);
  deps.emit(
    uploaded
      ? `subido ${uri} (${payload.length} bytes)`
      : `el artefacto ya está en S3, no se vuelve a subir: ${uri}`,
  );
  return { uri, uploaded };
}

interface PreparedBuild {
  readonly arn: string;
  readonly artifact: UploadedArtifact;
  readonly desired: Record<string, unknown>;
}

/** Sube el artefacto (si hace falta) y arma la configuración deseada; no decide si hay que construir. */
async function prepareBuild(deps: PublishDeps, settings: PublishSettings): Promise<PreparedBuild> {
  const arn = await imageArnOf(deps.ports, settings.imageName);
  const artifact = await uploadArtifact(deps, settings);
  const desired = desiredConfiguration({
    region: deps.ports.region,
    settings,
    artifactUri: artifact.uri,
    buildRoleArn: await buildRoleArnOf(deps.ports, settings),
  });
  return { arn, artifact, desired };
}

/** La versión lanzable más nueva que ya coincide, o `undefined` si no hay ninguna o `force` la ignora. */
async function reusableVersion(
  ports: PublishPorts,
  settings: PublishSettings,
  prepared: PreparedBuild,
): Promise<string | undefined> {
  if (settings.force || (await ports.getMicrovmImage(prepared.arn)) === undefined) {
    return undefined;
  }
  return findReusableVersion(ports, prepared.arn, prepared.desired);
}

async function reusableSummary(
  deps: PublishDeps,
  settings: PublishSettings,
  prepared: PreparedBuild,
): Promise<PublishSummary | undefined> {
  const existing = await reusableVersion(deps.ports, settings, prepared);
  if (existing === undefined) {
    return undefined;
  }
  deps.emit(
    `${settings.imageName}: la versión ${existing} ya está construida con este artefacto y esta ` +
      "configuración; se reutiliza",
  );
  const build = await deps.ports.latestBuild(prepared.arn, existing);
  return publishSummary({
    arn: prepared.arn,
    version: existing,
    artifact: prepared.artifact,
    buildSeconds: undefined,
    build,
    gate: undefined,
  });
}

async function submitBuild(
  deps: PublishDeps,
  settings: PublishSettings,
  prepared: PreparedBuild,
): Promise<{ arn: string; version: string }> {
  const request = { ...prepared.desired, description: `rayd ${isoUtc(deps.utcNow())}` };
  const submitted = await submitImageBuild(deps.ports, settings.imageName, prepared.arn, request);
  const operation = submitted.created ? "create" : "update";
  deps.emit(`${operation}-microvm-image aceptado: versión ${submitted.version}`);
  return { arn: submitted.arn, version: submitted.version };
}

async function printRecentLogs(deps: PublishDeps, group: string): Promise<void> {
  const recent = await deps.ports.recentLogs(group, RECENT_LOG_STREAMS, RECENT_LOG_EVENTS);
  if (recent.kind === "unavailable") {
    deps.emit(`sin logs de build en ${group}: ${recent.code}`);
    return;
  }
  for (const stream of recent.streams) {
    deps.emit(`===== ${group} / ${stream.name} (${stream.events.length} eventos)`);
    for (const event of stream.events) {
      deps.emit(`${event.timestamp} ${event.message.trimEnd()}`);
    }
  }
}

function gateFacts(gate: VersionGate) {
  return {
    imageState: gate.imageState,
    versionState: gate.versionState,
    versionStatus: gate.versionStatus,
    stateReason: gate.stateReason,
    launchable: gate.launchable,
  };
}

function reprReason(reason: string | null | undefined): string {
  return reason === undefined || reason === null ? "None" : pythonStringRepr(reason);
}

/**
 * Espera el gate de una construcción ya sometida y arma su resumen; si no
 * quedó lanzable, informa el motivo y la cola de logs.
 */
async function finalizeBuild(
  deps: PublishDeps,
  settings: PublishSettings,
  submitted: { arn: string; version: string },
  prepared: PreparedBuild,
  started: number,
): Promise<PublishSummary> {
  const { gate } = await waitForGate(
    deps.ports,
    submitted.arn,
    submitted.version,
    settings.timeoutMs,
    deps.sleep,
    {
      now: deps.now,
      onTick: (read, elapsedMs) =>
        deps.emit(
          `  image=${read.imageState} version=${submitted.version} state=${read.versionState} ` +
            `status=${read.versionStatus} (${Math.round(elapsedMs / MS_PER_SECOND)}s)`,
        ),
    },
  );
  const buildSeconds = (deps.now() - started) / MS_PER_SECOND;
  const build = await deps.ports.latestBuild(submitted.arn, submitted.version);
  const summary = publishSummary({
    arn: submitted.arn,
    version: submitted.version,
    artifact: prepared.artifact,
    buildSeconds,
    build,
    gate: gateFacts(gate),
  });
  if (!gate.launchable) {
    deps.emit(
      `${settings.imageName}: el build no quedó lanzable: imagen=${gate.imageState} ` +
        `versión=${gate.versionState}/${gate.versionStatus} stateReason=${reprReason(gate.stateReason)} ` +
        `buildStateReason=${reprReason(build.stateReason as string | null | undefined)}`,
    );
    await printRecentLogs(deps, logGroupOf(settings));
  }
  return summary;
}

/**
 * Construye o reutiliza una única versión de `settings.imageName`, sin
 * imprimir el documento final: `summary.launchable` dice si quedó lanzable.
 */
export async function buildOrReuse(
  deps: PublishDeps,
  settings: PublishSettings,
): Promise<PublishSummary> {
  const prepared = await prepareBuild(deps, settings);
  const reused = await reusableSummary(deps, settings, prepared);
  if (reused !== undefined) {
    return reused;
  }
  const started = deps.now();
  const submitted = await submitBuild(deps, settings, prepared);
  return finalizeBuild(deps, settings, submitted, prepared, started);
}

/** Con `--base-image-version latest` se publica sobre la versión más reciente de la base gestionada. */
export const LATEST_BASE_IMAGE_VERSION = "latest";

/**
 * La `baseImageVersion` sobre la que se publica: `requested` tal cual, o, con
 * `latest`, la más reciente que liste la base gestionada. Quien llama imprime
 * el valor resuelto, de modo que el cambio de la base gestionada nunca pasa en
 * silencio; un número explícito es lo que fija una build reproducible.
 */
export async function resolveBaseImageVersion(
  ports: PublishPorts,
  requested: string,
): Promise<string> {
  if (requested !== LATEST_BASE_IMAGE_VERSION) {
    return requested;
  }
  const newest = await ports.newestManagedVersion(baseImageArn(ports.region));
  if (newest === undefined) {
    throw new ImagePublishError(
      "la base gestionada no lista ninguna versión: pasa --base-image-version con una versión " +
        "de list-managed-microvm-image-versions",
    );
  }
  return newest;
}

/** `rayito image publish` sin `--sizes`: el baseline, tras comprobar que el artefacto casa con la variante. */
export async function publish(
  deps: PublishDeps,
  settings: PublishSettings,
): Promise<PublishSummary> {
  requireMatchingVariant(settings, deps.artifacts);
  return buildOrReuse(deps, settings);
}

interface PendingBuild {
  readonly sizeName: SizeName;
  readonly settings: PublishSettings;
  readonly prepared: PreparedBuild;
  readonly submitted: { arn: string; version: string };
  readonly started: number;
}

/**
 * Construye o reutiliza, desde el mismo artefacto, una imagen por cada
 * tamaño de `sizeNames` (nunca el baseline). Cada oleada de a lo sumo
 * `MAX_CONCURRENT_IMAGE_BUILDS` se somete entera antes de esperar a que
 * ninguna se asiente, así AWS las construye en paralelo.
 */
async function publishSizes(
  deps: PublishDeps,
  base: PublishSettings,
  sizeNames: readonly SizeName[],
): Promise<Record<string, PublishSummary>> {
  const summaries: Record<string, PublishSummary> = {};
  for (let start = 0; start < sizeNames.length; start += MAX_CONCURRENT_IMAGE_BUILDS) {
    const pending: PendingBuild[] = [];
    for (const sizeName of sizeNames.slice(start, start + MAX_CONCURRENT_IMAGE_BUILDS)) {
      const settings = sizedSettings(base, sizeName);
      const prepared = await prepareBuild(deps, settings);
      const reused = await reusableSummary(deps, settings, prepared);
      if (reused !== undefined) {
        summaries[sizeName] = reused;
        continue;
      }
      const started = deps.now();
      const submitted = await submitBuild(deps, settings, prepared);
      pending.push({ sizeName, settings, prepared, submitted, started });
    }
    for (const build of pending) {
      summaries[build.sizeName] = await finalizeBuild(
        deps,
        build.settings,
        build.submitted,
        build.prepared,
        build.started,
      );
    }
  }
  return summaries;
}

export interface SizedPublication {
  readonly baseline: PublishSummary;
  readonly sized: Readonly<Record<string, PublishSummary>>;
  /** `true` si el baseline o algún tamaño no quedó lanzable. */
  readonly failed: boolean;
  /** El documento único de `--json`. */
  readonly document: Record<string, unknown>;
}

/**
 * `rayito image publish --sizes`: el baseline y, si quedó lanzable, una imagen
 * adicional por tamaño; todo se informa en un único bloque, nunca uno por
 * imagen.
 */
export async function publishWithSizes(
  deps: PublishDeps,
  settings: PublishSettings,
  sizeNames: readonly SizeName[],
): Promise<SizedPublication> {
  requireMatchingVariant(settings, deps.artifacts);
  const baseline = await buildOrReuse(deps, settings);
  const sized = baseline.launchable ? await publishSizes(deps, settings, sizeNames) : {};
  const failed =
    !baseline.launchable || Object.values(sized).some((summary) => !summary.launchable);
  return { baseline, sized, failed, document: aggregateSummary(baseline, sized) };
}
