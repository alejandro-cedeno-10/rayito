/**
 * Adaptador de OpenCode del puerto `AgentRuntime` (`ai-agent-core`,
 * ADR-025, design.md §3). Espejo de `rayito._agent._opencode`: construye el
 * `opencode.json`, el script de bash de una ejecución y traduce el JSONL de
 * `opencode run --format json` (v1.18.34) a eventos de Rayito. El script, la
 * configuración y su sha256 son idénticos byte a byte a los de Python
 * (`testdata/agent/`).
 *
 * El prompt va por stdin, nunca en argv; la contraseña del servidor
 * residente se lee de su fichero dentro del script. Con `--attach` el código
 * de salida no refleja `session.error`, así que el fallo sale de los eventos
 * `error` y `finish()` sólo da `Done` con salida 0 y ningún `error` visto.
 * Las líneas propias del script llevan `type` con prefijo `rayito.`
 * (`busy`, `runtime_missing`, `attached`).
 */

import { createHash } from "node:crypto";
import { InvalidArgumentError } from "../errors.js";
import {
  AGENT_STATE_DIR,
  DEFAULT_AGENT_WORKDIR,
  MODEL_CREDENTIAL_PLACEHOLDER,
  OPENCODE_SERVE_PORT,
  OPENCODE_SESSION_TITLE,
} from "../limits.js";
import { type AgentPermissions, type AgentSpec, McpLocal } from "./domain.js";
import {
  type AgentEvent,
  type AgentFailed,
  type AgentFailureReason,
  agentFailed,
  type Done,
  TokenUsage,
  type ToolCall,
  truncateToolOutput,
} from "./events.js";
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

/** Directorio de OpenCode dentro de `AGENT_STATE_DIR`. */
export const OPENCODE_STATE_DIR = `${AGENT_STATE_DIR}/opencode`;
/** El `OPENCODE_CONFIG` de cada ejecución. */
export const OPENCODE_CONFIG_PATH = `${OPENCODE_STATE_DIR}/opencode.json`;
/** Las instrucciones de `AgentSpec.instructions`. */
export const OPENCODE_INSTRUCTIONS_PATH = `${OPENCODE_STATE_DIR}/AGENTS.md`;
/** Contraseña del `opencode serve` residente, 0600. */
export const OPENCODE_SERVE_SECRET_PATH = `${OPENCODE_STATE_DIR}/serve.secret`;
/** sha256 del `opencode.json` que el servidor residente cargó por última vez. */
export const OPENCODE_APPLIED_SHA_PATH = `${OPENCODE_STATE_DIR}/applied.sha256`;
/** Una ejecución a la vez por sandbox en la fase 1. */
export const AGENT_RUN_LOCK_PATH = `${AGENT_STATE_DIR}/run.lock`;
/** El servidor residente sólo escucha en loopback. */
export const OPENCODE_SERVE_URL = `http://127.0.0.1:${OPENCODE_SERVE_PORT}`;
/** Usuario fijo de la autenticación básica de `opencode serve`. */
export const OPENCODE_SERVE_USER = "opencode";
/** `$schema` de la documentación de OpenCode (opencode.ai/docs/config). */
export const OPENCODE_CONFIG_SCHEMA = "https://opencode.ai/config.json";
/** El agente principal de OpenCode. */
export const OPENCODE_PRIMARY_AGENT = "build";
/** Proveedor propio para `"openai-compatible"` (paquete empaquetado en OpenCode). */
export const OPENAI_COMPATIBLE_PROVIDER_ID = "rayito-openai";
export const OPENAI_COMPATIBLE_NPM = "@ai-sdk/openai-compatible";
/** `AgentModel.provider` -> id de proveedor de OpenCode. */
export const OPENCODE_PROVIDER_IDS: Readonly<Record<string, string>> = Object.freeze({
  bedrock: "amazon-bedrock",
  anthropic: "anthropic",
  "openai-compatible": OPENAI_COMPATIBLE_PROVIDER_ID,
});
/** Prefijo de la Messages API que el SDK de Anthropic añade a `baseURL`. */
export const ANTHROPIC_BASE_PATH = "/v1";
/** Las cinco variables de la plantilla más `OPENCODE_DISABLE_CLAUDE_CODE`. */
export const OPENCODE_FLAG_ENVS: Readonly<Record<string, string>> = Object.freeze({
  OPENCODE_DISABLE_AUTOUPDATE: "1",
  OPENCODE_DISABLE_MODELS_FETCH: "1",
  OPENCODE_DISABLE_LSP_DOWNLOAD: "1",
  OPENCODE_DISABLE_DEFAULT_PLUGINS: "1",
  OPENCODE_PURE: "1",
  OPENCODE_DISABLE_CLAUDE_CODE: "1",
});
const SESSION_ID_PATTERN = /^[A-Za-z0-9_-]{1,128}$/;
const HEALTH_TIMEOUT_SECONDS = 1;
const CONTROL_TIMEOUT_SECONDS = 5;
const SERVE_SECRET_BYTES = 32;
const UTF8 = new TextEncoder();
const UTF8_DECODER = new TextDecoder();

