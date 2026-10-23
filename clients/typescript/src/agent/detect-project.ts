/**
 * Del informe de detección a un `AgentProject` (ADR-029, D1 y D7). Espejo de
 * `rayito._agent._detect_project`: completa un documento de proyecto (el
 * fichero, si lo hay) con lo que la detección sabe y el fichero no dice: el
 * runtime (con el gestor de paquetes, los requisitos y las versiones del plan
 * de instalación, y el adaptador de Node de los frameworks de JavaScript), el
 * código (`source`), el modelo y su pasarela. Lo que el fichero ya dice gana,
 * clave por clave, y lo que no se puede saber sin adivinar (el proveedor, el id
 * del modelo, la región) es un error que nombra la clave que lo resuelve. Puro:
 * no lee el disco ni llama a AWS. Lee las mismas reglas (`bridge` de
 * `agent-detection.json`) y los mismos vectores que Python.
 */

import { AGENT_PROJECT_SCHEMA, AGENT_SECRET_NAME_TEMPLATE } from "../limits.js";
import { DEFAULT_SECRET_PREFIX } from "../secrets/names.js";
import { fillRuntime } from "./detect-domain.js";
import { INSTRUCTIONS_FILE_KEY, PERMISSIONS_KEY } from "./detect-opencode.js";
import type { DetectionReport, DetectOverrides } from "./detect-report.js";
import { type BridgeRules, DETECTION_RULES, type DetectionRules } from "./detect-rules.js";
import {
  AgentProjectError,
  ENTRYPOINT_KEY,
  GRAPH_KEY,
  GRAPH_RUNTIME_KIND,
  gatewayPresetKeys,
  gatewayPresetRequiredKeys,
  requireModelId,
} from "./project.js";

const MODEL_PATH = "model";
const REGION_PATH = "deploy.region";
const REGION_KEY = "region";
const MODELS_KEY = "models";
const UPSTREAM_KEY = "upstream";
/** Cómo se da el `upstream` que la detección no puede saber. */
export const UPSTREAM_HINT = "pásalo con --model-base-url o ";
const UV_LOCKFILE = "uv.lock";
const PYTHON_LANGUAGE = "python";
const TYPESCRIPT_LANGUAGE = "typescript";
const ENTRY_EXPORT_SEPARATOR = "#";
/** El secreto de la pasarela detectada, relativo al prefijo `rayito/` con el
 * que la pasarela lo lee (`SecretCache`) y `--create-secret` lo crea
 * (`SecretStore`): en Secrets Manager queda en `AGENT_SECRET_NAME_TEMPLATE`
 * (`rayito/agents/<proyecto>/<proveedor>`), sin el prefijo duplicado de 0.16. */
export const GATEWAY_SECRET_NAME_TEMPLATE = AGENT_SECRET_NAME_TEMPLATE.startsWith(
  DEFAULT_SECRET_PREFIX,
)
  ? AGENT_SECRET_NAME_TEMPLATE.slice(DEFAULT_SECRET_PREFIX.length)
  : AGENT_SECRET_NAME_TEMPLATE;
/** Las claves de `[agent]` que ya dan instrucciones: con cualquiera, el
 * `instructions_file` detectado no se usa. */
const INSTRUCTION_KEYS = ["instructions", INSTRUCTIONS_FILE_KEY] as const;

type Table = Record<string, unknown>;

function asTable(value: unknown): Table {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? { ...(value as Table) }
    : {};
}

function failure(
  path: string,
  code: "missing_key" | "not_detected" | "invalid_value",
  text: string,
) {
  return new AgentProjectError(`${path}: ${text}`, { path, code });
}

/** Con un grafo elegido (`--graph` o `[runtime] graph`) y sin un `entrypoint`
 * del fichero, el runtime `langgraph` guarda el nombre del grafo y no el
 * `entrypoint` que la detección resolvió; otro runtime (un proyecto de
 * LangGraph.js se sirve como `custom`) guarda el `entrypoint` resuelto y no el
 * nombre, salvo que el fichero lo escriba. */
