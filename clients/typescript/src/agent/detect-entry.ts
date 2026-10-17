/**
 * Búsqueda del punto de entrada de un agente en el código del proyecto
 * (ADR-029). Espejo de `rayito._agent._detect_entry`: patrones de línea sobre
 * asignaciones y exportaciones de nivel de módulo, nunca importando ni
 * ejecutando el código del usuario. Las reglas (`agent-detection.json`) son
 * datos; aquí sólo se interpretan.
 *
 * Un candidato tiene un nivel: 1 un objeto ya construido, 2 una fábrica sin
 * argumentos y 3 una fábrica con argumentos (que nunca se elige sola).
 */

import { ENTRY_FACTORY, ENTRY_OBJECT } from "./detect-report.js";
import type { DetectionRules, EntryFileRule, FrameworkRule } from "./detect-rules.js";
import {
  comparePaths,
  compiledRegex,
  matchesOf,
  PATH_SEPARATOR,
  type ProjectIndex,
  PYTHON,
  pathDepth,
  render,
  TYPESCRIPT,
} from "./detect-scan.js";
import { AgentProjectError } from "./project.js";

const TIER_OBJECT = 1;
const TIER_FACTORY_WITH_ARGS = 3;
const PYTHON_SUFFIX = ".py";
const PACKAGE_INIT = "__init__";
const MODULE_SEPARATOR = ".";
const TYPESCRIPT_EXPORT_SEPARATOR = "#";
const PYTHON_ATTRIBUTE_SEPARATOR = ":";
const CURRENT_DIRECTORY = "./";
const IDENTIFIER = /^[A-Za-z_]\w*$/;
const ENTRY_PATH = "runtime.entrypoint";
const OVERRIDE_RULE = "override:entrypoint";

/** El punto de entrada elegido: su fichero, el atributo (o export), si es un
 * objeto o una fábrica, la regla que acertó, el lenguaje y la cadena
 * `modulo:atributo` / `ruta#export` que el proyecto escribe. */
export interface EntryChoice {
  readonly path: string;
  readonly attr: string;
  readonly factory: boolean;
  readonly rule: string;
  readonly language: string;
  readonly entrypoint: string;
}

export function entryKind(entry: EntryChoice): string {
  return entry.factory ? ENTRY_FACTORY : ENTRY_OBJECT;
}

interface Candidate {
  readonly path: string;
  readonly attr: string;
  readonly tier: number;
  readonly rule: string;
}

/** El módulo importable de una ruta `.py`, o `undefined` si algún componente
 * no es un identificador válido. */
export function pythonModule(path: string): string | undefined {
  const stem = path.endsWith(PYTHON_SUFFIX) ? path.slice(0, -PYTHON_SUFFIX.length) : path;
  let parts = stem.split(PATH_SEPARATOR);
  if (parts[parts.length - 1] === PACKAGE_INIT && parts.length > 1) {
    parts = parts.slice(0, -1);
  }
  return parts.every((part) => IDENTIFIER.test(part)) ? parts.join(MODULE_SEPARATOR) : undefined;
}

/** `modulo:atributo` en Python, `ruta#export` en TypeScript. */
export function entrypointString(language: string, path: string, attr: string): string | undefined {
  if (language === PYTHON) {
    const module = pythonModule(path);
    return module === undefined ? undefined : `${module}${PYTHON_ATTRIBUTE_SEPARATOR}${attr}`;
  }
  return `${path}${TYPESCRIPT_EXPORT_SEPARATOR}${attr}`;
}

function stem(path: string): string {
  const name = path.slice(path.lastIndexOf(PATH_SEPARATOR) + 1);
  const dot = name.lastIndexOf(MODULE_SEPARATOR);
  return dot === -1 ? name : name.slice(0, dot);
}

