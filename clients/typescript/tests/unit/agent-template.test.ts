/**
 * `AgentTemplate` y `agentPoolWarmup` (`ai-agent-fast-start`): los vectores
 * de `testdata/agent/agent-template/cases.json` y
 * `testdata/agent/pool-warmup.json` que comparte
 * `clients/python/tests/unit/test_agent_template.py`/`test_pool_warmup.py`,
 * los pines de `limits.json`, la validación y el contexto que llega a
 * `Template.build`.
 */

import { createHash } from "node:crypto";
import { readdir, readFile, stat } from "node:fs/promises";
import { join } from "node:path";
import { afterEach, describe, expect, test, vi } from "vitest";
import { DEEPAGENTS_REQUIREMENTS } from "../../src/agent/assets/template-assets.gen.js";
import {
  AgentTemplate,
  type AgentTemplateRuntime,
  agentPoolWarmup,
  InvalidArgumentError,
  Template,
} from "../../src/index.js";
import {
  AGENT_DEEPAGENTS_REQUIREMENTS_SHA256,
  AGENT_OPENCODE_SHA256,
  AGENT_RIPGREP_SHA256,
  AGENT_TEMPLATE_MANIFEST_SCHEMA,
} from "../../src/limits.js";

const TESTDATA = join(import.meta.dirname, "..", "..", "..", "..", "testdata", "agent");

interface TemplateCase {
  readonly name: string;
  readonly options: { runtimes?: AgentTemplateRuntime[]; prefetch?: boolean };
  readonly dockerfile: string;
  readonly manifest: unknown;
  readonly startCmd: string | null;
  readonly contextFiles: string[];
}

interface WarmupCase {
  readonly runtime: string;
  readonly steps: { cmd: string; background: boolean; tag: string | null }[];
}

async function json<T>(name: string): Promise<T> {
  return JSON.parse(await readFile(join(TESTDATA, name), "utf8")) as T;
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("AgentTemplate", () => {
  test("shared vectors", async () => {
    const { cases } = await json<{ cases: TemplateCase[] }>("agent-template/cases.json");
    expect(cases.length).toBeGreaterThan(0);
    for (const c of cases) {
      const template = new AgentTemplate(c.options);
      expect(template.toDockerfile(), c.name).toBe(c.dockerfile);
      expect(template.manifest(), c.name).toEqual(c.manifest);
      expect(template.toTemplate().spec.start?.startCmd ?? null, c.name).toBe(c.startCmd);
      expect(Object.keys(template.contextFiles()).sort(), c.name).toEqual(c.contextFiles);
    }
  });

  test("pins come from limits.json and the requirements match their sha256", () => {
    const dockerfile = new AgentTemplate().toDockerfile();
    expect(dockerfile).toContain(`${AGENT_OPENCODE_SHA256}  /tmp/opencode.tar.gz`);
    expect(dockerfile).toContain(`${AGENT_RIPGREP_SHA256}  /tmp/ripgrep.tar.gz`);
    expect(dockerfile).toContain("--require-hashes --no-deps --only-binary=:all:");
    const sha = createHash("sha256").update(DEEPAGENTS_REQUIREMENTS, "utf8").digest("hex");
    expect(sha).toBe(AGENT_DEEPAGENTS_REQUIREMENTS_SHA256);
  });

  test("validation", () => {
    const bad: unknown[] = [
      { memoryMib: 1024 },
      { memoryMib: 2048.5 },
      { runtimes: [] },
      { runtimes: ["claude"] },
      { runtimes: ["opencode", "opencode"] },
      { name: "" },
    ];
    for (const options of bad) {
      expect(() => new AgentTemplate(options as never)).toThrow(InvalidArgumentError);
    }
  });

  test("prefetch off has no startCmd nor script", () => {
    const template = new AgentTemplate({ prefetch: false });
    expect(template.toTemplate().spec.start).toBeUndefined();
    expect(Object.keys(template.contextFiles())).not.toContain("rayito-agent-prefetch");
  });

  test("build writes the context, passes memory and cleans up", async () => {
    let seen: {
      name: string;
      memoryMb?: number | undefined;
      contextDir?: string;
      files?: string[];
    } = {
      name: "",
    };
    let manifest: { schema?: string } = {};
    vi.spyOn(Template, "build").mockImplementation(async (_template, name, options) => {
      const contextDir = options.contextDir ?? "";
      seen = {
        name,
        memoryMb: options.memoryMb,
        contextDir,
        files: (await readdir(contextDir)).sort(),
      };
      manifest = JSON.parse(await readFile(join(contextDir, "rayito-agent.json"), "utf8"));
      return { templateId: name, buildId: "1", alias: name };
    });

    const info = await new AgentTemplate({ name: "mi-agente", memoryMib: 4096 }).build({
      bucket: "amzn-s3-demo-bucket",
    });

    expect(info.templateId).toBe("mi-agente");
    expect(seen.memoryMb).toBe(4096);
    expect(seen.files).toEqual([
      "deepagents_runner.py",
      "rayito-agent-prefetch",
      "rayito-agent.json",
      "requirements-deepagents.txt",
    ]);
    expect(manifest.schema).toBe(AGENT_TEMPLATE_MANIFEST_SCHEMA);
    await expect(stat(seen.contextDir ?? "")).rejects.toThrow();
  });
});

describe("agentPoolWarmup", () => {
  test("shared vectors", async () => {
    const { cases } = await json<{ cases: WarmupCase[] }>("pool-warmup.json");
    for (const c of cases) {
      const steps = agentPoolWarmup(c.runtime);
      expect(
        steps.map((s) => ({ cmd: s.cmd, background: s.background ?? false, tag: s.tag ?? null })),
      ).toEqual(c.steps);
    }
  });
});
