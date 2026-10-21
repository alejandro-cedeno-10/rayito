/**
 * Los valores de la detección de un proyecto de agente (ADR-029). Espejo de
 * `rayito._agent._detect_report`: `DetectOverrides` (lo que el usuario fuerza)
 * y `DetectionReport` (lo que se detectó y de qué ficheros). Valores puros:
 * ninguno guarda el contenido de un fichero, sólo rutas relativas al proyecto
 * y el identificador de la regla que acertó. `DetectionReport.toObject()` es
 * la forma JSON que comparten los dos SDK (`testdata/agent-detection/`,
 * esquema `rayito.agent-detection/1`).
 */

import { AGENT_DETECTION_SCHEMA } from "../limits.js";

/** Los protocolos que el informe puede nombrar: `framework` lo sirve el driver
 * del framework; el resto es una app HTTP (`agentcore`, `json`, `raw` y, sólo
 * con `--protocol`, `langgraph-api`). */
export const PROTOCOL_FRAMEWORK = "framework";
export const APP_PROTOCOLS = ["agentcore", "json", "raw", "langgraph-api"] as const;
/** Cómo se obtiene el objeto del agente: ya construido o con una fábrica. */
export const ENTRY_OBJECT = "object";
export const ENTRY_FACTORY = "factory";

/** Lo que el usuario fuerza por encima de la detección (`--framework`,
 * `--language`, `--entry`, `--protocol`, `--model-id`, `--model-provider`,
 * `--model-base-url`, `--graph`). `entrypoint: "http"` sirve la app HTTP del
 * proyecto en vez del framework del agente. `modelProvider` es un preset de
 * pasarela (`openai-compatible`, `litellm`...) y `modelBaseUrl` la URL
 * `https` de la pasarela de LLM, que pasa a ser su `upstream`. `graph` elige
 * un grafo de `langgraph.json` por su nombre. */
export interface DetectOverrides {
  readonly framework?: string | undefined;
  readonly language?: string | undefined;
  readonly entrypoint?: string | undefined;
  readonly protocol?: string | undefined;
  readonly modelId?: string | undefined;
  readonly modelProvider?: string | undefined;
  readonly modelBaseUrl?: string | undefined;
  readonly graph?: string | undefined;
}

/** Un fichero (ruta relativa al proyecto) y la regla que lo reconoció; nunca
 * su contenido. */
export interface DetectionEvidence {
  readonly path: string | undefined;
  readonly rule: string;
}

/** Un aviso con código estable y el texto en español. */
export interface DetectionWarning {
  readonly code: string;
  readonly detail: string;
}

/** Un proveedor de modelo detectado: su preset de pasarela y la evidencia. */
export interface DetectionProvider {
  readonly preset: string;
  readonly evidence: readonly DetectionEvidence[];
}

/** La app HTTP del proyecto: el comando que la arranca, el puerto en el que
 * escucha dentro del sandbox, sus rutas de salud e invocación, con el
 * protocolo `json` los campos de la petición y la respuesta y, con
 * `langgraph-api`, el grafo que se ejecuta (`assistantId`, que sólo sale en
 * `toObject()` si tiene valor). */
export interface DetectionApp {
  readonly framework: string;
  readonly command: readonly string[];
  readonly port: number;
  readonly portEnv: string | undefined;
  readonly healthPath: string;
  readonly invokePath: string | undefined;
  readonly requestField: string | undefined;
  readonly responseField: string | undefined;
  readonly assistantId?: string;
}

/** Cómo se instalan las dependencias: el gestor, el fichero de bloqueo (o los
 * requisitos), si la instalación es reproducible, si hay un script de build y
 * la versión de intérprete elegida entre las compilaciones fijadas. */
export interface DetectionInstall {
  readonly manager: string | undefined;
  readonly lockfile: string | undefined;
  readonly requirements: string | undefined;
  readonly requireHashes: boolean;
  readonly reproducible: boolean;
  readonly buildScript: boolean;
  readonly pythonVersion: string | undefined;
  readonly nodeVersion: string | undefined;
}

/** Otra forma de servir el proyecto que la detección no eligió. */
export interface DetectionAlternative {
  readonly framework: string;
  readonly kind: string;
  readonly language: string | undefined;
  readonly entrypoint: string | undefined;
}

export interface DetectionReportInit {
  readonly language: string | undefined;
  readonly framework: string;
  readonly kind: string;
  readonly entrypoint: string | undefined;
  readonly entrypointKind: string | undefined;
  readonly protocol: string;
  readonly options?: Readonly<Record<string, string>> | undefined;
  readonly app?: DetectionApp | undefined;
  readonly install?: DetectionInstall | undefined;
  readonly providers?: readonly DetectionProvider[] | undefined;
  readonly models?: readonly string[] | undefined;
  readonly evidence?: readonly DetectionEvidence[] | undefined;
  readonly warnings?: readonly DetectionWarning[] | undefined;
  readonly alternatives?: readonly DetectionAlternative[] | undefined;
  readonly modelEnvs?: Readonly<Record<string, string>> | undefined;
}

function nullable<T>(value: T | undefined): T | null {
  return value === undefined ? null : value;
}

function evidenceObject(item: DetectionEvidence): Record<string, unknown> {
  return { path: nullable(item.path), rule: item.rule };
}

function appObject(app: DetectionApp): Record<string, unknown> {
  const shape: Record<string, unknown> = {
    framework: app.framework,
    command: [...app.command],
    port: app.port,
    port_env: nullable(app.portEnv),
    health_path: app.healthPath,
    invoke_path: nullable(app.invokePath),
    request_field: nullable(app.requestField),
    response_field: nullable(app.responseField),
  };
  if (app.assistantId !== undefined) {
    shape.assistant_id = app.assistantId;
  }
  return shape;
}

