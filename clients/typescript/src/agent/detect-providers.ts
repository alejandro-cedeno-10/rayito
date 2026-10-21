/**
 * Proveedores de modelo de un proyecto (ADR-029). Espejo de
 * `rayito._agent._detect_providers`: por sus dependencias, por los nombres de
 * variable de entorno que el código, el README o un `.env.example` mencionan y
 * por la configuración de Hermes u OpenCode. Cada proveedor es un preset de
 * pasarela existente (`bedrockGateway`, `openaiGateway`...). Sólo se leen
 * nombres: los valores de un `.env` nunca se leen ni se guardan, y los
 * literales de modelo son identificadores públicos.
 *
 * Sin literal de modelo, el código puede elegirlo por su configuración
 * (`modelFromCode`, ADR-033 D2): una URL base en el constructor del cliente
 * convierte `openai` en `openai-compatible`, y los nombres de las variables
 * del id, la URL base y la clave se guardan en `modelEnvs`.
 */

import { InvalidArgumentError } from "../errors.js";
import type { DetectionEvidence, DetectionProvider, DetectOverrides } from "./detect-report.js";
import type { DetectionRules, ProviderRule, SettingsRule } from "./detect-rules.js";
import {
  compiledRegex,
  matchesOf,
  type ProjectIndex,
  render,
  type WarningSink,
} from "./detect-scan.js";
import { type ModelEnvKey, validateModelId } from "./domain.js";
import { AgentProjectError, MODEL_ID_REQUIRED_PRESETS } from "./project.js";

const CONFIG_RULE = "config";
/** Evidencia de un valor que puso el usuario con una bandera. */
export const OVERRIDE_PROVIDER_RULE = "override:model_provider";
export const OVERRIDE_BASE_URL_RULE = "override:model_base_url";
/** Cada clave de `modelEnvs` y el patrón de `modelFromCode` que la reconoce. */
const MODEL_ENV_PATTERNS: readonly (readonly [ModelEnvKey, PatternKey])[] = [
  ["id_env", "modelEnvPattern"],
  ["base_url_env", "baseUrlEnvPattern"],
  ["api_key_env", "apiKeyEnvPattern"],
];
/** Con sólo el nombre de la clave no hay modelo elegido por el código: hace
 * falta el del id o el de la URL base. */
const MODEL_CHOICE_KEYS: readonly ModelEnvKey[] = ["id_env", "base_url_env"];
const MODEL_ID_PATH = "model.id";
const MODEL_PROVIDER_PATH = "model.provider";

type PatternKey = "modelEnvPattern" | "baseUrlEnvPattern" | "apiKeyEnvPattern";

/** Los proveedores (del más al menos probable), los literales de modelo y
 * los nombres de variable con los que el código elige su modelo. */
export interface ModelDetection {
  readonly providers: readonly DetectionProvider[];
  readonly models: readonly string[];
  readonly modelEnvs: Readonly<Record<string, string>>;
}

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

/** Donde se buscan literales de modelo: el código del lenguaje y, para Hermes u
 * OpenCode (que se configuran, no se programan), su fichero de configuración. */
function modelFiles(
  index: ProjectIndex,
  rules: DetectionRules,
  language: string | undefined,
  framework: string,
): string[] {
  const configs = rules.configProviders
    .filter((spec) => spec.framework === framework)
    .flatMap((spec) => spec.files)
    .filter((name) => index.hasRootFile(name));
  return [...(language === undefined ? [] : index.sources(language)), ...configs];
}

