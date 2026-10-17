/**
 * Detección de un proyecto de agente (ADR-029): los vectores compartidos con
 * Python (`testdata/agent-detection/`), las cotas del análisis, que no se lea
 * nada excluido, la precedencia de las banderas y que detectar no llame a AWS
 * ni ejecute el código del proyecto.
 */

import {
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { afterEach, describe, expect, test, vi } from "vitest";
import { detect, fillRuntime } from "../../src/agent/detect-domain.js";
import { LocalProjectTree } from "../../src/agent/detect-fs.js";
import type { DetectOverrides } from "../../src/agent/detect-report.js";
import { DETECTION_RULES } from "../../src/agent/detect-rules.js";
import {
  globMatch,
  type ProjectTree,
  pyprojectDependencies,
  requirementNames,
  requirementsDependencies,
} from "../../src/agent/detect-scan.js";
import { resolveBuild, satisfied } from "../../src/agent/detect-versions.js";
import { AGENT_PROJECT_ERROR_CODES, AgentProjectError } from "../../src/agent/project.js";
import {
  AGENT_DETECTION_MAX_DEPTH,
  AGENT_DETECTION_MAX_FILE_BYTES,
  AGENT_DETECTION_MAX_FILES,
  AGENT_DETECTION_RULES_SCHEMA,
  AGENT_DETECTION_SCHEMA,
} from "../../src/limits.js";

const ROOT = join(import.meta.dirname, "..", "..", "..", "..", "testdata", "agent-detection");
const CASES_DIR = join(ROOT, "cases");

interface Satisfied {
  readonly kind: string;
  readonly constraint: string;
  readonly version: string;
  readonly expect: boolean | null;
}

interface Resolve {
  readonly kind: string;
  readonly constraint: string;
  readonly builds: Record<string, string>;
  readonly default: string;
  readonly key: string | null;
  readonly understood: boolean;
}

const versions = JSON.parse(readFileSync(join(ROOT, "versions.json"), "utf8")) as {
  satisfied: Satisfied[];
  resolve: Resolve[];
};
const caseNames = readdirSync(CASES_DIR, { withFileTypes: true })
  .filter((entry) => entry.isDirectory())
  .map((entry) => entry.name)
  .sort();

/** Un `ProjectTree` en memoria que apunta lo que se lee. */
class MemoryTree implements ProjectTree {
  readonly reads: string[] = [];
  readonly files: Record<string, string>;

  constructor(files: Record<string, string>) {
    this.files = files;
  }

  listFiles(): string[] {
    return Object.keys(this.files);
  }

  readText(path: string, maxBytes: number): string | undefined {
    this.reads.push(path);
    return this.files[path]?.slice(0, maxBytes);
  }
}

function filesOf(directory: string, base = directory): Record<string, string> {
  const files: Record<string, string> = {};
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    const full = join(directory, entry.name);
    if (entry.isDirectory()) {
      Object.assign(files, filesOf(full, base));
    } else {
      files[
        full
          .slice(base.length + 1)
          .split("\\")
          .join("/")
      ] = readFileSync(full, "utf8");
    }
  }
  return files;
}

function caseTree(name: string): MemoryTree {
  return new MemoryTree(filesOf(join(CASES_DIR, name, "tree")));
}

function overridesOf(name: string): DetectOverrides {
  const file = join(CASES_DIR, name, "overrides.json");
  try {
    return JSON.parse(readFileSync(file, "utf8")) as DetectOverrides;
  } catch {
    return {};
  }
}

describe("shared detection vectors", () => {
  test.each(caseNames)("%s", (name) => {
    const expected = JSON.parse(readFileSync(join(CASES_DIR, name, "expected.json"), "utf8"));
    if ("error" in expected) {
      let thrown: unknown;
      try {
        detect(caseTree(name), overridesOf(name));
      } catch (error) {
        thrown = error;
      }
      expect(thrown).toBeInstanceOf(AgentProjectError);
      const failure = thrown as AgentProjectError;
      expect({ code: failure.code, path: failure.path }).toEqual(expected.error);
      expect(AGENT_PROJECT_ERROR_CODES as readonly string[]).toContain(failure.code);
      expect(failure.message).toContain(failure.path);
    } else {
      expect(detect(caseTree(name), overridesOf(name)).toObject()).toEqual(expected);
    }
  });

  test("every framework of the matrix has a vector", () => {
    const frameworks = new Set(
      caseNames.map(
        (name) =>
          JSON.parse(readFileSync(join(CASES_DIR, name, "expected.json"), "utf8")).framework,
      ),
    );
    for (const wanted of [
      "langgraph",
      "deepagents",
      "strands",
      "strands-harness",
      "hermes",
      "flask",
      "agentcore",
      "vercel-ai",
      "express",
      "hono",
      "fastapi",
      "opencode",
    ]) {
      expect(frameworks).toContain(wanted);
    }
  });
});

