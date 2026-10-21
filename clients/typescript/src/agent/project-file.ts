/**
 * Lectura de `rayito-agent.toml` / `rayito-agent.json` (ADR-028, adaptador de
 * entrada de `AgentProject.load`). Espejo de `rayito._agent._project_file`:
 * busca el fichero en un directorio, lo convierte en un objeto (`JSON.parse`,
 * o el peer opcional `smol-toml` sólo para un `.toml`) y ofrece el lector con
 * el que `AgentProject.fromObject` resuelve `agent.instructions_file`. No
 * interpreta el documento: eso es el dominio (`project.ts`).
 */

import { readFileSync, realpathSync, statSync } from "node:fs";
import { basename, dirname, join, resolve, sep } from "node:path";
import { AGENT_PROJECT_FILE_NAMES } from "../limits.js";
import { detect } from "./detect-domain.js";
import { graphEntrypoint } from "./detect-entry.js";
import { LocalProjectTree } from "./detect-fs.js";
import { detectedDocument } from "./detect-project.js";
import type { DetectionReport, DetectOverrides } from "./detect-report.js";
import { DETECTION_RULES } from "./detect-rules.js";
import {
  AgentProject,
  AgentProjectError,
  ENTRYPOINT_KEY,
  GRAPH_KEY,
  type GraphResolver,
  LANGGRAPH_FILE,
  type TextReader,
} from "./project.js";
import {
  nodeProjectReader,
  type ProjectSteps,
  readTextStep,
  runWithReader,
  runWithSyncReader,
  type SyncProjectReader,
  syncProjectReader,
  tomlParserStep,
} from "./project-reader.js";

/** Ruta que el error de un documento ilegible lleva: el documento entero. */
export const DOCUMENT_PATH = "<fichero>";
const TOML_SUFFIX = ".toml";
const JSON_SUFFIX = ".json";
const INSTRUCTIONS_FILE_KEY = "agent.instructions_file";
/** Lo que no es válido en el nombre de un proyecto (`[a-z0-9-]`) y el nombre
 * que sale de un directorio sin nombre utilizable. Una sola regla para el
 * andamiaje de `rayito agent init` y para la detección. */
const INVALID_NAME_CHARS = /[^a-z0-9]+/g;
const NAME_EDGE_CHAR = "-";
const DEFAULT_PROJECT_NAME = "my-agent";
const MAX_PROJECT_NAME_CHARS = 63;
const REGION_ENV_VARS = ["AWS_REGION", "AWS_DEFAULT_REGION"] as const;
const TOML_FEATURE = "AgentProject.load de un .toml";

/** El documento leído: su contenido, el directorio del fichero, el lector de
 * ficheros de texto que no puede salir de ese directorio y el resolvedor de
 * `[runtime] graph` que lee el `langgraph.json` de ese directorio. */
export interface ProjectDocument {
  readonly data: Record<string, unknown>;
  readonly baseDir: string;
  readonly readText: TextReader;
  readonly resolveGraph: GraphResolver;
}

function kindOf(path: string): "file" | "directory" | undefined {
  try {
    const info = statSync(path);
    return info.isFile() ? "file" : info.isDirectory() ? "directory" : undefined;
  } catch {
    return undefined;
  }
}

function locate(target: string): string {
  const kind = kindOf(target);
  if (kind === "file") {
    return target;
  }
  if (kind === "directory") {
    for (const name of AGENT_PROJECT_FILE_NAMES) {
      const candidate = join(target, name);
      if (kindOf(candidate) === "file") {
        return candidate;
      }
    }
  }
  throw new AgentProjectError(
    `${DOCUMENT_PATH}: no hay ningún fichero ${AGENT_PROJECT_FILE_NAMES.join(" ni ")} en la ruta`,
    { path: DOCUMENT_PATH, code: "file_not_found" },
  );
}

function invalid(detail: string, cause?: unknown): AgentProjectError {
  const error = new AgentProjectError(`${DOCUMENT_PATH}: ${detail}`, {
    path: DOCUMENT_PATH,
    code: "invalid_value",
  });
  if (cause !== undefined) {
    error.cause = cause;
  }
  return error;
}

