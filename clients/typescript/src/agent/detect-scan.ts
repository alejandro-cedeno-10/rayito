/**
 * Lectura acotada del árbol de un proyecto para la detección (ADR-029).
 * Espejo de `rayito._agent._detect_scan`: el puerto `ProjectTree`, el índice
 * de ficheros que filtra lo excluido y lo demasiado profundo, y la extracción
 * de dependencias de los manifiestos (`requirements*.txt`, `pyproject.toml`,
 * `package.json`) sin ejecutar nada ni interpretar TOML. Todo es puro: la E/S
 * está detrás del puerto.
 */

import {
  AGENT_DETECTION_MAX_DEPTH,
  AGENT_DETECTION_MAX_FILE_BYTES,
  AGENT_DETECTION_MAX_FILES,
  AGENT_SOURCE_DEFAULT_EXCLUDES,
} from "../limits.js";
import type { DetectionWarning } from "./detect-report.js";
import type { DetectionRules } from "./detect-rules.js";

export const PATH_SEPARATOR = "/";
/** Los identificadores de lenguaje de las reglas y el manifiesto de Node. */
export const PYTHON = "python";
export const TYPESCRIPT = "typescript";
export const PACKAGE_JSON = "package.json";
/** Las expresiones de las reglas se evalúan por líneas, como el modo
 * `re.MULTILINE | re.ASCII` de Python. */