/** Lo que el adaptador acumula de una ejecución. */
export interface OpenCodeState extends RuntimeState {
  sessionId: string | undefined;
  steps: number;
  usage: TokenUsage;
  attached: boolean;
  failed: AgentFailed | undefined;
  ignoredLines: number;
}

/** Comillas simples de POSIX, siempre, igual que `shell_quote` en Python. */
export function shellQuote(value: string): string {
  return `'${value.replaceAll("'", `'"'"'`)}'`;
}

function sortKeys(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map(sortKeys);
  }
  if (typeof value === "object" && value !== null) {
    const out: Record<string, unknown> = {};
    for (const key of Object.keys(value).sort()) {
      out[key] = sortKeys((value as Record<string, unknown>)[key]);
    }
    return out;
  }
  return value;
}

/** JSON con claves ordenadas, sangría de 2 y salto final (`canonical_json`). */
export function canonicalJson(value: unknown): Uint8Array {
  return UTF8.encode(`${JSON.stringify(sortKeys(value), null, 2)}\n`);
}

/** sha256 de `ruta NUL contenido NUL` de cada fichero, en orden. */
export function filesSha256(files: readonly RuntimeFile[]): string {
  const hash = createHash("sha256");
  for (const file of files) {
    hash.update(UTF8.encode(file.path));
    hash.update(Uint8Array.of(0));
    hash.update(file.data);
    hash.update(Uint8Array.of(0));
  }
  return hash.digest("hex");
}

