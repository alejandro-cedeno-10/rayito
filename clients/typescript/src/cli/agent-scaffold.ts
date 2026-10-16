/**
 * Andamiaje de `rayito agent init`: los ficheros que escribe para cada
 * runtime. Pura (devuelve un mapa ruta -> contenido); `writeScaffold` es la
 * única que toca el disco. Los casos esperados viven en
 * `testdata/cli/agent-scaffold/` y los lee también la CLI de Python, para
 * que las dos escriban exactamente lo mismo.
 */

import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import { basename, join, resolve } from "node:path";

export const SCHEMA = "rayito.agent-project/1";
export const PROJECT_FILE_NAME = "rayito-agent.toml";
export const INSTRUCTIONS_FILE_NAME = "AGENTS.md";
export const REQUIREMENTS_FILE_NAME = "requirements.txt";
export const DEFAULT_PROJECT_NAME = "my-agent";
export const MAX_PROJECT_NAME_LENGTH = 63;
export const SCAFFOLD_FALLBACK_REGION = "us-east-1";
export const PYTHON_LANGUAGE = "python";
export const TYPESCRIPT_LANGUAGE = "typescript";
export const LANGUAGES = [PYTHON_LANGUAGE, TYPESCRIPT_LANGUAGE] as const;
export const SCAFFOLD_RUNTIMES = [
  "opencode",
  "deepagents",
  "langgraph",
  "strands",
  "strands-harness",
  "hermes",
  "custom",
] as const;

/**
 * Proveedor -> preset de la pasarela y modelo por defecto (`undefined` si el
 * usuario tiene que elegirlo). Sólo los presets cuya pasarela no pide más
 * datos que el secreto; el resto se escribe a mano (guía de proveedores).
 */
export const PROVIDER_PRESETS: Readonly<
  Record<string, { readonly preset: string; readonly defaultModel: string | undefined }>
> = {
  bedrock: { preset: "bedrock", defaultModel: "us.anthropic.claude-haiku-4-5-20251001-v1:0" },
  anthropic: { preset: "anthropic", defaultModel: "claude-haiku-4-5" },
  openai: { preset: "openai", defaultModel: undefined },
};
export const DEFAULT_PROVIDER = "bedrock";
const SECRET_NAME = "provider-key";
const GATEWAY_NAME = "model";
const ENTRYPOINT_RUNTIMES: ReadonlySet<string> = new Set(["langgraph", "strands"]);
const PYTHON_ENTRYPOINTS: Readonly<Record<string, string>> = {
  langgraph: "graph:build",
  strands: "agent:agent",
};
const TYPESCRIPT_ENTRYPOINT = "agent.ts";
const EXCLUDES = [".venv", "node_modules", "__pycache__", ".git"];
const INSTRUCTIONS_TEXT = "Responde en español, con frases cortas.\n";
const CUSTOM_COMMAND = ["bash", "run.sh"];
const NON_NAME_CHARACTERS = /[^a-z0-9]+/g;
const EDGE_DASHES = /^-+|-+$/g;

const STRANDS_PY = `"""Agente de Strands. \`ctx.model\` ya apunta a la pasarela del modelo."""

from strands import Agent, tool


@tool
def word_count(text: str) -> int:
    """Cuenta las palabras de un texto."""
    return len(text.split())


def agent(ctx):
    return Agent(model=ctx.model, tools=[word_count])
`;

const STRANDS_TS = `import { Agent, type Model, tool } from "@strands-agents/sdk";
import { z } from "zod";

const wordCount = tool({
  name: "word_count",
  description: "Cuenta las palabras de un texto.",
  inputSchema: z.object({ text: z.string() }),
  callback: ({ text }) => text.split(/\\s+/).filter(Boolean).length,
});

export function agent(ctx: { model: Model }): Agent {
  return new Agent({ model: ctx.model, tools: [wordCount] });
}
`;

const STRANDS_PACKAGE_JSON = `{
  "name": "agent",
  "private": true,
  "type": "module",
  "dependencies": {
    "@strands-agents/sdk": "1.21.0",
    "zod": "^4.5.4"
  }
}
`;

const LANGGRAPH_PY = `"""Grafo del agente. \`ctx\` trae el modelo ya cableado a la pasarela."""

from deepagents import create_deep_agent


def build(ctx):
    return create_deep_agent(
        model=ctx.model,
        system_prompt=ctx.instructions,
        subagents=ctx.subagents,
        backend=ctx.backend,
        middleware=ctx.middleware,
    )
`;

const CUSTOM_RUN_SH = `#!/usr/bin/env bash
set -euo pipefail
python3 -c 'import json, sys; request = json.loads(sys.stdin.readline()); print(request["prompt"])'
`;

const STRANDS_REQUIREMENTS = `strands-agents
`;

/** `init` encontró ficheros que ya existen y no se pasó `--force`. */
export class ScaffoldConflictError extends Error {
  readonly paths: readonly string[];

  constructor(paths: readonly string[]) {
    super(paths.join(", "));
    this.name = "ScaffoldConflictError";
    this.paths = paths;
  }
}

