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

import { createHash } from "node:crypto";
import { InvalidArgumentError } from "../errors.js";
import {
  AGENT_DEEPAGENTS_REQUIREMENTS_SHA256,
  AGENT_PROTOCOL_VERSION,
  AGENT_STATE_DIR,
  DEFAULT_AGENT_WORKDIR,
  MODEL_CREDENTIAL_PLACEHOLDER,
} from "../limits.js";
import { DEEPAGENTS_RUNNER_SOURCE } from "./assets/deepagents-runner.js";
import { DEEPAGENTS_REQUIREMENTS } from "./assets/template-assets.gen.js";
import {
  type AgentPermissions,
  type AgentSpec,
  DEFAULT_DENIED_TOOLS,
  validateModelId,
} from "./domain.js";
import type { AgentEvent, AgentFailed, Done } from "./events.js";
import {
  checkRequirementsPath,
  DEFAULT_PYTHON_INSTALLER,
  PYTHON_INSTALLERS,
  pythonDependencySteps,
  pythonManifest,
} from "./install-plan.js";
import { AGENT_RUN_LOCK_PATH, canonicalJson, filesSha256, shellQuote } from "./opencode.js";
import {
  finishV1,
  newProtocolV1State,
  type ProtocolV1State,
  parseV1Line,
  SESSION_ID_PATTERN,
} from "./protocol-v1.js";
import {
  AGENT_APP_DIR,
  AGENT_INSTALL_DIR,
  AGENT_RAYITO_DIR,
  hashedVenvInstall,
} from "./recipes.js";
import {
  type AgentRuntime,
  copyStep,
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

/** Estado de deepagents dentro de `AGENT_STATE_DIR`. */
export const DEEPAGENTS_STATE_DIR = `${AGENT_STATE_DIR}/deepagents`;
/** La configuración estática que lee el runner. */
export const DEEPAGENTS_CONFIG_PATH = `${DEEPAGENTS_STATE_DIR}/config.json`;
/** Historial de cada sesión, `<id>.json`. */
export const DEEPAGENTS_SESSIONS_DIR = `${DEEPAGENTS_STATE_DIR}/sessions`;
/** El venv de deepagents de la plantilla y su Python. */
export const DEEPAGENTS_VENV_DIR = `${AGENT_INSTALL_DIR}/deepagents`;
export const DEEPAGENTS_PYTHON = `${DEEPAGENTS_VENV_DIR}/bin/python`;
export const DEEPAGENTS_REQUIREMENTS_NAME = "requirements-deepagents.txt";
/** Nombre con el que se copian los ficheros de dependencias de tu proyecto y el
 * instalador por defecto (`pip`). */
export const DEEPAGENTS_USER_SLUG = "deepagents";
/** El runner en el contexto de build; se instala en `DEEPAGENTS_RUNNER_PATH`. */
export const DEEPAGENTS_RUNNER_NAME = "deepagents_runner.py";
/** Donde la plantilla instala el runner, de root y 0755. */
export const DEEPAGENTS_RUNNER_PATH = `${AGENT_RAYITO_DIR}/${DEEPAGENTS_RUNNER_NAME}`;
/**
 * Lo que el build importa como prueba de humo del venv: deepagents y las
 * clases de modelo de cada proveedor (`langchain_openai` para OpenAI, xAI,
 * Azure y las APIs compatibles; `langchain_google_genai` para Gemini).
 */
export const DEEPAGENTS_SMOKE_MODULES =
  "deepagents, langchain_aws, langchain_anthropic, langchain_openai, langchain_google_genai";
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
/** Lo que la clase de LangChain espera en `base_url` con los proveedores
 * nativos (`ChatOpenAI` y `AzureChatOpenAI` añaden `/responses`;
 * `ChatGoogleGenerativeAI`, su `/v1beta/models/...`). Con
 * `"openai-compatible"` se usa `AgentModel.basePath`. */
export const DEEPAGENTS_NATIVE_BASE_PATHS: Readonly<Record<string, string>> = Object.freeze({
  openai: "/v1",
  google: "",
  azure: "/openai/v1",
});

const ENTRYPOINT_PATTERN = /^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$/;
const UTF8 = new TextEncoder();

function checkUserRequirements(
  entrypoint: string | undefined,
  requirements: string | undefined,
  packageManager: string | undefined,
): void {
  if (requirements === undefined && packageManager === undefined) {
    return;
  }
  if (entrypoint === undefined) {
    throw new InvalidArgumentError(
      "DeepAgents.requirements y packageManager necesitan un entrypoint: " +
        "sin código tuyo no hay dependencias que instalar",
    );
  }
  if (
    packageManager !== undefined &&
    !(PYTHON_INSTALLERS as readonly string[]).includes(packageManager)
  ) {
    throw new InvalidArgumentError(
      `DeepAgents.packageManager debe ser uno de ${JSON.stringify(PYTHON_INSTALLERS)}`,
    );
  }
  if (requirements === undefined) {
    throw new InvalidArgumentError("DeepAgents.packageManager necesita requirements");
  }
  checkRequirementsPath("DeepAgents", requirements, packageManager);
}

/** Opciones de `new DeepAgents(...)`. */
export interface DeepAgentsOptions {
  /** `"pkg.mod:build"`: una función de Python, importable desde el
   * directorio de trabajo, que recibe un `RunnerContext` y devuelve un grafo
   * compilado. Sin ella, el runner usa `create_deep_agent`. */
  readonly entrypoint?: string | undefined;
  /** `true` es el runtime registrado como `"langgraph"`: sin `entrypoint` no
   * hay agente que construir y `buildConfig()` / `hostConfig()` fallan. */
  readonly requireEntrypoint?: boolean | undefined;
  /** Un fichero de dependencias de tu `source` (`app/<requirements>`:
   * `requirements.txt` o, con `packageManager: "uv"`, `pyproject.toml` o
   * `uv.lock`) que se instala en el venv sin quitar lo que ya hay. Necesita un
   * `entrypoint`. */
  readonly requirements?: string | undefined;
  /** `pip` (por defecto) o `uv`. */
  readonly packageManager?: string | undefined;
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

/**
 * La configuración que lee el runner, como objeto. `gatewayUrls` ausente es
 * la configuración sin pasarela de un contenedor exportado: `base_url` y
 * `credential_placeholder` van a `null` y el proveedor usa su endpoint y
 * credencial estándar.
 */
export function buildDeepAgentsConfig(
  spec: AgentSpec,
  options: {
    readonly gatewayUrls: Readonly<Record<string, string>> | undefined;
    readonly workdir: string;
    readonly entrypoint: string | undefined;
  },
): Record<string, unknown> {
  const baseUrl = baseUrlOf(spec, options.gatewayUrls);
  if (Object.keys(spec.mcp).length > 0) {
    throw new InvalidArgumentError("deepagents no admite AgentSpec.mcp; usa runtime 'opencode'");
  }
  if (spec.rawConfig !== undefined && Object.keys(spec.rawConfig).length > 0) {
    throw new InvalidArgumentError(
      "AgentSpec.rawConfig es configuración de OpenCode; deepagents no la admite",
    );
  }
  const model = spec.model;
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
    base_url: baseUrl ?? null,
    credential_placeholder: baseUrl === undefined ? null : MODEL_CREDENTIAL_PLACEHOLDER,
    prompt_caching: model.promptCaching,
    instructions: spec.instructions ?? null,
    workdir: options.workdir,
    app_dir: AGENT_APP_DIR,
    sessions_dir: DEEPAGENTS_SESSIONS_DIR,
    entrypoint: options.entrypoint ?? null,
    permissions: toolRules(spec.permissions, "AgentSpec.permissions"),
    subagents,
  };
}

function baseUrlOf(
  spec: AgentSpec,
  gatewayUrls: Readonly<Record<string, string>> | undefined,
): string | undefined {
  const model = spec.model;
  if (gatewayUrls === undefined) {
    if (
      !(model.provider in DEEPAGENTS_NATIVE_BASE_PATHS) &&
      !["bedrock", "anthropic"].includes(model.provider)
    ) {
      throw new InvalidArgumentError(
        `el proveedor ${JSON.stringify(model.provider)} no tiene un endpoint estándar: ` +
          "sin pasarela necesita una URL explícita",
      );
    }
    return undefined;
  }
  spec.requireGateways(Object.keys(gatewayUrls));
  const basePath = DEEPAGENTS_NATIVE_BASE_PATHS[model.provider] ?? model.basePath;
  return stripTrailingSlashes(gatewayUrls[model.gateway] as string) + basePath;
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
export class DeepAgents implements AgentRuntime, InstallableRuntime, ServableRuntime {
  readonly name = "deepagents";
  readonly entrypoint: string | undefined;
  readonly requireEntrypoint: boolean;
  readonly requirements: string | undefined;
  readonly packageManager: string | undefined;

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
    checkUserRequirements(entrypoint, options.requirements, options.packageManager);
    this.entrypoint = entrypoint;
    this.requireEntrypoint = options.requireEntrypoint ?? false;
    this.requirements = options.requirements;
    this.packageManager = options.packageManager;
  }

  buildConfig(
    spec: AgentSpec,
    options: { readonly gatewayUrls: Readonly<Record<string, string>>; readonly workdir: string },
  ): RuntimeFiles {
    return this.#configFiles(spec, options.gatewayUrls, options.workdir);
  }

  #configFiles(
    spec: AgentSpec,
    gatewayUrls: Readonly<Record<string, string>> | undefined,
    workdir: string,
  ): RuntimeFiles {
    if (this.requireEntrypoint && this.entrypoint === undefined) {
      throw new InvalidArgumentError(
        "el runtime 'langgraph' necesita un entrypoint 'paquete.modulo:funcion'",
      );
    }
    const config = buildDeepAgentsConfig(spec, {
      gatewayUrls,
      workdir,
      entrypoint: this.entrypoint,
    });
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
    return {
      script: runScript(DEEPAGENTS_CONFIG_PATH),
      envs: runEnvs(request.spec, false),
      stdin: UTF8.encode(JSON.stringify(payload)),
    };
  }

  newState(): RuntimeState {
    return newProtocolV1State();
  }

  /** Una línea del protocolo v1 a cero o un evento; lo ilegible, de otra
   * versión o de tipo desconocido se ignora y se cuenta. */
  parseLine(line: Uint8Array, state: RuntimeState): readonly AgentEvent[] {
    return parseV1Line(line, state as ProtocolV1State);
  }

  /** `Done` sólo con salida 0, el `done` del runner y una sesión. */
  finish(state: RuntimeState, exitCode: number): Done | AgentFailed {
    return finishV1(state as ProtocolV1State, exitCode);
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

  /** Copia los requisitos con hash, crea el venv con `--require-hashes` y
   * copia el runner (0755). */
  installSteps(target: InstallTarget): readonly TemplateStep[] {
    const requirements = `${target.installDir}/${DEEPAGENTS_REQUIREMENTS_NAME}`;
    const venvDir = `${target.installDir}/deepagents`;
    return [
      copyStep(DEEPAGENTS_REQUIREMENTS_NAME, requirements),
      runStep(
        hashedVenvInstall(target, {
          venvDir,
          requirementsPath: requirements,
          requirementsSha256: AGENT_DEEPAGENTS_REQUIREMENTS_SHA256,
        }),
      ),
      ...(this.requirements === undefined ? [] : this.#userDependencySteps(target, venvDir)),
      copyStep(DEEPAGENTS_RUNNER_NAME, `${target.installDir}/rayito/${DEEPAGENTS_RUNNER_NAME}`, {
        executable: true,
      }),
    ];
  }

  #userDependencySteps(target: InstallTarget, venvDir: string): readonly TemplateStep[] {
    const requirements = this.requirements as string;
    const manifest = pythonManifest(requirements);
    return pythonDependencySteps(target, {
      installer: this.packageManager ?? DEFAULT_PYTHON_INSTALLER,
      manifest,
      path: manifest === "requirements" ? requirements : undefined,
      python: `${venvDir}/bin/python`,
      venvDir,
      slug: DEEPAGENTS_USER_SLUG,
      createVenv: false,
    });
  }

  /** Los requisitos con hash y el runner (datos de paquete). */
  contextFiles(): Readonly<Record<string, string>> {
    return {
      [DEEPAGENTS_REQUIREMENTS_NAME]: DEEPAGENTS_REQUIREMENTS,
      [DEEPAGENTS_RUNNER_NAME]: DEEPAGENTS_RUNNER_SOURCE,
    };
  }

  /** El sha256 de los requisitos y el del runner, para el manifiesto, y los de
   * tu proyecto si los pediste. */
  manifestEntry(): Readonly<Record<string, unknown>> {
    const entry: Record<string, unknown> = {
      requirements_sha256: AGENT_DEEPAGENTS_REQUIREMENTS_SHA256,
      runner_sha256: createHash("sha256").update(DEEPAGENTS_RUNNER_SOURCE, "utf8").digest("hex"),
    };
    if (this.requirements !== undefined) {
      entry.user_requirements = this.requirements;
      entry.package_manager = this.packageManager ?? DEFAULT_PYTHON_INSTALLER;
    }
    return entry;
  }

  /** Importa deepagents y los modelos de cada proveedor, y comprueba que el
   * runner es legible, todo como `user`. */
  smokeChecks(): readonly string[] {
    return [
      `su user -c "${DEEPAGENTS_PYTHON} -c 'import ${DEEPAGENTS_SMOKE_MODULES}'"`,
      `su user -c 'test -r ${DEEPAGENTS_RUNNER_PATH}'`,
    ];
  }

  /** Ninguna: el venv se carga con `warmupSteps()`. */
  prefetchPaths(): readonly string[] {
    return [];
  }

  /** La configuración del host para el driver `deepagents`: el mismo
   * fichero de configuración y las mismas variables que `command()`. */
  hostConfig(
    spec: AgentSpec,
    options: { readonly wiring: ModelWiring; readonly workdir: string },
  ): HostConfig {
    return hostConfig({
      driver: "deepagents",
      python: DEEPAGENTS_PYTHON,
      files: this.#configFiles(spec, wiringUrls(options.wiring), options.workdir),
      envs: runEnvs(spec, options.wiring.kind === "direct"),
      options: { config_path: DEEPAGENTS_CONFIG_PATH },
    });
  }
}

function runEnvs(spec: AgentSpec, direct: boolean): Record<string, string> {
  const envs: Record<string, string> = { HOME: DEFAULT_AGENT_WORKDIR, ...DEEPAGENTS_FLAG_ENVS };
  if (spec.model.provider === "bedrock" && !direct) {
    envs.AWS_BEARER_TOKEN_BEDROCK = MODEL_CREDENTIAL_PLACEHOLDER;
  }
  if (spec.model.region !== undefined) {
    envs.AWS_REGION = spec.model.region;
  }
  return envs;
}