function modelsOf(
  index: ProjectIndex,
  rules: DetectionRules,
  language: string | undefined,
  framework: string,
  presets: readonly string[],
): string[] {
  const patterns = rules.providers
    .filter((provider) => presets.includes(provider.preset))
    .flatMap((provider) => provider.models ?? []);
  const found = new Set<string>();
  for (const path of modelFiles(index, rules, language, framework)) {
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

/** Los proveedores detectados, del más al menos probable, los literales de
 * modelo que el código nombra y, sin literal, los nombres de variable del
 * modelo. `--model-provider` y `--model-id` ganan a la detección. */
export function detectProviders(
  index: ProjectIndex,
  rules: DetectionRules,
  language: string | undefined,
  framework: string,
  deps: ReadonlyMap<string, string>,
  sink: WarningSink,
  overrides: DetectOverrides = {},
): ModelDetection {
  const detected = detectedProviders(index, rules, language, framework, deps);
  let models: readonly string[] = modelsOf(
    index,
    rules,
    language,
    framework,
    detected.map((provider) => provider.preset),
  );
  if (overrides.modelId !== undefined) {
    models = [overrideModelId(overrides.modelId)];
  }
  let providers: readonly DetectionProvider[];
  if (overrides.modelProvider === undefined) {
    providers = withBaseUrl(detected, index, rules, language, overrides);
    warn(providers, sink);
  } else {
    providers = [overrideProvider(overrides.modelProvider, rules)];
  }
  const modelEnvs = modelEnvsOf(index, rules, language, providers, models);
  const names = Object.values(modelEnvs);
  if (names.length > 0) {
    sink.add("model_id_from_code", { envs: names.join(", ") });
  }
  return { providers, models, modelEnvs };
}

function detectedProviders(
  index: ProjectIndex,
  rules: DetectionRules,
  language: string | undefined,
  framework: string,
  deps: ReadonlyMap<string, string>,
): DetectionProvider[] {
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
  return strong.length > 0 ? strong : weak;
}

function overrideModelId(modelId: string): string {
  try {
    return validateModelId(modelId, "--model-id");
  } catch (error) {
    if (error instanceof InvalidArgumentError) {
      throw new AgentProjectError(`${MODEL_ID_PATH}: ${error.message}`, {
        path: MODEL_ID_PATH,
        code: "invalid_value",
      });
    }
    throw error;
  }
}

function overrideProvider(preset: string, rules: DetectionRules): DetectionProvider {
  const presets = rules.providers.map((provider) => provider.preset);
  if (!presets.includes(preset)) {
    const text = render(rules.messages.invalid_model_provider as string, {
      value: preset,
      candidates: presets.join(", "),
    });
    throw new AgentProjectError(`${MODEL_PROVIDER_PATH}: ${text}`, {
      path: MODEL_PROVIDER_PATH,
      code: "invalid_value",
    });
  }
  return { preset, evidence: [{ path: undefined, rule: OVERRIDE_PROVIDER_RULE }] };
}

/** De dónde sale una URL base propia: el primer fichero cuyo constructor de
 * cliente la pasa (`clientBaseUrl`) o, si ninguno, `--model-base-url`. */
function baseUrlEvidence(
  index: ProjectIndex,
  rules: DetectionRules,
  language: string | undefined,
  overrides: DetectOverrides,
): DetectionEvidence | undefined {
  const spec = rules.modelFromCode;
  const rule = language === undefined ? undefined : spec.clientBaseUrl[language];
  if (rule !== undefined) {
    const pattern = compiledRegex(rule.pattern);
    for (const path of index.sources(language as string)) {
      const text = index.read(path);
      if (text !== undefined && pattern.test(text)) {
        return { path, rule: spec.baseUrlRule };
      }
    }
  }
  if (overrides.modelBaseUrl !== undefined) {
    return { path: undefined, rule: OVERRIDE_BASE_URL_RULE };
  }
  return undefined;
}

/** `fromPreset` pasa a ser `toPreset` (en su sitio, sin añadir otro
 * proveedor) si el cliente apunta a una URL base propia. */
function withBaseUrl(
  providers: readonly DetectionProvider[],
  index: ProjectIndex,
  rules: DetectionRules,
  language: string | undefined,
  overrides: DetectOverrides,
): readonly DetectionProvider[] {
  const spec = rules.modelFromCode;
  if (!providers.some((provider) => provider.preset === spec.fromPreset)) {
    return providers;
  }
  const evidence = baseUrlEvidence(index, rules, language, overrides);
  if (evidence === undefined) {
    return providers;
  }
  return providers.map((provider) =>
    provider.preset === spec.fromPreset
      ? { preset: spec.toPreset, evidence: [...provider.evidence, evidence] }
      : provider,
  );
}

function groupsOf(pattern: string, text: string | undefined): string[] {
  return text === undefined ? [] : matchesOf(pattern, text).map((match) => match[1] as string);
}

/** Los campos de una clase de pydantic-settings como variables: `env_prefix`
 * más el nombre del campo, en mayúsculas. */
function settingsNames(text: string | undefined, settings: SettingsRule | undefined): string[] {
  if (
    text === undefined ||
    settings === undefined ||
    !compiledRegex(settings.marker.pattern).test(text)
  ) {
    return [];
  }
  const prefix = groupsOf(settings.prefix.pattern, text)[0] ?? "";
  return groupsOf(settings.field.pattern, text).map((name) => `${prefix}${name}`.toUpperCase());
}

/** Las claves del mapa `env` de `langgraph.json`; nunca sus valores. */
function langgraphEnvKeys(index: ProjectIndex, name: string): string[] {
  const document = index.hasRootFile(name) ? index.readJson(name) : undefined;
  const env =
    typeof document === "object" && document !== null
      ? (document as Record<string, unknown>).env
      : undefined;
  return typeof env === "object" && env !== null && !Array.isArray(env) ? Object.keys(env) : [];
}

/** Los nombres de variable que el proyecto declara o lee, ordenados. */
function envNamesOf(
  index: ProjectIndex,
  rules: DetectionRules,
  language: string | undefined,
): string[] {
  const spec = rules.modelFromCode;
  const names = new Set<string>();
  for (const name of rules.envExampleFiles) {
    if (index.hasRootFile(name)) {
      for (const found of groupsOf(spec.envExampleName.pattern, index.read(name))) {
        names.add(found);
      }
    }
  }
  if (language !== undefined) {
    const reads = spec.envReads[language] ?? [];
    const settings = spec.settings[language];
    for (const path of index.sources(language)) {
      const text = index.read(path);
      for (const read of reads) {
        for (const found of groupsOf(read.pattern, text)) {
          names.add(found);
        }
      }
      for (const found of settingsNames(text, settings)) {
        names.add(found);
      }
    }
  }
  for (const found of langgraphEnvKeys(index, spec.langgraphJson)) {
    names.add(found);
  }
  return [...names].sort();
}

/** `modelEnvs` sólo cuando no hay literal, la pasarela no exige el id y el
 * código nombra la variable del id o la de la URL base. */
function modelEnvsOf(
  index: ProjectIndex,
  rules: DetectionRules,
  language: string | undefined,
  providers: readonly DetectionProvider[],
  models: readonly string[],
): Record<string, string> {
  const [first] = providers;
  if (
    models.length > 0 ||
    first === undefined ||
    MODEL_ID_REQUIRED_PRESETS.includes(first.preset)
  ) {
    return {};
  }
  const spec = rules.modelFromCode;
  const names = envNamesOf(index, rules, language);
  const found: Record<string, string> = {};
  for (const [key, patternKey] of MODEL_ENV_PATTERNS) {
    const pattern = compiledRegex(spec[patternKey].pattern);
    const chosen = names.find((name) => pattern.test(name));
    if (chosen !== undefined) {
      found[key] = chosen;
    }
  }
  return MODEL_CHOICE_KEYS.some((key) => key in found) ? found : {};
}
