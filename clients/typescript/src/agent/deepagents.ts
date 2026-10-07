/**
 * Adaptador de deepagents del puerto `AgentRuntime` (`ai-agent-deepagents`,
 * ADR-025, design.md §3). Espejo de `rayito._agent._deepagents`: escribe la
 * configuración JSON que lee el runner (`DEEPAGENTS_RUNNER_PATH`, cuyo
 * código está en `assets/deepagents-runner.ts`), construye el script de una
 * ejecución y traduce el protocolo JSONL v1 de Rayito a eventos. La
 * configuración, su sha256, el script y el stdin son idénticos byte a byte
 * a los de Python (`testdata/agent/`).
 *
 * Los permisos se traducen a los nombres de herramienta de deepagents
 * (`DEEPAGENTS_TOOL_NAMES`); sólo `bash` admite patrones. `mcp` y
 * `rawConfig` no existen en este runtime y fallan con
 * `InvalidArgumentError` antes de cualquier RPC.
 */

import { InvalidArgumentError, UnimplementedError } from "../errors.js";
import {
  AGENT_PROTOCOL_VERSION,
  AGENT_STATE_DIR,
  DEFAULT_AGENT_WORKDIR,
  MODEL_CREDENTIAL_PLACEHOLDER,
} from "../limits.js";
import {
  type AgentPermissions,
  type AgentSpec,
  DEFAULT_DENIED_TOOLS,
  validateModelId,
} from "./domain.js";
import {
  type AgentEvent,
  type AgentFailed,
  agentFailed,
  type Done,
  TokenUsage,
  truncateToolOutput,
} from "./events.js";
import { AGENT_RUN_LOCK_PATH, canonicalJson, filesSha256, shellQuote } from "./opencode.js";
import type {
  AgentRuntime,
  RunCommand,
  RunRequest,
  RuntimeFile,
  RuntimeFiles,
  RuntimeState,
  TemplateStep,
  WarmupStep,
} from "./runtime.js";

/** Estado de deepagents dentro de `AGENT_STATE_DIR`. */
export const DEEPAGENTS_STATE_DIR = `${AGENT_STATE_DIR}/deepagents`;
/** La configuración estática que lee el runner. */
export const DEEPAGENTS_CONFIG_PATH = `${DEEPAGENTS_STATE_DIR}/config.json`;
/** Historial de cada sesión, `<id>.json`. */
export const DEEPAGENTS_SESSIONS_DIR = `${DEEPAGENTS_STATE_DIR}/sessions`;
/** El Python del venv de deepagents de la plantilla. */
export const DEEPAGENTS_PYTHON = "/opt/agents/deepagents/bin/python";
/** Donde la plantilla instala el runner, de root y 0755. */
export const DEEPAGENTS_RUNNER_PATH = "/opt/agents/rayito/deepagents_runner.py";
/** Herramienta de `AgentPermissions` -> herramientas de deepagents 0.7. */
export const DEEPAGENTS_TOOL_NAMES: Readonly<Record<string, readonly string[]>> = Object.freeze({
  read: ["read_file"],
  edit: ["write_file", "edit_file", "delete"],
  list: ["ls"],
  glob: ["glob"],
  grep: ["grep"],
  bash: ["execute"],
  task: ["task"],
  todowrite: ["write_todos"],
});
/** La única herramienta cuyos patrones se comparan (con el comando). */
export const PATTERN_TOOL = "bash";
/** Sin `.pyc` en el directorio del usuario y con stdout sin búfer. */
export const DEEPAGENTS_FLAG_ENVS: Readonly<Record<string, string>> = Object.freeze({
  PYTHONDONTWRITEBYTECODE: "1",
  PYTHONUNBUFFERED: "1",
});
/** Fallos que el runner puede declarar; el resto sólo los decide el SDK. */
export const RUNNER_FAILURE_REASONS = ["model_error", "runtime_error", "protocol_error"] as const;

/** Lo que la clase de LangChain espera en `base_url` con los proveedores
 * nativos (`ChatOpenAI` añade `/responses`; `ChatGoogleGenerativeAI`, su
 * `/v1beta/models/...`). Con `"openai-compatible"` se usa
 * `AgentModel.basePath`. */
export const DEEPAGENTS_NATIVE_BASE_PATHS: Readonly<Record<string, string>> = Object.freeze({
  openai: "/v1",
  google: "",
});
/** Proveedores que el runner aún no sabe hablar a través de la pasarela. */
export const DEEPAGENTS_UNSUPPORTED_PROVIDERS: readonly string[] = Object.freeze(["azure"]);

