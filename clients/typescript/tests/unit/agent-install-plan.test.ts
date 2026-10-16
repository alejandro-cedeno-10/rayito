/**
 * El plan de instalación de un agente que ya existe (`install-plan.ts`, 0.14 D1
 * y D3) contra los vectores que comparte con Python
 * (`testdata/agent/install-plans/`). Espejo de `test_agent_install_plan.py`.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, test } from "vitest";
import { HERMES_PYTHON_MINOR, HermesRuntime } from "../../src/agent/hermes.js";
import {
  ecosystem,
  NODE_LOCKFILES,
  NODE_PACKAGE_MANAGERS,
  nodeDependencySteps,
  nodePackageFiles,
  nodeRuntimeSteps,
  pythonDependencySteps,
  pythonDownloadCommand,
  pythonInterpreter,
  pythonRuntimeSteps,
  uvSteps,
} from "../../src/agent/install-plan.js";
import { installTarget } from "../../src/agent/runtime.js";
import { InvalidArgumentError } from "../../src/errors.js";
import {
  AGENT_DEFAULT_NODE_MAJOR,
  AGENT_DEFAULT_PYTHON_MINOR,
  AGENT_NODE_SHA256S,
  AGENT_NODE_VERSIONS,
  AGENT_PYTHON_BUILD_SHA256S,
  AGENT_PYTHON_BUILD_VERSIONS,
} from "../../src/limits.js";

const REPO = join(import.meta.dirname, "..", "..", "..", "..");
// biome-ignore lint/suspicious/noExplicitAny: vectores JSON compartidos, sin esquema.
type Json = any;
const VECTORS: Json = JSON.parse(
  readFileSync(join(REPO, "testdata", "agent", "install-plans", "install-plans.json"), "utf8"),
);

function call(vector: Json): unknown {
  const { fn, args } = vector.call;
  const target = installTarget({
    platform: vector.call.target.platform,
    python: vector.call.target.python,
    installDir: vector.call.target.install_dir,
  });
  switch (fn) {
    case "node_runtime_steps":
      return nodeRuntimeSteps(target, args.node_version);
    case "node_dependency_steps":
      return nodeDependencySteps(target, args.manager);
    case "python_runtime_steps":
      return pythonRuntimeSteps(target, args.python_version);
    case "python_interpreter":
      return pythonInterpreter(target, args.python_version);
    case "python_download_command":
      return pythonDownloadCommand(args.minor, args.parent_dir, args.tarball_path);
    case "uv_steps":
      return uvSteps(target);
    case "python_dependency_steps":
      return pythonDependencySteps(target, {
        installer: args.installer,
        manifest: args.manifest,
        path: args.path ?? undefined,
        python: args.python,
        venvDir: args.venv_dir,
        slug: args.slug,
        createVenv: args.create_venv,
      });
    default:
      throw new Error(fn);
  }
}

describe("install plan vectors", () => {
  const names: string[] = VECTORS.cases.map((c: Json) => c.name);
  test.each(names)("%s", (name) => {
    const vector = VECTORS.cases.find((c: Json) => c.name === name);
    if (vector.expected.error !== undefined) {
      expect(() => call(vector)).toThrow(InvalidArgumentError);
      return;
    }
    const value = call(vector);
    if (vector.expected.value !== undefined) {
      expect(value).toBe(vector.expected.value);
      return;
    }
    expect((value as Json[]).map((step) => ({ op: step.op, args: { ...step.args } }))).toEqual(
      vector.expected.steps,
    );
  });
});

describe("pinned builds", () => {
  test("every pinned build has a version and a sha256", () => {
    expect(Object.keys(AGENT_PYTHON_BUILD_VERSIONS).sort()).toEqual(
      Object.keys(AGENT_PYTHON_BUILD_SHA256S).sort(),
    );
    expect(Object.keys(AGENT_NODE_VERSIONS).sort()).toEqual(Object.keys(AGENT_NODE_SHA256S).sort());
    expect(AGENT_DEFAULT_NODE_MAJOR in AGENT_NODE_VERSIONS).toBe(true);
    expect(AGENT_DEFAULT_PYTHON_MINOR in AGENT_PYTHON_BUILD_VERSIONS).toBe(true);
    for (const [minor, version] of Object.entries(AGENT_PYTHON_BUILD_VERSIONS)) {
      expect(version.startsWith(`${minor}.`)).toBe(true);
    }
    for (const [major, version] of Object.entries(AGENT_NODE_VERSIONS)) {
      expect(version.startsWith(`${major}.`)).toBe(true);
    }
    for (const digest of [
      ...Object.values(AGENT_PYTHON_BUILD_SHA256S),
      ...Object.values(AGENT_NODE_SHA256S),
    ]) {
      expect(digest).toHaveLength(64);
    }
  });

  test("Hermes uses the shared pinned Python", () => {
    const [first] = new HermesRuntime().installSteps(installTarget());
    const command = String(first?.args?.cmd);
    expect(command).toContain(AGENT_PYTHON_BUILD_SHA256S[HERMES_PYTHON_MINOR]);
    expect(command).toContain(`cpython-${AGENT_PYTHON_BUILD_VERSIONS[HERMES_PYTHON_MINOR]}%2B`);
  });
});

describe("package managers", () => {
  test.each([
    ["pip", "python"],
    ["uv", "python"],
    ["npm", "node"],
    ["pnpm", "node"],
    ["yarn", "node"],
  ])("the ecosystem of %s is %s", (manager, kind) => {
    expect(ecosystem(manager)).toBe(kind);
  });

  test("an unknown package manager is rejected", () => {
    expect(() => ecosystem("bun")).toThrow(InvalidArgumentError);
  });

  test.each(NODE_PACKAGE_MANAGERS)("%s has a lockfile and a frozen install", (manager) => {
    expect(nodePackageFiles(manager)).toEqual(["package.json", NODE_LOCKFILES[manager]]);
    const steps = nodeDependencySteps(installTarget(), manager);
    expect(steps.slice(0, 2).map((step) => step.args?.src)).toEqual([
      "app/package.json",
      `app/${NODE_LOCKFILES[manager]}`,
    ]);
    const install = String(steps[steps.length - 1]?.args?.cmd);
    expect(install).toContain("--ignore-scripts");
    expect(install.includes("frozen") || install.includes(" ci ")).toBe(true);
  });
});
