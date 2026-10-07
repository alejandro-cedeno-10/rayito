/**
 * Dominio puro del agente de IA (`ai-agent-core`, ADR-025): qué es un
 * `AgentSpec` válido. Espejo de `rayito._agent._domain`. Nada de AWS, gRPC,
 * reloj ni I/O: el puerto `AgentRuntime` y sus adaptadores traducen estos
 * valores a ficheros de configuración y a un comando; este módulo sólo
 * valida la forma de lo que el llamante escribió, antes de cualquier RPC.
 *
 * Las credenciales del modelo nunca pasan por aquí: `AgentModel.gateway` y
 * `McpRemote.gateway` nombran una entrada de `sbx.gateways` (ADR-023), y la
 * pasarela inyecta la cabecera real. Ningún tipo tiene campo de clave ni de
 * cabeceras.
 */

import { InvalidArgumentError } from "../errors.js";
import { defineHidden } from "../hidden.js";
import {
  COMMAND_OUTPUT_MAX_BYTES,
  DEFAULT_AGENT_MAX_OUTPUT_BYTES,
  DEFAULT_AGENT_MAX_STEPS,
  DEFAULT_AGENT_MAX_TOTAL_TOKENS,
  DEFAULT_AGENT_TIMEOUT_SECONDS,
} from "../limits.js";
import { isSafeRequestPath, validateRouteName } from "../secret-gateway/domain.js";

/** Bedrock `Converse`, la Messages API de Anthropic, cualquier API con
 * `chat/completions` de OpenAI (OpenRouter, Groq, Mistral, DeepSeek,
 * LiteLLM), la Responses API de OpenAI (también xAI), Gemini y Azure OpenAI
 * v1, siempre a través de una pasarela
 * (`testdata/agent/provider-catalogue.json`). */
export const MODEL_PROVIDERS = [
  "bedrock",
  "anthropic",
  "openai-compatible",
  "openai",
  "google",
  "azure",
] as const;
export type ModelProvider = (typeof MODEL_PROVIDERS)[number];
export const PERMISSION_ACTIONS = ["allow", "deny"] as const;
export type PermissionAction = (typeof PERMISSION_ACTIONS)[number];
export type ToolPermission = PermissionAction | Readonly<Record<string, PermissionAction>>;

/** El runtime que usa `sbx.agent.run()` si no se pasa `runtime`. */
export const DEFAULT_AGENT_RUNTIME = "opencode";
/** `DEFAULT_AGENT_TIMEOUT_SECONDS` de `limits.json`, en milisegundos. */
export const DEFAULT_AGENT_TIMEOUT_MS = DEFAULT_AGENT_TIMEOUT_SECONDS * 1000;
/** La ejecución es desatendida: una acción `"ask"` la colgaría hasta el
 * timeout. */
const ASK_ACTION = "ask";
/**
 * Denegadas por defecto, por debajo de las entradas del llamante:
 * `question` espera una respuesta humana que nunca llega, y
 * `webfetch`/`websearch` sólo gastan turnos con el egress cerrado.
 */
export const DEFAULT_DENIED_TOOLS = ["question", "webfetch", "websearch"] as const;
/**
 * Claves de la configuración de OpenCode que escribe el adaptador:
 * `rawConfig` no puede tocarlas, porque llevarían el modelo fuera de la
 * pasarela, reactivarían descargas o anularían permisos y límites.
 */
export const RESERVED_CONFIG_KEYS = [
  "provider",
  "autoupdate",
  "share",
  "enabled_providers",
  "model",
  "small_model",
  "mcp",
  "agent",
  "permission",
  "instructions",
  "plugin",
] as const;
/** El agente principal que configura el adaptador de OpenCode. */
export const RESERVED_AGENT_NAMES = ["build"] as const;
/** Nombres de `agents`, `mcp` y herramientas: claves JSON de la
 * configuración del runtime, nunca secretos. */
export const MAX_NAME_LEN = 64;
/** Un ARN de perfil de inferencia de Bedrock mide menos de 200
 * caracteres. */
export const MAX_MODEL_ID_LEN = 256;
/** Un patrón de herramienta se compara como glob en el runtime. */
export const MAX_TOOL_PATTERN_LEN = 256;

