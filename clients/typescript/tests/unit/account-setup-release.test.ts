/**
 * `GithubReleaseSource` con un `fetch` falso: las URL exactas, el zip escrito en
 * streaming con su sha256, y los tres motivos de fallo sin la URL en el mensaje.
 */

import { createHash } from "node:crypto";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { AccountSetupError, releaseAssetUrl } from "../../src/account-setup/domain.js";
import { type FetchFunction, GithubReleaseSource } from "../../src/account-setup/release.js";

const TAG = "rayd-v0.14.0";
const CHECKSUMS = "1111  rayd\n";
const ZIP_CHUNKS = [Buffer.from("PK-first-"), Buffer.from("second")];
const HTTP_NOT_FOUND = 404;
const SHORT_TIMEOUT_MS = 5;

const directories: string[] = [];
afterEach(() => {
  for (const directory of directories.splice(0)) {
    rmSync(directory, { recursive: true, force: true });
  }
});

function workdir(): string {
  const directory = mkdtempSync(join(tmpdir(), "rayito-release-test-"));
  directories.push(directory);
  return directory;
}

function streamOf(chunks: readonly Buffer[]): ReadableStream<Uint8Array> {
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) {
        controller.enqueue(new Uint8Array(chunk));
      }
      controller.close();
    },
  });
}

function serving(requested: string[]): FetchFunction {
  return async (url) => {
    requested.push(url);
    const body = url.endsWith("SHA256SUMS") ? [Buffer.from(CHECKSUMS)] : ZIP_CHUNKS;
    return new Response(streamOf(body), { status: 200 });
  };
}

async function failureOf(source: GithubReleaseSource): Promise<AccountSetupError> {
  const error = await source
    .fetch(TAG, workdir(), () => undefined)
    .catch((caught: unknown) => caught);
  expect(error).toBeInstanceOf(AccountSetupError);
  return error as AccountSetupError;
}

describe("GithubReleaseSource", () => {
  it("downloads SHA256SUMS and streams the zip to disk with its sha256", async () => {
    const requested: string[] = [];
    const directory = workdir();
    const lines: string[] = [];
    const release = await new GithubReleaseSource({ fetch: serving(requested) }).fetch(
      TAG,
      directory,
      (line) => lines.push(line),
    );
    expect(requested).toEqual([
      releaseAssetUrl(TAG, "SHA256SUMS"),
      releaseAssetUrl(TAG, "rayito-image.zip"),
    ]);
    const bytes = Buffer.concat(ZIP_CHUNKS);
    expect(release.checksumsText).toBe(CHECKSUMS);
    expect(release.artifact).toBe(join(directory, "rayito-image.zip"));
    expect(readFileSync(release.artifact)).toEqual(bytes);
    expect(release.artifactSha256).toBe(createHash("sha256").update(bytes).digest("hex"));
    expect(lines).toEqual([
      `descargando SHA256SUMS de ${TAG}`,
      `descargando rayito-image.zip de ${TAG}`,
    ]);
  });

  it("an HTTP error is release_download with the status and no URL", async () => {
    const source = new GithubReleaseSource({
      fetch: async () => new Response("no", { status: HTTP_NOT_FOUND }),
    });
    const error = await failureOf(source);
    expect(error.code).toBe("release_download");
    expect(error.message).toContain(`HTTP ${HTTP_NOT_FOUND}`);
    expect(error.message).not.toContain("https://");
  });

  it("a network failure is release_download with 'error de red'", async () => {
    const source = new GithubReleaseSource({
      fetch: async () => {
        throw new TypeError("fetch failed");
      },
    });
    const error = await failureOf(source);
    expect(error.message).toContain("error de red");
    expect(error.message).not.toContain("https://");
  });

  it("a deadline is release_download with 'tiempo agotado'", async () => {
    const hanging: FetchFunction = (_url, init) =>
      new Promise((_resolve, reject) => {
        init.signal.addEventListener("abort", () => reject(init.signal.reason));
      });
    const error = await failureOf(
      new GithubReleaseSource({ fetch: hanging, timeoutMs: SHORT_TIMEOUT_MS }),
    );
    expect(error.code).toBe("release_download");
    expect(error.message).toContain("tiempo agotado");
  });

  it("a response without a body is a network failure", async () => {
    const source = new GithubReleaseSource({
      fetch: async () => new Response(null, { status: 200 }),
    });
    expect((await failureOf(source)).message).toContain("error de red");
  });
});
