/**
 * Detalles de la app HTTP de un proyecto (ADR-029): su comando de arranque,
 * el puerto, las rutas de salud e invocación, el protocolo y los campos de la
 * petición. Espejo de `rayito._agent._detect_app`: todo sale de patrones de
 * línea sobre el fichero de la app y de `package.json`; nunca se ejecuta
 * nada. Las reglas son datos de `agent-detection.json`.
 */

import { AGENT_APP_INTERNAL_PORT, AGENT_APP_PORT_ENV, AGENT_SERVICE_PORT } from "../limits.js";
import type { EntryChoice } from "./detect-entry.js";
import { APP_PROTOCOLS, type DetectionApp } from "./detect-report.js";
import type { DetectionRules, FrameworkRule, LauncherRule } from "./detect-rules.js";
import {
  compiledRegex,
  matchesOf,
  PACKAGE_JSON,
  type ProjectIndex,
  render,
  type WarningSink,
} from "./detect-scan.js";
import { AgentProjectError } from "./project.js";

const PROTOCOL_AGENTCORE = "agentcore";
const PROTOCOL_JSON = "json";
const PROTOCOL_RAW = "raw";
const METHOD_GET = "GET";
const METHOD_POST = "POST";
const NODE_MAIN_COMMAND = "node";
const RUN_COMMAND = "run";
/** El gestor de Node -> el ejecutable con el que se lanza un script. */
const SCRIPT_RUNNERS: Readonly<Record<string, string>> = {
  npm: "npm",
  pnpm: "pnpm",
  yarn: "yarn",
  "yarn-berry": "yarn",
};
const DEFAULT_RUNNER = "npm";
const APP_COMMAND_PATH = "runtime.app.command";
const INVOKE_PATH = "runtime.app.invoke_path";
const PROTOCOL_PATH = "runtime.app.protocol";

type Route = readonly [string, string];

function routesOf(
  text: string,
  language: string,
  rules: DetectionRules,
  fixed: ReadonlyArray<readonly [string, string]>,
): Route[] {
  const routes: Route[] = fixed.map(([method, path]) => [method, path]);
  for (const rule of rules.app.routes[language] ?? []) {
    for (const match of matchesOf(rule.pattern, text)) {
      const method = rule.method ?? (match[rule.methodGroup as number] as string).toUpperCase();
      const route: Route = [method, match[rule.pathGroup] as string];
      if (!routes.some(([known, path]) => known === route[0] && path === route[1])) {
        routes.push(route);
      }
    }
  }
  return routes;
}

function firstCandidate(
  candidates: readonly string[],
  available: readonly string[],
): string | undefined {
  return candidates.find((candidate) => available.includes(candidate));
}

function healthPath(getPaths: readonly string[], rules: DetectionRules, sink: WarningSink): string {
  const candidates = rules.app.healthCandidates;
  const found = firstCandidate(candidates, getPaths);
  if (found !== undefined) {
    return found;
  }
  const assumed = getPaths[0] ?? (candidates[candidates.length - 1] as string);
  sink.add("health_path_assumed", { path: assumed });
  return assumed;
}

function invokePath(postPaths: readonly string[], rules: DetectionRules): string | undefined {
  if (postPaths.length <= 1) {
    return postPaths[0];
  }
  const found = firstCandidate(rules.app.invokeCandidates, postPaths);
  if (found === undefined) {
    const text = render(rules.messages.ambiguous_route as string, {
      candidates: postPaths.join(", "),
    });
    throw new AgentProjectError(`${INVOKE_PATH}: ${text}`, {
      path: INVOKE_PATH,
      code: "ambiguous",
    });
  }
  return found;
}

function fieldOf(
  text: string,
  candidates: readonly string[],
  templates: readonly string[],
): string | undefined {
  for (const candidate of candidates) {
    for (const template of templates) {
      if (compiledRegex(template.replaceAll("{field}", candidate)).test(text)) {
        return candidate;
      }
    }
  }
  return undefined;
}

function checkedPort(port: number, rules: DetectionRules): number {
  const reserved = [...rules.app.reservedPorts, AGENT_SERVICE_PORT];
  if (reserved.includes(port)) {
    const text = render(rules.messages.unsupported_port as string, { port: String(port) });
    throw new AgentProjectError(`${APP_COMMAND_PATH}: ${text}`, {
      path: APP_COMMAND_PATH,
      code: "unsupported",
    });
  }
  return port;
}

function portOf(
  text: string,
  language: string,
  launcher: LauncherRule,
  rules: DetectionRules,
): number {
  if (!launcher.portLiteral) {
    return AGENT_APP_INTERNAL_PORT;
  }
  for (const template of rules.app.portEnvTemplates[language] ?? []) {
    if (compiledRegex(template.replaceAll("{env}", AGENT_APP_PORT_ENV)).test(text)) {
      return AGENT_APP_INTERNAL_PORT;
    }
  }
  for (const pattern of rules.app.portLiterals[language] ?? []) {
    const match = compiledRegex(pattern).exec(text);
    if (match !== null) {
      return checkedPort(Number.parseInt(match[1] as string, 10), rules);
    }
  }
  return AGENT_APP_INTERNAL_PORT;
}