const ENTRYPOINT_PATTERN = /^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$/;
const SESSION_ID_PATTERN = /^rda_[0-9a-f]{32}$/;
const UTF8 = new TextEncoder();
const UTF8_DECODER = new TextDecoder();

/** Opciones de `new DeepAgents(...)`. */
export interface DeepAgentsOptions {
  /** `"pkg.mod:build"`: una función de Python, importable desde el
   * directorio de trabajo, que recibe un `RunnerContext` y devuelve un grafo
   * compilado. Sin ella, el runner usa `create_deep_agent`. */
  readonly entrypoint?: string | undefined;
}

export interface DeepAgentsState extends RuntimeState {
  sessionId: string | undefined;
  steps: number;
  usage: TokenUsage;
  done: boolean;
  failed: AgentFailed | undefined;
  ignoredLines: number;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function toolRules(permissions: AgentPermissions, label: string): Record<string, unknown> {
  const tools: Record<string, unknown> = {};
  for (const [tool, rule] of Object.entries(permissions.effectiveTools())) {
    if (
      (DEFAULT_DENIED_TOOLS as readonly string[]).includes(tool) &&
      !(tool in permissions.tools)
    ) {
      continue;
    }
    const names = DEEPAGENTS_TOOL_NAMES[tool];
    if (names === undefined) {
      throw new InvalidArgumentError(
        `${label}: deepagents no tiene la herramienta '${tool}'; usa una de ${JSON.stringify(Object.keys(DEEPAGENTS_TOOL_NAMES).sort())}`,
      );
    }
    if (typeof rule !== "string" && tool !== PATTERN_TOOL) {
      throw new InvalidArgumentError(
        `${label}: con deepagents sólo '${PATTERN_TOOL}' admite patrones`,
      );
    }
    for (const name of names) {
      tools[name] = typeof rule === "string" ? rule : { ...rule };
    }
  }
  return { default: permissions.default, tools };
}

/** La configuración que lee el runner, como objeto. */
export function buildDeepAgentsConfig(
  spec: AgentSpec,
  options: {
    readonly gatewayUrls: Readonly<Record<string, string>>;
    readonly workdir: string;
    readonly entrypoint: string | undefined;
  },
): Record<string, unknown> {
  spec.requireGateways(Object.keys(options.gatewayUrls));
  if (Object.keys(spec.mcp).length > 0) {
    throw new InvalidArgumentError("deepagents no admite AgentSpec.mcp; usa runtime 'opencode'");
  }
  if (spec.rawConfig !== undefined && Object.keys(spec.rawConfig).length > 0) {
    throw new InvalidArgumentError(
      "AgentSpec.rawConfig es configuración de OpenCode; deepagents no la admite",
    );
  }
  const model = spec.model;
  if (DEEPAGENTS_UNSUPPORTED_PROVIDERS.includes(model.provider)) {
    throw new UnimplementedError(
      `deepagents con AgentModel({ provider: '${model.provider}' })`,
      "AzureChatOpenAI y ChatOpenAI mandan una cabecera que Azure lee como credencial y la " +
        "pasarela aún no puede quitarla; usa runtime 'opencode'",
    );
  }
  const gatewayUrl = options.gatewayUrls[model.gateway] as string;
  const basePath = DEEPAGENTS_NATIVE_BASE_PATHS[model.provider] ?? model.basePath;
  const subagents = Object.entries(spec.agents).map(([name, sub]) => ({
    name,
    description: sub.description,
    system_prompt: sub.instructions,
    model: sub.model ?? null,
    permissions:
      sub.permissions === undefined
        ? null
        : toolRules(sub.permissions, `AgentSpec.agents['${name}'].permissions`),
  }));
  return {
    v: AGENT_PROTOCOL_VERSION,
    provider: model.provider,
    model: model.id,
    region: model.region ?? null,
    base_url: stripTrailingSlashes(gatewayUrl) + basePath,
    credential_placeholder: MODEL_CREDENTIAL_PLACEHOLDER,
    prompt_caching: model.promptCaching,
    instructions: spec.instructions ?? null,
    workdir: options.workdir,
    sessions_dir: DEEPAGENTS_SESSIONS_DIR,
    entrypoint: options.entrypoint ?? null,
    permissions: toolRules(spec.permissions, "AgentSpec.permissions"),
    subagents,
  };
}

function stripTrailingSlashes(url: string): string {
  let end = url.length;
  while (end > 0 && url[end - 1] === "/") {
    end -= 1;
  }
  return url.slice(0, end);
}

function protocolLine(kind: string): string {
  return `printf '%s\\n' '{"v":${AGENT_PROTOCOL_VERSION},"type":"rayito.${kind}"}'`;
}

function runScript(configPath: string): string {
  const lines = [
    "set -u",
    `mkdir -p ${shellQuote(DEEPAGENTS_SESSIONS_DIR)}`,
    `exec 9>${shellQuote(AGENT_RUN_LOCK_PATH)}`,
    `if ! flock -n 9; then ${protocolLine("busy")}; exit 0; fi`,
    `if [ ! -x ${shellQuote(DEEPAGENTS_PYTHON)} ] || [ ! -r ${shellQuote(DEEPAGENTS_RUNNER_PATH)} ]; then ${protocolLine("runtime_missing")}; exit 0; fi`,
    `exec ${shellQuote(DEEPAGENTS_PYTHON)} ${shellQuote(DEEPAGENTS_RUNNER_PATH)} ${shellQuote(configPath)}`,
  ];
  return `${lines.join("\n")}\n`;
}

function count(value: unknown): number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : 0;
}

