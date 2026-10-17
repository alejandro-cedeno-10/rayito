/**
 * Adaptador de OpenCode del puerto `AgentRuntime` (`ai-agent-core`,
 * ADR-025, design.md §3). Espejo de `rayito._agent._opencode`: construye el
 * `opencode.json`, el script de bash de una ejecución y traduce el JSONL de
 * `opencode run --format json` (v1.18.34) a eventos de Rayito. El script, la
 * configuración y su sha256 son idénticos byte a byte a los de Python
 * (`testdata/agent/`).
 *
 * El prompt va por stdin, nunca en argv. `finish()` sólo da `Done` con
 * salida 0, ningún `error` visto y una sesión conocida.
 *
 * Las líneas propias del script llevan `type` con prefijo `rayito.`
 * (`busy` y `runtime_missing`).
 */

import { createHash } from "node:crypto";
import { InvalidArgumentError } from "../errors.js";
import {
  AGENT_OPENCODE_SHA256,
  AGENT_OPENCODE_VERSION,
  AGENT_RIPGREP_SHA256,
  AGENT_RIPGREP_VERSION,
  AGENT_STATE_DIR,
  DEFAULT_AGENT_WORKDIR,
  MODEL_CREDENTIAL_PLACEHOLDER,
  OPENCODE_SESSION_TITLE,
} from "../limits.js";
import { type AgentPermissions, type AgentSpec, McpLocal, McpRemote } from "./domain.js";
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
import { AGENT_BIN_DIR } from "./recipes.js";
import {
  type AgentRuntime,
  containerPackagesStep,
  DEFAULT_INSTALL_PYTHON,
  DOWNLOAD_PACKAGES,
  type HostConfig,
  hostConfig,
  type InstallableRuntime,
  type InstallTarget,
  type ModelWiring,
  type RunCommand,
  type RunRequest,
  type RuntimeFile,
  type RuntimeFiles,
  type RuntimeState,
  runStep,
  type ServableRuntime,
  type TemplateStep,
  type WarmupStep,
  wiringUrls,
} from "./runtime.js";

/** Directorio de OpenCode dentro de `AGENT_STATE_DIR`. */
export const OPENCODE_STATE_DIR = `${AGENT_STATE_DIR}/opencode`;
/** El `OPENCODE_CONFIG` de cada ejecución. */
export const OPENCODE_CONFIG_PATH = `${OPENCODE_STATE_DIR}/opencode.json`;
/** Las instrucciones de `AgentSpec.instructions`. */
export const OPENCODE_INSTRUCTIONS_PATH = `${OPENCODE_STATE_DIR}/AGENTS.md`;
/** Una ejecución a la vez por sandbox en la fase 1. */
export const AGENT_RUN_LOCK_PATH = `${AGENT_STATE_DIR}/run.lock`;
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
  openai: "openai",
  google: "google",
  azure: "azure",
});
/**
 * Lo que cada paquete nativo de OpenCode espera en `baseURL` delante de su
 * ruta: `@ai-sdk/openai` y `@ai-sdk/azure` añaden `/responses` y
 * `@ai-sdk/google` `/models/<m>:streamGenerateContent`. Con `baseURL`
 * hacia la pasarela, `@ai-sdk/azure` no necesita `resourceName` ni añade
 * `api-version` (medido con el binario fijado en
 * docs/research/2026-10-provider-runtimes-spike.md).
 */
export const OPENCODE_NATIVE_BASE_PATHS: Readonly<Record<string, string>> = Object.freeze({
  openai: "/v1",
  google: "/v1beta",
  azure: "/openai/v1",
});
/**
 * El paquete del AI SDK de cada proveedor nativo, explícito para no
 * depender del catálogo de models.dev (`OPENCODE_DISABLE_MODELS_FETCH`):
 * los tres van empaquetados en el binario de OpenCode fijado.
 */
export const OPENCODE_NATIVE_NPM: Readonly<Record<string, string>> = Object.freeze({
  openai: "@ai-sdk/openai",
  google: "@ai-sdk/google",
  azure: "@ai-sdk/azure",
});
/** Prefijo de la Messages API que el SDK de Anthropic añade a `baseURL`. */
export const ANTHROPIC_BASE_PATH = "/v1";
/**
 * Las cinco variables de la plantilla más `OPENCODE_DISABLE_CLAUDE_CODE` y
 * `OPENCODE_EXPERIMENTAL_WEBSOCKETS` apagado de forma explícita: con él, el
 * proveedor `openai` abriría un WebSocket que la pasarela no reenvía.
 */
