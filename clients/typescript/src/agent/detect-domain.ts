/**
 * Detección de un proyecto de agente (ADR-029, D1): servicio de dominio puro
 * sobre el puerto `ProjectTree`. Espejo de `rayito._agent._detect_domain`. Lee
 * manifiestos y busca patrones de línea; nunca importa, ejecuta ni evalúa el
 * código del usuario, así que es seguro sobre cualquier repositorio. La
 * precedencia es fichero de proyecto > bandera o argumento (`DetectOverrides`)
 * > detección > valor por defecto con nombre. Un caso poco claro falla con
 * `ambiguous` o `not_detected` y nombra la clave o la bandera que lo resuelve;
 * nunca adivina.
 *
 * Las reglas son datos (`agent-detection.json`, generado a
 * `detection-rules.gen.ts`) y los mismos vectores (`testdata/agent-detection/`)
 * los comprueban en TypeScript y en Python.
 */

import {
  CUSTOM_KIND,
  detectApp,
  detectLanggraphApi,
  PROTOCOL_LANGGRAPH_API,
  validateProtocolOverride,
} from "./detect-app.js";
import {
  type EntryChoice,
  entryKind,
  explicitEntry,
  findEntry,
  findEntryInFile,
  GRAPH_PATH,
  graphNotFound,
} from "./detect-entry.js";
import { detectInstall } from "./detect-install.js";
import { detectProviders } from "./detect-providers.js";
import {
  type DetectionAlternative,
  type DetectionApp,
  type DetectionEvidence,
  DetectionReport,
  type DetectOverrides,
  PROTOCOL_FRAMEWORK,
} from "./detect-report.js";
import { DETECTION_RULES, type DetectionRules, type FrameworkRule } from "./detect-rules.js";
import { ProjectIndex, type ProjectTree, render, WarningSink } from "./detect-scan.js";
import { AGENT_LANGUAGES, AgentProjectError, type AgentProjectErrorCode } from "./project.js";

const FRAMEWORK_PATH = "runtime.framework";
const LANGUAGE_PATH = "runtime.language";
const KIND_PATH = "runtime.kind";
const SOURCE_PATH = "source";
const FORCED_RULE = "override:framework";
/** La opción del informe (y la clave de `[runtime]`) con el grafo elegido. */
const GRAPH_OPTION = "graph";

interface Candidate {
  readonly order: number;
  readonly framework: FrameworkRule;
  readonly language: string | undefined;
  readonly evidence: readonly DetectionEvidence[];
}

function failure(path: string, code: AgentProjectErrorCode, text: string): AgentProjectError {
  return new AgentProjectError(`${path}: ${text}`, { path, code });
}

function invalidOverride(
  option: string,
  path: string,
  value: string,
  allowed: readonly string[],
  rules: DetectionRules,
): AgentProjectError {
  const text = render(rules.messages.invalid_override as string, {
    value,
    option,
    allowed: allowed.join(", "),
  });
  return failure(path, "invalid_value", text);
}

function frameworkNames(rules: DetectionRules): string[] {
  const names = new Set<string>([rules.httpEntryKeyword]);
  for (const item of rules.frameworks) {
    names.add(item.id);
    names.add(item.framework);
  }
  return [...names].sort();
}

function checkOverrides(overrides: DetectOverrides, rules: DetectionRules): void {
  if (
    overrides.language !== undefined &&
    !(AGENT_LANGUAGES as readonly string[]).includes(overrides.language)
  ) {
    throw invalidOverride("--language", LANGUAGE_PATH, overrides.language, AGENT_LANGUAGES, rules);
  }
  const names = frameworkNames(rules);
  if (overrides.framework !== undefined && !names.includes(overrides.framework)) {
    throw invalidOverride("--framework", FRAMEWORK_PATH, overrides.framework, names, rules);
  }
  validateProtocolOverride(overrides.protocol);
}