const NAME_PATTERN = new RegExp(`^[a-z0-9][a-z0-9_-]{0,${MAX_NAME_LEN - 1}}$`);
const TOOL_NAME_PATTERN = new RegExp(`^[a-z0-9*][a-z0-9_*-]{0,${MAX_NAME_LEN - 1}}$`);
const MODEL_ID_PATTERN = new RegExp(`^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,${MAX_MODEL_ID_LEN - 1}}$`);
const REGION_PATTERN = /^[a-z]{2}(-[a-z]+)+-[0-9]+$/;
const ENV_NAME_PATTERN = /^[A-Za-z_][A-Za-z0-9_]*$/;
const FIRST_PRINTABLE_ASCII = 0x20;
const ASCII_DELETE = 0x7f;

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function frozenRecord(value: unknown, label: string): Readonly<Record<string, unknown>> {
  if (value === undefined) {
    return Object.freeze({});
  }
  if (!isPlainObject(value)) {
    throw new InvalidArgumentError(`${label} debe ser un objeto`);
  }
  return Object.freeze({ ...value });
}

function requireText(value: unknown, label: string): string {
  if (typeof value !== "string" || value.trim() === "") {
    throw new InvalidArgumentError(`${label} debe ser una cadena no vacía`);
  }
  return value;
}

function hasControl(value: string): boolean {
  for (const char of value) {
    const code = char.charCodeAt(0);
    if (code < FIRST_PRINTABLE_ASCII || code === ASCII_DELETE) {
      return true;
    }
  }
  return false;
}

/** 1-64 caracteres `[a-z0-9_-]`, empezando por letra o dígito. */
export function validateName(name: unknown, label: string): string {
  if (typeof name !== "string" || !NAME_PATTERN.test(name)) {
    throw new InvalidArgumentError(
      `nombre inválido en ${label}: 1-${MAX_NAME_LEN} caracteres [a-z0-9_-]`,
    );
  }
  return name;
}

/** Un id de modelo del proveedor, sin espacios ni controles. */
export function validateModelId(modelId: unknown, label: string): string {
  if (typeof modelId !== "string" || !MODEL_ID_PATTERN.test(modelId)) {
    throw new InvalidArgumentError(
      `${label} debe ser un id de modelo de 1-${MAX_MODEL_ID_LEN} caracteres [A-Za-z0-9._:/@+-]`,
    );
  }
  return modelId;
}

/** `""` o una ruta absoluta sin `/` final (`"/v1"`) que la pasarela pueda
 * reenviar tal cual. */
export function validateBasePath(basePath: unknown, label: string): string {
  if (typeof basePath !== "string") {
    throw new InvalidArgumentError(`${label} debe ser una cadena`);
  }
  if (basePath === "") {
    return basePath;
  }
  if (basePath.endsWith("/") || !isSafeRequestPath(basePath)) {
    throw new InvalidArgumentError(
      `${label} debe ser '' o una ruta absoluta sin '/' final ('/v1')`,
    );
  }
  return basePath;
}

export function validateRegion(region: unknown, label: string): string {
  if (typeof region !== "string" || !REGION_PATTERN.test(region)) {
    throw new InvalidArgumentError(`${label} debe ser una región de AWS ('us-east-1')`);
  }
  return region;
}

export interface AgentModelOptions {
  readonly provider: ModelProvider;
  readonly id: string;
  readonly gateway: string;
  readonly region?: string | undefined;
  readonly basePath?: string | undefined;
  readonly promptCaching?: boolean | undefined;
}

/**
 * El modelo que usa el agente y la pasarela por la que llega a él.
 * `gateway` nombra una entrada de `sbx.gateways`; si el sandbox no la
 * tiene, `sbx.agent.run()` falla con `InvalidArgumentError` antes de
 * cualquier RPC. `region` es obligatoria con `"bedrock"`; `basePath` sólo
 * se usa con `"openai-compatible"` y es el del preset (`"/api/v1"` con
 * `openrouterGateway`, `"/openai/v1"` con `groqGateway`, `"/v1"` con
 * `mistralGateway` y `litellmGateway`, `""` con `deepseekGateway`).
 * `"openai"` va con `openaiGateway` o `xaiGateway`, `"google"` con
 * `geminiGateway` y `"azure"` con `azureOpenaiGateway`. `promptCaching`
 * (por defecto `true`) deja que el runtime marque puntos de caché.
 */
