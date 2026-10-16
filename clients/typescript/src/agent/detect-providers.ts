/**
 * Proveedores de modelo de un proyecto (ADR-029). Espejo de
 * `rayito._agent._detect_providers`: por sus dependencias, por los nombres de
 * variable de entorno que el código, el README o un `.env.example` mencionan y
 * por la configuración de Hermes u OpenCode. Cada proveedor es un preset de
 * pasarela existente (`bedrockGateway`, `openaiGateway`...). Sólo se leen
 * nombres: los valores de un `.env` nunca se leen ni se guardan, y los
 * literales de modelo son identificadores públicos.
 */

import type { DetectionEvidence, DetectionProvider } from "./detect-report.js";
import type { DetectionRules, ProviderRule } from "./detect-rules.js";
import { compiledRegex, matchesOf, type ProjectIndex, type WarningSink } from "./detect-scan.js";

const CONFIG_RULE = "config";

function envFiles(index: ProjectIndex, rules: DetectionRules, language: string | undefined) {
  const named = [...rules.envScanFiles, ...rules.envExampleFiles];
  const sources = language === undefined ? [] : index.sources(language);
  return [...sources, ...named.filter((name) => index.hasRootFile(name))];
}

/** Nombre de variable -> el primer fichero que lo menciona. */
function envMentions(
  index: ProjectIndex,
  rules: DetectionRules,
  language: string | undefined,
): Map<string, string> {
  const names = [...new Set(rules.providers.flatMap((provider) => provider.envs))].sort();
  const found = new Map<string, string>();
  if (names.length === 0) {
    return found;
  }
  const pattern = `\\b(${names.join("|")})\\b`;
  for (const path of envFiles(index, rules, language)) {
    const text = index.read(path);
    if (text === undefined) {
      continue;
    }
    for (const match of matchesOf(pattern, text)) {
      const name = match[1] as string;
      if (!found.has(name)) {
        found.set(name, path);
      }
    }
  }
  return found;
}

function depEvidence(
  names: readonly string[] | undefined,
  deps: ReadonlyMap<string, string>,
): DetectionEvidence[] {
  const found: DetectionEvidence[] = [];
  for (const name of names ?? []) {
    const path = deps.get(name);
    if (path !== undefined) {
      found.push({ path, rule: `dep:${name}` });
    }
  }
  return found;
}

function configProvider(
  index: ProjectIndex,
  rules: DetectionRules,
  framework: string,
): DetectionProvider | undefined {
  const presets = new Set(rules.providers.map((provider) => provider.preset));
  for (const spec of rules.configProviders) {
    if (spec.framework !== framework) {
      continue;
    }
    for (const name of spec.files) {
      const text = index.hasRootFile(name) ? index.read(name) : undefined;
      const match = text === undefined ? null : compiledRegex(spec.pattern).exec(text);
      if (match === null) {
        continue;
      }
      const lowered = (match[1] as string).toLowerCase();
      const preset = spec.aliases[lowered] ?? lowered;
      if (presets.has(preset)) {
        return { preset, evidence: [{ path: name, rule: CONFIG_RULE }] };
      }
    }
  }
  return undefined;
}

function modelsOf(
  index: ProjectIndex,
  rules: DetectionRules,
  language: string | undefined,
  presets: readonly string[],
): string[] {
  const patterns = rules.providers
    .filter((provider) => presets.includes(provider.preset))
    .flatMap((provider) => provider.models ?? []);
  const found = new Set<string>();
  for (const path of language === undefined ? [] : index.sources(language)) {
    const text = index.read(path);
    for (const pattern of patterns) {
      if (text !== undefined) {
        for (const match of matchesOf(pattern, text)) {
          found.add(match[1] as string);
        }
      }
    }
  }
  return [...found].sort().slice(0, rules.maxModelsListed);
}

function warn(providers: readonly DetectionProvider[], sink: WarningSink): void {
  const [first] = providers;
  if (first === undefined) {
    sink.add("no_provider");
  } else if (providers.length > 1) {
    sink.add("several_providers", {
      providers: providers.map((provider) => provider.preset).join(", "),
      used: first.preset,
    });
  }
}

function providerEvidence(
  provider: ProviderRule,
  language: string | undefined,
  deps: ReadonlyMap<string, string>,
  mentions: ReadonlyMap<string, string>,
): DetectionEvidence[] {
  const strong = depEvidence(language === undefined ? [] : provider.deps[language], deps);
  for (const env of provider.envs) {
    const path = mentions.get(env);
    if (path !== undefined) {
      strong.push({ path, rule: `env:${env}` });
    }
  }
  return strong;
}

/** Los proveedores detectados, del más al menos probable, y los literales de
 * modelo que el código nombra. */
export function detectProviders(
  index: ProjectIndex,
  rules: DetectionRules,
  language: string | undefined,
  framework: string,
  deps: ReadonlyMap<string, string>,
  sink: WarningSink,
): { readonly providers: readonly DetectionProvider[]; readonly models: readonly string[] } {
  const mentions = envMentions(index, rules, language);
  const strong: DetectionProvider[] = [];
  const weak: DetectionProvider[] = [];
  const configured = configProvider(index, rules, framework);
  if (configured !== undefined) {
    strong.push(configured);
  }
  for (const provider of rules.providers) {
    const evidence = providerEvidence(provider, language, deps, mentions);
    if (evidence.length > 0 && !strong.some((item) => item.preset === provider.preset)) {
      strong.push({ preset: provider.preset, evidence });
    }
    const weakEvidence = depEvidence(
      language === undefined ? [] : provider.weakDeps?.[language],
      deps,
    );
    if (weakEvidence.length > 0) {
      weak.push({ preset: provider.preset, evidence: weakEvidence });
    }
  }
  const providers = strong.length > 0 ? strong : weak;
  warn(providers, sink);
  const presets = providers.map((provider) => provider.preset);
  return { providers, models: modelsOf(index, rules, language, presets) };
}