export const OPENCODE_FLAG_ENVS: Readonly<Record<string, string>> = Object.freeze({
  OPENCODE_DISABLE_AUTOUPDATE: "1",
  OPENCODE_DISABLE_MODELS_FETCH: "1",
  OPENCODE_DISABLE_LSP_DOWNLOAD: "1",
  OPENCODE_DISABLE_DEFAULT_PLUGINS: "1",
  OPENCODE_PURE: "1",
  OPENCODE_DISABLE_CLAUDE_CODE: "1",
  OPENCODE_EXPERIMENTAL_WEBSOCKETS: "0",
});
const SESSION_ID_PATTERN = /^[A-Za-z0-9_-]{1,128}$/;
export const OPENCODE_BINARY_PATH = `${AGENT_BIN_DIR}/opencode`;
export const RIPGREP_BINARY_PATH = `${AGENT_BIN_DIR}/rg`;
/** Asset de la release de OpenCode (linux arm64 glibc, binario único). */
export const OPENCODE_RELEASE_URL =
  "https://github.com/anomalyco/opencode/releases/download/" +
  `v${AGENT_OPENCODE_VERSION}/opencode-linux-arm64.tar.gz`;
export const RIPGREP_RELEASE_DIR = `ripgrep-${AGENT_RIPGREP_VERSION}-aarch64-unknown-linux-gnu`;
export const RIPGREP_RELEASE_URL =
  "https://github.com/BurntSushi/ripgrep/releases/download/" +
  `${AGENT_RIPGREP_VERSION}/${RIPGREP_RELEASE_DIR}.tar.gz`;
const UTF8 = new TextEncoder();
const UTF8_DECODER = new TextDecoder();