function collect(index: ProjectIndex, framework: FrameworkRule, language: string): Candidate[] {
  const found = new Map<string, Candidate>();
  const sources = index.sources(language);
  for (const rule of framework.entries?.[language] ?? []) {
    const requires = rule.requires === undefined ? undefined : compiledRegex(rule.requires);
    for (const path of sources) {
      const text = index.read(path);
      if (text === undefined || (requires !== undefined && !requires.test(text))) {
        continue;
      }
      for (const match of matchesOf(rule.pattern, text)) {
        const attr = match[1] ?? rule.name;
        if (attr === undefined || entrypointString(language, path, attr) === undefined) {
          continue;
        }
        const key = `${path}\0${attr}`;
        const known = found.get(key);
        if (known === undefined || rule.tier < known.tier) {
          found.set(key, { path, attr, tier: rule.tier, rule: rule.id });
        }
      }
    }
  }
  return [...found.values()];
}

function narrow(pool: Candidate[], keep: (candidate: Candidate) => boolean): Candidate[] {
  const preferred = pool.filter(keep);
  return preferred.length > 0 ? preferred : pool;
}

function listing(language: string, candidates: Candidate[], rules: DetectionRules): string {
  const names = [
    ...new Set(candidates.map((item) => entrypointString(language, item.path, item.attr) ?? "")),
  ].sort(comparePaths);
  return names.slice(0, rules.maxCandidatesListed).join(", ");
}

function ambiguous(
  message: string,
  language: string,
  pool: Candidate[],
  rules: DetectionRules,
): AgentProjectError {
  const text = render(rules.messages[message] as string, {
    candidates: listing(language, pool, rules),
  });
  return new AgentProjectError(`${ENTRY_PATH}: ${text}`, { path: ENTRY_PATH, code: "ambiguous" });
}

function select(
  candidates: Candidate[],
  rules: DetectionRules,
  language: string,
): Candidate | undefined {
  if (candidates.length === 0) {
    return undefined;
  }
  const best = Math.min(...candidates.map((candidate) => candidate.tier));
  let pool = candidates
    .filter((candidate) => candidate.tier === best)
    .sort(
      (left, right) =>
        pathDepth(left.path) - pathDepth(right.path) ||
        comparePaths(left.path, right.path) ||
        comparePaths(left.attr, right.attr),
    );
  const names = new Set(rules.preferNames);
  const files = new Set(rules.preferFiles);
  if (pool.length > 1) {
    pool = narrow(pool, (item) => names.has(item.attr));
  }
  if (pool.length > 1) {
    pool = narrow(pool, (item) => files.has(stem(item.path)));
  }
  if (pool.length > 1) {
    throw ambiguous("ambiguous_entrypoint", language, pool, rules);
  }
  const chosen = pool[0] as Candidate;
  if (chosen.tier === TIER_FACTORY_WITH_ARGS) {
    throw ambiguous("ambiguous_factory", language, pool, rules);
  }
  return chosen;
}

/** El punto de entrada de `framework` en el código de `language`, o
 * `undefined` si no hay ninguno. Varios candidatos del mismo nivel sin una
 * preferencia clara son `ambiguous`, y una fábrica con argumentos también. */
export function findEntry(
  index: ProjectIndex,
  rules: DetectionRules,
  framework: FrameworkRule,
  language: string,
): EntryChoice | undefined {
  const chosen = select(collect(index, framework, language), rules, language);
  if (chosen === undefined) {
    return undefined;
  }
  return {
    path: chosen.path,
    attr: chosen.attr,
    factory: chosen.tier !== TIER_OBJECT,
    rule: chosen.rule,
    language,
    entrypoint: entrypointString(language, chosen.path, chosen.attr) as string,
  };
}