function presentLanguages(
  index: ProjectIndex,
  rules: DetectionRules,
  overrides: DetectOverrides,
): string[] {
  if (overrides.language !== undefined) {
    return [overrides.language];
  }
  return Object.entries(rules.languages)
    .filter(([, spec]) => spec.markers.some((marker) => index.rootMatches(marker).length > 0))
    .map(([language]) => language);
}

/** La evidencia de un framework que se reconoce por ficheros de la raíz, o
 * `undefined` si no tiene reglas de ficheros. */
function fileEvidence(
  framework: FrameworkRule,
  index: ProjectIndex,
): DetectionEvidence[] | undefined {
  const anywhere = framework.agnosticFiles ?? [];
  const every = framework.agnosticAllFiles ?? [];
  const some = framework.agnosticAnyFiles ?? [];
  if (anywhere.length === 0 && every.length === 0 && some.length === 0) {
    return undefined;
  }
  if (every.length > 0 && !every.every((name) => index.hasRootFile(name))) {
    return [];
  }
  const found = [...anywhere, ...every, ...some].filter((name) => index.hasRootFile(name));
  if ((some.length > 0 && !some.some((name) => index.hasRootFile(name))) || found.length === 0) {
    return [];
  }
  return found.map((name) => ({ path: name, rule: `file:${name}` }));
}

function isForced(framework: FrameworkRule, overrides: DetectOverrides): boolean {
  const name = overrides.framework;
  return name !== undefined && (name === framework.id || name === framework.framework);
}

function forcedEvidence(): DetectionEvidence[] {
  return [{ path: undefined, rule: FORCED_RULE }];
}

function dependencyCandidates(
  index: ProjectIndex,
  order: number,
  framework: FrameworkRule,
  language: string,
  forced: boolean,
): Candidate[] {
  const wanted = framework.deps?.[language];
  if (wanted === undefined) {
    return [];
  }
  const dependencies = index.dependencies(language);
  const evidence: DetectionEvidence[] = [];
  for (const name of wanted) {
    const path = dependencies.get(name);
    if (path !== undefined) {
      evidence.push({ path, rule: `dep:${name}` });
    }
  }
  if (evidence.length === 0 && !forced) {
    return [];
  }
  return [
    { order, framework, language, evidence: evidence.length > 0 ? evidence : forcedEvidence() },
  ];
}

function candidatesOf(
  index: ProjectIndex,
  rules: DetectionRules,
  languages: readonly string[],
  overrides: DetectOverrides,
): Candidate[] {
  const http = rules.httpEntryKeyword;
  const httpOnly = overrides.framework === http || overrides.entrypoint === http;
  const found: Candidate[] = [];
  rules.frameworks.forEach((framework, order) => {
    const forced = isForced(framework, overrides);
    if (
      (overrides.framework !== undefined && !httpOnly && !forced) ||
      (httpOnly && !framework.webOnly)
    ) {
      return;
    }
    const files = fileEvidence(framework, index);
    if (files !== undefined) {
      if (files.length > 0 || forced) {
        found.push({
          order,
          framework,
          language: undefined,
          evidence: files.length > 0 ? files : forcedEvidence(),
        });
      }
      return;
    }
    const forcedLanguages = forced ? Object.keys(framework.deps ?? {}) : [];
    for (const language of languages.length > 0 ? languages : forcedLanguages) {
      found.push(...dependencyCandidates(index, order, framework, language, forced));
    }
  });
  return found;
}

function entryFor(
  candidate: Candidate,
  index: ProjectIndex,
  rules: DetectionRules,
  overrides: DetectOverrides,
): EntryChoice | undefined {
  const framework = candidate.framework;
  if (overrides.graph !== undefined && overrides.entrypoint !== undefined) {
    throw failure(GRAPH_PATH, "conflict", rules.messages.graph_conflict as string);
  }
  if (overrides.entrypoint !== undefined && overrides.entrypoint !== rules.httpEntryKeyword) {
    return explicitEntry(overrides.entrypoint, candidate.language);
  }
  if (framework.entryFile !== undefined) {
    return findEntryInFile(index, rules, framework.entryFile, framework.id, overrides.graph);
  }
  if (overrides.graph !== undefined) {
    throw graphNotFound(overrides.graph, new Map(), rules);
  }
  if (framework.entries !== undefined && candidate.language !== undefined) {
    return findEntry(index, rules, framework, candidate.language);
  }
  return undefined;
}