const REGEX_FLAGS = "m";
const GLOBAL_REGEX_FLAGS = "gm";
/** Las cuatro secciones de dependencias de un `package.json`. */
export const PACKAGE_JSON_SECTIONS = [
  "dependencies",
  "devDependencies",
  "peerDependencies",
  "optionalDependencies",
] as const;
const REQUIREMENT = /^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[([^\]]*)\])?/;
const SECTION_HEADER = /^\[+([^\]]+)\]+\s*$/;
const ARRAY_OPEN = /^\s*[A-Za-z0-9_-]+\s*=\s*\[/;
const POETRY_DEPENDENCIES = /^tool\.poetry(?:\.group\.[\w-]+)?\.dependencies$/;
const POETRY_KEY = /^([A-Za-z0-9][A-Za-z0-9._-]*)\s*=/;
const QUOTED = /"([^"]*)"|'([^']*)'/g;
const NAME_SEPARATORS = /[-_.]+/g;
const PROJECT_SECTION = "project";
const OPTIONAL_DEPENDENCIES_SECTION = "project.optional-dependencies";
const DEPENDENCY_GROUPS_SECTION = "dependency-groups";
const DEPENDENCIES_KEY = /^\s*dependencies\s*=\s*\[/;
const POETRY_PYTHON = "python";

/** El puerto de lectura del proyecto: `listFiles` devuelve rutas relativas con
 * `/` (como mucho `maxFiles`, hasta `maxDepth` carpetas de profundidad) y
 * `readText` el texto de un fichero (como mucho `maxBytes`), o `undefined` si
 * no existe o no es texto. */
export interface ProjectTree {
  listFiles(maxDepth: number, maxFiles: number): readonly string[];
  readText(path: string, maxBytes: number): string | undefined;
}

const regexCache = new Map<string, RegExp>();
const globalRegexCache = new Map<string, RegExp>();

/** La expresión de una regla, compilada una vez. Sin estado: para buscar. */
export function compiledRegex(pattern: string): RegExp {
  let regex = regexCache.get(pattern);
  if (regex === undefined) {
    regex = new RegExp(pattern, REGEX_FLAGS);
    regexCache.set(pattern, regex);
  }
  return regex;
}

/** Todas las coincidencias de una expresión de regla en `text`. */
export function matchesOf(pattern: string, text: string): RegExpExecArray[] {
  let regex = globalRegexCache.get(pattern);
  if (regex === undefined) {
    regex = new RegExp(pattern, GLOBAL_REGEX_FLAGS);
    globalRegexCache.set(pattern, regex);
  }
  regex.lastIndex = 0;
  return [...text.matchAll(regex)] as RegExpExecArray[];
}

/** Orden de rutas por código de carácter, el mismo en los dos SDK. */
export function comparePaths(left: string, right: string): number {
  return left < right ? -1 : left > right ? 1 : 0;
}

export function pathDepth(path: string): number {
  return path.split(PATH_SEPARATOR).length - 1;
}

function escapeRegex(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

const globCache = new Map<string, RegExp>();

/** `*` es la única comodín: cualquier cosa menos `/`. */
export function globMatch(pattern: string, name: string): boolean {
  let regex = globCache.get(pattern);
  if (regex === undefined) {
    regex = new RegExp(`^${pattern.split("*").map(escapeRegex).join("[^/]*")}$`);
    globCache.set(pattern, regex);
  }
  return regex.test(name);
}

/** Rellena los `{nombre}` de una plantilla de mensaje. */
export function render(template: string, values: Readonly<Record<string, string>>): string {
  let text = template;
  for (const [key, value] of Object.entries(values)) {
    text = text.split(`{${key}}`).join(value);
  }
  return text;
}

/** Acumula los avisos de una detección con el texto de las reglas. */
export class WarningSink {
  readonly items: DetectionWarning[] = [];
  readonly #templates: Readonly<Record<string, string>>;

  constructor(rules: DetectionRules) {
    this.#templates = rules.warnings;
  }

  add(code: string, values: Readonly<Record<string, string>> = {}): void {
    const template = this.#templates[code] ?? code;
    const detail = render(template, values);
    if (!this.items.some((item) => item.code === code && item.detail === detail)) {
      this.items.push({ code, detail });
    }
  }
}

/** El nombre de un paquete de Python normalizado (PEP 503). */
export function normalizePythonName(name: string): string {
  return name.replace(NAME_SEPARATORS, "-").toLowerCase();
}

/** Los nombres con que un requisito cuenta como dependencia: el del paquete y
 * uno por extra (`strands-agents[openai]`). */
export function requirementNames(spec: string): string[] {
  const match = REQUIREMENT.exec(spec);
  if (match === null) {
    return [];
  }
  const name = normalizePythonName(match[1] as string);
  const extras = (match[2] ?? "")
    .split(",")
    .map((extra) => extra.trim())
    .filter((extra) => extra !== "");
  return [name, ...extras.map((extra) => `${name}[${normalizePythonName(extra)}]`)];
}

export function requirementsDependencies(text: string): string[] {
  const names: string[] = [];
  for (const line of text.split("\n")) {
    const stripped = line.trim();
    if (stripped !== "" && !stripped.startsWith("#") && !stripped.startsWith("-")) {
      names.push(...requirementNames(stripped));
    }
  }
  return names;
}

function quotedStrings(line: string): string[] {
  return [...line.matchAll(QUOTED)].map((match) => match[1] || match[2] || "");
}

function closesArray(line: string): boolean {
  return line.replace(QUOTED, "").includes("]");
}

function namesOfStrings(line: string): string[] {
  return quotedStrings(line).flatMap(requirementNames);
}

function opensDependencyArray(section: string, line: string): boolean {
  if (section === PROJECT_SECTION) {
    return DEPENDENCIES_KEY.test(line);
  }
  if (section === OPTIONAL_DEPENDENCIES_SECTION || section === DEPENDENCY_GROUPS_SECTION) {
    return ARRAY_OPEN.test(line);
  }
  return false;
}

/** Las dependencias declaradas en un `pyproject.toml` (`[project]`, sus
 * extras, `[dependency-groups]` y las tablas de Poetry), leídas por líneas:
 * sin intérprete de TOML, igual en los dos SDK. */
export function pyprojectDependencies(text: string): string[] {
  const names: string[] = [];
  let section = "";
  let inArray = false;
  for (const line of text.split("\n")) {
    const stripped = line.trim();
    const header = SECTION_HEADER.exec(stripped);
    if (header !== null && !inArray) {
      section = (header[1] as string).trim();
    } else if (inArray) {
      names.push(...namesOfStrings(line));
      inArray = !closesArray(line);
    } else if (opensDependencyArray(section, line)) {
      names.push(...namesOfStrings(line));
      inArray = !closesArray(line);
    } else if (POETRY_DEPENDENCIES.test(section)) {
      const key = POETRY_KEY.exec(stripped);
      if (key !== null && key[1] !== POETRY_PYTHON) {
        names.push(normalizePythonName(key[1] as string));
      }
    }
  }
  return names;
}

export function packageJsonDependencies(document: unknown): string[] {
  if (typeof document !== "object" || document === null || Array.isArray(document)) {
    return [];
  }
  const record = document as Record<string, unknown>;
  return PACKAGE_JSON_SECTIONS.flatMap((section) => {
    const entries = record[section];
    return typeof entries === "object" && entries !== null && !Array.isArray(entries)
      ? Object.keys(entries)
      : [];
  });
}

function byDepthThenPath(left: string, right: string): number {
  return pathDepth(left) - pathDepth(right) || comparePaths(left, right);
}

function isExcluded(path: string): boolean {
  return path
    .split(PATH_SEPARATOR)
    .some((segment) =>
      AGENT_SOURCE_DEFAULT_EXCLUDES.some((pattern) => globMatch(pattern, segment)),
    );
}

/**
 * Los ficheros del proyecto ya filtrados (sin lo excluido, sin pasar de
 * `AGENT_DETECTION_MAX_DEPTH` ni de `AGENT_DETECTION_MAX_FILES`) y su lectura
 * con caché. Los manifiestos y ficheros de configuración se buscan sólo en la
 * raíz del proyecto; el código fuente, en todo el árbol.
 */
export class ProjectIndex {
  readonly paths: readonly string[];
  readonly rootFiles: readonly string[];
  readonly #tree: ProjectTree;
  readonly #rules: DetectionRules;
  readonly #warnings: WarningSink;
  readonly #texts = new Map<string, string | undefined>();
  readonly #json = new Map<string, unknown>();

  constructor(tree: ProjectTree, rules: DetectionRules, warnings: WarningSink) {
    this.#tree = tree;
    this.#rules = rules;
    this.#warnings = warnings;
    this.paths = this.#filtered();
    this.rootFiles = this.paths.filter((path) => !path.includes(PATH_SEPARATOR));
  }

  #filtered(): readonly string[] {
    const exempt = new Set(this.#rules.envExampleFiles);
    const kept = this.#tree
      .listFiles(AGENT_DETECTION_MAX_DEPTH, AGENT_DETECTION_MAX_FILES + 1)
      .filter(
        (path) =>
          (exempt.has(path) || !isExcluded(path)) && pathDepth(path) <= AGENT_DETECTION_MAX_DEPTH,
      );
    const ordered = [...new Set(kept)].sort(byDepthThenPath);
    if (ordered.length > AGENT_DETECTION_MAX_FILES) {
      this.#warnings.add("scan_truncated");
    }
    return ordered.slice(0, AGENT_DETECTION_MAX_FILES);
  }

  read(path: string): string | undefined {
    if (!this.#texts.has(path)) {
      const text = this.#tree.readText(path, AGENT_DETECTION_MAX_FILE_BYTES);
      this.#texts.set(path, text === undefined ? undefined : text.replaceAll("\r\n", "\n"));
    }
    return this.#texts.get(path);
  }

  /** El JSON de un fichero, o `undefined` (con un aviso) si no se entiende. */
  readJson(path: string): unknown {
    if (!this.#json.has(path)) {
      const text = this.read(path);
      let parsed: unknown;
      if (text !== undefined) {
        try {
          parsed = JSON.parse(text);
        } catch {
          this.#warnings.add("file_unreadable", { file: path });
        }
      }
      this.#json.set(path, parsed);
    }
    return this.#json.get(path);
  }

  rootMatches(pattern: string): string[] {
    return this.rootFiles.filter((path) => globMatch(pattern, path));
  }

  hasRootFile(name: string): boolean {
    return this.rootFiles.includes(name);
  }

  hasDirectory(name: string): boolean {
    const prefix = `${name}${PATH_SEPARATOR}`;
    return this.paths.some((path) => path.startsWith(prefix));
  }

  /** Los ficheros de código del lenguaje, de menos a más profundos, sin las
   * carpetas de pruebas, ejemplos ni documentación. */
  sources(language: string): string[] {
    const spec = this.#rules.languages[language];
    if (spec === undefined) {
      return [];
    }
    const ignored = new Set(this.#rules.ignoredPathSegments);
    return this.paths.filter((path) => {
      const folders = path.split(PATH_SEPARATOR);
      const name = folders.pop() as string;
      return (
        !folders.some((folder) => ignored.has(folder)) &&
        spec.sources.some((pattern) => globMatch(pattern, name)) &&
        !spec.sourceExcludes.some((pattern) => globMatch(pattern, name))
      );
    });
  }

  /** Nombre de dependencia -> el primer manifiesto de la raíz que la declara. */
  dependencies(language: string): ReadonlyMap<string, string> {
    const found = new Map<string, string>();
    for (const entry of this.#rules.languages[language]?.dependencyFiles ?? []) {
      for (const path of this.rootMatches(entry.glob)) {
        for (const name of this.#namesIn(path, entry.format)) {
          if (!found.has(name)) {
            found.set(name, path);
          }
        }
      }
    }
    return found;
  }

  #namesIn(path: string, manifestFormat: string): string[] {
    if (manifestFormat === "package-json") {
      return packageJsonDependencies(this.readJson(path));
    }
    const text = this.read(path);
    if (text === undefined) {
      return [];
    }
    return manifestFormat === "pyproject"
      ? pyprojectDependencies(text)
      : requirementsDependencies(text);
  }
}