function* parse(file: string, text: string): ProjectSteps<Record<string, unknown>> {
  if (file.endsWith(TOML_SUFFIX)) {
    const toml = yield* tomlParserStep(TOML_FEATURE);
    try {
      return toml.parse(text);
    } catch (cause) {
      throw invalid("el fichero no es un documento de proyecto válido", cause);
    }
  }
  if (file.endsWith(JSON_SUFFIX)) {
    let document: unknown;
    try {
      document = JSON.parse(text);
    } catch (cause) {
      throw invalid("el fichero no es un documento de proyecto válido", cause);
    }
    if (typeof document !== "object" || document === null || Array.isArray(document)) {
      throw invalid("el fichero no es un documento de proyecto válido");
    }
    return document as Record<string, unknown>;
  }
  throw invalid(`la extensión debe ser ${TOML_SUFFIX} o ${JSON_SUFFIX}`);
}

function reader(baseDir: string): TextReader {
  const root = realpathSync(baseDir);
  return (path: string): string => {
    if (kindOf(path) !== "file") {
      throw new AgentProjectError(`${INSTRUCTIONS_FILE_KEY}: el fichero no existe`, {
        path: INSTRUCTIONS_FILE_KEY,
        code: "file_not_found",
      });
    }
    const real = realpathSync(path);
    if (real !== root && !real.startsWith(root + sep)) {
      throw new AgentProjectError(`${INSTRUCTIONS_FILE_KEY}: sale del directorio del fichero`, {
        path: INSTRUCTIONS_FILE_KEY,
        code: "path_escape",
      });
    }
    return readFileSync(real, "utf8");
  };
}

/** El JSON de `langgraph.json` de `baseDir`, o `undefined` si no existe o no
 * es JSON. */
function langgraphDocument(baseDir: string, readText: TextReader): unknown {
  try {
    return JSON.parse(readText(join(baseDir, LANGGRAPH_FILE)));
  } catch {
    return undefined;
  }
}

/** El resolvedor de `[runtime] graph` de un directorio: el `entrypoint` del
 * grafo de ese nombre en su `langgraph.json` (`selectGraph`). */
export function graphResolver(baseDir: string, readText: TextReader): GraphResolver {
  return (graph) => graphEntrypoint(langgraphDocument(baseDir, readText), graph, DETECTION_RULES);
}

/** Los pasos de `readProjectDocument`, para la ruta asíncrona y la síncrona. */
export function* projectDocumentSteps(path: string): ProjectSteps<ProjectDocument> {
  const file = locate(path);
  const data = yield* parse(file, yield* readTextStep(file));
  const baseDir = realpathSync(dirname(resolve(file)));
  const readText = reader(baseDir);
  return { data, baseDir, readText, resolveGraph: graphResolver(baseDir, readText) };
}

/** Lee el fichero de proyecto de `path`: un fichero, o un directorio donde se
 * usa el primero de `AGENT_PROJECT_FILE_NAMES` que exista. */
export async function readProjectDocument(path: string): Promise<ProjectDocument> {
  return runWithReader(projectDocumentSteps(path), nodeProjectReader);
}

/** Quita los `-` de los dos extremos, sin expresión regular (una con
 * alternancia de extremos es polinómica con entradas largas). */
function trimNameEdges(text: string): string {
  let start = 0;
  let end = text.length;
  while (start < end && text.charAt(start) === NAME_EDGE_CHAR) start += 1;
  while (end > start && text.charAt(end - 1) === NAME_EDGE_CHAR) end -= 1;
  return text.slice(start, end);
}

/** El nombre de proyecto que sale del nombre del directorio: en minúsculas,
 * con `-` en lugar de cualquier otro carácter, como mucho
 * `MAX_PROJECT_NAME_CHARS`, y `DEFAULT_PROJECT_NAME` si no queda nada. */
export function projectNameOf(directory: string): string {
  const cleaned = trimNameEdges(
    basename(resolve(directory)).toLowerCase().replace(INVALID_NAME_CHARS, "-"),
  );
  return trimNameEdges(cleaned.slice(0, MAX_PROJECT_NAME_CHARS)) || DEFAULT_PROJECT_NAME;
}

