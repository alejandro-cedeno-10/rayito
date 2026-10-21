/**
 * Selección de grafo (ADR-033 D3): `selectGraph`, `--graph` en la detección,
 * `[runtime] graph` en el fichero de proyecto (resuelto al `entrypoint` de
 * `langgraph.json` antes de construir el runtime) y el ejemplo
 * `langgraph-structured`. Espejo de `test_agent_structured_input_project.py`;
 * nada ejecuta el código del proyecto ni llama a AWS.
 */

import { copyFileSync, mkdtempSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Ajv2020 } from "ajv/dist/2020.js";
import { describe, expect, test } from "vitest";
import { DeepAgents } from "../../src/agent/deepagents.js";
import { detect } from "../../src/agent/detect-domain.js";
import { type EntryChoice, graphEntrypoint, selectGraph } from "../../src/agent/detect-entry.js";
import type { DetectOverrides } from "../../src/agent/detect-report.js";
import { DETECTION_RULES } from "../../src/agent/detect-rules.js";
import { AgentProject, AgentProjectError } from "../../src/agent/project.js";
import { readProjectDocument, withFileGraph } from "../../src/agent/project-file.js";
import { InvalidArgumentError } from "../../src/errors.js";
import { project as fakeProject } from "./agent-project-fake.js";

type Json = Record<string, unknown>;

const TESTDATA = join(import.meta.dirname, "..", "..", "..", "..", "testdata");
const CASES = join(TESTDATA, "agent-detection", "cases");
const PROJECTS = join(TESTDATA, "agent-project");
const GRAPH_PROJECT = join(PROJECTS, "valid", "langgraph-graph");
const SAMPLES = join(TESTDATA, "agent-structured-samples");
const SAMPLE = join(SAMPLES, "python", "langgraph-structured");
const REGION = "us-east-1";
const CONTRACT = JSON.parse(
  readFileSync(join(TESTDATA, "agnostic-deploy", "contract.json"), "utf8"),
) as { readonly agentDetectionRules: readonly Json[] };

function choice(name: string): EntryChoice {
  return {
    path: `src/${name}.py`,
    attr: "graph",
    factory: false,
    rule: "test",
    language: "python",
    entrypoint: `src.${name}:graph`,
  };
}

function graphs(...names: string[]): Map<string, EntryChoice> {
  return new Map(names.map((name) => [name, choice(name)]));
}

