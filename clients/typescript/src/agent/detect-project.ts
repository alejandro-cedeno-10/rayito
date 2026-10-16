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
import { fillRuntime } from "./detect-domain.js";
import type { DetectionReport } from "./detect-report.js";
import { type BridgeRules, DETECTION_RULES, type DetectionRules } from "./detect-rules.js";
import { AgentProjectError, gatewayPresetKeys } from "./project.js";

const MODEL_PATH = "model";
const MODEL_ID_PATH = "model.id";
const REGION_PATH = "deploy.region";
const REGION_KEY = "region";
const MODELS_KEY = "models";
const UV_LOCKFILE = "uv.lock";
const PYTHON_LANGUAGE = "python";
const TYPESCRIPT_LANGUAGE = "typescript";
const ENTRY_EXPORT_SEPARATOR = "#";

type Table = Record<string, unknown>;

function asTable(value: unknown): Table {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? { ...(value as Table) }
    : {};
}

function failure(path: string, code: "missing_key" | "not_detected" | "ambiguous", text: string) {
  return new AgentProjectError(`${path}: ${text}`, { path, code });
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

function gatewayTable(
  preset: string,
  name: string,
  report: DetectionReport,
  region: string | undefined,
): Table {
  const table: Table = {
    preset,
    secret: AGENT_SECRET_NAME_TEMPLATE.replace("{project}", name).replace("{provider}", preset),
  };
  for (const key of gatewayPresetKeys(preset)) {
    if (key === REGION_KEY) {
      table[key] = requiredRegion(region);
    } else if (key === MODELS_KEY) {
      table[key] = [...report.models];
    } else {
      throw failure(
        `gateways.${preset}.${key}`,
        "missing_key",
        `el preset '${preset}' necesita ${key} y no se puede detectar: escríbelo en ` +
          "rayito-agent.toml (rayito agent detect --write)",
      );
    }
  }
  return table;
}

function modelTables(
  report: DetectionReport,
  name: string,
  region: string | undefined,
  bridge: BridgeRules,
): [Table, Table] {
  const presets = [...new Set(report.providers.map((provider) => provider.preset))].sort();
  const [preset] = presets;
  if (preset === undefined) {
    throw failure(
      MODEL_PATH,
      "not_detected",
      "no se detectó el proveedor del modelo: escribe [model] y [gateways] en " +
        "rayito-agent.toml (rayito agent detect --write)",
    );
  }
  if (presets.length > 1) {
    throw failure(
      MODEL_PATH,
      "ambiguous",
      `el proyecto usa varios proveedores de modelo (${presets.join(", ")}): elige uno ` +
        "escribiendo [model] y [gateways] en rayito-agent.toml",
    );
  }
  const [modelId] = report.models;
  if (modelId === undefined) {
    throw failure(
      MODEL_ID_PATH,
      "not_detected",
      "no se encontró el id del modelo en el código: escríbelo como literal o define " +
        "[model] id en rayito-agent.toml (rayito agent detect --write)",
    );
  }
  const gateway = gatewayTable(preset, name, report, region);
  const model: Table = {
    provider: bridge.modelProviders[preset],
    id: modelId,
    gateway: preset,
  };
  if (REGION_KEY in gateway) {
    model[REGION_KEY] = gateway[REGION_KEY];
  }
  return [model, { [preset]: gateway }];
}

/** El documento de proyecto con lo detectado donde el fichero calla. */
export function detectedDocument(
  document: Readonly<Record<string, unknown>>,
  report: DetectionReport,
  options: {
    readonly name: string;
    readonly region: string | undefined;
    readonly rules?: DetectionRules;
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
  if (!("model" in filled)) {
    const [model, gateways] = modelTables(report, String(filled.name), options.region, bridge);
    filled.model = model;
    filled.gateways = { ...asTable(filled.gateways), ...gateways };
  }
  return filled;
}