/** Los `overrides` de la detección con el `[runtime] graph` del fichero de
 * proyecto si la bandera no fija otro, para que la detección elija ese grafo
 * y no falle con varios. */
export function withFileGraph(
  overrides: DetectOverrides | undefined,
  document: Record<string, unknown>,
): DetectOverrides | undefined {
  const runtime = document.runtime;
  const table =
    typeof runtime === "object" && runtime !== null ? (runtime as Record<string, unknown>) : {};
  const graph = table[GRAPH_KEY];
  if (typeof graph !== "string" || overrides?.graph !== undefined || ENTRYPOINT_KEY in table) {
    return overrides;
  }
  return { ...overrides, graph };
}

function declaresRuntime(document: Record<string, unknown>): boolean {
  const runtime = document.runtime;
  return typeof runtime === "object" && runtime !== null && "kind" in runtime;
}

function documentRegion(document: Record<string, unknown>): string | undefined {
  const deploy = document.deploy;
  const value =
    typeof deploy === "object" && deploy !== null
      ? (deploy as Record<string, unknown>).region
      : undefined;
  return typeof value === "string" && value !== "" ? value : undefined;
}

function environmentRegion(): string | undefined {
  for (const name of REGION_ENV_VARS) {
    const value = process.env[name];
    if (value) {
      return value;
    }
  }
  return undefined;
}

/** El `AgentProject` de `path` con lo que el fichero de proyecto (si lo hay)
 * no dice completado por la detección, y su informe. Lee ficheros locales y no
 * ejecuta nada del proyecto; no llama a AWS. Un fichero que ya declara
 * `[runtime] kind` basta por sí solo: si el directorio no tiene nada que
 * detectar, el informe es `undefined` y el proyecto es el del fichero. */
export async function detectProject(
  path: string,
  overrides?: DetectOverrides,
  options: { readonly region?: string | undefined } = {},
): Promise<readonly [AgentProject, DetectionReport | undefined]> {
  return runWithReader(detectProjectSteps(path, overrides, options), nodeProjectReader);
}

/** `detectProject` con un lector síncrono (`planAgentCoreSync`, los constructos CDK). */
export function detectProjectSync(
  path: string,
  overrides?: DetectOverrides,
  options: { readonly region?: string | undefined } = {},
  projectReader: SyncProjectReader = syncProjectReader,
): readonly [AgentProject, DetectionReport | undefined] {
  return runWithSyncReader(detectProjectSteps(path, overrides, options), projectReader);
}

/** Los pasos de `detectProject`, para la ruta asíncrona y la síncrona. */
export function* detectProjectSteps(
  path: string,
  overrides: DetectOverrides | undefined,
  options: { readonly region?: string | undefined },
): ProjectSteps<readonly [AgentProject, DetectionReport | undefined]> {
  let document: Record<string, unknown> = {};
  let baseDir: string;
  let readText: TextReader;
  try {
    const located = yield* projectDocumentSteps(path);
    document = { ...located.data };
    baseDir = located.baseDir;
    readText = located.readText;
  } catch (error) {
    if (
      !(error instanceof AgentProjectError) ||
      error.code !== "file_not_found" ||
      kindOf(path) !== "directory"
    ) {
      throw error;
    }
    baseDir = realpathSync(resolve(path));
    readText = reader(baseDir);
  }
  let report: DetectionReport;
  try {
    report = detect(new LocalProjectTree(baseDir), withFileGraph(overrides, document));
  } catch (error) {
    if (
      !(error instanceof AgentProjectError) ||
      error.code !== "not_detected" ||
      !declaresRuntime(document)
    ) {
      throw error;
    }
    const resolveGraph = graphResolver(baseDir, readText);
    return [AgentProject.fromObject(document, { baseDir, readText, resolveGraph }), undefined];
  }
  const region = options.region ?? documentRegion(document) ?? environmentRegion();
  const filled = detectedDocument(document, report, { name: projectNameOf(baseDir), region });
  const resolveGraph = graphResolver(baseDir, readText);
  return [AgentProject.fromObject(filled, { baseDir, readText, resolveGraph }), report];
}