/** Lo que el adaptador acumula de una ejecución. */
export interface OpenCodeState extends RuntimeState {
  sessionId: string | undefined;
  steps: number;
  usage: TokenUsage;
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

/** `baseURL` y la clave de marcador sólo con pasarela; sin ella (`baseUrl`
 * ausente) OpenCode usa el endpoint y la credencial estándar del proveedor. */
function endpointOptions(baseUrl: string | undefined): Record<string, unknown> {
  if (baseUrl === undefined) {
    return {};
  }
  return { baseURL: baseUrl, apiKey: MODEL_CREDENTIAL_PLACEHOLDER };
}

function provider(spec: AgentSpec, gatewayUrl: string | undefined): Record<string, unknown> {
  const model = spec.model;
  if (model.provider === "bedrock") {
    const options: Record<string, unknown> = { region: model.region };
    if (gatewayUrl !== undefined) {
      options.endpoint = gatewayUrl;
    }
    return { options };
  }
  if (model.provider === "anthropic") {
    return {
      options: endpointOptions(
        gatewayUrl === undefined ? undefined : joinUrl(gatewayUrl, ANTHROPIC_BASE_PATH),
      ),
    };
  }
  const nativeBasePath = OPENCODE_NATIVE_BASE_PATHS[model.provider];
  if (nativeBasePath !== undefined) {
    return {
      npm: OPENCODE_NATIVE_NPM[model.provider],
      options: endpointOptions(
        gatewayUrl === undefined ? undefined : joinUrl(gatewayUrl, nativeBasePath),
      ),
      models: models(spec),
    };
  }
  if (gatewayUrl === undefined) {
    throw new InvalidArgumentError(
      `el proveedor ${JSON.stringify(model.provider)} no tiene un endpoint estándar: ` +
        "sin pasarela necesita una URL explícita",
    );
  }
  return {
    npm: OPENAI_COMPATIBLE_NPM,
    options: endpointOptions(joinUrl(gatewayUrl, model.basePath)),
    models: models(spec),
  };
}

/** Los modelos que usa `spec`: sin descargar el catálogo, OpenCode sólo
 * conoce los que se declaran. */
function models(spec: AgentSpec): Record<string, unknown> {
  const ids = new Set([spec.model.id, spec.effectiveSmallModel]);
  for (const sub of Object.values(spec.agents)) {
    if (sub.model !== undefined) {
      ids.add(sub.model);
    }
  }
  return Object.fromEntries([...ids].sort().map((id) => [id, {}]));
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

function requireNoRemoteMcp(spec: AgentSpec): void {
  if (Object.values(spec.mcp).some((server) => server instanceof McpRemote)) {
    throw new InvalidArgumentError(
      "AgentSpec.mcp con McpRemote necesita una pasarela: no existe sin ella",
    );
  }
}

/**
 * El `opencode.json` de `spec` como objeto (design.md §3). `gatewayUrls`
 * ausente es la configuración sin pasarela de un contenedor exportado: el
 * proveedor usa su endpoint y credencial estándar, y un `McpRemote` (que sólo
 * existe tras una pasarela) falla.
 */
export function buildOpenCodeConfig(
  spec: AgentSpec,
  options: { readonly gatewayUrls: Readonly<Record<string, string>> | undefined },
): Record<string, unknown> {
  const gatewayUrls = options.gatewayUrls;
  let gatewayUrl: string | undefined;
  if (gatewayUrls === undefined) {
    requireNoRemoteMcp(spec);
  } else {
    spec.requireGateways(Object.keys(gatewayUrls));
    gatewayUrl = gatewayUrls[spec.model.gateway] ?? "";
  }
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
    provider: { [providerId]: provider(spec, gatewayUrl) },
    agent: agents(spec, providerId),
  };
  if (spec.instructions !== undefined) {
    config.instructions = [OPENCODE_INSTRUCTIONS_PATH];
  }
  if (Object.keys(spec.mcp).length > 0) {
    config.mcp = mcp(spec, gatewayUrls ?? {});
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

function runEnvs(spec: AgentSpec, direct = false): Record<string, string> {
  const envs: Record<string, string> = {
    HOME: DEFAULT_AGENT_WORKDIR,
    OPENCODE_CONFIG: OPENCODE_CONFIG_PATH,
    ...OPENCODE_FLAG_ENVS,
  };
  if (spec.model.provider === "bedrock" && !direct) {
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

function runScript(command: string): string {
  const lines = [
    "set -u",
    `mkdir -p ${shellQuote(OPENCODE_STATE_DIR)}`,
    `exec 9>${shellQuote(AGENT_RUN_LOCK_PATH)}`,
    `if ! flock -n 9; then ${protocolLine("busy")}; exit 0; fi`,
    `if ! command -v opencode >/dev/null 2>&1; then ${protocolLine("runtime_missing")}; exit 0; fi`,
    `exec ${command}`,
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
export class OpenCodeRuntime implements AgentRuntime, InstallableRuntime, ServableRuntime {
  readonly name = "opencode";

  /** `opencode.json` y, si hay `instructions`, su `AGENTS.md`. */
  buildConfig(
    spec: AgentSpec,
    options: { readonly gatewayUrls: Readonly<Record<string, string>>; readonly workdir: string },
  ): RuntimeFiles {
    return configFiles(spec, options.gatewayUrls);
  }

  /** El script de una ejecución: cerrojo, comprobación del binario y `exec opencode run`. */
  command(request: RunRequest): RunCommand {
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
      if (!SESSION_ID_PATTERN.test(request.sessionId)) {
        throw new InvalidArgumentError("sessionId no es un id de sesión de OpenCode");
      }
      args.push("-s", request.sessionId);
    }
    if (request.reasoning === true) {
      args.push("--thinking");
    }
    return {
      script: runScript(args.map(shellQuote).join(" ")),
      envs: runEnvs(request.spec),
      stdin: UTF8.encode(request.prompt),
    };
  }

  newState(): RuntimeState {
    const state: OpenCodeState = {
      sessionId: undefined,
      steps: 0,
      usage: new TokenUsage(),
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
    const partEvents = this.mapPart(kind, part, state);
    if (partEvents !== undefined) {
      return partEvents;
    }
    switch (kind) {
      case "rayito.busy":
        state.failed = failed("busy", state);
        return [state.failed];
      case "rayito.runtime_missing":
        state.failed = failed("runtime_missing", state);
        return [state.failed];
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

  private mapPart(
    kind: unknown,
    part: Record<string, unknown>,
    state: OpenCodeState,
  ): AgentEvent[] | undefined {
    switch (kind) {
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
      usageReported: true,
    });
  }

  /** Vacío: la plantilla de agente declara sus propios pasos. */
  templateSteps(): readonly TemplateStep[] {
    return [];
  }

  /** Carga el binario en la caché de páginas. */
  warmupSteps(): readonly WarmupStep[] {
    return [{ cmd: "opencode --version >/dev/null" }];
  }

  /** OpenCode y ripgrep desde sus assets de release, comprobados con
   * `sha256sum -c` contra los pines de `limits.json`. */
  installSteps(target: InstallTarget): readonly TemplateStep[] {
    return [...containerPackagesStep(target, DOWNLOAD_PACKAGES), runStep(opencodeInstall(target))];
  }

  /** Nada: todo se descarga y se comprueba en el build. */
  contextFiles(): Readonly<Record<string, string>> {
    return {};
  }

  /** Versión y sha256 de OpenCode para el manifiesto de la imagen. */
  manifestEntry(): Readonly<Record<string, unknown>> {
    return { version: AGENT_OPENCODE_VERSION, sha256: AGENT_OPENCODE_SHA256 };
  }

  /** Prueba de humo, como `user`, de los dos binarios. */
  smokeChecks(): readonly string[] {
    return ["su user -c 'opencode --version'", "su user -c 'rg --version'"];
  }

  /** Los binarios que el demonio de precarga lee tras cada restauración. */
  prefetchPaths(): readonly string[] {
    return [OPENCODE_BINARY_PATH, RIPGREP_BINARY_PATH];
  }

  /** La configuración del host para el driver `opencode`: los mismos ficheros
   * y variables que `buildConfig()` y `command()`. */
  hostConfig(
    spec: AgentSpec,
    options: { readonly wiring: ModelWiring; readonly workdir: string },
  ): HostConfig {
    return hostConfig({
      driver: "opencode",
      python: DEFAULT_INSTALL_PYTHON,
      files: configFiles(spec, wiringUrls(options.wiring)),
      envs: runEnvs(spec, options.wiring.kind === "direct"),
      options: { workdir: options.workdir },
    });
  }
}

function configFiles(
  spec: AgentSpec,
  gatewayUrls: Readonly<Record<string, string>> | undefined,
): RuntimeFiles {
  const files: RuntimeFile[] = [
    {
      path: OPENCODE_CONFIG_PATH,
      data: canonicalJson(buildOpenCodeConfig(spec, { gatewayUrls })),
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

function opencodeInstall(target: InstallTarget): string {
  const binDir = `${target.installDir}/bin`;
  const opencode = `${binDir}/opencode`;
  const ripgrep = `${binDir}/rg`;
  return (
    `curl -fsSL --retry 3 -o /tmp/opencode.tar.gz ${OPENCODE_RELEASE_URL}` +
    ` && echo '${AGENT_OPENCODE_SHA256}  /tmp/opencode.tar.gz' | sha256sum -c -` +
    ` && curl -fsSL --retry 3 -o /tmp/ripgrep.tar.gz ${RIPGREP_RELEASE_URL}` +
    ` && echo '${AGENT_RIPGREP_SHA256}  /tmp/ripgrep.tar.gz' | sha256sum -c -` +
    ` && mkdir -p ${binDir}` +
    ` && tar -xzf /tmp/opencode.tar.gz -C ${binDir} opencode` +
    " && tar -xzf /tmp/ripgrep.tar.gz -C /tmp" +
    ` && mv /tmp/${RIPGREP_RELEASE_DIR}/rg ${ripgrep}` +
    ` && rm -rf /tmp/opencode.tar.gz /tmp/ripgrep.tar.gz /tmp/${RIPGREP_RELEASE_DIR}` +
    ` && chown -R root:root ${target.installDir}` +
    ` && chmod 0755 ${target.installDir} ${binDir} ${opencode}` +
    ` ${ripgrep}` +
    ` && ln -sf ${opencode} /usr/local/bin/opencode` +
    ` && ln -sf ${ripgrep} /usr/local/bin/rg`
  );
}