function keepGraph(runtime: Table, declared: Table, kind: string): void {
  if (!(GRAPH_KEY in runtime) || ENTRYPOINT_KEY in declared) {
    return;
  }
  if (kind === GRAPH_RUNTIME_KIND) {
    delete runtime[ENTRYPOINT_KEY];
  } else if (!(GRAPH_KEY in declared)) {
    delete runtime[GRAPH_KEY];
  }
}

function completeRuntime(
  runtime: Table,
  declared: Table,
  report: DetectionReport,
  bridge: BridgeRules,
): void {
  const nodeRuntime = bridge.nodeRuntimes[report.framework];
  if (report.language === TYPESCRIPT_LANGUAGE && nodeRuntime !== undefined) {
    if (!("kind" in declared)) {
      runtime.kind = nodeRuntime.kind;
    }
    runtime.adapter ??= nodeRuntime.adapter;
  }
  const kind = String(runtime.kind ?? report.kind);
  keepGraph(runtime, declared, kind);
  const entrypoint = runtime.entrypoint;
  const pathKinds =
    report.language === undefined ? [] : (bridge.modulePathKinds[report.language] ?? []);
  if (
    typeof entrypoint === "string" &&
    pathKinds.includes(kind) &&
    entrypoint.includes(ENTRY_EXPORT_SEPARATOR)
  ) {
    runtime.entrypoint = entrypoint.split(ENTRY_EXPORT_SEPARATOR)[0];
  }
  const install = report.install;
  if (install === undefined || !bridge.installKinds.includes(kind)) {
    return;
  }
  runtime.package_manager ??= install.manager;
  if (report.language === PYTHON_LANGUAGE) {
    runtime.requirements ??=
      install.requirements ??
      (install.lockfile === UV_LOCKFILE ? install.lockfile : bridge.defaultUvManifest);
    if (install.pythonVersion !== undefined) {
      runtime.python_version ??= install.pythonVersion;
    }
  } else if (install.nodeVersion !== undefined) {
    runtime.node_version ??= install.nodeVersion;
  }
}

function requiredRegion(region: string | undefined): string {
  if (region === undefined) {
    throw failure(
      REGION_PATH,
      "missing_key",
      "no hay región de AWS para la pasarela del modelo: pasa --region, exporta AWS_REGION " +
        "o escribe [deploy] region en rayito-agent.toml",
    );
  }
  return region;
}

/** La tabla `[gateways.<preset>]` del proveedor detectado, con el secreto de
 * la convención de nombre y lo que el preset exige (región, modelos, la URL de
 * `--model-base-url` como `upstream`); lo opcional se deja fuera. */
function gatewayTable(
  preset: string,
  name: string,
  report: DetectionReport,
  region: string | undefined,
  baseUrl: string | undefined,
): Table {
  const table: Table = {
    preset,
    secret: GATEWAY_SECRET_NAME_TEMPLATE.replace("{project}", name).replace("{provider}", preset),
  };
  const keys = gatewayPresetKeys(preset);
  const required = gatewayPresetRequiredKeys(preset);
  if (baseUrl !== undefined && !keys.includes(UPSTREAM_KEY)) {
    throw failure(
      `gateways.${preset}.${UPSTREAM_KEY}`,
      "invalid_value",
      `el preset '${preset}' no admite --model-base-url: usa openai-compatible o litellm`,
    );
  }
  for (const key of keys) {
    if (key === REGION_KEY) {
      table[key] = requiredRegion(region);
    } else if (key === MODELS_KEY) {
      table[key] = [...report.models];
    } else if (key === UPSTREAM_KEY && baseUrl !== undefined) {
      table[key] = baseUrl;
    } else if (required.includes(key)) {
      const hint = key === UPSTREAM_KEY ? UPSTREAM_HINT : "";
      throw failure(
        `gateways.${preset}.${key}`,
        "missing_key",
        `el preset '${preset}' necesita ${key} y no se puede detectar: ${hint}escríbelo en ` +
          "rayito-agent.toml (rayito agent detect --write)",
      );
    }
  }
  return table;
}

