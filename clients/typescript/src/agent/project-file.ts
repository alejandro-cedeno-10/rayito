/**
 * Lectura de `rayito-agent.toml` / `rayito-agent.json` (ADR-028, adaptador de
 * entrada de `AgentProject.load`). Espejo de `rayito._agent._project_file`:
 * busca el fichero en un directorio, lo convierte en un objeto (`JSON.parse`,
 * o el peer opcional `smol-toml` sólo para un `.toml`) y ofrece el lector con
 * el que `AgentProject.fromObject` resuelve `agent.instructions_file`. No
 * interpreta el documento: eso es el dominio (`project.ts`).
 */

import { readFileSync, realpathSync, statSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { basename, dirname, join, resolve, sep } from "node:path";
import { AGENT_PROJECT_FILE_NAMES } from "../limits.js";
import { loadOptionalPeer } from "../optional.js";
import { detect } from "./detect-domain.js";
import { LocalProjectTree } from "./detect-fs.js";
import { detectedDocument } from "./detect-project.js";
import type { DetectionReport, DetectOverrides } from "./detect-report.js";
import { AgentProject, AgentProjectError, type TextReader } from "./project.js";

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
/** El peer opcional que parsea TOML. */
const TOML_PEER = "smol-toml";

interface TomlModule {
  parse(text: string): Record<string, unknown>;
}

/** El documento leído: su contenido, el directorio del fichero y el lector de
 * ficheros de texto que no puede salir de ese directorio. */
export interface ProjectDocument {
  readonly data: Record<string, unknown>;
  readonly baseDir: string;
  readonly readText: TextReader;
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

async function parse(file: string, text: string): Promise<Record<string, unknown>> {
  if (file.endsWith(TOML_SUFFIX)) {
    const toml = await loadOptionalPeer<TomlModule>(TOML_PEER, "AgentProject.load de un .toml");
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

/** Lee el fichero de proyecto de `path`: un fichero, o un directorio donde se
 * usa el primero de `AGENT_PROJECT_FILE_NAMES` que exista. */
export async function readProjectDocument(path: string): Promise<ProjectDocument> {
  const file = locate(path);
  const data = await parse(file, await readFile(file, "utf8"));
  const baseDir = realpathSync(dirname(resolve(file)));
  return { data, baseDir, readText: reader(baseDir) };
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
  let document: Record<string, unknown> = {};
  let baseDir: string;
  let readText: TextReader;
  try {
    const located = await readProjectDocument(path);
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
    report = detect(new LocalProjectTree(baseDir), overrides);
  } catch (error) {
    if (
      !(error instanceof AgentProjectError) ||
      error.code !== "not_detected" ||
      !declaresRuntime(document)
    ) {
      throw error;
    }
    return [AgentProject.fromObject(document, { baseDir, readText }), undefined];
  }
  const region = options.region ?? documentRegion(document) ?? environmentRegion();
  const filled = detectedDocument(document, report, { name: projectNameOf(baseDir), region });
  return [AgentProject.fromObject(filled, { baseDir, readText }), report];
}