export class AgentModel {
  readonly provider: ModelProvider;
  readonly id: string;
  readonly gateway: string;
  readonly region: string | undefined;
  readonly basePath: string;
  readonly promptCaching: boolean;

  constructor(options: AgentModelOptions) {
    if (!(MODEL_PROVIDERS as readonly string[]).includes(options.provider)) {
      throw new InvalidArgumentError(
        `AgentModel.provider debe ser uno de ${JSON.stringify(MODEL_PROVIDERS)}`,
      );
    }
    validateModelId(options.id, "AgentModel.id");
    validateRouteName(options.gateway);
    if (options.provider === "bedrock" && options.region === undefined) {
      throw new InvalidArgumentError("AgentModel.region es obligatoria con 'bedrock'");
    }
    if (options.region !== undefined) {
      validateRegion(options.region, "AgentModel.region");
    }
    const basePath = validateBasePath(options.basePath ?? "", "AgentModel.basePath");
    if (basePath !== "" && options.provider !== "openai-compatible") {
      throw new InvalidArgumentError("AgentModel.basePath sólo se admite con 'openai-compatible'");
    }
    const promptCaching = options.promptCaching ?? true;
    if (typeof promptCaching !== "boolean") {
      throw new InvalidArgumentError("AgentModel.promptCaching debe ser un boolean");
    }
    this.provider = options.provider;
    this.id = options.id;
    this.gateway = options.gateway;
    this.region = options.region;
    this.basePath = basePath;
    this.promptCaching = promptCaching;
  }
}

function validateAction(action: unknown, label: string): PermissionAction {
  if (action === ASK_ACTION) {
    throw new InvalidArgumentError(
      `${label}: 'ask' no se admite, la ejecución no tiene a nadie que conteste; usa 'allow' o 'deny'`,
    );
  }
  if (action === "allow" || action === "deny") {
    return action;
  }
  throw new InvalidArgumentError(`${label} debe ser 'allow' o 'deny'`);
}

function validateToolPermission(tool: string, rule: unknown): ToolPermission {
  const label = `AgentPermissions.tools[${JSON.stringify(tool)}]`;
  if (!isPlainObject(rule)) {
    return validateAction(rule, label);
  }
  const entries = Object.entries(rule);
  if (entries.length === 0) {
    throw new InvalidArgumentError(`${label} no puede ser un objeto vacío`);
  }
  const patterns: Record<string, PermissionAction> = {};
  for (const [pattern, action] of entries) {
    if (pattern === "" || pattern.length > MAX_TOOL_PATTERN_LEN || hasControl(pattern)) {
      throw new InvalidArgumentError(
        `${label}: cada patrón debe ser una cadena de 1-${MAX_TOOL_PATTERN_LEN} caracteres sin controles`,
      );
    }
    patterns[pattern] = validateAction(action, label);
  }
  return Object.freeze(patterns);
}

export interface AgentPermissionsOptions {
  readonly default?: PermissionAction | undefined;
  readonly tools?: Readonly<Record<string, ToolPermission>> | undefined;
}

/**
 * Qué herramientas puede usar el agente: `default` para las que no tienen
 * entrada y `tools` con `"allow"`/`"deny"` por herramienta o por patrón de
 * su argumento (`{ bash: { "git *": "allow", "*": "deny" } }`). `"ask"` no
 * existe. No es una frontera de seguridad (SECURITY.md T29).
 */
export class AgentPermissions {
  readonly default: PermissionAction;
  readonly tools: Readonly<Record<string, ToolPermission>>;

  constructor(options: AgentPermissionsOptions = {}) {
    this.default = validateAction(options.default ?? "allow", "AgentPermissions.default");
    const tools = frozenRecord(options.tools, "AgentPermissions.tools");
    const validated: Record<string, ToolPermission> = {};
    for (const [tool, rule] of Object.entries(tools)) {
      if (!TOOL_NAME_PATTERN.test(tool)) {
        throw new InvalidArgumentError(
          `nombre de herramienta inválido en AgentPermissions.tools: 1-${MAX_NAME_LEN} caracteres [a-z0-9_*-]`,
        );
      }
      validated[tool] = validateToolPermission(tool, rule);
    }
    this.tools = Object.freeze(validated);
  }

