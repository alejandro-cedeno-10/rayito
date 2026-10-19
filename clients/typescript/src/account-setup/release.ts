/**
 * `GithubReleaseSource` (ADR-030, D5): descarga `SHA256SUMS` y
 * `rayito-image.zip` de la release `rayd-v<versión>` por HTTPS con el `fetch`
 * global de Node, el zip en streaming a disco y con su sha256 calculado al
 * escribirlo. Espejo de `rayito._account_setup._release`. Los mensajes de error
 * nunca llevan la URL.
 */

import { createHash } from "node:crypto";
import { type FileHandle, open } from "node:fs/promises";
import { join } from "node:path";
import {
  AccountSetupError,
  CHECKSUMS_ASSET_NAME,
  DOWNLOAD_REASONS,
  type DownloadedRelease,
  formatTemplate,
  IMAGE_ASSET_NAME,
  PROGRESS_MESSAGES,
  RELEASE_DOWNLOAD_TIMEOUT_MS,
  releaseAssetUrl,
} from "./domain.js";
import type { Progress, ReleaseArtifactSource } from "./ports.js";

export type FetchFunction = (
  url: string,
  init: { readonly signal: AbortSignal },
) => Promise<Response>;

export interface GithubReleaseSourceOptions {
  readonly fetch?: FetchFunction;
  /** El plazo de cada descarga, en milisegundos. */
  readonly timeoutMs?: number;
}

const TIMEOUT_ERROR_NAMES: ReadonlySet<string> = new Set(["TimeoutError", "AbortError"]);

/** Un fallo de la descarga de un asset, ya con su motivo. */
class DownloadFailure extends Error {
  constructor(readonly reason: string) {
    super(reason);
    this.name = "DownloadFailure";
  }
}

function failureReason(error: unknown): string {
  if (error instanceof DownloadFailure) {
    return error.reason;
  }
  const name = error instanceof Error ? error.name : "";
  return TIMEOUT_ERROR_NAMES.has(name) ? DOWNLOAD_REASONS.timeout : DOWNLOAD_REASONS.network;
}

export class GithubReleaseSource implements ReleaseArtifactSource {
  readonly #fetch: FetchFunction;
  readonly #timeoutMs: number;

  constructor(options: GithubReleaseSourceOptions = {}) {
    this.#fetch = options.fetch ?? ((url, init) => globalThis.fetch(url, init));
    this.#timeoutMs = options.timeoutMs ?? RELEASE_DOWNLOAD_TIMEOUT_MS;
  }

  async fetch(tag: string, directory: string, onProgress: Progress): Promise<DownloadedRelease> {
    const checksums = await this.#download(tag, CHECKSUMS_ASSET_NAME, onProgress);
    const artifact = join(directory, IMAGE_ASSET_NAME);
    const zip = await this.#download(tag, IMAGE_ASSET_NAME, onProgress, artifact);
    return {
      checksumsText: checksums.text,
      artifact,
      artifactSha256: zip.sha256,
    };
  }

  /** Descarga `asset`; con `destination` lo escribe en disco, si no lo devuelve como texto. */
  async #download(
    tag: string,
    asset: string,
    onProgress: Progress,
    destination?: string,
  ): Promise<{ readonly text: string; readonly sha256: string }> {
    onProgress(formatTemplate(PROGRESS_MESSAGES.download, { asset, release: tag }));
    try {
      const response = await this.#fetch(releaseAssetUrl(tag, asset), {
        signal: AbortSignal.timeout(this.#timeoutMs),
      });
      if (!response.ok) {
        throw new DownloadFailure(
          formatTemplate(DOWNLOAD_REASONS.http, { status: response.status }),
        );
      }
      return await readBody(response, destination);
    } catch (error) {
      throw new AccountSetupError("release_download", {
        asset,
        release: tag,
        reason: failureReason(error),
      });
    }
  }
}

/**
 * `FileHandle.write` puede escribir menos bytes de los pedidos: se repite hasta
 * escribir el trozo entero, para que el fichero en disco sea exactamente lo que
 * se hasheó.
 */
async function writeFully(handle: FileHandle, chunk: Uint8Array): Promise<void> {
  let offset = 0;
  while (offset < chunk.byteLength) {
    const { bytesWritten } = await handle.write(chunk, offset, chunk.byteLength - offset);
    offset += bytesWritten;
  }
}

/** El cuerpo en trozos: calcula el sha256 y lo escribe en `destination` o lo junta como texto. */
async function readBody(
  response: Response,
  destination: string | undefined,
): Promise<{ readonly text: string; readonly sha256: string }> {
  if (response.body === null) {
    throw new DownloadFailure(DOWNLOAD_REASONS.network);
  }
  const hash = createHash("sha256");
  const chunks: Buffer[] = [];
  const handle = destination === undefined ? undefined : await open(destination, "w");
  try {
    const reader = response.body.getReader();
    for (;;) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      hash.update(value);
      if (handle === undefined) {
        chunks.push(Buffer.from(value));
      } else {
        await writeFully(handle, value);
      }
    }
  } finally {
    await handle?.close();
  }
  return { text: Buffer.concat(chunks).toString("utf8"), sha256: hash.digest("hex") };
}
