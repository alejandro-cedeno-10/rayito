/**
 * Las reglas de detección de proyectos de agente (`agent-detection.json`,
 * ADR-029) con tipos. El JSON lo renderiza `scripts/gen_agent_detection.py`
 * en `detection-rules.gen.ts`; aquí se lee una vez y se describe su forma.
 * Espejo de `rayito._agent._detection_rules`.
 */

import { DETECTION_RULES_JSON } from "./detection-rules.gen.js";

export interface EntryRule {
  readonly id: string;
  readonly pattern: string;
  readonly tier: number;
  readonly factory: boolean;
  readonly requires?: string;
  readonly name?: string;
}

export interface FrameworkApp {
  readonly launcher: string;
  readonly fixedRoutes: ReadonlyArray<readonly [string, string]>;
}

export interface FrameworkRule {
  readonly id: string;
  readonly framework: string;
  readonly webOnly?: boolean;
  readonly kind: Readonly<Record<string, string>>;
  readonly deps?: Readonly<Record<string, readonly string[]>>;
  readonly entries?: Readonly<Record<string, readonly EntryRule[]>>;
  readonly entryFile?: string;
  readonly agnosticFiles?: readonly string[];
  readonly agnosticAllFiles?: readonly string[];
  readonly agnosticAnyFiles?: readonly string[];
  readonly options?: Readonly<Record<string, { readonly directory: string }>>;
  readonly app?: FrameworkApp;
}

export interface LanguageRule {
  readonly markers: readonly string[];
  readonly sources: readonly string[];
  readonly sourceExcludes: readonly string[];
  readonly dependencyFiles: ReadonlyArray<{ readonly glob: string; readonly format: string }>;
}

export interface EntryFileRule {
  readonly file: string;
  readonly key: string;
  readonly preferKeys: readonly string[];
  readonly languagesBySuffix: Readonly<Record<string, string>>;
}

export interface LauncherRule {
  readonly command?: readonly string[];
  readonly portLiteral: boolean;
  readonly startScript?: string;
}

export interface RouteRule {
  readonly pattern: string;
  readonly methodGroup?: number;
  readonly pathGroup: number;
  readonly method?: string;
}

export interface AppRules {
  readonly launchers: Readonly<Record<string, LauncherRule>>;
  readonly routes: Readonly<Record<string, readonly RouteRule[]>>;
  readonly healthCandidates: readonly string[];
  readonly invokeCandidates: readonly string[];
  readonly agentcorePaths: { readonly health: string; readonly invoke: string };
  readonly requestFields: readonly string[];
  readonly requestFieldTemplates: readonly string[];
  readonly responseFields: readonly string[];
  readonly responseFieldTemplates: readonly string[];
  readonly portLiterals: Readonly<Record<string, readonly string[]>>;
  readonly portEnvTemplates: Readonly<Record<string, readonly string[]>>;
  readonly reservedPorts: readonly number[];
}

export interface InstallEntry {
  readonly file?: string;
  readonly glob?: string;
  readonly manager: string;
  readonly reproducible?: boolean;
  readonly preferred?: readonly string[];
  readonly ignored?: readonly string[];
  readonly hashMarker?: string;
  readonly berryMarker?: string;
  readonly berryManager?: string;
  readonly notDetected?: boolean;
  readonly unsupported?: boolean;
}

export interface InstallRules {
  readonly python: readonly InstallEntry[];
  readonly typescript: readonly InstallEntry[];
  readonly buildScript: string;
}

export interface VersionSource {
  readonly file: string;
  readonly kind: string;
  readonly pattern?: string;
  readonly json?: readonly string[];
}

export interface ProviderRule {
  readonly preset: string;
  readonly deps: Readonly<Record<string, readonly string[]>>;
  readonly weakDeps?: Readonly<Record<string, readonly string[]>>;
  readonly envs: readonly string[];
  readonly models?: readonly string[];
}

export interface ConfigProviderRule {
  readonly framework: string;
  readonly files: readonly string[];
  readonly pattern: string;
  readonly aliases: Readonly<Record<string, string>>;
}

export interface BridgeRules {
  readonly modelProviders: Readonly<Record<string, string>>;
  readonly nodeRuntimes: Readonly<
    Record<string, { readonly kind: string; readonly adapter: string }>
  >;
  readonly installKinds: readonly string[];
  readonly modulePathKinds: Readonly<Record<string, readonly string[]>>;
  readonly defaultSource: string;
  readonly defaultUvManifest: string;
}

/** Una expresión de regla. */
export interface PatternRule {
  readonly pattern: string;
}

/** Los campos de una clase de pydantic-settings (`BaseSettings`). */
export interface SettingsRule {
  readonly marker: PatternRule;
  readonly prefix: PatternRule;
  readonly field: PatternRule;
}

/** Modelo elegido por el código (`modelFromCode`, ADR-033 D2). */
export interface ModelFromCodeRules {
  readonly clientBaseUrl: Readonly<Record<string, PatternRule>>;
  readonly officialBaseUrl: PatternRule;
  readonly fromPreset: string;
  readonly toPreset: string;
  readonly baseUrlRule: string;
  readonly modelEnvPattern: PatternRule;
  readonly baseUrlEnvPattern: PatternRule;
  readonly apiKeyEnvPattern: PatternRule;
  readonly envExampleName: PatternRule;
  readonly envReads: Readonly<Record<string, readonly PatternRule[]>>;
  readonly settings: Readonly<Record<string, SettingsRule>>;
  readonly langgraphJson: string;
}

/** La configuración cero de un framework que se configura (OpenCode): su
 * fichero de instrucciones, sus ficheros de configuración, la clave de los
 * permisos, la acción con la que se aplica `ask` y el motivo de cada aviso. */
export interface ZeroConfigRule {
  readonly instructionsFile: string;
  readonly configFiles: readonly string[];
  readonly permissionKey: string;
  readonly askAction: string;
  readonly reasons: Readonly<Record<string, string>>;
}

export interface DetectionRules {
  readonly schema: string;
  readonly ignoredPathSegments: readonly string[];
  readonly envExampleFiles: readonly string[];
  readonly envScanFiles: readonly string[];
  readonly httpEntryKeyword: string;
  readonly preferNames: readonly string[];
  readonly preferFiles: readonly string[];
  readonly maxCandidatesListed: number;
  readonly maxModelsListed: number;
  readonly modelFromCode: ModelFromCodeRules;
  readonly languages: Readonly<Record<string, LanguageRule>>;
  readonly entryFiles: Readonly<Record<string, EntryFileRule>>;
  readonly frameworks: readonly FrameworkRule[];
  readonly app: AppRules;
  readonly install: InstallRules;
  readonly versions: Readonly<Record<string, { readonly sources: readonly VersionSource[] }>>;
  readonly providers: readonly ProviderRule[];
  readonly configProviders: readonly ConfigProviderRule[];
  readonly zeroConfig: Readonly<Record<string, ZeroConfigRule | string>>;
  readonly messages: Readonly<Record<string, string>>;
  readonly warnings: Readonly<Record<string, string>>;
  readonly bridge: BridgeRules;
}

export const DETECTION_RULES: DetectionRules = JSON.parse(DETECTION_RULES_JSON) as DetectionRules;
