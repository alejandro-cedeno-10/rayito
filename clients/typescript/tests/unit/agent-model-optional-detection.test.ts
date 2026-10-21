/**
 * Detección del modelo elegido por el código (ADR-033 D2), espejo de
 * `test_agent_model_optional_detection.py`: el proyecto que sale de cada caso
 * nuevo (`bridge.json`), que nunca se lee un valor, las banderas `--model-id`,
 * `--model-provider` y `--model-base-url`, y que las banderas de la CLI son las
 * de `testdata/agnostic-deploy/contract.json`.
 */

import { mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, test } from "vitest";
import { detect } from "../../src/agent/detect-domain.js";
import { detectedDocument } from "../../src/agent/detect-project.js";
import type { DetectOverrides } from "../../src/agent/detect-report.js";
import type { ProjectTree } from "../../src/agent/detect-scan.js";
import { AgentProject, AgentProjectError } from "../../src/agent/project.js";
import { SdkAgentProjectTools } from "../../src/cli/agent-project-adapter.js";
import { buildWorld } from "./cli/case-world.js";
import { runWithServices } from "./cli/run-case.js";

const TESTDATA = join(import.meta.dirname, "..", "..", "..", "..", "testdata");
const CASES_DIR = join(TESTDATA, "agent-detection", "cases");
const CONTRACT = JSON.parse(
  readFileSync(join(TESTDATA, "agnostic-deploy", "contract.json"), "utf8"),
) as { cliFlags: ContractFlag[] };
const SURFACE = JSON.parse(readFileSync(join(TESTDATA, "cli", "surface.json"), "utf8")) as {
  commands: { path: string[]; parameters: SurfaceParameter[] }[];
};
const PROJECT_NAME = "ticket-agent";
const REGION = "us-east-1";
const SECRET_VALUES = ["not-a-real-key-value", "https://private-llm.internal", "real-model-name"];
const BRIDGE_CASES = readdirSync(CASES_DIR)
  .filter((name) => readdirSync(join(CASES_DIR, name)).includes("bridge.json"))
  .sort();
const scratches: string[] = [];

afterEach(() => {
  for (const directory of scratches.splice(0)) {
    rmSync(directory, { recursive: true, force: true });
  }
});

interface ContractFlag {
  readonly feature: string;
  readonly commands: string[][];
  readonly flags: string[];
  readonly name: string;
  readonly type: string;
  readonly multiple: boolean;
  readonly default: unknown;
  readonly isFlag: boolean;
  readonly after: string;
}

interface SurfaceParameter {
  readonly name: string;
  readonly flags: string[];
  readonly type: string;
  readonly multiple: boolean;
  readonly default: unknown;
  readonly is_flag?: boolean;
}

interface BridgeVector {
  readonly overrides?: Record<string, string>;
  readonly model?: Record<string, unknown>;
  readonly gateways?: Record<string, unknown>;
  readonly error?: { readonly code: string; readonly path: string };
}

/** Un `ProjectTree` en memoria. */
class MemoryTree implements ProjectTree {
  constructor(readonly files: Record<string, string>) {}

  listFiles(): string[] {
    return Object.keys(this.files);
  }

