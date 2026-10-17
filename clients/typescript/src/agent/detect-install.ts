/**
 * Plan de instalación y versiones de intérprete de un proyecto (ADR-029).
 * Espejo de `rayito._agent._detect_install`: qué gestor instala sus
 * dependencias (por su fichero de bloqueo o de requisitos), si la instalación
 * es reproducible y qué compilación fijada de Python o de Node corresponde a
 * la restricción del proyecto. Las reglas son datos de `agent-detection.json`;
 * aquí sólo se interpretan, sin tocar el disco ni ejecutar nada.
 */

import {
  AGENT_DEFAULT_NODE_MAJOR,
  AGENT_DEFAULT_PYTHON_MINOR,
  AGENT_NODE_BUILD_VERSIONS,
  AGENT_PYTHON_BUILD_VERSIONS,
} from "../limits.js";
import type { DetectionEvidence, DetectionInstall } from "./detect-report.js";
import type { DetectionRules, InstallEntry, VersionSource } from "./detect-rules.js";
import {
  compiledRegex,
  globMatch,
  PACKAGE_JSON,
  type ProjectIndex,
  PYTHON,
  render,
  type WarningSink,
} from "./detect-scan.js";
import { resolveBuild } from "./detect-versions.js";
import { AgentProjectError } from "./project.js";

const INSTALL_PATH = "install";
const REQUIREMENTS_PATH = "source.requirements";

interface Plan {
  readonly manager: string | undefined;
  readonly lockfile: string | undefined;
  readonly requirements: string | undefined;
  readonly requireHashes: boolean;
  readonly reproducible: boolean;
}

const NO_PLAN: Plan = {
  manager: undefined,
  lockfile: undefined,
  requirements: undefined,
  requireHashes: false,
  reproducible: false,
};

function notDetected(key: string, rules: DetectionRules): AgentProjectError {
  return new AgentProjectError(`${INSTALL_PATH}: ${rules.messages[key]}`, {
    path: INSTALL_PATH,
    code: "not_detected",
  });
}

function requirementsFile(
  entry: InstallEntry,
  index: ProjectIndex,
  rules: DetectionRules,
): string | undefined {
  const files = index
    .rootMatches(entry.glob as string)
    .filter((path) => !(entry.ignored ?? []).some((pattern) => globMatch(pattern, path)));
  for (const preferred of entry.preferred ?? []) {
    if (files.includes(preferred)) {
      return preferred;
    }
  }
  if (files.length > 1) {
    const text = render(rules.messages.ambiguous_requirements as string, {
      candidates: files.join(", "),
    });
    throw new AgentProjectError(`${REQUIREMENTS_PATH}: ${text}`, {
      path: REQUIREMENTS_PATH,
      code: "ambiguous",
    });
  }
  return files[0];
}

function planFor(
  entry: InstallEntry,
  index: ProjectIndex,
  rules: DetectionRules,
  sink: WarningSink,
): Plan | undefined {
  if (entry.glob !== undefined) {
    const path = requirementsFile(entry, index, rules);
    if (path === undefined) {
      return undefined;
    }
    const text = index.read(path) ?? "";
    return {
      manager: entry.manager,
      lockfile: undefined,
      requirements: path,
      requireHashes: text.includes(entry.hashMarker as string),
      reproducible: true,
    };
  }
  const name = entry.file as string;
  if (!index.hasRootFile(name)) {
    return undefined;
  }
  if (entry.notDetected) {
    throw notDetected(`not_detected_${entry.manager}`, rules);
  }
  if (entry.unsupported) {
    sink.add("unsupported_lockfile", { file: name });
    return undefined;
  }
  const berry =
    entry.berryMarker !== undefined && (index.read(name) ?? "").includes(entry.berryMarker);
  const reproducible = Boolean(entry.reproducible);
  return {
    manager: berry ? entry.berryManager : entry.manager,
    lockfile: reproducible ? name : undefined,
    requirements: undefined,
    requireHashes: false,
    reproducible,
  };
}

