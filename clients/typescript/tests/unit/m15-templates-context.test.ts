/**
 * `src/templates/context.ts`/`context-node.ts` (m15-templates): espejo de
 * `test_m15_templates_context.py`.
 */

import { mkdir, mkdtemp, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, relative } from "node:path";
import { describe, expect, test } from "vitest";
import { BuildError } from "../../src/errors.js";
import { DockerIgnore, filesHash } from "../../src/templates/context.js";
import { collectContextFiles } from "../../src/templates/context-node.js";
import type { CopyStep } from "../../src/templates/instructions.js";

function copy(src: string, dst: string): CopyStep {
  return { kind: "copy", src, dst };
}

describe("templates/context", () => {
  test("DockerIgnore matches simple globs and negation", () => {
    const ignore = DockerIgnore.fromText("*.pyc\n!keep.pyc\n");
    expect(ignore.matches("a.pyc")).toBe(true);
    expect(ignore.matches("keep.pyc")).toBe(false);
    expect(ignore.matches("a.py")).toBe(false);
  });

  test("collectContextFiles reads a single file and a directory, filtered by .dockerignore", async () => {
    const dir = await mkdtemp(join(tmpdir(), "rayito-templates-"));
    await mkdir(join(dir, "app", "__pycache__"), { recursive: true });
    await writeFile(join(dir, "app", "main.py"), "print(1)");
    await writeFile(join(dir, "app", "__pycache__", "main.pyc"), Buffer.from([0]));
    await writeFile(join(dir, "requirements.txt"), "pandas\n");
    await writeFile(join(dir, ".dockerignore"), "**/__pycache__/*\n__pycache__/*\n");

    const entries = await collectContextFiles(dir, [
      copy("app/", "/srv/app/"),
      copy("requirements.txt", "/srv/requirements.txt"),
    ]);
    const paths = entries.map(([path]) => path);
    expect(paths).toContain("app/main.py");
    expect(paths).toContain("requirements.txt");
    expect(paths.some((path) => path.includes("pyc"))).toBe(false);
    expect(paths).toEqual([...paths].sort());
  });

  test("collectContextFiles raises BuildError for a missing source", async () => {
    const dir = await mkdtemp(join(tmpdir(), "rayito-templates-"));
    await expect(collectContextFiles(dir, [copy("missing/", "/srv/missing/")])).rejects.toThrow(
      BuildError,
    );
  });

  test("filesHash is order-independent and content-sensitive", () => {
    const a: Array<[string, Uint8Array]> = [
      ["x.py", new TextEncoder().encode("1")],
      ["y.py", new TextEncoder().encode("2")],
    ];
    const b: Array<[string, Uint8Array]> = [
      a[1] as [string, Uint8Array],
      a[0] as [string, Uint8Array],
    ];
    const c: Array<[string, Uint8Array]> = [
      a[0] as [string, Uint8Array],
      ["y.py", new TextEncoder().encode("3")],
    ];
    expect(filesHash(a)).toBe(filesHash(b));
    expect(filesHash(a)).not.toBe(filesHash(c));
  });

  test("collectContextFiles accepts a relative contextDir, like the default '.'", async () => {
    const dir = await mkdtemp(join(tmpdir(), "rayito-templates-"));
    await mkdir(join(dir, "app"), { recursive: true });
    await writeFile(join(dir, "app", "main.py"), "print(1)");

    const entries = await collectContextFiles(relative(process.cwd(), dir), [
      copy("app/", "/srv/app/"),
    ]);

    expect(entries.map(([path]) => path)).toEqual(["app/main.py"]);
  });

  test("collectContextFiles rejects a source outside the context", async () => {
    const dir = await mkdtemp(join(tmpdir(), "rayito-templates-"));
    await mkdir(join(dir, "context"));
    await writeFile(join(dir, "secret.txt"), "outside");

    await expect(
      collectContextFiles(join(dir, "context"), [copy("../secret.txt", "/srv/secret.txt")]),
    ).rejects.toMatchObject({ reason: "context_path_outside" });
  });

  test("collectContextFiles skips file and directory symlinks that point outside", async () => {
    const dir = await mkdtemp(join(tmpdir(), "rayito-templates-"));
    await mkdir(join(dir, "outside", "dir"), { recursive: true });
    await writeFile(join(dir, "outside", "secret"), "AWS_SECRET=shh\n");
    await writeFile(join(dir, "outside", "dir", "inner"), "INNER\n");
    await mkdir(join(dir, "ctx", "app"), { recursive: true });
    await writeFile(join(dir, "ctx", "app", "main.py"), "print(1)\n");
    await symlink(join(dir, "outside", "secret"), join(dir, "ctx", "app", "config"));
    await symlink(join(dir, "outside", "dir"), join(dir, "ctx", "app", "linked"), "dir");

    const entries = await collectContextFiles(join(dir, "ctx"), [copy("app", "/app")]);

    expect(entries.map(([path]) => path)).toEqual(["app/main.py"]);
  });

  test("collectContextFiles skips symlinks inside the context too (same rule as Python)", async () => {
    const dir = await mkdtemp(join(tmpdir(), "rayito-templates-"));
    await mkdir(join(dir, "app"));
    await writeFile(join(dir, "app", "real.txt"), "real");
    await symlink(join(dir, "app", "real.txt"), join(dir, "app", "alias.txt"));

    const entries = await collectContextFiles(dir, [copy("app", "/app")]);

    expect(entries.map(([path]) => path)).toEqual(["app/real.txt"]);
  });
});