function installObject(install: DetectionInstall): Record<string, unknown> {
  return {
    manager: nullable(install.manager),
    lockfile: nullable(install.lockfile),
    requirements: nullable(install.requirements),
    require_hashes: install.requireHashes,
    reproducible: install.reproducible,
    build_script: install.buildScript,
    python_version: nullable(install.pythonVersion),
    node_version: nullable(install.nodeVersion),
  };
}

function appTable(app: DetectionApp, protocol: string): Record<string, unknown> {
  const table: Record<string, unknown> = {
    command: [...app.command],
    port: app.port,
    health_path: app.healthPath,
    protocol,
  };
  if (app.portEnv !== undefined) {
    table.port_env = app.portEnv;
  }
  if (app.invokePath !== undefined) {
    table.invoke_path = app.invokePath;
  }
  if (app.requestField !== undefined) {
    table.request_field = app.requestField;
  }
  if (app.responseField !== undefined) {
    table.response_field = app.responseField;
  }
  if (app.assistantId !== undefined) {
    table.assistant_id = app.assistantId;
  }
  return table;
}

/**
 * Lo que se detectó de un proyecto sin ejecutar su código: `language`,
 * `framework` (la regla que acertó), `kind` (el `[runtime] kind` que lo
 * sirve), el `entrypoint` (`modulo:atributo` en Python, `ruta#export` en
 * TypeScript) y si es un objeto o una fábrica, el `protocol`, las `options`
 * propias del runtime, la `app` HTTP, el plan de instalación, los proveedores
 * de modelo y los literales de modelo vistos, la evidencia, los avisos y las
 * alternativas. `modelEnvs` (claves `id_env`, `base_url_env`, `api_key_env`
 * de `[model]`) son los nombres de variable con los que el código elige su
 * modelo cuando no hay literal; sólo nombres, nunca valores.
 */
export class DetectionReport {
  readonly language: string | undefined;
  readonly framework: string;
  readonly kind: string;
  readonly entrypoint: string | undefined;
  readonly entrypointKind: string | undefined;
  readonly protocol: string;
  readonly options: Readonly<Record<string, string>>;
  readonly app: DetectionApp | undefined;
  readonly install: DetectionInstall | undefined;
  readonly providers: readonly DetectionProvider[];
  readonly models: readonly string[];
  readonly evidence: readonly DetectionEvidence[];
  readonly warnings: readonly DetectionWarning[];
  readonly alternatives: readonly DetectionAlternative[];
  readonly modelEnvs: Readonly<Record<string, string>>;

  constructor(init: DetectionReportInit) {
    this.language = init.language;
    this.framework = init.framework;
    this.kind = init.kind;
    this.entrypoint = init.entrypoint;
    this.entrypointKind = init.entrypointKind;
    this.protocol = init.protocol;
    this.options = Object.freeze({ ...(init.options ?? {}) });
    this.app = init.app;
    this.install = init.install;
    this.providers = Object.freeze([...(init.providers ?? [])]);
    this.models = Object.freeze([...(init.models ?? [])]);
    this.evidence = Object.freeze([...(init.evidence ?? [])]);
    this.warnings = Object.freeze([...(init.warnings ?? [])]);
    this.alternatives = Object.freeze([...(init.alternatives ?? [])]);
    this.modelEnvs = Object.freeze({ ...(init.modelEnvs ?? {}) });
  }

  /** La forma JSON compartida con Python; `model_envs` sólo si hay. */
  toObject(): Record<string, unknown> {
    const document: Record<string, unknown> = {
      schema: AGENT_DETECTION_SCHEMA,
      language: nullable(this.language),
      framework: this.framework,
      kind: this.kind,
      entrypoint: nullable(this.entrypoint),
      entrypoint_kind: nullable(this.entrypointKind),
      protocol: this.protocol,
      options: { ...this.options },
      app: this.app === undefined ? null : appObject(this.app),
      install: this.install === undefined ? null : installObject(this.install),
      providers: this.providers.map((provider) => ({
        preset: provider.preset,
        evidence: provider.evidence.map(evidenceObject),
      })),
      models: [...this.models],
      evidence: this.evidence.map(evidenceObject),
      warnings: this.warnings.map((warning) => ({ code: warning.code, detail: warning.detail })),
      alternatives: this.alternatives.map((alternative) => ({
        framework: alternative.framework,
        kind: alternative.kind,
        language: nullable(alternative.language),
        entrypoint: nullable(alternative.entrypoint),
      })),
    };
    if (Object.keys(this.modelEnvs).length > 0) {
      document.model_envs = { ...this.modelEnvs };
    }
    return document;
  }

  /** La tabla `[runtime]` de `rayito-agent.toml` que describe este informe:
   * `kind`, `framework`, `language`, las opciones del runtime y el
   * `entrypoint`, o `[runtime.app]` si el proyecto se sirve como app HTTP (que
   * ya lleva su comando). */
  runtimeTable(): Record<string, unknown> {
    const table: Record<string, unknown> = { kind: this.kind, framework: this.framework };
    if (this.language !== undefined) {
      table.language = this.language;
    }
    if (this.app !== undefined) {
      table.app = appTable(this.app, this.protocol);
    } else if (this.entrypoint !== undefined) {
      table.entrypoint = this.entrypoint;
    }
    Object.assign(table, this.options);
    return table;
  }
}