/** `{ graph: nombre }` cuando el grafo de `langgraph.json` lo eligió el
 * usuario; si no, nada, así que un informe sin `--graph` no cambia. */
function graphOption(framework: FrameworkRule, overrides: DetectOverrides): Record<string, string> {
  if (overrides.graph === undefined || framework.entryFile === undefined) {
    return {};
  }
  return { [GRAPH_OPTION]: overrides.graph };
}

function needsEntry(framework: FrameworkRule): boolean {
  return framework.entries !== undefined || framework.entryFile !== undefined;
}

function choose(
  candidates: Candidate[],
  index: ProjectIndex,
  rules: DetectionRules,
  overrides: DetectOverrides,
  sink: WarningSink,
): { readonly candidate: Candidate; readonly entry: EntryChoice | undefined } {
  const remaining = [...candidates].sort((left, right) => {
    const byLanguage =
      (left.language ?? "") < (right.language ?? "")
        ? -1
        : (left.language ?? "") > (right.language ?? "")
          ? 1
          : 0;
    return left.order - right.order || byLanguage;
  });
  while (remaining.length > 0) {
    const first = remaining[0] as Candidate;
    const ties = remaining.filter((item) => item.order === first.order);
    const languages = new Set(
      ties.map((item) => item.language).filter((item) => item !== undefined),
    );
    if (languages.size > 1) {
      throw failure(LANGUAGE_PATH, "ambiguous", rules.messages.ambiguous_language as string);
    }
    const head = ties[0] as Candidate;
    const entry = entryFor(head, index, rules, overrides);
    if (entry !== undefined || !needsEntry(head.framework)) {
      return { candidate: head, entry };
    }
    sink.add("entrypoint_not_found", { framework: head.framework.framework });
    remaining.splice(remaining.indexOf(head), 1);
  }
  throw failure(KIND_PATH, "not_detected", rules.messages.not_detected_agent as string);
}

function kindOf(framework: FrameworkRule, language: string | undefined): string {
  const fromLanguage = language === undefined ? undefined : framework.kind[language];
  return fromLanguage ?? (Object.values(framework.kind)[0] as string);
}

function quietEntry(
  candidate: Candidate,
  index: ProjectIndex,
  rules: DetectionRules,
): EntryChoice | undefined {
  try {
    return findEntry(index, rules, candidate.framework, candidate.language ?? "");
  } catch (error) {
    if (error instanceof AgentProjectError) {
      return undefined;
    }
    throw error;
  }
}

function alternativesOf(
  index: ProjectIndex,
  rules: DetectionRules,
  languages: readonly string[],
  sink: WarningSink,
): DetectionAlternative[] {
  const found: DetectionAlternative[] = [];
  rules.frameworks.forEach((framework, order) => {
    if (!framework.webOnly) {
      return;
    }
    for (const language of languages) {
      for (const candidate of dependencyCandidates(index, order, framework, language, false)) {
        const entry = quietEntry(candidate, index, rules);
        if (entry !== undefined) {
          found.push({
            framework: framework.framework,
            kind: kindOf(framework, language),
            language,
            entrypoint: entry.entrypoint,
          });
        }
      }
    }
  });
  const [first] = found;
  if (first !== undefined) {
    sink.add("http_app_alternative", { framework: first.framework });
  }
  return found;
}

function skillsOptions(framework: FrameworkRule, index: ProjectIndex): Record<string, string> {
  const options: Record<string, string> = {};
  for (const [key, spec] of Object.entries(framework.options ?? {})) {
    if (index.hasDirectory(spec.directory)) {
      options[key] = spec.directory;
    }
  }
  return options;
}