function usageOf(raw: unknown): TokenUsage {
  if (!isPlainObject(raw)) {
    return new TokenUsage();
  }
  return new TokenUsage({
    input: count(raw.input),
    output: count(raw.output),
    reasoning: count(raw.reasoning),
    cacheRead: count(raw.cache_read),
    cacheWrite: count(raw.cache_write),
  });
}

function asState(state: RuntimeState): DeepAgentsState {
  return state as DeepAgentsState;
}

/**
 * `AgentRuntime` de deepagents (`runtime: new DeepAgents(...)` o
 * `runtime: "deepagents"`).
 *
 * Sin `entrypoint`, el runner construye `create_deep_agent(model,
 * system_prompt=instructions, subagents, backend=LocalShellBackend(
 * root_dir=workdir), middleware=ctx.middleware)`. Con `entrypoint:
 * "pkg.mod:build"`, importa ese módulo de Python desde el directorio de
 * trabajo y llama a `build(ctx)`; debe devolver un grafo compilado. Si no
 * pasa `ctx.middleware` a su grafo, se pierden los permisos de `AgentSpec`.
 *
 * El modelo siempre llega por la pasarela con un marcador como clave.
 * deepagents marca puntos de caché de prompts para Claude;
 * `AgentModel({ promptCaching: false })` lo apaga.
 */
export class DeepAgents implements AgentRuntime {
  readonly name = "deepagents";
  readonly entrypoint: string | undefined;

  constructor(options: DeepAgentsOptions = {}) {
    const entrypoint = options.entrypoint;
    if (
      entrypoint !== undefined &&
      (typeof entrypoint !== "string" || !ENTRYPOINT_PATTERN.test(entrypoint))
    ) {
      throw new InvalidArgumentError(
        "DeepAgents.entrypoint debe tener la forma 'paquete.modulo:funcion'",
      );
    }
    this.entrypoint = entrypoint;
  }

  buildConfig(
    spec: AgentSpec,
    options: { readonly gatewayUrls: Readonly<Record<string, string>>; readonly workdir: string },
  ): RuntimeFiles {
    const config = buildDeepAgentsConfig(spec, { ...options, entrypoint: this.entrypoint });
    const files: RuntimeFile[] = [
      { path: DEEPAGENTS_CONFIG_PATH, data: canonicalJson(config), mode: 0o600 },
    ];
    return { files, configSha256: filesSha256(files) };
  }

  /** El script (cerrojo, comprobación del runner, `exec`) y la petición
   * JSON por stdin. */
  command(request: RunRequest): RunCommand {
    if (request.sessionId !== undefined && !SESSION_ID_PATTERN.test(request.sessionId)) {
      throw new InvalidArgumentError("sessionId de deepagents debe tener la forma rda_<hex>");
    }
    if (request.model !== undefined) {
      validateModelId(request.model, "model");
    }
    const payload = {
      v: AGENT_PROTOCOL_VERSION,
      prompt: request.prompt,
      session_id: request.sessionId ?? null,
      model: request.model ?? null,
      reasoning: request.reasoning ?? false,
    };
    const envs: Record<string, string> = { HOME: DEFAULT_AGENT_WORKDIR, ...DEEPAGENTS_FLAG_ENVS };
    if (request.spec.model.provider === "bedrock") {
      envs.AWS_BEARER_TOKEN_BEDROCK = MODEL_CREDENTIAL_PLACEHOLDER;
    }
    if (request.spec.model.region !== undefined) {
      envs.AWS_REGION = request.spec.model.region;
    }
    return {
      script: runScript(DEEPAGENTS_CONFIG_PATH),
      envs,
      stdin: UTF8.encode(JSON.stringify(payload)),
    };
  }

