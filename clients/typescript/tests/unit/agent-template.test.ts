/**
 * `AgentTemplate` y `agentPoolWarmup` (`ai-agent-fast-start`): los vectores
 * de `testdata/agent/agent-template/cases.json` y
 * `testdata/agent/pool-warmup.json` que comparte
 * `clients/python/tests/unit/test_agent_template.py`/`test_pool_warmup.py`,
 * los pines de `limits.json`, la validación y el contexto que llega a
 * `Template.build`.
 */

import { createHash } from "node:crypto";
import { chmod, mkdir, mkdtemp, readdir, readFile, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, test, vi } from "vitest";
import { DEEPAGENTS_REQUIREMENTS } from "../../src/agent/assets/template-assets.gen.js";
import { DeepAgents } from "../../src/agent/deepagents.js";
import { OpenCodeRuntime } from "../../src/agent/opencode.js";
import { AGENT_APP_DIR, hostSha256 } from "../../src/agent/recipes.js";
import { copyStep, runStep } from "../../src/agent/runtime.js";
import { appFiles } from "../../src/agent/template.js";
import {
  AgentTemplate,
  agentPoolWarmup,
  type InstallableRuntime,
  InvalidArgumentError,
  Template,
} from "../../src/index.js";
import {
  AGENT_DEEPAGENTS_REQUIREMENTS_SHA256,
  AGENT_KERNEL_WARMUP_MARKER_PATH,
  AGENT_OPENCODE_SHA256,
  AGENT_PREFETCH_BUILD_MARKER_PATH,
  AGENT_PREFETCH_BUILD_TIMEOUT_SECONDS,
  AGENT_RIPGREP_SHA256,
  AGENT_TEMPLATE_MANIFEST_SCHEMA,
} from "../../src/limits.js";

const TESTDATA = join(import.meta.dirname, "..", "..", "..", "..", "testdata", "agent");

interface TemplateCase {
  readonly name: string;
  readonly options: {
    runtimes?: string[];
    prefetch?: boolean;
    kernelWarmup?: boolean;
  };
  readonly dockerfile: string;
  readonly manifest: unknown;
  readonly startCmd: string | null;
  readonly readyCmd: string | null;
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
      expect(template.manifest(), c.name).toMatchObject(c.manifest as object);
      expect(template.toTemplate().spec.start?.startCmd ?? null, c.name).toBe(c.startCmd);
      expect(template.toTemplate().spec.start?.readyCmd ?? null, c.name).toBe(c.readyCmd);
      expect(
        Object.keys(template.contextFiles())
          .filter((name) => !name.startsWith("agent_host/"))
          .sort(),
        c.name,
      ).toEqual(c.contextFiles);
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

  test("the requirements include the socks extra of httpx", () => {
    // `rayd` exporta `ALL_PROXY=socks5h://...` en el sandbox: sin `socksio`,
    // httpx no crea el cliente de ningún modelo que no sea de Bedrock.
    expect(DEEPAGENTS_REQUIREMENTS).toContain("\nsocksio==");
  });

  test("validation", () => {
    const bad: unknown[] = [
      { memoryMib: 1024 },
      { memoryMib: 2048.5 },
      { runtimes: [] },
      { runtimes: ["claude"] },
      { runtimes: ["custom"] },
      { runtimes: ["opencode", "opencode"] },
      { runtimes: ["deepagents", "langgraph"] },
      { name: "" },
      { app: "/nonexistent/rayito-app-dir" },
    ];
    for (const options of bad) {
      expect(() => new AgentTemplate(options as never)).toThrow(InvalidArgumentError);
    }
  });

  test("the kernel warm-up is off by default", () => {
    const marker = `> ${AGENT_KERNEL_WARMUP_MARKER_PATH}`;
    expect(new AgentTemplate().toDockerfile()).toContain(marker);
    expect(new AgentTemplate({ kernelWarmup: true }).toDockerfile()).not.toContain(marker);
  });