function fromGraphValue(
  value: unknown,
  spec: EntryFileRule,
  rule: string,
): EntryChoice | undefined {
  if (typeof value !== "string" || !value.includes(PYTHON_ATTRIBUTE_SEPARATOR)) {
    return undefined;
  }
  const split = value.lastIndexOf(PYTHON_ATTRIBUTE_SEPARATOR);
  const location = value.slice(0, split);
  const attr = value.slice(split + 1);
  const path = location.startsWith(CURRENT_DIRECTORY)
    ? location.slice(CURRENT_DIRECTORY.length)
    : location;
  const suffix = MODULE_SEPARATOR + path.slice(path.lastIndexOf(MODULE_SEPARATOR) + 1);
  const language = spec.languagesBySuffix[suffix];
  const entrypoint =
    language === undefined || attr === "" ? undefined : entrypointString(language, path, attr);
  if (language === undefined || entrypoint === undefined) {
    return undefined;
  }
  return { path, attr, factory: false, rule, language, entrypoint };
}

/** El punto de entrada que declara un fichero de configuración (el `graphs`
 * de `langgraph.json`): uno solo, el preferido por nombre, o `ambiguous`. */
export function findEntryInFile(
  index: ProjectIndex,
  rules: DetectionRules,
  specName: string,
  rule: string,
): EntryChoice | undefined {
  const spec = rules.entryFiles[specName] as EntryFileRule;
  const document = index.readJson(spec.file);
  const graphs =
    typeof document === "object" && document !== null && !Array.isArray(document)
      ? (document as Record<string, unknown>)[spec.key]
      : undefined;
  if (typeof graphs !== "object" || graphs === null || Array.isArray(graphs)) {
    return undefined;
  }
  const usable = new Map<string, EntryChoice>();
  for (const key of Object.keys(graphs).sort(comparePaths)) {
    const choice = fromGraphValue((graphs as Record<string, unknown>)[key], spec, rule);
    if (choice !== undefined) {
      usable.set(key, choice);
    }
  }
  if (usable.size === 0) {
    return undefined;
  }
  if (usable.size === 1) {
    return [...usable.values()][0];
  }
  for (const key of spec.preferKeys) {
    const preferred = usable.get(key);
    if (preferred !== undefined) {
      return preferred;
    }
  }
  const names = [...usable.values()]
    .map((choice) => choice.entrypoint)
    .sort(comparePaths)
    .slice(0, rules.maxCandidatesListed)
    .join(", ");
  const text = render(rules.messages.ambiguous_entrypoint as string, { candidates: names });
  throw new AgentProjectError(`${ENTRY_PATH}: ${text}`, { path: ENTRY_PATH, code: "ambiguous" });
}

/** Un punto de entrada que da el usuario: se usa tal cual. `ruta#export` es
 * TypeScript, `modulo:atributo` Python y una ruta sin más se reconoce por su
 * extensión. */
export function explicitEntry(entrypoint: string, language: string | undefined): EntryChoice {
  if (entrypoint.includes(TYPESCRIPT_EXPORT_SEPARATOR)) {
    const split = entrypoint.indexOf(TYPESCRIPT_EXPORT_SEPARATOR);
    const path = entrypoint.slice(0, split);
    const attr = entrypoint.slice(split + 1);
    return { path, attr, factory: false, rule: OVERRIDE_RULE, language: TYPESCRIPT, entrypoint };
  }
  if (entrypoint.includes(PYTHON_ATTRIBUTE_SEPARATOR)) {
    const split = entrypoint.indexOf(PYTHON_ATTRIBUTE_SEPARATOR);
    const module = entrypoint.slice(0, split);
    const attr = entrypoint.slice(split + 1);
    const path = module.split(MODULE_SEPARATOR).join(PATH_SEPARATOR) + PYTHON_SUFFIX;
    return { path, attr, factory: false, rule: OVERRIDE_RULE, language: PYTHON, entrypoint };
  }
  const resolved = language ?? (entrypoint.endsWith(PYTHON_SUFFIX) ? PYTHON : TYPESCRIPT);
  return {
    path: entrypoint,
    attr: "",
    factory: false,
    rule: OVERRIDE_RULE,
    language: resolved,
    entrypoint,
  };
}
