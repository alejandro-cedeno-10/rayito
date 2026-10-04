/**
 * Las cadenas que el llamante entrega al SDK (una URL de `git.clone`, la ruta
 * de `files.download`, un access token, una línea de `.dockerignore`) se
 * recortan y trocean en tiempo lineal: ninguna expresión regular con un
 * cuantificador anclado al final (`/\/+$/`, `/=+$/`) ni la antigua
 * `HTTP_URL` con `(.*)$` retrocede en tiempo cuadrático. Con la versión
 * anterior, 50 000 caracteres bloqueaban el bucle de eventos más de un
 * segundo (medido en Node 22; 500 000, unos dos minutos).
 *
 * Además fija que la semántica no cambia: una URL cuya cola lleva un salto
 * de línea sigue sin reconocerse como http(s), igual que antes.
 */

import { describe, expect, test } from "vitest";
import { InvalidArgumentError } from "../../src/errors.js";
import { ACCESS_TOKEN_MIN_BYTES } from "../../src/limits.js";
import { decodeAccessToken, encodeAccessToken } from "../../src/payload.js";
import {
  deriveRepoDirFromUrl,
  stripCredentials,
  withCredentials,
} from "../../src/sandbox/git-args.js";
import { downloadFilename } from "../../src/sandbox/transfer.js";
import { stripLeading, stripTrailing } from "../../src/strings.js";
import { DockerIgnore } from "../../src/templates/context.js";

/** Lo bastante largo para que un recorrido cuadrático tarde más de 1 s. */
const ADVERSARIAL_LENGTH = 50_000;
/**
 * Un recorrido lineal de 50 000 caracteres tarda menos de 1 ms; el margen
 * absorbe un runner de CI lento sin dejar pasar el caso cuadrático (> 1 s).
 */
const LINEAR_BUDGET_MS = 150;

function elapsedMs(work: () => unknown): number {
  const started = performance.now();
  work();
  return performance.now() - started;
}

describe("linear-time trimming and splitting (CodeQL js/polynomial-redos)", () => {
  test("downloadFilename trims trailing slashes in linear time", () => {
    const path = `${"/".repeat(ADVERSARIAL_LENGTH)}x`;
    expect(elapsedMs(() => downloadFilename(path, undefined))).toBeLessThan(LINEAR_BUDGET_MS);
    expect(downloadFilename("/home/user/report.csv///", undefined)).toBe("report.csv");
    expect(downloadFilename("///", undefined)).toBe("download");
  });

  test("deriveRepoDirFromUrl trims trailing slashes in linear time", () => {
    const url = `https://example.com/${"/".repeat(ADVERSARIAL_LENGTH)}x`;
    expect(elapsedMs(() => deriveRepoDirFromUrl(url))).toBeLessThan(LINEAR_BUDGET_MS);
    expect(deriveRepoDirFromUrl("https://example.com/acme/app.git///")).toBe("app");
  });

  test("decodeAccessToken strips padding in linear time", () => {
    const token = `${"=".repeat(ADVERSARIAL_LENGTH)}x`;
    expect(
      elapsedMs(() => expect(() => decodeAccessToken(token)).toThrow(InvalidArgumentError)),
    ).toBeLessThan(LINEAR_BUDGET_MS);
    const secret = new Uint8Array(ACCESS_TOKEN_MIN_BYTES + 1).fill(7);
    expect(decodeAccessToken(`${encodeAccessToken(secret)}==`)).toEqual(secret);
  });

  test("withCredentials and stripCredentials split the URL in linear time", () => {
    const url = `http://${"a".repeat(ADVERSARIAL_LENGTH)}/\nb`;
    expect(elapsedMs(() => stripCredentials(url))).toBeLessThan(LINEAR_BUDGET_MS);
    expect(
      elapsedMs(() =>
        expect(() => withCredentials(url, "bot", "pw")).toThrow(InvalidArgumentError),
      ),
    ).toBeLessThan(LINEAR_BUDGET_MS);
  });

  test("a line terminator after the authority still means 'not an http(s) URL'", () => {
    for (const terminator of ["\n", "\r", " ", " "]) {
      const url = `https://bot:pw@example.com/acme${terminator}app.git`;
      expect(stripCredentials(url)).toBe(url);
      expect(() => withCredentials(url, "bot", "pw")).toThrow(InvalidArgumentError);
    }
    expect(stripCredentials("https://bot:pw@example.com/acme/app.git")).toBe(
      "https://example.com/acme/app.git",
    );
    expect(withCredentials("https://example.com/acme/app.git", "bot", "p@ss")).toBe(
      "https://bot:p%40ss@example.com/acme/app.git",
    );
  });

  test("a .dockerignore line is trimmed of slashes in linear time", () => {
    const line = `x${"/".repeat(ADVERSARIAL_LENGTH)}x`;
    expect(elapsedMs(() => DockerIgnore.fromText(line))).toBeLessThan(LINEAR_BUDGET_MS);
    expect(DockerIgnore.fromText("//build//\n!/keep/").matches("build/out.o")).toBe(true);
  });

  test("stripLeading and stripTrailing remove only runs of the given character", () => {
    expect(stripTrailing("a///", "/")).toBe("a");
    expect(stripTrailing("///", "/")).toBe("");
    expect(stripTrailing("a/b", "/")).toBe("a/b");
    expect(stripLeading("///a/", "/")).toBe("a/");
    expect(stripLeading("", "/")).toBe("");
  });
});
