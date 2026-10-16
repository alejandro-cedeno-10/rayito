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
import { dirname, join, resolve, sep } from "node:path";
import { AGENT_PROJECT_FILE_NAMES } from "../limits.js";
import { loadOptionalPeer } from "../optional.js";
import { AgentProjectError, type TextReader } from "./project.js";

/** Ruta que el error de un documento ilegible lleva: el documento entero. */
export const DOCUMENT_PATH = "<fichero>";
const TOML_SUFFIX = ".toml";
const JSON_SUFFIX = ".json";
const INSTRUCTIONS_FILE_KEY = "agent.instructions_file";
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