  readText(path: string, maxBytes: number): string | undefined {
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

function bridgeOf(name: string): BridgeVector {
  return JSON.parse(readFileSync(join(CASES_DIR, name, "bridge.json"), "utf8")) as BridgeVector;
}

/** Las claves de `bridge.json` (las del fichero de Python) como `DetectOverrides`. */
function overridesOf(raw: Readonly<Record<string, string>> = {}): DetectOverrides {
  return {
    ...(raw.model_id === undefined ? {} : { modelId: raw.model_id }),
    ...(raw.model_provider === undefined ? {} : { modelProvider: raw.model_provider }),
    ...(raw.model_base_url === undefined ? {} : { modelBaseUrl: raw.model_base_url }),
  };
}

function catchError(run: () => unknown): AgentProjectError {
  try {
    run();
  } catch (error) {
    if (error instanceof AgentProjectError) {
      return error;
    }
    throw error;
  }
  throw new Error("no lanzó");
}

describe("el proyecto detectado", () => {
  test.each(BRIDGE_CASES)("%s", (name) => {
    const vector = bridgeOf(name);
    const overrides = overridesOf(vector.overrides);
    const report = detect(new MemoryTree(filesOf(join(CASES_DIR, name, "tree"))), overrides);
    const bridge = () =>
      detectedDocument({}, report, { name: PROJECT_NAME, region: REGION, overrides });
    if (vector.error !== undefined) {
      const error = catchError(bridge);
      expect({ code: error.code, path: error.path }).toEqual(vector.error);
      return;
    }
    const document = bridge();
    expect(document.model).toEqual(vector.model);
    expect(document.gateways).toEqual(vector.gateways);
    const project = AgentProject.fromObject(document, { baseDir: "." });
    expect(project.spec.model.id).toBe(vector.model?.id);
  });
});

function leakyTree(): Record<string, string> {
  return {
    "pyproject.toml":
      '[project]\nname = "x"\nrequires-python = ">=3.11"\n' +
      'dependencies = ["langgraph>=1.0", "langchain-openai>=1.0"]\n',
    "uv.lock": 'version = 1\nrequires-python = ">=3.11"\n',
    "agent.py":
      "import os\nfrom langchain_openai import ChatOpenAI\n" +
      "from langgraph.prebuilt import create_react_agent\n\n\n" +
      "def build_graph():\n" +
      '    model = ChatOpenAI(model=os.getenv("LLM_MODEL"), ' +
      'base_url=os.getenv("LLM_BASE_URL"))\n' +
      "    return create_react_agent(model, [])\n",
    ".env.example":
      `LLM_MODEL=${SECRET_VALUES[2]}\nLLM_BASE_URL=${SECRET_VALUES[1]}\n` +
      `export LLM_API_KEY='${SECRET_VALUES[0]}'\n`,
    "langgraph.json": JSON.stringify({
      graphs: { agent: "./agent.py:build_graph" },
      env: { LLM_API_KEY: SECRET_VALUES[0] },
    }),
  };
}

describe("la detección del modelo", () => {
  test("el informe y el proyecto guardan nombres, nunca valores", () => {
    const overrides = { modelBaseUrl: "https://llm-gateway.example.com" };
    const report = detect(new MemoryTree(leakyTree()), overrides);
    expect(report.modelEnvs).toEqual({
      id_env: "LLM_MODEL",
      base_url_env: "LLM_BASE_URL",
      api_key_env: "LLM_API_KEY",
    });
    const document = detectedDocument({}, report, {
      name: PROJECT_NAME,
      region: REGION,
      overrides,
    });
    const text = JSON.stringify([report.toObject(), document]);
    expect(SECRET_VALUES.filter((value) => text.includes(value))).toEqual([]);
  });

  test("un --model-provider desconocido nombra los presets", () => {
    const error = catchError(() => detect(new MemoryTree(leakyTree()), { modelProvider: "acme" }));
    expect([error.code, error.path]).toEqual(["invalid_value", "model.provider"]);
    expect(error.message).toContain("--model-provider acme");
    expect(error.message).toContain("litellm");
  });

  test("un --model-id inválido es invalid_value", () => {
    const error = catchError(() =>
      detect(new MemoryTree(leakyTree()), { modelId: "no spaces allowed" }),
    );
    expect([error.code, error.path]).toEqual(["invalid_value", "model.id"]);
  });

  test("una URL base necesita un preset con upstream", () => {
    const overrides = { modelProvider: "anthropic", modelBaseUrl: "https://x.example.com" };
    const report = detect(new MemoryTree(leakyTree()), overrides);
    const error = catchError(() =>
      detectedDocument({}, report, { name: PROJECT_NAME, region: REGION, overrides }),
    );
    expect([error.code, error.path]).toEqual(["invalid_value", "gateways.anthropic.upstream"]);
  });

  test("sin URL base el upstream que falta nombra la bandera", () => {
    const report = detect(new MemoryTree(leakyTree()));
    const error = catchError(() =>
      detectedDocument({}, report, { name: PROJECT_NAME, region: REGION }),
    );
    expect(error.path).toBe("gateways.openai-compatible.upstream");
    expect(error.message).toContain("--model-base-url");
  });

  test("el proveedor forzado sustituye a los detectados sin avisos de proveedor", () => {
    const report = detect(new MemoryTree(leakyTree()), { modelProvider: "openai" });
    expect(report.providers.map((provider) => provider.preset)).toEqual(["openai"]);
    expect(report.warnings.map((warning) => warning.code)).toEqual(["model_id_from_code"]);
  });

  test("agent detect toma las banderas del modelo", async () => {
    const directory = mkdtempSync(join(tmpdir(), "rayito-model-"));
    scratches.push(directory);
    const caseName = "override-model-id-and-provider";
    for (const [name, text] of Object.entries(filesOf(join(CASES_DIR, caseName, "tree")))) {
      writeFileSync(join(directory, name), text);
    }
    const flags = bridgeOf(caseName).overrides as Record<string, string>;
    const built = buildWorld({});
    const outcome = await runWithServices({
      args: [
        "--json",
        "--region",
        REGION,
        "agent",
        "detect",
        directory,
        "--model-id",
        flags.model_id as string,
        "--model-provider",
        flags.model_provider as string,
        "--model-base-url",
        flags.model_base_url as string,
      ],
      services: built.services,
      agentProjects: new SdkAgentProjectTools(
        async () => built.services,
        () => REGION,
      ),
      env: {},
    });
    expect(outcome.exitCode, outcome.stderr).toBe(0);
    const project = (JSON.parse(outcome.stdout) as { project: { model: Record<string, unknown> } })
      .project;
    expect(project.model.id).toBe(flags.model_id);
    expect(project.model.gateway).toBe(flags.model_provider);
  });
});

describe("las banderas de la CLI son las del contrato", () => {
  const cases = CONTRACT.cliFlags
    .filter((flag) => flag.feature === "A")
    .flatMap((flag) =>
      flag.commands.map(
        (command) => [`${command.join(" ")} ${flag.flags[0]}`, flag, command] as const,
      ),
    );

  test.each(cases)("%s", (_, flag, command) => {
    const entry = SURFACE.commands.find((item) => item.path.join(" ") === command.join(" "));
    const parameters = entry?.parameters ?? [];
    const names = parameters.map((parameter) => parameter.flags[0]);
    const found = parameters.find((parameter) => parameter.name === flag.name);
    expect(found?.flags).toEqual(flag.flags);
    expect([found?.type, found?.multiple, found?.default, found?.is_flag]).toEqual([
      flag.type,
      flag.multiple,
      flag.default,
      flag.isFlag,
    ]);
    expect(names.indexOf(flag.flags[0] as string)).toBe(names.indexOf(flag.after) + 1);
  });
});