  /** `DEFAULT_DENIED_TOOLS` en `"deny"` y encima las entradas de `tools`. */
  effectiveTools(): Readonly<Record<string, ToolPermission>> {
    const merged: Record<string, ToolPermission> = {};
    for (const tool of DEFAULT_DENIED_TOOLS) {
      merged[tool] = "deny";
    }
    return Object.freeze({ ...merged, ...this.tools });
  }
}

export interface SubAgentOptions {
  readonly description: string;
  readonly instructions: string;
  readonly model?: string | undefined;
  readonly permissions?: AgentPermissions | undefined;
}

/** Un subagente que el agente principal puede invocar. `model` es un id del
 * mismo proveedor y pasarela que `AgentSpec.model`; sin él hereda el suyo. */
export class SubAgent {
  readonly description: string;
  readonly instructions: string;
  readonly model: string | undefined;
  readonly permissions: AgentPermissions | undefined;

  constructor(options: SubAgentOptions) {
    this.description = requireText(options.description, "SubAgent.description");
    this.instructions = requireText(options.instructions, "SubAgent.instructions");
    if (options.model !== undefined) {
      validateModelId(options.model, "SubAgent.model");
    }
    if (options.permissions !== undefined && !(options.permissions instanceof AgentPermissions)) {
      throw new InvalidArgumentError("SubAgent.permissions debe ser un AgentPermissions");
    }
    this.model = options.model;
    this.permissions = options.permissions;
  }
}

export interface McpLocalOptions {
  readonly command: readonly string[];
  readonly envs?: Readonly<Record<string, string>> | undefined;
  readonly timeoutMs?: number | undefined;
}

/**
 * Un servidor MCP que el runtime arranca dentro del sandbox (`command` como
 * lista, sin shell). `envs` se escribe en la configuración del runtime,
 * legible por uid 1000: nunca pongas ahí una credencial, usa una pasarela.
 * `envs` no es enumerable, así que `console.log` no lo muestra.
 */
export class McpLocal {
  readonly command: readonly string[];
  declare readonly envs: Readonly<Record<string, string>>;
  readonly timeoutMs: number | undefined;

  constructor(options: McpLocalOptions) {
    const command = options.command;
    if (!Array.isArray(command) || command.length === 0) {
      throw new InvalidArgumentError("McpLocal.command debe ser una lista no vacía");
    }
    for (const part of command) {
      if (typeof part !== "string" || part === "" || part.includes("\0")) {
        throw new InvalidArgumentError("McpLocal.command sólo admite cadenas no vacías sin NUL");
      }
    }
    const envs = frozenRecord(options.envs, "McpLocal.envs");
    for (const [name, value] of Object.entries(envs)) {
      if (!ENV_NAME_PATTERN.test(name)) {
        throw new InvalidArgumentError("nombre de variable inválido en McpLocal.envs");
      }
      if (typeof value !== "string" || value.includes("\0")) {
        throw new InvalidArgumentError("McpLocal.envs sólo admite cadenas sin NUL");
      }
    }
    const timeoutMs = options.timeoutMs;
    if (timeoutMs !== undefined && (typeof timeoutMs !== "number" || !(timeoutMs > 0))) {
      throw new InvalidArgumentError("McpLocal.timeoutMs debe ser > 0");
    }
    this.command = Object.freeze([...command]);
    defineHidden(this, "envs", envs as Readonly<Record<string, string>>);
    this.timeoutMs = timeoutMs;
  }
}

export interface McpRemoteOptions {
  readonly gateway: string;
  readonly path?: string | undefined;
}

/** Un servidor MCP remoto alcanzado a través de la pasarela `gateway`. No
 * hay `headers` a propósito: la credencial sólo la pone la pasarela. */
export class McpRemote {
  readonly gateway: string;
  readonly path: string;