describe("version constraints", () => {
  test.each(versions.satisfied)("$kind $constraint vs $version", (entry) => {
    expect(satisfied(entry.kind, entry.constraint, entry.version) ?? null).toBe(entry.expect);
  });

  test.each(versions.resolve)("resolves $kind $constraint", (entry) => {
    const resolution = resolveBuild(entry.kind, entry.constraint, entry.builds, entry.default);
    expect({ key: resolution.key ?? null, understood: resolution.understood }).toEqual({
      key: entry.key,
      understood: entry.understood,
    });
  });
});

function allPatterns(node: unknown, template = false): string[] {
  const templates = new Set([
    "requestFieldTemplates",
    "responseFieldTemplates",
    "portEnvTemplates",
    "portLiterals",
    "models",
  ]);
  if (Array.isArray(node)) {
    return node.flatMap((item) =>
      template && typeof item === "string"
        ? [item.replace(/\{(?:field|env)\}/g, "word")]
        : allPatterns(item, template),
    );
  }
  if (typeof node === "object" && node !== null) {
    return Object.entries(node).flatMap(([key, value]) =>
      (key === "pattern" || key === "requires") && typeof value === "string"
        ? [value]
        : allPatterns(value, template || templates.has(key)),
    );
  }
  return [];
}

describe("rules", () => {
  test("every pattern is in the subset both languages share", () => {
    const patterns = allPatterns(DETECTION_RULES);
    expect(patterns.length).toBeGreaterThan(50);
    for (const pattern of patterns) {
      expect(() => new RegExp(pattern, "m")).not.toThrow();
      expect(pattern).not.toMatch(/\(\?[<=!P]|\(\?[aiLmsux]|\\[AZ]/);
    }
  });

  test("the rules and the report name their schemas", () => {
    expect(DETECTION_RULES.schema).toBe(AGENT_DETECTION_RULES_SCHEMA);
    expect(detect(caseTree("python-strands")).toObject().schema).toBe(AGENT_DETECTION_SCHEMA);
  });
});

describe("bounds and exclusions", () => {
  test("the scan never reads what the default excludes drop", () => {
    const tree = new MemoryTree({
      "requirements.txt": "strands-agents\n",
      "agent.py": "from strands import Agent\n\nagent = Agent()\n",
      ".env": "OPENAI_API_KEY=value\n",
      ".env.local": "ANTHROPIC_API_KEY=value\n",
      ".git/config": "[core]\n",
      ".venv/lib/strands.py": "from strands import Agent\n\nhidden = Agent()\n",
      "node_modules/pkg/index.js": "module.exports = {}\n",
      "pkg/__pycache__/agent.cpython-312.pyc": "binary",
      ".rayito/agent-token": "token\n",
    });
    const report = detect(tree);
    expect(report.entrypoint).toBe("agent:agent");
    for (const forbidden of [".env", ".env.local", ".git/config", ".rayito/agent-token"]) {
      expect(tree.reads).not.toContain(forbidden);
    }
    expect(tree.reads.filter((path) => /^(\.venv|node_modules)\//.test(path))).toEqual([]);
    expect(report.providers[0]?.preset).toBe("bedrock");
  });

  test("an env example is read for names only and a real env never", () => {
    const tree = new MemoryTree({
      "requirements.txt": "strands-agents\n",
      "agent.py": "from strands import Agent\n\nagent = Agent()\n",
      ".env": "OPENAI_API_KEY=secret-value\n",
      ".env.example": "GROQ_API_KEY=\n",
    });
    const report = detect(tree);
    expect(tree.reads).not.toContain(".env");
    expect(report.providers.map((provider) => provider.preset)).toEqual(["groq"]);
    expect(JSON.stringify(report.toObject())).not.toContain("secret-value");
  });

  test("the report never carries file contents", () => {
    const marker = "contenido-que-no-debe-salir";
    const tree = new MemoryTree({
      "requirements.txt": "strands-agents\n",
      "agent.py": `# ${marker}\nfrom strands import Agent\n\nagent = Agent(p='${marker}')\n`,
    });
    expect(JSON.stringify(detect(tree).toObject())).not.toContain(marker);
  });

  test("a project with too many files is truncated with a warning", () => {
    const files: Record<string, string> = {};
    for (let index = 0; index < AGENT_DETECTION_MAX_FILES + 5; index += 1) {
      files[`data/part${String(index).padStart(5, "0")}.txt`] = "x";
    }
    files["requirements.txt"] = "strands-agents\n";
    files["agent.py"] = "from strands import Agent\n\nagent = Agent()\n";
    const report = detect(new MemoryTree(files));
    expect(report.entrypoint).toBe("agent:agent");
    expect(report.warnings.map((warning) => warning.code)).toContain("scan_truncated");
  });

  test("files deeper than the bound are ignored", () => {
    const deep = `${new Array(AGENT_DETECTION_MAX_DEPTH + 1).fill("d").join("/")}/deep.py`;
    const tree = new MemoryTree({
      "requirements.txt": "strands-agents\n",
      "agent.py": "from strands import Agent\n\nagent = Agent()\n",
      [deep]: "from strands import Agent\n\nother = Agent()\n",
    });
    expect(detect(tree).entrypoint).toBe("agent:agent");
  });

  test("a file is read up to the byte bound", () => {
    const padding = `# ${"x".repeat(AGENT_DETECTION_MAX_FILE_BYTES)}\n`;
    const tree = new MemoryTree({
      "requirements.txt": "strands-agents\n",
      "agent.py": `${padding}from strands import Agent\n\nlate = Agent()\n`,
      "other.py": "from strands import Agent\n\nearly = Agent()\n",
    });
    expect(detect(tree).entrypoint).toBe("other:early");
  });
});

describe("static analysis", () => {
  const folders: string[] = [];
  afterEach(() => {
    for (const folder of folders.splice(0)) {
      rmSync(folder, { recursive: true, force: true });
    }
  });

  function temporary(): string {
    const folder = mkdtempSync(join(tmpdir(), "rayito-detect-"));
    folders.push(folder);
    return folder;
  }

  function write(root: string, path: string, text: string): void {
    mkdirSync(dirname(join(root, path)), { recursive: true });
    writeFileSync(join(root, path), text);
  }

  test("detecting never runs the code of the project", () => {
    const root = temporary();
    write(root, "requirements.txt", "strands-agents\n");
    write(
      root,
      "agent.py",
      "from strands import Agent\n\nraise SystemExit('se ejecutó')\nagent = Agent()\n",
    );
    expect(detect(new LocalProjectTree(root)).entrypoint).toBe("agent:agent");
  });

  test("detecting makes no network call", () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(() => {
      throw new Error("detect() no debe llamar a la red");
    });
    try {
      detect(caseTree("python-langgraph-demo"));
      expect(fetchSpy).not.toHaveBeenCalled();
    } finally {
      fetchSpy.mockRestore();
    }
  });

  test("the local tree lists by breadth, skips excluded folders and links", () => {
    const root = temporary();
    write(root, "pyproject.toml", "[project]\n");
    write(root, "pkg/mod.py", "x = 1\n");
    write(root, "node_modules/dep.js", "x\n");
    write(root, ".venv/tool.py", "x\n");
    const outside = temporary();
    write(outside, "fuera.txt", "fuera\n");
    symlinkSync(join(outside, "fuera.txt"), join(root, "link.txt"));
    const tree = new LocalProjectTree(root);
    expect(tree.listFiles(AGENT_DETECTION_MAX_DEPTH, 100)).toEqual([
      "pyproject.toml",
      "pkg/mod.py",
    ]);
    expect(tree.readText("link.txt", 100)).toBeUndefined();
    expect(tree.readText("../fuera.txt", 100)).toBeUndefined();
    expect(tree.readText("pkg/mod.py", 100)).toBe("x = 1\n");
    expect(tree.readText("pkg/mod.py", 3)).toBe("x =");
    expect(tree.readText("missing.txt", 100)).toBeUndefined();
  });

  test("the local tree keeps the root files when it must truncate", () => {
    const root = temporary();
    write(root, "requirements.txt", "x\n");
    for (let index = 0; index < 5; index += 1) {
      write(root, `a/f${index}.txt`, "x\n");
    }
    const listed = new LocalProjectTree(root).listFiles(AGENT_DETECTION_MAX_DEPTH, 3);
    expect(listed[0]).toBe("requirements.txt");
    expect(listed).toHaveLength(3);
  });

  test("a binary file is not text", () => {
    const root = temporary();
    writeFileSync(join(root, "blob.bin"), Buffer.from([97, 98, 0, 99, 100]));
    expect(new LocalProjectTree(root).readText("blob.bin", 100)).toBeUndefined();
  });
});

describe("overrides", () => {
  test.each([{ language: "go" }, { framework: "crewai" }, { protocol: "grpc" }])(
    "an invalid override %j is an invalid value",
    (overrides) => {
      const tree = new MemoryTree({
        "requirements.txt": "strands-agents\n",
        "agent.py": "agent = Agent()\n",
      });
      expect(() => detect(tree, overrides)).toThrowError(
        expect.objectContaining({ code: "invalid_value" }),
      );
    },
  );

  test("a forced framework without its dependency still looks for its entrypoint", () => {
    const tree = new MemoryTree({
      "requirements.txt": "requests\n",
      "agent.py": "from strands import Agent\n\nagent = Agent()\n",
    });
    const report = detect(tree, { framework: "strands" });
    expect([report.framework, report.entrypoint]).toEqual(["strands", "agent:agent"]);
    expect(report.evidence[0]?.rule).toBe("override:framework");
  });

  test("an explicit entrypoint is used as given", () => {
    const tree = new MemoryTree({
      "requirements.txt": "strands-agents\n",
      "agent.py": "agent = None\n",
    });
    expect(detect(tree, { entrypoint: "pkg.mod:build" }).entrypoint).toBe("pkg.mod:build");
  });
});

describe("scanning helpers", () => {
  test.each([
    ["requirements*.txt", "requirements-dev.txt", true],
    ["requirements*.txt", "requirements.txt", true],
    ["requirements*.txt", "sub/requirements.txt", false],
    ["*.py", "agent.py", true],
    ["*.py", "agent.pyc", false],
    [".env.*", ".env.local", true],
    [".env", ".env", true],
    ["*.d.ts", "types.d.ts", true],
    ["*dev*", "requirements-dev.txt", true],
  ])("glob %s on %s", (pattern, name, expected) => {
    expect(globMatch(pattern as string, name as string)).toBe(expected);
  });

  test("requirement names include extras and normalise", () => {
    expect(requirementNames("Strands_Agents[OpenAI, Gemini]>=1.0")).toEqual([
      "strands-agents",
      "strands-agents[openai]",
      "strands-agents[gemini]",
    ]);
    expect(
      requirementsDependencies("# nota\n-r base.txt\n\nlanggraph==1.0 ; python_version>'3.10'\n"),
    ).toEqual(["langgraph"]);
  });

  test("pyproject dependencies are read without a toml parser", () => {
    const text =
      '[project]\nname = "x"\ndependencies = [\n    "strands-agents[openai]>=1.0",\n' +
      '    "requests",\n]\n\n[project.optional-dependencies]\nextra = ["flask>=3"]\n\n' +
      '[tool.poetry.dependencies]\npython = "^3.11"\nfastapi = "^0.1"\n';
    expect(pyprojectDependencies(text)).toEqual([
      "strands-agents",
      "strands-agents[openai]",
      "requests",
      "flask",
      "fastapi",
    ]);
  });
});

describe("runtime table", () => {
  test("the table of an app carries its command and not an entrypoint", () => {
    const table = detect(caseTree("python-flask-custom")).runtimeTable();
    expect(table.kind).toBe("custom");
    expect(table).not.toHaveProperty("entrypoint");
    const app = table.app as Record<string, unknown>;
    expect((app.command as string[]).slice(0, 3)).toEqual(["python", "-m", "flask"]);
    expect(app.protocol).toBe("json");
  });

  test("filling a document without runtime uses the detected one", () => {
    const report = detect(caseTree("python-strands"));
    const filled = fillRuntime({ schema: "x", name: "demo" }, report);
    expect(filled.runtime).toEqual(report.runtimeTable());
    expect(filled.name).toBe("demo");
  });

  test("what the file declares wins over the detection", () => {
    const report = detect(caseTree("python-strands"));
    const runtime = fillRuntime(
      { runtime: { kind: "strands", entrypoint: "mi.modulo:otro" } },
      report,
    ).runtime as Record<string, unknown>;
    expect(runtime.entrypoint).toBe("mi.modulo:otro");
    expect(runtime.framework).toBe("strands");
  });

  test("a file with another kind is left untouched", () => {
    const report = detect(caseTree("python-strands"));
    expect(fillRuntime({ runtime: { kind: "opencode" } }, report).runtime).toEqual({
      kind: "opencode",
    });
  });

  test("the app table merges key by key", () => {
    const report = detect(caseTree("python-flask-custom"));
    const runtime = fillRuntime({ runtime: { kind: "custom", app: { port: 9001 } } }, report)
      .runtime as { app: Record<string, unknown> };
    expect(runtime.app.port).toBe(9001);
    expect(runtime.app.health_path).toBe("/healthz");
  });
});
