/**
 * La SPA del panel tiene una sola fuente (ADR-027, D4): la del paquete de
 * Python. El build la copia a `dist/dashboard-static/` y `pack:check` falla si
 * el tarball difiere. Aquí se prueba el comparador y que la fuente existe.
 */

import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

const SCRIPT = new URL("../../../scripts/dashboard-static.mjs", import.meta.url).href;

interface StaticScript {
  readonly SOURCE_DIR: string;
  listFiles(directory: string): string[];
  compareDirectories(source: string, copy: string): string[];
}

let script: StaticScript;
let root = "";

function write(directory: string, path: string, content: string): void {
  const target = join(directory, path);
  mkdirSync(join(target, ".."), { recursive: true });
  writeFileSync(target, content);
}

beforeEach(async () => {
  script = (await import(SCRIPT)) as StaticScript;
  root = mkdtempSync(join(tmpdir(), "rayito-static-"));
  write(join(root, "source"), "index.html", "<html/>");
  write(join(root, "source"), "js/app.js", "export {};");
  write(join(root, "copy"), "index.html", "<html/>");
  write(join(root, "copy"), "js/app.js", "export {};");
});

afterEach(() => {
  rmSync(root, { recursive: true, force: true });
});

describe("compareDirectories", () => {
  it("an identical copy has no problems", () => {
    expect(script.compareDirectories(join(root, "source"), join(root, "copy"))).toEqual([]);
  });

  it("reports a missing, an extra and a differing file", () => {
    rmSync(join(root, "copy", "js", "app.js"));
    write(join(root, "copy"), "css/extra.css", "a {}");
    write(join(root, "copy"), "index.html", "<html>otro</html>");
    expect(script.compareDirectories(join(root, "source"), join(root, "copy"))).toEqual([
      "falta js/app.js",
      "sobra css/extra.css",
      "difiere index.html",
    ]);
  });

  it("lists nested files with forward slashes, sorted", () => {
    expect(script.listFiles(join(root, "source"))).toEqual(["index.html", "js/app.js"]);
  });
});

describe("the canonical SPA", () => {
  it("is the one in the Python package", () => {
    expect(script.SOURCE_DIR).toContain(join("python", "src", "rayito", "_dashboard", "_static"));
    expect(script.listFiles(script.SOURCE_DIR)).toEqual(
      expect.arrayContaining(["index.html", "js/app.js", "css/app.css", "css/tokens.css"]),
    );
  });
});