function treeOf(directory: string, base = directory): Record<string, string> {
  const files: Record<string, string> = {};
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    const full = join(directory, entry.name);
    if (entry.isDirectory()) {
      Object.assign(files, treeOf(full, base));
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

function memoryTree(files: Record<string, string>) {
  return {
    listFiles: () => Object.keys(files),
    readText: (path: string, maxBytes: number) => files[path]?.slice(0, maxBytes),
  };
}

function overridesOf(name: string): DetectOverrides {
  try {
    return JSON.parse(readFileSync(join(CASES, name, "overrides.json"), "utf8")) as DetectOverrides;
  } catch {
    return {};
  }
}

function errorOf(action: () => unknown): AgentProjectError {
  try {
    action();
  } catch (error) {
    if (error instanceof AgentProjectError) {
      return error;
    }
    throw error;
  }
  throw new Error("no lanzó");
}

describe("selectGraph", () => {
  test("follows the precedence", () => {
    expect(selectGraph(new Map(), undefined, DETECTION_RULES)).toBeUndefined();
    expect(selectGraph(graphs("echo"), undefined, DETECTION_RULES)).toEqual([
      "echo",
      choice("echo"),
    ]);
    expect(selectGraph(graphs("agent", "echo"), undefined, DETECTION_RULES)?.[0]).toBe("agent");
    expect(selectGraph(graphs("agent", "echo"), "echo", DETECTION_RULES)?.[0]).toBe("echo");
  });

  test("names the graphs when it cannot choose", () => {
    const ambiguous = errorOf(() =>
      selectGraph(graphs("echo", "triage"), undefined, DETECTION_RULES),
    );
    expect([ambiguous.code, ambiguous.path]).toEqual(["ambiguous", "runtime.entrypoint"]);
    expect(ambiguous.message).toContain("echo, triage");
    expect(ambiguous.message).toContain("--graph");
    const missing = errorOf(() =>
      selectGraph(graphs("echo", "triage"), "billing", DETECTION_RULES),
    );
    expect([missing.code, missing.path]).toEqual(["invalid_value", "runtime.graph"]);
    expect(missing.message).toContain("billing");
  });

  test("caps the listing", () => {
    const names = Array.from(
      { length: DETECTION_RULES.maxCandidatesListed + 2 },
      (_, index) => `g${index}`,
    );
    const error = errorOf(() => selectGraph(graphs(...names), undefined, DETECTION_RULES));
    expect(error.message).not.toContain(names.at(-1));
  });

  test("a document without graphs has no graph of that name", () => {
    expect(errorOf(() => graphEntrypoint(undefined, "triage", DETECTION_RULES)).path).toBe(
      "runtime.graph",
    );
  });
});

describe("detection", () => {
  test.each([
    ["error-langgraph-json-ambiguous-graphs", ["echo, triage", "--graph"]],
    ["error-langgraph-json-graph-not-found", ["billing", "echo, triage"]],
    ["override-graph-and-entrypoint-conflict", ["--graph", "--entry"]],
  ])("%s names what fixes it", (name, fragments) => {
    const error = errorOf(() =>
      detect(memoryTree(treeOf(join(CASES, name, "tree"))), overridesOf(name)),
    );
    for (const fragment of fragments) {
      expect(error.message).toContain(fragment);
    }
  });

  test("--graph on a framework without langgraph.json is not found", () => {
    const error = errorOf(() =>
      detect(memoryTree(treeOf(join(CASES, "python-strands", "tree"))), { graph: "triage" }),
    );
    expect([error.code, error.path]).toEqual(["invalid_value", "runtime.graph"]);
  });

  test("the contract messages are the rules messages", () => {
    for (const rule of CONTRACT.agentDetectionRules.filter((item) => item.feature === "B")) {
      expect(DETECTION_RULES.messages[rule.key as string]).toBe(rule.text);
    }
  });
});

describe("[runtime] graph", () => {
  test("resolves the entrypoint and keeps the name", async () => {
    const expected = JSON.parse(readFileSync(join(GRAPH_PROJECT, "expected.json"), "utf8"));
    const project = await AgentProject.load(GRAPH_PROJECT);
    expect(project.graph).toBe("triage");
    expect((project.runtime as DeepAgents).entrypoint).toBe("src.triage:graph");
    expect(project.toObject()).toEqual(expected);
  });

  test("round-trips with the file resolver and fails without it", async () => {
    const document = await readProjectDocument(GRAPH_PROJECT);
    const options = {
      baseDir: document.baseDir,
      readText: document.readText,
      resolveGraph: document.resolveGraph,
    };
    const project = AgentProject.fromObject(document.data, options);
    expect(AgentProject.fromObject(project.toObject(), options).toObject()).toEqual(
      project.toObject(),
    );
    const error = errorOf(() =>
      AgentProject.fromObject(project.toObject(), { baseDir: document.baseDir }),
    );
    expect([error.code, error.path]).toEqual(["invalid_value", "runtime.graph"]);
  });

  test("the schema accepts the graph project", async () => {
    const { parse } = await import("smol-toml");
    const schema = JSON.parse(readFileSync(join(PROJECTS, "schema.json"), "utf8"));
    const validate = new Ajv2020({ strict: false }).compile(schema);
    expect(validate(parse(readFileSync(join(GRAPH_PROJECT, "rayito-agent.toml"), "utf8")))).toBe(
      true,
    );
  });

  test("a directory without langgraph.json cannot resolve the graph", async () => {
    const directory = mkdtempSync(join(tmpdir(), "rayito-graph-"));
    copyFileSync(join(GRAPH_PROJECT, "rayito-agent.toml"), join(directory, "rayito-agent.toml"));
    await expect(AgentProject.load(directory)).rejects.toMatchObject({
      code: "invalid_value",
      path: "runtime.graph",
    });
  });

  test.each(["", "-bad", "a".repeat(65), "con espacio"])("refuses the name %j", (graph) => {
    const runtime = new DeepAgents({ entrypoint: "src.triage:graph", requireEntrypoint: true });
    expect(() => fakeProject({ runtime, graph })).toThrowError(InvalidArgumentError);
  });

  test("only goes with the langgraph runtime", () => {
    expect(() => fakeProject({ graph: "triage" })).toThrowError(InvalidArgumentError);
    const runtime = new DeepAgents({ entrypoint: "src.triage:graph", requireEntrypoint: true });
    expect(fakeProject({ runtime, graph: "triage" }).graph).toBe("triage");
  });

  test("the file graph reaches the detection unless a flag or an entrypoint wins", () => {
    const document = { runtime: { kind: "langgraph", graph: "triage" } };
    expect(withFileGraph(undefined, document)).toEqual({ graph: "triage" });
    const flagged = { graph: "echo" };
    expect(withFileGraph(flagged, document)).toBe(flagged);
    expect(
      withFileGraph(undefined, { runtime: { graph: "x", entrypoint: "a:b" } }),
    ).toBeUndefined();
  });
});

describe("the langgraph-structured sample", () => {
  test("needs a graph and deploys the chosen one", async () => {
    await expect(AgentProject.detect(SAMPLE, undefined, { region: REGION })).rejects.toThrowError(
      /echo, triage/,
    );
    const expected = JSON.parse(
      readFileSync(join(SAMPLES, "expected", "python-langgraph-structured.json"), "utf8"),
    );
    const [project, report] = await AgentProject.detect(
      SAMPLE,
      { graph: "triage" },
      { region: REGION },
    );
    expect(project.toObject()).toEqual(expected);
    expect(report?.options).toEqual({ graph: "triage" });
    expect((project.runtime as DeepAgents).entrypoint).toBe("triage:graph");
  });

  test("a file graph is used by the detection", async () => {
    const directory = mkdtempSync(join(tmpdir(), "rayito-graph-"));
    for (const name of ["pyproject.toml", "uv.lock", "langgraph.json", "triage.py", "echo.py"]) {
      copyFileSync(join(SAMPLE, name), join(directory, name));
    }
    const document = readFileSync(join(SAMPLE, "rayito-agent.toml"), "utf8");
    writeFileSync(
      join(directory, "rayito-agent.toml"),
      `${document}\n[runtime]\nkind = "langgraph"\ngraph = "echo"\n`,
    );
    const [project] = await AgentProject.detect(directory, undefined, { region: REGION });
    expect(project.graph).toBe("echo");
    expect((project.runtime as DeepAgents).entrypoint).toBe("echo:graph");
    expect(project.toObject().runtime).not.toHaveProperty("entrypoint");
  });
});