  newState(): RuntimeState {
    const state: DeepAgentsState = {
      sessionId: undefined,
      steps: 0,
      usage: new TokenUsage(),
      done: false,
      failed: undefined,
      ignoredLines: 0,
    };
    return state;
  }

  /** Una línea del protocolo v1 a cero o un evento; lo ilegible, de otra
   * versión o de tipo desconocido se ignora y se cuenta. */
  parseLine(line: Uint8Array, rawState: RuntimeState): readonly AgentEvent[] {
    const state = asState(rawState);
    let payload: unknown;
    try {
      payload = JSON.parse(UTF8_DECODER.decode(line));
    } catch {
      state.ignoredLines += 1;
      return [];
    }
    if (!isPlainObject(payload) || payload.v !== AGENT_PROTOCOL_VERSION) {
      state.ignoredLines += 1;
      return [];
    }
    const events = this.map(payload, state);
    if (events === undefined) {
      state.ignoredLines += 1;
      return [];
    }
    return events;
  }

  private map(payload: Record<string, unknown>, state: DeepAgentsState): AgentEvent[] | undefined {
    const kind = payload.type;
    switch (kind) {
      case "rayito.busy":
      case "rayito.runtime_missing":
        state.failed = agentFailed(kind.slice("rayito.".length));
        return [state.failed];
      case "session": {
        const sessionId = payload.session_id;
        if (typeof sessionId !== "string" || !SESSION_ID_PATTERN.test(sessionId)) {
          return undefined;
        }
        state.sessionId = sessionId;
        return [];
      }
      case "step_started":
        state.steps += 1;
        return [Object.freeze({ type: "step_started", index: state.steps })];
      case "step_finished": {
        const usage = usageOf(payload.usage);
        state.usage = state.usage.plus(usage);
        return [
          Object.freeze({
            type: "step_finished",
            index: state.steps,
            usage,
            finishReason:
              typeof payload.finish_reason === "string" ? payload.finish_reason : undefined,
          }),
        ];
      }
      case "text_delta":
      case "text":
      case "reasoning": {
        const text = payload.text;
        return typeof text === "string" ? [Object.freeze({ type: kind, text })] : undefined;
      }
      case "tool_call":
        return toolCallOf(payload);
      case "agent_failed": {
        const reason = (RUNNER_FAILURE_REASONS as readonly unknown[]).includes(payload.reason)
          ? (payload.reason as string)
          : "protocol_error";
        state.failed = agentFailed(reason, {
          detailCode: typeof payload.detail_code === "string" ? payload.detail_code : undefined,
          sessionId: state.sessionId,
        });
        return [state.failed];
      }
      case "done":
        state.done = true;
        return [];
      default:
        return undefined;
    }
  }

  /** `Done` sólo con salida 0, el `done` del runner y una sesión. */
  finish(rawState: RuntimeState, exitCode: number): Done | AgentFailed {
    const state = asState(rawState);
    if (state.failed !== undefined) {
      return state.failed;
    }
    if (exitCode !== 0) {
      return agentFailed("runtime_error", { exitCode, sessionId: state.sessionId });
    }
    if (!state.done || state.sessionId === undefined) {
      return agentFailed("protocol_error", { exitCode, sessionId: state.sessionId });
    }
    return Object.freeze({
      type: "done",
      sessionId: state.sessionId,
      exitCode,
      usage: state.usage,
    });
  }

  /** Vacío: la plantilla de agente declara sus propios pasos. */
  templateSteps(): readonly TemplateStep[] {
    return [];
  }

  /** Carga deepagents y langchain en la caché de páginas. */
  warmupSteps(): readonly WarmupStep[] {
    return [
      {
        cmd: `${shellQuote(DEEPAGENTS_PYTHON)} -c 'import deepagents, langchain_aws' >/dev/null 2>&1 || true`,
      },
    ];
  }
}

function toolCallOf(payload: Record<string, unknown>): AgentEvent[] | undefined {
  const status = payload.status;
  if (status !== "completed" && status !== "error") {
    return undefined;
  }
  let output: string | undefined;
  let truncated = false;
  if (typeof payload.output === "string") {
    const cut = truncateToolOutput(payload.output);
    output = cut.text;
    truncated = cut.truncated;
  }
  return [
    Object.freeze({
      type: "tool_call",
      callId: String(payload.call_id ?? ""),
      name: String(payload.name ?? ""),
      status,
      input: isPlainObject(payload.input) ? { ...payload.input } : undefined,
      output,
      outputTruncated: truncated || payload.output_truncated === true,
    }),
  ];
}
