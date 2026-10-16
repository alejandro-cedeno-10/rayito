/**
 * `AgentProject.export()` (`agent/export.ts`): el árbol de ficheros y el
 * `Dockerfile` de cada caso de `testdata/agent/export/` (los dos SDK leen los
 * mismos), el rechazo de un directorio no vacío y que no hay ninguna llamada
 * de red ni cliente de AWS. Espejo de `test_agent_export.py`.
 */

import { mkdtempSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { afterEach, describe, expect, test, vi } from "vitest";
import {
  BASE_IMAGE_REFERENCE,
  EXPORT_PLATFORM,
  exportProject,
  renderExport,
} from "../../src/agent/export.js";
import { AgentProject } from "../../src/agent/project.js";
import { InvalidArgumentError } from "../../src/errors.js";
import { AGENT_SERVICE_EXPORT_PORT } from "../../src/limits.js";
import { FakeAgentRuntime } from "./agent-fake.js";
import { project } from "./agent-project-fake.js";

const EXPORT_DIR = resolve(__dirname, "../../../../testdata/agent/export");
const CASES = readdirSync(EXPORT_DIR)
  .filter((name) => {
    try {
      readFileSync(join(EXPORT_DIR, name, "case.json"));
      return true;
    } catch {
      return false;
    }
  })
  .sort();
const HOST_PREFIX = "agent_host/";
const SNAPSHOT_FILES = ["Dockerfile", "host.json", ".dockerignore", "README.md"] as const;

function loadCase(name: string): { project: AgentProject; target: string } {
  const caseDir = join(EXPORT_DIR, name);
  const raw = JSON.parse(readFileSync(join(caseDir, "case.json"), "utf8")) as {
    target: string;
    project: Record<string, unknown>;
  };
  return {
    project: AgentProject.fromObject(raw.project, { baseDir: caseDir }),
    target: raw.target,
  };
}

async function tree(name: string): Promise<Map<string, string | Uint8Array>> {
  const { project: the, target } = loadCase(name);
  return renderExport(the, target);
}

function text(value: string | Uint8Array | undefined): string {
  return typeof value === "string" ? value : new TextDecoder().decode(value);
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe.each(CASES)("export case %s", (name) => {
  test("matches the snapshot", async () => {
    const rendered = await tree(name);
    const caseDir = join(EXPORT_DIR, name);
    for (const file of SNAPSHOT_FILES) {
      expect(text(rendered.get(file)), file).toBe(
        readFileSync(join(caseDir, "expected", file), "utf8"),
      );
    }
    const expected = JSON.parse(readFileSync(join(caseDir, "expected", "files.json"), "utf8"));
    expect([...rendered.keys()].filter((path) => !path.startsWith(HOST_PREFIX))).toEqual(expected);
    expect(rendered.has(`${HOST_PREFIX}__main__.py`)).toBe(true);
    expect(rendered.has(`${HOST_PREFIX}serve.py`)).toBe(true);
  });

  test("the dockerfile is arm64, pinned and serves on 8080", async () => {
    const dockerfile = text((await tree(name)).get("Dockerfile"));
    expect(dockerfile).toContain(`--platform=${EXPORT_PLATFORM}`);
    expect(dockerfile).toContain(BASE_IMAGE_REFERENCE);
    expect(dockerfile).toContain(`EXPOSE ${AGENT_SERVICE_EXPORT_PORT}`);
    expect(dockerfile).toContain(`"--port", "${AGENT_SERVICE_EXPORT_PORT}"`);
    expect(dockerfile).toContain("USER user");
    expect(dockerfile).not.toContain("RAYITO_AGENT_SERVICE_TOKEN");
  });

  test("the exported host config has no ready marker and no secret", async () => {
    const host = JSON.parse(text((await tree(name)).get("host.json")));
    expect(host).not.toHaveProperty("ready_marker");
    expect(JSON.stringify(host)).not.toContain("bedrock-key");
  });
});

describe("export", () => {
  test("writes the tree and describes it", async () => {
    const { project: the, target } = loadCase(CASES[0] as string);
    const out = join(mkdtempSync(join(tmpdir(), "rayito-export-")), "build");
    const exported = await exportProject(the, { target, outDir: out });
    expect([exported.port, exported.platform, exported.path]).toEqual([
      AGENT_SERVICE_EXPORT_PORT,
      EXPORT_PLATFORM,
      out,
    ]);
    expect([...exported.files]).toEqual([...exported.files].sort());
    expect(readFileSync(join(out, "Dockerfile"), "utf8")).toBe(
      text((await renderExport(the, target)).get("Dockerfile")),
    );
  });

  test("refuses a non-empty directory unless overwrite", async () => {
    const { project: the, target } = loadCase(CASES[0] as string);
    const out = mkdtempSync(join(tmpdir(), "rayito-export-"));
    writeFileSync(join(out, "ya-estaba.txt"), "mío");
    await expect(exportProject(the, { target, outDir: out })).rejects.toThrow(/no está vacío/);
    expect(readdirSync(out)).toEqual(["ya-estaba.txt"]);
    await exportProject(the, { target, outDir: out, overwrite: true });
    expect(readFileSync(join(out, "ya-estaba.txt"), "utf8")).toBe("mío");
    expect(readFileSync(join(out, "Dockerfile"), "utf8")).toContain("FROM ");
  });

  test("accepts an existing empty directory", async () => {
    const { project: the, target } = loadCase(CASES[0] as string);
    const out = mkdtempSync(join(tmpdir(), "rayito-export-"));
    await exportProject(the, { target, outDir: out });
    expect(readdirSync(out)).toContain("Dockerfile");
  });

  test("an unknown target is rejected before writing", async () => {
    const out = join(mkdtempSync(join(tmpdir(), "rayito-export-")), "x");
    await expect(exportProject(project(), { target: "ecs", outDir: out })).rejects.toBeInstanceOf(
      InvalidArgumentError,
    );
    expect(() => readdirSync(out)).toThrow();
  });

  test("a runtime that cannot be installed fails before writing", async () => {
    const out = join(mkdtempSync(join(tmpdir(), "rayito-export-")), "x");
    await expect(
      exportProject(project({}, new FakeAgentRuntime()), { outDir: out }),
    ).rejects.toThrow(/Runtime/);
    expect(() => readdirSync(out)).toThrow();
  });

  test("the readme states that the gateway guarantee does not apply", async () => {
    const readme = text((await tree(CASES[0] as string)).get("README.md"));
    expect(readme).toContain("no hay pasarela");
    expect(readme).toContain("sólo vale dentro de Rayito");
  });

  test("agentcore adds its instructions", async () => {
    const readmes = await Promise.all(
      CASES.map(async (name) => text((await tree(name)).get("README.md"))),
    );
    expect(readmes.some((readme) => readme.includes("Bedrock AgentCore Runtime"))).toBe(true);
    expect(readmes.some((readme) => !readme.includes("Bedrock AgentCore Runtime"))).toBe(true);
  });

  test("makes no network call", async () => {
    const forbidden = vi.fn(() => {
      throw new Error("export() no debe usar la red");
    });
    vi.stubGlobal("fetch", forbidden);
    const out = join(mkdtempSync(join(tmpdir(), "rayito-export-")), "x");
    for (const name of CASES) {
      const { project: the, target } = loadCase(name);
      await exportProject(the, { target, outDir: join(out, name) });
    }
    expect(forbidden).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });
});