  constructor(options: McpRemoteOptions) {
    validateRouteName(options.gateway);
    const path = options.path ?? "/";
    if (typeof path !== "string" || !isSafeRequestPath(path)) {
      throw new InvalidArgumentError(
        "McpRemote.path debe ser una ruta absoluta que la pasarela pueda reenviar",
      );
    }
    this.gateway = options.gateway;
    this.path = path;
  }
}

export type McpServer = McpLocal | McpRemote;

export interface AgentSpecOptions {
  readonly model: AgentModel;
  readonly smallModel?: string | undefined;
  readonly instructions?: string | undefined;
  readonly mcp?: Readonly<Record<string, McpServer>> | undefined;
  readonly permissions?: AgentPermissions | undefined;
  readonly agents?: Readonly<Record<string, SubAgent>> | undefined;
  readonly rawConfig?: Readonly<Record<string, unknown>> | undefined;
  readonly runtimeVersion?: string | undefined;
}

/**
 * La configuración estática de un agente. Construirla no llama a nada.
 * `smallModel` es por defecto `model.id`; `instructions` va a un
 * `AGENTS.md` propio, nunca al del directorio de trabajo; `rawConfig` se
 * fusiona al final y no puede tocar `RESERVED_CONFIG_KEYS`;
 * `runtimeVersion` sólo se valida como texto: hoy no se compara con el
 * manifiesto de la plantilla y nunca produce `runtime_version_mismatch`.
 */
export class AgentSpec {
  readonly model: AgentModel;
  readonly smallModel: string | undefined;
  readonly instructions: string | undefined;
  readonly mcp: Readonly<Record<string, McpServer>>;
  readonly permissions: AgentPermissions;
  readonly agents: Readonly<Record<string, SubAgent>>;
  readonly rawConfig: Readonly<Record<string, unknown>> | undefined;
  readonly runtimeVersion: string | undefined;

  constructor(options: AgentSpecOptions) {
    if (!(options.model instanceof AgentModel)) {
      throw new InvalidArgumentError("AgentSpec.model debe ser un AgentModel");
    }
    if (options.smallModel !== undefined) {
      validateModelId(options.smallModel, "AgentSpec.smallModel");
    }
    if (options.instructions !== undefined) {
      requireText(options.instructions, "AgentSpec.instructions");
    }
    const permissions = options.permissions ?? new AgentPermissions();
    if (!(permissions instanceof AgentPermissions)) {
      throw new InvalidArgumentError("AgentSpec.permissions debe ser un AgentPermissions");
    }
    if (options.runtimeVersion !== undefined) {
      requireText(options.runtimeVersion, "AgentSpec.runtimeVersion");
    }
    this.model = options.model;
    this.smallModel = options.smallModel;
    this.instructions = options.instructions;
    this.mcp = validateMcp(options.mcp);
    this.permissions = permissions;
    this.agents = validateAgents(options.agents);
    this.rawConfig =
      options.rawConfig === undefined ? undefined : validateRawConfig(options.rawConfig);
    this.runtimeVersion = options.runtimeVersion;
  }

  get effectiveSmallModel(): string {
    return this.smallModel ?? this.model.id;
  }

  /** La pasarela del modelo y la de cada `McpRemote`. */
  gatewayNames(): ReadonlySet<string> {
    const names = new Set([this.model.gateway]);
    for (const server of Object.values(this.mcp)) {
      if (server instanceof McpRemote) {
        names.add(server.gateway);
      }
    }
    return names;
  }

  /** Falla con `InvalidArgumentError` si falta alguna pasarela de
   * `gatewayNames()` en `available` (los nombres de `sbx.gateways`). */
  requireGateways(available: Iterable<string>): void {
    const present = new Set(available);
    const missing = [...this.gatewayNames()].filter((name) => !present.has(name)).sort();
    const first = missing[0];
    if (first !== undefined) {
      throw new InvalidArgumentError(
        `el sandbox no tiene la pasarela '${first}': créalo con gateways: { "${first}": new SecretGateway(...) }`,
      );
    }
  }
}