function nodeCommand(
  index: ProjectIndex,
  launcher: LauncherRule,
  manager: string | undefined,
): string[] {
  const document = index.readJson(PACKAGE_JSON);
  const record =
    typeof document === "object" && document !== null && !Array.isArray(document)
      ? (document as Record<string, unknown>)
      : {};
  const scripts = record.scripts;
  const start = launcher.startScript as string;
  if (
    typeof scripts === "object" &&
    scripts !== null &&
    typeof (scripts as Record<string, unknown>)[start] === "string"
  ) {
    return [SCRIPT_RUNNERS[manager ?? ""] ?? DEFAULT_RUNNER, RUN_COMMAND, start];
  }
  const main = record.main;
  return typeof main === "string" && main !== "" ? [NODE_MAIN_COMMAND, main] : [];
}

function commandOf(
  index: ProjectIndex,
  launcher: LauncherRule,
  entry: EntryChoice,
  manager: string | undefined,
  port: number,
  rules: DetectionRules,
): string[] {
  if (launcher.command !== undefined) {
    const module = entry.entrypoint.split(":", 1)[0] as string;
    const values = { module, attr: entry.attr, port: String(port) };
    return launcher.command.map((part) => render(part, values));
  }
  const command = nodeCommand(index, launcher, manager);
  if (command.length === 0) {
    throw new AgentProjectError(`${APP_COMMAND_PATH}: ${rules.messages.not_detected_start}`, {
      path: APP_COMMAND_PATH,
      code: "not_detected",
    });
  }
  return command;
}

function protocolOf(override: string | undefined, agentcore: boolean, invoke: string | undefined) {
  if (override !== undefined) {
    return override;
  }
  if (agentcore) {
    return PROTOCOL_AGENTCORE;
  }
  return invoke !== undefined ? PROTOCOL_JSON : PROTOCOL_RAW;
}

function fieldsOf(
  text: string,
  protocol: string,
  rules: DetectionRules,
  sink: WarningSink,
): [string | undefined, string | undefined] {
  if (protocol !== PROTOCOL_JSON) {
    return [undefined, undefined];
  }
  const app = rules.app;
  let request = fieldOf(text, app.requestFields, app.requestFieldTemplates);
  if (request === undefined) {
    request = app.requestFields[0] as string;
    sink.add("request_field_assumed", { field: request });
  }
  return [request, fieldOf(text, app.responseFields, app.responseFieldTemplates)];
}

/** La app de `entry` y su protocolo. Sin ruta POST el protocolo es `raw`;
 * `agentcore` si la app ya habla el contrato (`/ping` y `/invocations`) y
 * `json` en otro caso. */
export function detectApp(
  index: ProjectIndex,
  rules: DetectionRules,
  framework: FrameworkRule,
  entry: EntryChoice,
  manager: string | undefined,
  protocolOverride: string | undefined,
  sink: WarningSink,
): { readonly app: DetectionApp; readonly protocol: string } {
  const spec = framework.app as NonNullable<FrameworkRule["app"]>;
  const launcher = rules.app.launchers[spec.launcher] as LauncherRule;
  const text = index.read(entry.path) ?? "";
  const routes = routesOf(text, entry.language, rules, spec.fixedRoutes);
  const getPaths = routes.filter(([method]) => method === METHOD_GET).map(([, path]) => path);
  const postPaths = routes.filter(([method]) => method === METHOD_POST).map(([, path]) => path);
  const contract = rules.app.agentcorePaths;
  const agentcore = getPaths.includes(contract.health) && postPaths.includes(contract.invoke);
  let health: string;
  let invoke: string | undefined;
  if (protocolOverride === PROTOCOL_AGENTCORE || (protocolOverride === undefined && agentcore)) {
    health = contract.health;
    invoke = contract.invoke;
  } else {
    health = healthPath(getPaths, rules, sink);
    invoke = invokePath(postPaths, rules);
  }
  const protocol = protocolOf(protocolOverride, agentcore, invoke);
  if ((protocol === PROTOCOL_AGENTCORE || protocol === PROTOCOL_JSON) && invoke === undefined) {
    throw new AgentProjectError(`${PROTOCOL_PATH}: ${rules.messages.not_detected_route}`, {
      path: PROTOCOL_PATH,
      code: "not_detected",
    });
  }
  const [requestField, responseField] = fieldsOf(text, protocol, rules, sink);
  const port = portOf(text, entry.language, launcher, rules);
  const command = commandOf(index, launcher, entry, manager, port, rules);
  return {
    app: {
      framework: framework.id,
      command,
      port,
      healthPath: health,
      invokePath: invoke,
      requestField,
      responseField,
    },
    protocol,
  };
}

/** Un `--protocol` desconocido es `invalid_value`. */
export function validateProtocolOverride(protocol: string | undefined): void {
  if (protocol !== undefined && !(APP_PROTOCOLS as readonly string[]).includes(protocol)) {
    throw new AgentProjectError(`${PROTOCOL_PATH}: debe ser uno de ${APP_PROTOCOLS.join(", ")}`, {
      path: PROTOCOL_PATH,
      code: "invalid_value",
    });
  }
}