export interface ScaffoldRequest {
  readonly name: string;
  readonly runtime: string;
  readonly language: string;
  readonly provider: string;
  readonly modelId: string;
  readonly region: string;
}

/**
 * El nombre del proyecto: el del directorio en minúsculas, con `-` en lugar
 * de cualquier otro carácter, y `my-agent` si no queda nada.
 */
export function projectNameOf(directory: string): string {
  const slug = basename(resolve(directory))
    .toLowerCase()
    .replace(NON_NAME_CHARACTERS, "-")
    .replace(EDGE_DASHES, "");
  return slug.slice(0, MAX_PROJECT_NAME_LENGTH).replace(EDGE_DASHES, "") || DEFAULT_PROJECT_NAME;
}

function quoted(value: string): string {
  return JSON.stringify(value);
}

function isTypescriptStrands(request: ScaffoldRequest): boolean {
  return request.runtime === "strands" && request.language === TYPESCRIPT_LANGUAGE;
}

function runtimeTable(request: ScaffoldRequest): string[] {
  const lines = ["[runtime]", `kind = ${quoted(request.runtime)}`];
  if (ENTRYPOINT_RUNTIMES.has(request.runtime)) {
    const entrypoint =
      request.language === TYPESCRIPT_LANGUAGE
        ? TYPESCRIPT_ENTRYPOINT
        : (PYTHON_ENTRYPOINTS[request.runtime] ?? "");
    lines.push(`entrypoint = ${quoted(entrypoint)}`);
  }
  if (isTypescriptStrands(request)) {
    lines.push(`language = ${quoted(TYPESCRIPT_LANGUAGE)}`);
  }
  if (request.runtime === "custom") {
    lines.push(`command = [${CUSTOM_COMMAND.map(quoted).join(", ")}]`, 'protocol = "text"');
  }
  return lines;
}

function sourceTable(request: ScaffoldRequest): string[] {
  const lines = ["[source]", 'path = "."'];
  if (request.runtime === "strands" && request.language === PYTHON_LANGUAGE) {
    lines.push(`requirements = ${quoted(REQUIREMENTS_FILE_NAME)}`);
  }
  lines.push(`exclude = [${EXCLUDES.map(quoted).join(", ")}]`);
  return lines;
}

function modelTables(request: ScaffoldRequest): string[] {
  const preset = PROVIDER_PRESETS[request.provider]?.preset ?? request.provider;
  const model = [
    "[model]",
    `provider = ${quoted(request.provider)}`,
    `id = ${quoted(request.modelId)}`,
    `gateway = ${quoted(GATEWAY_NAME)}`,
  ];
  const gateway = [
    `[gateways.${GATEWAY_NAME}]`,
    `preset = ${quoted(preset)}`,
    `secret = ${quoted(SECRET_NAME)}`,
  ];
  if (request.provider === "bedrock") {
    model.splice(3, 0, `region = ${quoted(request.region)}`);
    gateway.push(`region = ${quoted(request.region)}`, `models = [${quoted(request.modelId)}]`);
  }
  return [...model, "", ...gateway];
}

export function projectFile(request: ScaffoldRequest): string {
  const sections = [
    [`schema = ${quoted(SCHEMA)}`, `name = ${quoted(request.name)}`],
    runtimeTable(request),
    sourceTable(request),
    modelTables(request),
    ["[agent]", `instructions_file = ${quoted(INSTRUCTIONS_FILE_NAME)}`],
  ];
  return `${sections.map((section) => section.join("\n")).join("\n\n")}\n`;
}

/** Todos los ficheros de `rayito agent init`, por ruta relativa. */
export function scaffoldFiles(request: ScaffoldRequest): Record<string, string> {
  const files: Record<string, string> = {
    [PROJECT_FILE_NAME]: projectFile(request),
    [INSTRUCTIONS_FILE_NAME]: INSTRUCTIONS_TEXT,
  };
  if (isTypescriptStrands(request)) {
    files[TYPESCRIPT_ENTRYPOINT] = STRANDS_TS;
    files["package.json"] = STRANDS_PACKAGE_JSON;
  } else if (request.runtime === "strands") {
    files["agent.py"] = STRANDS_PY;
    files[REQUIREMENTS_FILE_NAME] = STRANDS_REQUIREMENTS;
  } else if (request.runtime === "langgraph") {
    files["graph.py"] = LANGGRAPH_PY;
  } else if (request.runtime === "custom") {
    files["run.sh"] = CUSTOM_RUN_SH;
  }
  return files;
}

/**
 * Escribe `files` bajo `directory` y devuelve las rutas escritas. Sin
 * `force`, si alguna existe no escribe nada y lanza `ScaffoldConflictError`.
 */
export function writeScaffold(
  directory: string,
  files: Readonly<Record<string, string>>,
  options: { readonly force: boolean },
): string[] {
  const existing = Object.keys(files)
    .filter((name) => existsSync(join(directory, name)))
    .sort();
  if (existing.length > 0 && !options.force) {
    throw new ScaffoldConflictError(existing);
  }
  mkdirSync(directory, { recursive: true });
  for (const [name, content] of Object.entries(files)) {
    writeFileSync(join(directory, name), content, "utf8");
  }
  return Object.keys(files);
}