function validateMcp(mcp: unknown): Readonly<Record<string, McpServer>> {
  const validated: Record<string, McpServer> = {};
  for (const [name, server] of Object.entries(frozenRecord(mcp, "AgentSpec.mcp"))) {
    validateName(name, "AgentSpec.mcp");
    if (!(server instanceof McpLocal || server instanceof McpRemote)) {
      throw new InvalidArgumentError(
        "cada valor de AgentSpec.mcp debe ser un McpLocal o un McpRemote",
      );
    }
    validated[name] = server;
  }
  return Object.freeze(validated);
}

function validateAgents(agents: unknown): Readonly<Record<string, SubAgent>> {
  const validated: Record<string, SubAgent> = {};
  for (const [name, agent] of Object.entries(frozenRecord(agents, "AgentSpec.agents"))) {
    validateName(name, "AgentSpec.agents");
    if ((RESERVED_AGENT_NAMES as readonly string[]).includes(name)) {
      throw new InvalidArgumentError(
        `AgentSpec.agents no puede usar el nombre reservado '${name}'`,
      );
    }
    if (!(agent instanceof SubAgent)) {
      throw new InvalidArgumentError("cada valor de AgentSpec.agents debe ser un SubAgent");
    }
    validated[name] = agent;
  }
  return Object.freeze(validated);
}

/** Un objeto cuyas claves de primer nivel no están en
 * `RESERVED_CONFIG_KEYS`; el error nombra la clave. */
export function validateRawConfig(rawConfig: unknown): Readonly<Record<string, unknown>> {
  const config = frozenRecord(rawConfig, "AgentSpec.rawConfig");
  for (const key of Object.keys(config)) {
    if ((RESERVED_CONFIG_KEYS as readonly string[]).includes(key)) {
      throw new InvalidArgumentError(
        `AgentSpec.rawConfig no puede fijar '${key}': la escribe Rayito`,
      );
    }
  }
  return config;
}

function positiveInt(value: unknown, label: string, maximum?: number): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 1) {
    throw new InvalidArgumentError(`${label} debe ser un entero >= 1`);
  }
  if (maximum !== undefined && value > maximum) {
    throw new InvalidArgumentError(`${label} no puede pasar de ${maximum}`);
  }
  return value;
}

export interface AgentLimitsOptions {
  readonly maxSteps?: number | undefined;
  readonly timeoutMs?: number | undefined;
  readonly maxOutputBytes?: number | undefined;
  /** `null` desactiva el presupuesto de tokens. */
  readonly maxTotalTokens?: number | null | undefined;
}

/**
 * Topes duros de una ejecución, impuestos por el SDK: `maxSteps` aborta al
 * empezar el paso `maxSteps + 1`; `maxTotalTokens` se comprueba tras cada
 * paso (puede pasarse en uno; `null` lo desactiva); `timeoutMs` y
 * `maxOutputBytes` son los del comando del runtime. Los valores por defecto
 * acotan el coste de una ejecución.
 */
export class AgentLimits {
  readonly maxSteps: number;
  readonly timeoutMs: number;
  readonly maxOutputBytes: number;
  readonly maxTotalTokens: number | null;

  constructor(options: AgentLimitsOptions = {}) {
    this.maxSteps = positiveInt(
      options.maxSteps ?? DEFAULT_AGENT_MAX_STEPS,
      "AgentLimits.maxSteps",
    );
    const timeoutMs = options.timeoutMs ?? DEFAULT_AGENT_TIMEOUT_MS;
    if (typeof timeoutMs !== "number" || !(timeoutMs > 0)) {
      throw new InvalidArgumentError("AgentLimits.timeoutMs debe ser > 0");
    }
    this.timeoutMs = timeoutMs;
    this.maxOutputBytes = positiveInt(
      options.maxOutputBytes ?? DEFAULT_AGENT_MAX_OUTPUT_BYTES,
      "AgentLimits.maxOutputBytes",
      COMMAND_OUTPUT_MAX_BYTES,
    );
    const maxTotalTokens =
      options.maxTotalTokens === undefined
        ? DEFAULT_AGENT_MAX_TOTAL_TOKENS
        : options.maxTotalTokens;
    this.maxTotalTokens =
      maxTotalTokens === null ? null : positiveInt(maxTotalTokens, "AgentLimits.maxTotalTokens");
  }
}