/** `[model]` y `[gateways.<preset>]` de lo detectado. Sin literal de modelo
 * el id sólo es obligatorio donde Rayito lo usa (`requireModelId`); si no,
 * `[model]` lleva los nombres de variable de `modelEnvs`. */
function modelTables(
  report: DetectionReport,
  name: string,
  region: string | undefined,
  bridge: BridgeRules,
  runtime: Table,
  baseUrl: string | undefined,
): [Table, Table] {
  const [provider] = report.providers;
  if (provider === undefined) {
    throw failure(
      MODEL_PATH,
      "not_detected",
      "no se detectó el proveedor del modelo: escribe [model] y [gateways] en " +
        "rayito-agent.toml (rayito agent detect --write)",
    );
  }
  const preset = provider.preset;
  const [modelId] = report.models;
  requireModelId(modelId, {
    runtime: String(runtime.kind ?? report.kind),
    entrypoint: runtime.entrypoint !== undefined || "app" in runtime,
    gatewayPreset: preset,
    detected: true,
  });
  const gateway = gatewayTable(preset, name, report, region, baseUrl);
  const model: Table = { provider: bridge.modelProviders[preset] };
  if (modelId !== undefined) {
    model.id = modelId;
  }
  model.gateway = preset;
  if (REGION_KEY in gateway) {
    model[REGION_KEY] = gateway[REGION_KEY];
  }
  return [{ ...model, ...report.modelEnvs }, { [preset]: gateway }];
}

/**
 * `[agent]` con lo que aporta la configuración del framework
 * (`report.agentTable`: el `AGENTS.md` y los permisos de OpenCode) donde el
 * fichero calla: unas instrucciones del fichero (en línea o en otro fichero)
 * ganan al `AGENTS.md`, y sus permisos, a los de la configuración. Sólo si el
 * runtime es el detectado; un `[agent]` que no es una tabla se deja
 * para que lo valide el proyecto.
 */
function agentTable(current: unknown, runtime: Table, report: DetectionReport): unknown {
  const detected = report.agentTable;
  if (Object.keys(detected).length === 0 || runtime.kind !== report.kind) {
    return current;
  }
  const isTable = typeof current === "object" && current !== null && !Array.isArray(current);
  if (current !== undefined && current !== null && !isTable) {
    return current;
  }
  const agent = asTable(current);
  if (INSTRUCTIONS_FILE_KEY in detected && !INSTRUCTION_KEYS.some((key) => key in agent)) {
    agent[INSTRUCTIONS_FILE_KEY] = detected[INSTRUCTIONS_FILE_KEY];
  }
  if (PERMISSIONS_KEY in detected && !(PERMISSIONS_KEY in agent)) {
    agent[PERMISSIONS_KEY] = detected[PERMISSIONS_KEY];
  }
  return agent;
}

/** El documento de proyecto con lo detectado donde el fichero calla.
 * `overrides.modelBaseUrl` es el `upstream` de la pasarela detectada. */
export function detectedDocument(
  document: Readonly<Record<string, unknown>>,
  report: DetectionReport,
  options: {
    readonly name: string;
    readonly region: string | undefined;
    readonly rules?: DetectionRules;
    readonly overrides?: DetectOverrides | undefined;
  },
): Record<string, unknown> {
  const bridge = (options.rules ?? DETECTION_RULES).bridge;
  const filled = fillRuntime(document, report);
  filled.schema ??= AGENT_PROJECT_SCHEMA;
  filled.name ??= options.name;
  const runtime = asTable(filled.runtime);
  completeRuntime(runtime, asTable(document.runtime), report, bridge);
  filled.runtime = runtime;
  filled.source ??= { path: bridge.defaultSource };
  const agent = agentTable(filled.agent, runtime, report);
  if (agent !== undefined) {
    filled.agent = agent;
  }
  if (!("model" in filled)) {
    const [model, gateways] = modelTables(
      report,
      String(filled.name),
      options.region,
      bridge,
      runtime,
      options.overrides?.modelBaseUrl,
    );
    filled.model = model;
    filled.gateways = { ...asTable(filled.gateways), ...gateways };
  }
  return filled;
}
