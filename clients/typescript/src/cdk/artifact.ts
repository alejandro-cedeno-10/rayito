/**
 * `MicrovmImageArtifact` (ADR-032 D9.1 y D9.5): el zip de la imagen que
 * `RayitoMicrovmImage` publica, siempre un fichero local y explícito para
 * que la síntesis no lea nada de AWS. `fromFile` toma un zip que ya tienes
 * (con su sha256 si lo sabes); `downloadRelease` descarga
 * `rayito-image.zip` y `SHA256SUMS` de la release `rayd-v<versión>` **antes**
 * de construir el stack, los verifica con la misma regla que `rayito setup`
 * y los deja en un directorio que las síntesis siguientes reutilizan sin red.
 * No importa `aws-cdk-lib`.
 */

import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import {
  AccountSetupError,
  CHECKSUMS_ASSET_NAME,
  type DownloadedRelease,
  IMAGE_ASSET_NAME,
  releaseTag,
  verifyChecksum,
} from "../account-setup/domain.js";
import type { ReleaseArtifactSource } from "../account-setup/ports.js";
import { GithubReleaseSource } from "../account-setup/release.js";
import { VERSION } from "../version.js";
import { RayitoCdkError } from "./errors.js";

/** Donde `downloadRelease` guarda la release por defecto: `.rayito/releases/<tag>/` bajo el directorio de trabajo. */
export const RELEASES_DIRECTORY = join(".rayito", "releases");

/** Las opciones de `MicrovmImageArtifact.fromFile`. */
export interface FromFileOptions {
  /** El sha256 esperado del zip (64 hex); si no coincide, `artifact_checksum_mismatch` al crearlo. */
  readonly sha256?: string | undefined;
}

/** Las opciones de `MicrovmImageArtifact.downloadRelease`. */
export interface DownloadReleaseOptions {
  /** La versión de `rayd` (sin `v`); por defecto, la del SDK, como `rayito setup`. */
  readonly version?: string | undefined;
  /** Dónde guardar (y buscar) los dos ficheros; por defecto `.rayito/releases/rayd-v<versión>/`. */
  readonly directory?: string | undefined;
}

let releaseSourceFactory: () => ReleaseArtifactSource = () => new GithubReleaseSource();

/**
 * Sustituye la fuente de la release de `downloadRelease` (como
 * `release_source_factory` en Python) y devuelve la que había. Sólo para los
 * tests: `rayito/cdk` (`index.ts`) no la reexporta.
 */
export function replaceReleaseSourceFactory(
  factory: () => ReleaseArtifactSource,
): () => ReleaseArtifactSource {
  const previous = releaseSourceFactory;
  releaseSourceFactory = factory;
  return previous;
}

function sha256Of(path: string): string {
  return createHash("sha256").update(readFileSync(path)).digest("hex");
}

function isFile(path: string): boolean {
  return existsSync(path) && statSync(path).isFile();
}

function verify(release: DownloadedRelease, tag: string): void {
  try {
    verifyChecksum(release, tag);
  } catch (error) {
    if (error instanceof AccountSetupError) {
      throw new RayitoCdkError("artifact_checksum_mismatch", { path: release.artifact });
    }
    throw error;
  }
}

function downloadFailed(error: AccountSetupError, tag: string): RayitoCdkError {
  return new RayitoCdkError(
    "release_download_failed",
    {
      asset: IMAGE_ASSET_NAME,
      release: tag,
      reason: String(error.fields.reason ?? error.message),
    },
    { cause: error },
  );
}

/**
 * El zip de una imagen de Rayito para `RayitoMicrovmImage`.
 *
 * Coste y activación
 * -------------------
 * Activa: pasarlo a `new RayitoMicrovmImage(...)`; por sí solo no crea nada.
 * Recursos y llamadas AWS: ninguno. `downloadRelease` descarga dos ficheros
 *     de las releases públicas del repositorio por HTTPS, una sola vez.
 * Coste aproximado: gratis.
 * IAM: ninguno.
 * Cómo apagarla: no usarlo.
 * Ejemplo:
 *   const artifact = await MicrovmImageArtifact.downloadRelease();
 *   new RayitoMicrovmImage(stack, "Image", { name: "team-sandbox", artifact, baseImageVersion: "1" });
 */
export class MicrovmImageArtifact {
  /** La ruta absoluta del zip. */
  readonly path: string;
  /** El sha256 del zip, ya comprobado. */
  readonly sha256: string;

  private constructor(path: string, sha256: string) {
    this.path = path;
    this.sha256 = sha256;
  }

  /**
   * Un zip local (`rayito image zip`, o el `rayito-image.zip` de una
   * release): `artifact_not_found` si no existe y
   * `artifact_checksum_mismatch` si `sha256` no es el suyo.
   */
  static fromFile(path: string, options: FromFileOptions = {}): MicrovmImageArtifact {
    const artifact = resolve(path);
    if (!isFile(artifact)) {
      throw new RayitoCdkError("artifact_not_found", { path: artifact });
    }
    const digest = sha256Of(artifact);
    if (options.sha256 !== undefined && options.sha256.toLowerCase() !== digest) {
      throw new RayitoCdkError("artifact_checksum_mismatch", { path: artifact });
    }
    return new MicrovmImageArtifact(artifact, digest);
  }

  /**
   * `rayito-image.zip` de la release `rayd-v<version>` (por defecto la del
   * SDK, como `rayito setup`) verificado contra su `SHA256SUMS`. Se guarda con
   * `SHA256SUMS` en `directory` (por defecto `.rayito/releases/<release>/`) y
   * la siguiente síntesis lo reutiliza sin red, verificándolo otra vez.
   * Llamarla antes de crear el stack: la síntesis no espera promesas.
   */
  static async downloadRelease(
    options: DownloadReleaseOptions = {},
  ): Promise<MicrovmImageArtifact> {
    const tag = releaseTag(options.version || VERSION);
    const target = resolve(options.directory ?? join(RELEASES_DIRECTORY, tag));
    const artifact = join(target, IMAGE_ASSET_NAME);
    const checksums = join(target, CHECKSUMS_ASSET_NAME);
    if (isFile(artifact) && isFile(checksums)) {
      const release: DownloadedRelease = {
        checksumsText: readFileSync(checksums, "utf8"),
        artifact,
        artifactSha256: sha256Of(artifact),
      };
      verify(release, tag);
      return new MicrovmImageArtifact(artifact, release.artifactSha256);
    }
    mkdirSync(target, { recursive: true });
    let release: DownloadedRelease;
    try {
      release = await releaseSourceFactory().fetch(tag, target, () => {});
    } catch (error) {
      throw error instanceof AccountSetupError ? downloadFailed(error, tag) : error;
    }
    try {
      verify(release, tag);
    } catch (error) {
      rmSync(release.artifact, { force: true });
      throw error;
    }
    writeFileSync(checksums, release.checksumsText);
    return new MicrovmImageArtifact(release.artifact, release.artifactSha256);
  }
}