  test("the build snapshot waits for the prefetch warm", () => {
    const start = new AgentTemplate().toTemplate().spec.start;
    expect(start?.startCmd.endsWith(` ${AGENT_PREFETCH_BUILD_MARKER_PATH}`)).toBe(true);
    expect(start?.readyCmd).toBe(`test -e ${AGENT_PREFETCH_BUILD_MARKER_PATH}`);
    expect(start?.readyPoll?.timeoutSeconds).toBe(AGENT_PREFETCH_BUILD_TIMEOUT_SECONDS);
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
      "agent_host",
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

const HOST_DIGEST_VECTOR = "d117cfc5d0205cd37922effa154432631131df48d5047972b1b1be53aab25660";

const fakeInstallable: InstallableRuntime = {
  name: "fake",
  installSteps: (target) => [
    copyStep("fake.bin", `${target.installDir}/fake/fake.bin`, { executable: true }),
    runStep("echo instalado"),
  ],
  contextFiles: () => ({ "fake.bin": "\u0000" }),
  manifestEntry: () => ({ version: "0" }),
  smokeChecks: () => ["test -x /opt/agents/fake/fake.bin"],
  prefetchPaths: () => ["/opt/agents/fake/fake.bin"],
};

describe("AgentTemplate with runtime objects and an app", () => {
  test("the manifest adds runtimes and the host digest without moving the old keys", () => {
    const manifest = new AgentTemplate().manifest();
    expect(Object.keys(manifest.runtimes).sort()).toEqual(["deepagents", "opencode"]);
    expect(manifest.runtimes.deepagents?.runner_sha256).toBe(manifest.runner_sha256);
    expect(Object.keys(new AgentTemplate({ runtimes: ["opencode"] }).manifest().runtimes)).toEqual([
      "opencode",
    ]);
  });

  test("the host digest is a fixed function of path and content", () => {
    expect(hostSha256({})).toBeUndefined();
    expect(hostSha256({ "b.py": "two", "a/__main__.py": "one" })).toBe(HOST_DIGEST_VECTOR);
  });

  test("runtime objects compose the image from their install steps", () => {
    const template = new AgentTemplate({ runtimes: [fakeInstallable as never] });
    const dockerfile = template.toDockerfile();
    expect(dockerfile).toContain('COPY ["__rayito_context/fake.bin", "/opt/agents/fake/fake.bin"]');
    expect(dockerfile).toContain(
      "chmod 0755 /opt/agents/fake/fake.bin /opt/agents/bin/rayito-agent-prefetch",
    );
    expect(dockerfile).not.toContain("su user -c");
    expect(dockerfile).toContain("RUN test -x /opt/agents/fake/fake.bin");
    const manifest = template.manifest();
    expect(manifest.runtimes).toEqual({ fake: { version: "0" } });
    expect(manifest.opencode).toBeNull();
    expect(manifest.deepagents).toBeNull();
    expect(manifest.prefetch_paths).toEqual(["/opt/agents/fake/fake.bin"]);
    expect(Object.keys(template.contextFiles())).toContain("fake.bin");
  });

  test("a task-only runtime cannot be installed", () => {
    expect(() => new AgentTemplate({ runtimes: [{ name: "task-only" } as never] })).toThrow(
      /InstallableRuntime/,
    );
  });

  test("the framework runtime names resolve and an unknown one is rejected", () => {
    expect(() => new AgentTemplate({ runtimes: ["crewai"] })).toThrow(InvalidArgumentError);
    for (const name of ["strands", "strands-harness", "hermes"]) {
      expect(() => new AgentTemplate({ runtimes: [name] })).not.toThrow();
    }
  });

  test("runtime objects and names are equivalent", () => {
    const byName = new AgentTemplate({ runtimes: ["opencode", "deepagents"] });
    const byObject = new AgentTemplate({ runtimes: [new OpenCodeRuntime(), new DeepAgents()] });
    expect(byName.toDockerfile()).toBe(byObject.toDockerfile());
    expect(byName.manifest()).toEqual(byObject.manifest());
  });

  async function makeApp(root: string): Promise<string> {
    const app = join(root, "app-src");
    await mkdir(join(app, "pkg"), { recursive: true });
    await mkdir(join(app, ".venv"), { recursive: true });
    await writeFile(join(app, "pkg", "agent.py"), "agent = None\n");
    await writeFile(join(app, "keep.txt"), "keep\n");
    await writeFile(join(app, "skip.log"), "log\n");
    await writeFile(join(app, ".venv", "pyvenv.cfg"), "home = /usr\n");
    await writeFile(join(app, ".dockerignore"), "*.log\n");
    return app;
  }

  test("the app is copied to the app dir as root", async () => {
    const root = await mkdtemp(join(tmpdir(), "rayito-app-"));
    try {
      const dockerfile = new AgentTemplate({ app: await makeApp(root) }).toDockerfile();
      expect(dockerfile).toContain(`COPY ["__rayito_context/app", "${AGENT_APP_DIR}"]`);
      expect(dockerfile.indexOf('COPY ["__rayito_context/app"')).toBeLessThan(
        dockerfile.indexOf("chown -R root:root /opt/agents && chmod -R a+rX,go-w /opt/agents"),
      );
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  });

  test("build writes the app honouring dockerignore and exclude", async () => {
    const root = await mkdtemp(join(tmpdir(), "rayito-app-"));
    let appFiles: string[] = [];
    vi.spyOn(Template, "build").mockImplementation(async (_template, name, options) => {
      const appDir = join(options.contextDir ?? "", "app");
      const walk = async (dir: string, prefix: string): Promise<string[]> => {
        const out: string[] = [];
        for (const entry of await readdir(dir, { withFileTypes: true })) {
          const relative = prefix === "" ? entry.name : `${prefix}/${entry.name}`;
          if (entry.isDirectory()) {
            out.push(...(await walk(join(dir, entry.name), relative)));
          } else {
            out.push(relative);
          }
        }
        return out;
      };
      appFiles = (await walk(appDir, "")).sort();
      return { templateId: name, buildId: "1", alias: name };
    });
    try {
      await new AgentTemplate({
        app: await makeApp(root),
        appExclude: [".venv"],
        prefetch: false,
      }).build({ bucket: "amzn-s3-demo-bucket" });
      expect(appFiles).toEqual([".dockerignore", "keep.txt", "pkg/agent.py"]);
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  });

  test("excluded paths are never read nor warned about", async () => {
    const root = await mkdtemp(join(tmpdir(), "rayito-app-"));
    const app = await makeApp(root);
    const certificate = join(app, ".venv", "cacert.pem");
    await writeFile(certificate, "-----BEGIN CERTIFICATE-----\n");
    await chmod(certificate, 0);
    const warnings: string[] = [];
    const onWarning = (warning: Error) => warnings.push(warning.message);
    process.on("warning", onWarning);
    try {
      const files = await appFiles(app, [".venv"]);
      expect(files.map(([name]) => name)).toEqual([".dockerignore", "keep.txt", "pkg/agent.py"]);
      await new Promise((resolve) => setImmediate(resolve));
      expect(warnings).toEqual([]);
    } finally {
      process.off("warning", onWarning);
      await chmod(certificate, 0o600);
      await rm(root, { recursive: true, force: true });
    }
  });

  test("the state dir never enters the app even without excludes", async () => {
    const root = await mkdtemp(join(tmpdir(), "rayito-app-"));
    try {
      const app = await makeApp(root);
      for (const relative of [".rayito/agent-token", "pkg/.rayito/agent-token"]) {
        await mkdir(join(app, relative, ".."), { recursive: true });
        await writeFile(join(app, relative), "secret");
      }
      await writeFile(join(app, ".dockerignore"), ".venv\n!.rayito/agent-token\n");
      const files = (await appFiles(app, [])).map(([name]) => name);
      expect(files.filter((name) => name.includes(".rayito"))).toEqual([]);
      expect(files).toContain("pkg/agent.py");
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  });

  test("without an app there is no app copy", () => {
    expect(new AgentTemplate().toDockerfile()).not.toContain("__rayito_context/app");
  });
});