/**
 * Detecta lenguaje, framework, punto de entrada, protocolo, app HTTP, plan de
 * instalación y proveedor de modelo de un proyecto, sin ejecutar su código.
 * `overrides` fuerza lo que el usuario indicó.
 */
export function detect(
  tree: ProjectTree,
  overrides: DetectOverrides = {},
  rules: DetectionRules = DETECTION_RULES,
): DetectionReport {
  checkOverrides(overrides, rules);
  const sink = new WarningSink(rules);
  const index = new ProjectIndex(tree, rules, sink);
  const languages = presentLanguages(index, rules, overrides);
  const candidates = candidatesOf(index, rules, languages, overrides);
  if (candidates.length === 0) {
    const [path, key] =
      languages.length > 0
        ? [KIND_PATH, "not_detected_agent"]
        : [SOURCE_PATH, "not_detected_manifest"];
    throw failure(path as string, "not_detected", rules.messages[key as string] as string);
  }
  const { candidate: winner, entry } = choose(candidates, index, rules, overrides, sink);
  const framework = winner.framework;
  const language =
    entry !== undefined && winner.language === undefined ? entry.language : winner.language;
  const evidence: DetectionEvidence[] = [...winner.evidence];
  if (entry !== undefined) {
    evidence.push({ path: entry.path || undefined, rule: `entry:${entry.rule}` });
  }
  const install =
    language === undefined ? undefined : detectInstall(language, index, rules, sink, evidence);
  let app: DetectionApp | undefined;
  let protocol: string = PROTOCOL_FRAMEWORK;
  if (framework.app !== undefined && entry !== undefined) {
    const detected = detectApp(
      index,
      rules,
      framework,
      entry,
      install?.manager,
      overrides.protocol,
      sink,
    );
    app = detected.app;
    protocol = detected.protocol;
  } else if (overrides.protocol === PROTOCOL_LANGGRAPH_API) {
    const detected = detectLanggraphApi(
      index,
      rules,
      framework,
      entry,
      language,
      sink,
      overrides.graph,
    );
    app = detected.app;
    protocol = detected.protocol;
  } else if (overrides.protocol !== undefined) {
    sink.add("protocol_ignored");
  }
  const alternatives =
    framework.app !== undefined
      ? []
      : alternativesOf(index, rules, language === undefined ? languages : [language], sink);
  const deps = language === undefined ? new Map<string, string>() : index.dependencies(language);
  const { providers, models, modelEnvs } = detectProviders(
    index,
    rules,
    language,
    framework.framework,
    deps,
    sink,
    overrides,
  );
  return new DetectionReport({
    language,
    framework: framework.framework,
    kind: protocol === PROTOCOL_LANGGRAPH_API ? CUSTOM_KIND : kindOf(framework, language),
    entrypoint: entry?.entrypoint,
    entrypointKind: entry === undefined ? undefined : entryKind(entry),
    protocol,
    options: { ...skillsOptions(framework, index), ...graphOption(framework, overrides) },
    app,
    install,
    providers,
    models,
    evidence,
    warnings: sink.items,
    alternatives,
    modelEnvs,
  });
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * El documento de proyecto con la tabla `[runtime]` que falta rellenada por la
 * detección. Lo que el fichero ya dice gana, clave por clave; si el fichero
 * declara otro `kind` que el detectado, se deja tal cual.
 */
export function fillRuntime(
  document: Readonly<Record<string, unknown>>,
  report: DetectionReport,
): Record<string, unknown> {
  const filled: Record<string, unknown> = { ...document };
  const detected = report.runtimeTable();
  const current = document.runtime;
  if (!isRecord(current)) {
    filled.runtime = detected;
    return filled;
  }
  if ("kind" in current && current.kind !== detected.kind) {
    return filled;
  }
  const merged: Record<string, unknown> = { ...detected, ...current };
  if (isRecord(detected.app) && isRecord(current.app)) {
    merged.app = { ...detected.app, ...current.app };
  }
  filled.runtime = merged;
  return filled;
}