/** `encodeURIComponent` más `!'()*`, como `urllib.parse.quote(safe="")`. */
function quoteUrlComponent(value: string): string {
  return encodeURIComponent(value).replace(
    /[!'()*]/g,
    (c) => `%${c.charCodeAt(0).toString(16).toUpperCase()}`,
  );
}

function joinUrl(base: string, path: string): string {
  return base.replace(/\/+$/, "") + path;
}

function modelRef(providerId: string, modelId: string): string {
  return `${providerId}/${modelId}`;
}

function providerIdOf(spec: AgentSpec): string {
  return OPENCODE_PROVIDER_IDS[spec.model.provider] ?? spec.model.provider;
}

function permission(permissions: AgentPermissions): Record<string, unknown> {
  const rules: Record<string, unknown> = { "*": permissions.default };
  for (const [tool, rule] of Object.entries(permissions.effectiveTools())) {
    rules[tool] = typeof rule === "string" ? rule : { ...rule };
  }
  return rules;
}

function provider(spec: AgentSpec, gatewayUrl: string): Record<string, unknown> {
  const model = spec.model;
  if (model.provider === "bedrock") {
    return { options: { region: model.region, endpoint: gatewayUrl } };
  }
  if (model.provider === "anthropic") {
    return {
      options: {
        baseURL: joinUrl(gatewayUrl, ANTHROPIC_BASE_PATH),
        apiKey: MODEL_CREDENTIAL_PLACEHOLDER,
      },
    };
  }
  const ids = new Set([model.id, spec.effectiveSmallModel]);
  for (const sub of Object.values(spec.agents)) {
    if (sub.model !== undefined) {
      ids.add(sub.model);
    }
  }
  return {
    npm: OPENAI_COMPATIBLE_NPM,
    options: { baseURL: joinUrl(gatewayUrl, model.basePath), apiKey: MODEL_CREDENTIAL_PLACEHOLDER },
    models: Object.fromEntries([...ids].sort().map((id) => [id, {}])),
  };
}

function mcp(
  spec: AgentSpec,
  gatewayUrls: Readonly<Record<string, string>>,
): Record<string, unknown> {
  const servers: Record<string, unknown> = {};
  for (const [name, server] of Object.entries(spec.mcp)) {
    if (server instanceof McpLocal) {
      const entry: Record<string, unknown> = {
        type: "local",
        command: [...server.command],
        enabled: true,
      };
      if (Object.keys(server.envs).length > 0) {
        entry.environment = { ...server.envs };
      }
      if (server.timeoutMs !== undefined) {
        entry.timeout = Math.round(server.timeoutMs);
      }
      servers[name] = entry;
    } else {
      servers[name] = {
        type: "remote",
        url: joinUrl(gatewayUrls[server.gateway] ?? "", server.path),
        enabled: true,
      };
    }
  }
  return servers;
}

function agents(spec: AgentSpec, providerId: string): Record<string, unknown> {
  const out: Record<string, unknown> = {
    [OPENCODE_PRIMARY_AGENT]: { permission: permission(spec.permissions) },
  };
  for (const [name, sub] of Object.entries(spec.agents)) {
    const entry: Record<string, unknown> = {
      mode: "subagent",
      description: sub.description,
      prompt: sub.instructions,
    };
    if (sub.model !== undefined) {
      entry.model = modelRef(providerId, sub.model);
    }
    if (sub.permissions !== undefined) {
      entry.permission = permission(sub.permissions);
    }
    out[name] = entry;
  }
  return out;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function deepMerge(
  base: Record<string, unknown>,
  extra: Readonly<Record<string, unknown>>,
): Record<string, unknown> {
  const merged: Record<string, unknown> = { ...base };
  for (const [key, value] of Object.entries(extra)) {
    const current = merged[key];
    merged[key] =
      isPlainObject(current) && isPlainObject(value) ? deepMerge(current, value) : value;
  }
  return merged;
}

/** El `opencode.json` de `spec` como objeto (design.md §3). */
export function buildOpenCodeConfig(
  spec: AgentSpec,
  options: { readonly gatewayUrls: Readonly<Record<string, string>> },
): Record<string, unknown> {
  const gatewayUrls = options.gatewayUrls;
  spec.requireGateways(Object.keys(gatewayUrls));
  const providerId = providerIdOf(spec);
  let config: Record<string, unknown> = {
    // biome-ignore lint/style/useNamingConvention: clave fija del esquema de OpenCode.
    $schema: OPENCODE_CONFIG_SCHEMA,
    model: modelRef(providerId, spec.model.id),
    small_model: modelRef(providerId, spec.effectiveSmallModel),
    autoupdate: false,
    share: "disabled",
    snapshot: false,
    enabled_providers: [providerId],
    provider: { [providerId]: provider(spec, gatewayUrls[spec.model.gateway] ?? "") },
    agent: agents(spec, providerId),
  };
  if (spec.instructions !== undefined) {
    config.instructions = [OPENCODE_INSTRUCTIONS_PATH];
  }
  if (Object.keys(spec.mcp).length > 0) {
    config.mcp = mcp(spec, gatewayUrls);
  }
  if (spec.rawConfig !== undefined && Object.keys(spec.rawConfig).length > 0) {
    config = deepMerge(config, spec.rawConfig);
  }
  return config;
}

function nonNegative(value: unknown): number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : 0;
}

function usageOf(tokens: unknown): TokenUsage {
  if (!isPlainObject(tokens)) {
    return new TokenUsage();
  }
  const cache = isPlainObject(tokens.cache) ? tokens.cache : {};
  return new TokenUsage({
    input: nonNegative(tokens.input),
    output: nonNegative(tokens.output),
    reasoning: nonNegative(tokens.reasoning),
    cacheRead: nonNegative(cache.read),
    cacheWrite: nonNegative(cache.write),
  });
}

function toolCallOf(part: Record<string, unknown>): ToolCall | undefined {
  const toolState = part.state;
  if (!isPlainObject(toolState)) {
    return undefined;
  }
  const status = toolState.status;
  if (status !== "completed" && status !== "error") {
    return undefined;
  }
  const rawOutput = status === "completed" ? toolState.output : toolState.error;
  const truncated = typeof rawOutput === "string" ? truncateToolOutput(rawOutput) : undefined;
  return Object.freeze({
    type: "tool_call",
    callId: String(part.callID ?? ""),
    name: String(part.tool ?? ""),
    status,
    input: isPlainObject(toolState.input) ? { ...toolState.input } : undefined,
    output: truncated?.text,
    outputTruncated: truncated?.truncated ?? false,
  });
}

function attachMode(attach: unknown): string {
  if (attach === true) {
    return "always";
  }
  if (attach === false) {
    return "never";
  }
  if (attach === "auto" || attach === undefined) {
    return "auto";
  }
  throw new InvalidArgumentError("attach debe ser true, false o 'auto'");
}

function runEnvs(spec: AgentSpec): Record<string, string> {
  const envs: Record<string, string> = {
    HOME: DEFAULT_AGENT_WORKDIR,
    OPENCODE_CONFIG: OPENCODE_CONFIG_PATH,
    ...OPENCODE_FLAG_ENVS,
  };
  if (spec.model.provider === "bedrock") {
    envs.AWS_BEARER_TOKEN_BEDROCK = MODEL_CREDENTIAL_PLACEHOLDER;
  }
  if (spec.model.region !== undefined) {
    envs.AWS_REGION = spec.model.region;
  }
  return envs;
}

function protocolLine(kind: string): string {
  return `printf '%s\\n' '{"type":"rayito.${kind}"}'`;
}

function runScript(attach: string, command: string, workdir: string): string {
  const auth = `"${OPENCODE_SERVE_USER}:$OPENCODE_SERVER_PASSWORD"`;
  const dispose = `${OPENCODE_SERVE_URL}/instance/dispose?directory=${quoteUrlComponent(workdir)}`;
  const lines = [
    "set -u",
    `mkdir -p ${shellQuote(OPENCODE_STATE_DIR)}`,
    `exec 9>${shellQuote(AGENT_RUN_LOCK_PATH)}`,
    `if ! flock -n 9; then ${protocolLine("busy")}; exit 0; fi`,
    `if ! command -v opencode >/dev/null 2>&1; then ${protocolLine("runtime_missing")}; exit 0; fi`,
    `attach=${attach}`,
    "attached=0",
    `secret=${shellQuote(OPENCODE_SERVE_SECRET_PATH)}`,
    'if [ "$attach" != never ] && [ -r "$secret" ]; then',
    '  OPENCODE_SERVER_PASSWORD="$(cat "$secret")"',
    "  export OPENCODE_SERVER_PASSWORD",
    `  if curl -fsS -m ${HEALTH_TIMEOUT_SECONDS} -u ${auth} ${shellQuote(`${OPENCODE_SERVE_URL}/global/health`)} >/dev/null 2>&1; then`,
    "    attached=1",
    "  fi",
    "fi",
    'if [ "$attach" = always ] && [ "$attached" = 0 ]; then',
    `  ${protocolLine("runtime_missing")}`,
    "  exit 0",
    "fi",
    `set -- ${command}`,
    'if [ "$attached" = 1 ]; then',
    '  sha="$(sha256sum "$OPENCODE_CONFIG" | cut -d " " -f 1)"',
    `  if [ "$sha" != "$(cat ${shellQuote(OPENCODE_APPLIED_SHA_PATH)} 2>/dev/null)" ]; then`,
    `    curl -fsS -m ${CONTROL_TIMEOUT_SECONDS} -u ${auth} -X POST ${shellQuote(dispose)} >/dev/null 2>&1 || true`,
    `    printf '%s\\n' "$sha" > ${shellQuote(OPENCODE_APPLIED_SHA_PATH)}`,
    "  fi",
    `  ${protocolLine("attached")}`,
    `  set -- "$@" --attach ${shellQuote(OPENCODE_SERVE_URL)}`,
    "fi",
    'exec "$@"',
  ];
  return `${lines.join("\n")}\n`;
}

function serveScript(): string {
  const secret = shellQuote(OPENCODE_SERVE_SECRET_PATH);
  const envs = Object.entries(OPENCODE_FLAG_ENVS)
    .map(([name, value]) => `${name}=${value}`)
    .join(" ");
  const lines = [
    "set -u",
    `mkdir -p ${shellQuote(OPENCODE_STATE_DIR)}`,
    `umask 077 && head -c ${SERVE_SECRET_BYTES} /dev/urandom | base64 > ${secret}`,
    `OPENCODE_SERVER_PASSWORD="$(cat ${secret})" OPENCODE_CONFIG=${shellQuote(OPENCODE_CONFIG_PATH)} ${envs} AWS_BEARER_TOKEN_BEDROCK=${MODEL_CREDENTIAL_PLACEHOLDER} exec opencode serve --hostname 127.0.0.1 --port ${OPENCODE_SERVE_PORT}`,
  ];
  return `${lines.join("\n")}\n`;
}

function asState(state: RuntimeState): OpenCodeState {
  return state as OpenCodeState;
}

function failed(
  reason: AgentFailureReason,
  state: OpenCodeState,
  exitCode?: number,
  detailCode?: unknown,
): AgentFailed {
  return agentFailed(reason, {
    sessionId: state.sessionId,
    exitCode,
    detailCode: typeof detailCode === "string" ? detailCode : undefined,
  });
}

/** `AgentRuntime` de OpenCode; lo de cada ejecución vive en su `OpenCodeState`. */
export class OpenCodeRuntime implements AgentRuntime {
  readonly name = "opencode";

  /** `opencode.json` y, si hay `instructions`, su `AGENTS.md`. */
  buildConfig(
    spec: AgentSpec,
    options: { readonly gatewayUrls: Readonly<Record<string, string>>; readonly workdir: string },
  ): RuntimeFiles {
    const files: RuntimeFile[] = [
      {
        path: OPENCODE_CONFIG_PATH,
        data: canonicalJson(buildOpenCodeConfig(spec, { gatewayUrls: options.gatewayUrls })),
        mode: 0o644,
      },
    ];
    if (spec.instructions !== undefined) {
      files.push({
        path: OPENCODE_INSTRUCTIONS_PATH,
        data: UTF8.encode(spec.instructions),
        mode: 0o644,
      });
    }
    return { files, configSha256: filesSha256(files) };
  }

  /** El script de una ejecución: cerrojo, `attach`, `dispose`, `exec opencode run`. */
  command(request: RunRequest): RunCommand {
    const mode = attachMode(request.attach);
    const providerId = providerIdOf(request.spec);
    const args = [
      "opencode",
      "run",
      "--format",
      "json",
      "--auto",
      "--title",
      OPENCODE_SESSION_TITLE,
      "--dir",
      request.workdir,
    ];
    if (request.model !== undefined) {
      args.push("-m", modelRef(providerId, request.model));
    }
    if (request.sessionId !== undefined) {
      args.push("-s", request.sessionId);
    }
    if (request.reasoning === true) {
      args.push("--thinking");
    }
    return {
      script: runScript(mode, args.map(shellQuote).join(" "), request.workdir),
      envs: runEnvs(request.spec),
      stdin: UTF8.encode(request.prompt),
    };
  }

  newState(): RuntimeState {
    const state: OpenCodeState = {
      sessionId: undefined,
      steps: 0,
      usage: new TokenUsage(),
      attached: false,
      failed: undefined,
      ignoredLines: 0,
    };
    return state;
  }

  /** Una línea JSONL a cero o más eventos; lo ilegible o desconocido se
   * ignora y se cuenta en `ignoredLines`. */
  parseLine(line: Uint8Array, rawState: RuntimeState): readonly AgentEvent[] {
    const state = asState(rawState);
    let payload: unknown;
    try {
      payload = JSON.parse(UTF8_DECODER.decode(line));
    } catch {
      state.ignoredLines += 1;
      return [];
    }
    if (!isPlainObject(payload)) {
      state.ignoredLines += 1;
      return [];
    }
    const sessionId = payload.sessionID;
    if (typeof sessionId === "string" && sessionId !== "") {
      state.sessionId = sessionId;
    }
    const events = this.map(payload, state);
    if (events === undefined) {
      state.ignoredLines += 1;
      return [];
    }
    return events;
  }

  private map(payload: Record<string, unknown>, state: OpenCodeState): AgentEvent[] | undefined {
    const kind = payload.type;
    const part = isPlainObject(payload.part) ? payload.part : {};
    switch (kind) {
      case "rayito.attached":
        state.attached = true;
        return [];
      case "rayito.busy":
        state.failed = failed("busy", state);
        return [state.failed];
      case "rayito.runtime_missing":
        state.failed = failed("runtime_missing", state);
        return [state.failed];
      case "step_start":
        state.steps += 1;
        return [Object.freeze({ type: "step_started", index: state.steps })];
      case "step_finish": {
        const usage = usageOf(part.tokens);
        state.usage = state.usage.plus(usage);
        return [
          Object.freeze({
            type: "step_finished",
            index: state.steps,
            usage,
            finishReason: typeof part.reason === "string" ? part.reason : undefined,
          }),
        ];
      }
      case "text":
      case "reasoning": {
        const text = part.text;
        if (typeof text !== "string") {
          return undefined;
        }
        return [Object.freeze({ type: kind, text })];
      }
      case "tool_use": {
        const call = toolCallOf(part);
        return call === undefined ? undefined : [call];
      }
      case "error": {
        const error = payload.error;
        state.failed = failed(
          "model_error",
          state,
          undefined,
          isPlainObject(error) ? error.name : undefined,
        );
        return [state.failed];
      }
      default:
        return undefined;
    }
  }

  /** `Done` sólo con salida 0, ningún `error` y una sesión vista. */
  finish(rawState: RuntimeState, exitCode: number): Done | AgentFailed {
    const state = asState(rawState);
    if (state.failed !== undefined) {
      return state.failed;
    }
    if (exitCode !== 0) {
      return failed("runtime_error", state, exitCode);
    }
    if (state.sessionId === undefined) {
      return failed("protocol_error", state, exitCode);
    }
    return Object.freeze({
      type: "done",
      sessionId: state.sessionId,
      exitCode,
      usage: state.usage,
    });
  }

  /** Con `--attach`, `POST /session/<id>/abort` antes de matar el proceso. */
  abortCommand(rawState: RuntimeState): string | undefined {
    const state = asState(rawState);
    if (
      !state.attached ||
      state.sessionId === undefined ||
      !SESSION_ID_PATTERN.test(state.sessionId)
    ) {
      return undefined;
    }
    return (
      `curl -fsS -m ${CONTROL_TIMEOUT_SECONDS} ` +
      `-u "${OPENCODE_SERVE_USER}:$(cat ${shellQuote(OPENCODE_SERVE_SECRET_PATH)})" ` +
      `-X POST ${shellQuote(`${OPENCODE_SERVE_URL}/session/${state.sessionId}/abort`)}` +
      " >/dev/null 2>&1 || true"
    );
  }

  /** Vacío: la plantilla de agente declara sus propios pasos. */
  templateSteps(): readonly TemplateStep[] {
    return [];
  }

  /** Sin `serve`, sólo carga el binario; con `serve`, además arranca
   * `opencode serve` residente en loopback con una contraseña aleatoria. */
  warmupSteps(options: { readonly serve: boolean }): readonly WarmupStep[] {
    const steps: WarmupStep[] = [{ cmd: "opencode --version >/dev/null" }];
    if (options.serve) {
      steps.push({ cmd: serveScript(), background: true, tag: "rayito-agent-serve" });
    }
    return steps;
  }
}