function chooseManager(
  language: string,
  index: ProjectIndex,
  rules: DetectionRules,
  sink: WarningSink,
): Plan {
  let chosen: Plan | undefined;
  const locks: string[] = [];
  for (const entry of rules.install[language as "python" | "typescript"]) {
    chosen ??= planFor(entry, index, rules, sink);
    if (entry.reproducible && entry.file !== undefined && index.hasRootFile(entry.file)) {
      locks.push(entry.file);
    }
  }
  if (chosen === undefined) {
    return NO_PLAN;
  }
  if (locks.length > 1) {
    sink.add("multiple_lockfiles", { files: locks.join(", "), used: chosen.lockfile ?? "" });
  }
  if (!chosen.reproducible) {
    sink.add("not_reproducible");
  }
  return chosen;
}

function sourceText(index: ProjectIndex, source: VersionSource): string | undefined {
  if (source.json !== undefined) {
    let document: unknown = index.readJson(source.file);
    for (const key of source.json) {
      document =
        typeof document === "object" && document !== null && !Array.isArray(document)
          ? (document as Record<string, unknown>)[key]
          : undefined;
    }
    return typeof document === "string" && document !== "" ? document : undefined;
  }
  const text = index.read(source.file);
  if (text === undefined) {
    return undefined;
  }
  return compiledRegex(source.pattern as string).exec(text)?.[1];
}

function resolveVersion(
  language: string,
  index: ProjectIndex,
  rules: DetectionRules,
  sink: WarningSink,
  evidence: DetectionEvidence[],
): string {
  const python = language === PYTHON;
  const builds = python ? AGENT_PYTHON_BUILD_VERSIONS : AGENT_NODE_BUILD_VERSIONS;
  const defaultKey = python ? AGENT_DEFAULT_PYTHON_MINOR : String(AGENT_DEFAULT_NODE_MAJOR);
  for (const source of rules.versions[language]?.sources ?? []) {
    const text = sourceText(index, source);
    if (text === undefined) {
      continue;
    }
    const resolution = resolveBuild(source.kind, text, builds, defaultKey);
    if (!resolution.understood) {
      sink.add("version_ignored", { file: source.file });
      continue;
    }
    if (resolution.key === undefined) {
      const message = render(rules.messages.unsupported_version as string, {
        runtime: python ? "Python" : "Node",
      });
      const path = python ? "runtime.python_version" : "runtime.node_version";
      throw new AgentProjectError(`${path}: ${message}`, { path, code: "unsupported" });
    }
    evidence.push({ path: source.file, rule: `version:${source.kind}` });
    return resolution.key;
  }
  return defaultKey;
}

function hasBuildScript(index: ProjectIndex, rules: DetectionRules): boolean {
  const document = index.readJson(PACKAGE_JSON);
  const scripts =
    typeof document === "object" && document !== null && !Array.isArray(document)
      ? (document as Record<string, unknown>).scripts
      : undefined;
  return (
    typeof scripts === "object" &&
    scripts !== null &&
    !Array.isArray(scripts) &&
    rules.install.buildScript in scripts
  );
}

/** El plan de instalación de un proyecto de `language`. */
export function detectInstall(
  language: string,
  index: ProjectIndex,
  rules: DetectionRules,
  sink: WarningSink,
  evidence: DetectionEvidence[],
): DetectionInstall {
  const plan = chooseManager(language, index, rules, sink);
  const source = plan.lockfile ?? plan.requirements;
  if (source !== undefined) {
    evidence.push({ path: source, rule: `install:${plan.manager}` });
  }
  const version = resolveVersion(language, index, rules, sink, evidence);
  const python = language === PYTHON;
  return {
    manager: plan.manager,
    lockfile: plan.lockfile,
    requirements: plan.requirements,
    requireHashes: plan.requireHashes,
    reproducible: plan.reproducible,
    buildScript: python ? false : hasBuildScript(index, rules),
    pythonVersion: python ? version : undefined,
    nodeVersion: python ? undefined : version,
  };
}
