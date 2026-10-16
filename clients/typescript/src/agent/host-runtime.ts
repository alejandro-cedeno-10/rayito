/**
 * Base de los runtimes que corren por el host del agente (ADR-028, D1 y D3):
 * Strands, Hermes y `CustomRuntime`. Espejo de `rayito._agent._host_runtime`:
 * cada uno aporta un driver del host, el intérprete que lo ejecuta, su
 * configuración y sus variables de entorno; esta base arma con eso el
 * contrato de tareas de `AgentRuntime` (`buildConfig`, `command`, `parseLine`,
 * `finish`) y el puerto `ServableRuntime` (`hostConfig`), de modo que
 * `sbx.agent.run` y `AgentProject.deploy` ejecutan exactamente lo mismo.
 *
 * Las variables que dependen de la pasarela (la URL de Bedrock, el marcador
 * de la clave) se escriben en un fichero `wiring.env` que el script de una
 * ejecución carga antes de lanzar el host, porque `command()` no conoce las
 * URLs de las pasarelas; en modo servicio van en `HostConfig.envs`. Ningún
 * secreto pasa por aquí: la clave es siempre el marcador
 * `MODEL_CREDENTIAL_PLACEHOLDER` y la real la pone la pasarela.
 *
 * Puro: sin E/S de red ni reloj.
 */

import { InvalidArgumentError } from "../errors.js";
import {
  AGENT_HOST_SCHEMA,
  AGENT_PROTOCOL_VERSION,
  AGENT_STATE_DIR,
  DEFAULT_AGENT_WORKDIR,
  MODEL_CREDENTIAL_PLACEHOLDER,
} from "../limits.js";
import { type AgentPermissions, type AgentSpec, validateModelId } from "./domain.js";
import type { AgentEvent, AgentFailed, Done } from "./events.js";
import { AGENT_RUN_LOCK_PATH, canonicalJson, filesSha256, shellQuote } from "./opencode.js";
import {
  finishV1,
  newProtocolV1State,
  type ProtocolV1State,
  parseV1Line,
  SESSION_ID_PATTERN,
} from "./protocol-v1.js";
import { AGENT_HOST_DIR } from "./recipes.js";
import {
  type AgentRuntime,
  copyStep,
  type HostConfig,
  hostConfig,
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

/** Modo `task` del host (`python agent_host task <host.json>`). */
export const HOST_TASK_MODE = "task";
export const CONFIG_FILE_NAME = "config.json";
export const HOST_FILE_NAME = "host.json";
export const WIRING_ENV_FILE_NAME = "wiring.env";
/** Modo de los ficheros de configuración: sólo los lee el usuario del sandbox. */
export const CONFIG_FILE_MODE = 0o600;
/** Variables de todo proceso Python del host: sin `.pyc` en el directorio del
 * usuario y con la salida sin búfer. */
export const PYTHON_FLAG_ENVS: Readonly<Record<string, string>> = Object.freeze({
  PYTHONDONTWRITEBYTECODE: "1",
  PYTHONUNBUFFERED: "1",
});
/** Variables estándar que leen los SDK de cada proveedor: variable de la
 * clave, variable de la URL base y la ruta que cuelga de la pasarela (`null`:
 * la `basePath` de `AgentModel`). */
export const STANDARD_PROVIDER_ENVS: Readonly<
  Record<string, readonly [string, string, string | null]>
> = Object.freeze({
  anthropic: ["ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", ""],
  openai: ["OPENAI_API_KEY", "OPENAI_BASE_URL", "/v1"],
  "openai-compatible": ["OPENAI_API_KEY", "OPENAI_BASE_URL", null],
  google: ["GEMINI_API_KEY", "GOOGLE_GEMINI_BASE_URL", ""],
});
/** Bedrock no tiene variable de URL base propia: el SDK de AWS lee el
 * endpoint de `bedrock-runtime` y la clave de portador de estas dos. */
export const BEDROCK_KEY_ENV = "AWS_BEARER_TOKEN_BEDROCK";
export const BEDROCK_ENDPOINT_ENV = "AWS_ENDPOINT_URL_BEDROCK_RUNTIME";
export const BEDROCK_REGION_ENV = "AWS_REGION";

const UTF8 = new TextEncoder();

/** Lo que un runtime decide para el host: el driver, la configuración que
 * escribe en `config.json` (o `undefined` si el driver recibe sus opciones en
 * línea) y las opciones del driver. Con `config`, las opciones llevan además
 * `config_path`. */
export interface HostSetup {
  readonly driver: HostConfig["driver"];
  readonly config?: Readonly<Record<string, unknown>> | undefined;
  readonly options?: Readonly<Record<string, unknown>> | undefined;
}

/** El `printf` del script que escribe una línea `rayito.<kind>` del
 * protocolo (`busy`, `runtime_missing`). */
export function failureLine(kind: string): string {
  return `printf '%s\\n' '{"v":${AGENT_PROTOCOL_VERSION},"type":"rayito.${kind}"}'`;
}

function sortedKeys(record: Readonly<Record<string, string>>): string[] {
  return Object.keys(record).sort();
}

/** Un fichero `KEY='value'` por variable, ordenado, para `set -a; . fichero`. */
export function envFileBytes(envs: Readonly<Record<string, string>>): Uint8Array {
  return UTF8.encode(
    sortedKeys(envs)
      .map((key) => `${key}=${shellQuote(envs[key] as string)}\n`)
      .join(""),
  );
}

/** Los permisos de `AgentSpec` como los lee un driver: `default` y una
 * entrada por herramienta (con `DEFAULT_DENIED_TOOLS` por debajo), un patrón
 * por comando en las que los admiten. */
export function permissionRules(permissions: AgentPermissions): Record<string, unknown> {
  const tools: Record<string, unknown> = {};
  for (const [tool, rule] of Object.entries(permissions.effectiveTools())) {
    tools[tool] = typeof rule === "string" ? rule : { ...rule };
  }
  return { default: permissions.default, tools };
}

/** `false` sólo si `tool` está denegada del todo; una herramienta con patrones
 * sigue disponible y el driver aplica cada patrón al llamarla. */
export function toolAllowed(permissions: AgentPermissions, tool: string): boolean {
  const rule = permissions.effectiveTools()[tool] ?? permissions.default;
  return rule !== "deny";
}

/** Cómo un framework habla con un proveedor del modelo a través de la
 * pasarela: la clase de cliente y la ruta que cuelga de la URL de la pasarela
 * (`null`: la `basePath` de `AgentModel`, sólo `openai-compatible`). */
export interface ProviderClient {
  readonly client: string;
  readonly basePath: string | null;
}

/** El cliente del proveedor del modelo de `spec`, o `InvalidArgumentError`
 * (antes de cualquier RPC) si `label` no lo admite. */
export function requireSupportedProvider(
  spec: AgentSpec,
  supported: Readonly<Record<string, ProviderClient>>,
  label: string,
): ProviderClient {
  const entry = supported[spec.model.provider];
  if (entry === undefined) {
    throw new InvalidArgumentError(
      `${label} no admite el proveedor ${JSON.stringify(spec.model.provider)}; usa uno de ${JSON.stringify(Object.keys(supported).sort())}`,
    );
  }
  return entry;
}

function trimTrailingSlashes(url: string): string {
  let end = url.length;
  while (end > 0 && url[end - 1] === "/") {
    end -= 1;
  }
  return url.slice(0, end);
}

/** La URL base del modelo: la de su pasarela más la ruta del cliente;
 * `undefined` sin pasarela (contenedor exportado). */
export function providerBaseUrl(
  spec: AgentSpec,
  entry: ProviderClient,
  urls: Readonly<Record<string, string>> | undefined,
): string | undefined {
  if (urls === undefined) {
    return undefined;
  }
  spec.requireGateways(Object.keys(urls));
  const path = entry.basePath === null ? spec.model.basePath : entry.basePath;
  return trimTrailingSlashes(urls[spec.model.gateway] as string) + path;
}

/** La URL que el SDK del proveedor espera como base: la de su variable
 * estándar o, si no tiene, la raíz de la pasarela. */
export function modelBaseUrl(spec: AgentSpec, urls: Readonly<Record<string, string>>): string {
  spec.requireGateways(Object.keys(urls));
  const gateway = trimTrailingSlashes(urls[spec.model.gateway] as string);
  const standard = STANDARD_PROVIDER_ENVS[spec.model.provider];
  if (standard === undefined) {
    return gateway;
  }
  return gateway + (standard[2] === null ? spec.model.basePath : standard[2]);
}

/** Las variables estándar del proveedor del modelo apuntadas a la pasarela con
 * el marcador como clave, de modo que también un cliente que construyas tú
 * (sin el `ctx.model` ya cableado) pase por ella y no por credenciales que el
 * agente pudiera leer. `urls` ausente es el contenedor exportado: sólo la
 * región. */
export function standardModelEnvs(
  spec: AgentSpec,
  urls: Readonly<Record<string, string>> | undefined,
): Record<string, string> {
  const envs: Record<string, string> = {};
  if (spec.model.region !== undefined) {
    envs[BEDROCK_REGION_ENV] = spec.model.region;
  }
  if (urls === undefined) {
    return envs;
  }
  const baseUrl = modelBaseUrl(spec, urls);
  if (spec.model.provider === "bedrock") {
    envs[BEDROCK_KEY_ENV] = MODEL_CREDENTIAL_PLACEHOLDER;
    envs[BEDROCK_ENDPOINT_ENV] = baseUrl;
    return envs;
  }
  const standard = STANDARD_PROVIDER_ENVS[spec.model.provider];
  if (standard !== undefined) {
    envs[standard[0]] = MODEL_CREDENTIAL_PLACEHOLDER;
    envs[standard[1]] = baseUrl;
  }
  return envs;
}

/** Instala en `venvDir` el fichero de requisitos del usuario
 * (`app/<requirements>` del contexto de build) después de los pines de Rayito.
 * `--require-hashes` se añade solo si el fichero lleva algún `--hash=`, y el
 * paso termina con `pip check`. */
export function userRequirementsSteps(
  target: InstallTarget,
  options: { readonly venvDir: string; readonly requirements: string; readonly slug: string },
): readonly TemplateStep[] {
  const destination = `${target.installDir}/requirements-user-${options.slug}.txt`;
  const venvPython = `${options.venvDir}/bin/python`;
  const command =
    `hashes=''; if grep -q -- '--hash=' ${destination}; then hashes='--require-hashes'; fi` +
    ` && ${venvPython} -m pip install --no-cache-dir --only-binary=:all: $hashes` +
    ` -r ${destination} && ${venvPython} -m pip check`;
  return [copyStep(`app/${options.requirements}`, destination), runStep(command)];
}

/**
 * `AgentRuntime` y `ServableRuntime` de un runtime que corre por el host.
 *
 * Una subclase implementa `hostPython()` (el intérprete que ejecuta el host),
 * `hostSetup()` (qué driver, qué configuración), `flagEnvs()` y `wiringEnvs()`,
 * y los puertos de instalación si es `InstallableRuntime`. `name` identifica
 * el adaptador en la telemetría y `stateName` es su carpeta bajo
 * `AGENT_STATE_DIR`.
 */
export abstract class HostBackedRuntime implements AgentRuntime, ServableRuntime {
  abstract readonly name: string;
  abstract readonly stateName: string;

  /** El intérprete con el que se ejecuta el host (stdlib únicamente). */
  abstract hostPython(): string;

  abstract hostSetup(
    spec: AgentSpec,
    urls: Readonly<Record<string, string>> | undefined,
    workdir: string,
  ): HostSetup;

  /** Variables que no dependen de la pasarela. */
  flagEnvs(): Readonly<Record<string, string>> {
    return PYTHON_FLAG_ENVS;
  }

  /** Variables del proveedor del modelo; `urls` ausente es el contenedor
   * exportado. */
  wiringEnvs(
    _spec: AgentSpec,
    _urls: Readonly<Record<string, string>> | undefined,
  ): Readonly<Record<string, string>> {
    return {};
  }

  get stateDir(): string {
    return `${AGENT_STATE_DIR}/${this.stateName}`;
  }

  get configPath(): string {
    return `${this.stateDir}/${CONFIG_FILE_NAME}`;
  }

  get hostPath(): string {
    return `${this.stateDir}/${HOST_FILE_NAME}`;
  }

  get wiringEnvPath(): string {
    return `${this.stateDir}/${WIRING_ENV_FILE_NAME}`;
  }

  #options(setup: HostSetup): Record<string, unknown> {
    const options: Record<string, unknown> = { ...(setup.options ?? {}) };
    if (setup.config !== undefined) {
      options.config_path = this.configPath;
    }
    return options;
  }

  #configFiles(setup: HostSetup): RuntimeFile[] {
    if (setup.config === undefined) {
      return [];
    }
    return [{ path: this.configPath, data: canonicalJson(setup.config), mode: CONFIG_FILE_MODE }];
  }

  /** La configuración del host para el modo servicio. */
  hostConfig(
    spec: AgentSpec,
    options: { readonly wiring: ModelWiring; readonly workdir: string },
  ): HostConfig {
    const urls = wiringUrls(options.wiring);
    const setup = this.hostSetup(spec, urls, options.workdir);
    const files = this.#configFiles(setup);
    return hostConfig({
      driver: setup.driver,
      python: this.hostPython(),
      files: { files, configSha256: filesSha256(files) },
      envs: { ...this.flagEnvs(), ...this.wiringEnvs(spec, urls) },
      options: this.#options(setup),
    });
  }

  /** Los ficheros de una ejecución `sbx.agent.run`: la configuración del
   * driver, el `host.json` del modo `task` y las variables del proveedor. */
  buildConfig(
    spec: AgentSpec,
    options: { readonly gatewayUrls: Readonly<Record<string, string>>; readonly workdir: string },
  ): RuntimeFiles {
    const setup = this.hostSetup(spec, options.gatewayUrls, options.workdir);
    const hostDocument = {
      schema: AGENT_HOST_SCHEMA,
      v: AGENT_PROTOCOL_VERSION,
      driver: setup.driver,
      options: this.#options(setup),
    };
    const files: RuntimeFile[] = [
      ...this.#configFiles(setup),
      { path: this.hostPath, data: canonicalJson(hostDocument), mode: CONFIG_FILE_MODE },
      {
        path: this.wiringEnvPath,
        data: envFileBytes(this.wiringEnvs(spec, options.gatewayUrls)),
        mode: CONFIG_FILE_MODE,
      },
    ];
    return { files, configSha256: filesSha256(files) };
  }

  /** El script (cerrojo, comprobación del host, variables del proveedor y
   * `exec` del host en modo `task`) y la petición JSON por stdin. El prompt
   * nunca va en argv. */
  command(request: RunRequest): RunCommand {
    if (request.sessionId !== undefined && !SESSION_ID_PATTERN.test(request.sessionId)) {
      throw new InvalidArgumentError(`session_id de ${this.name} debe tener la forma rda_<hex>`);
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
      script: this.#runScript(this.hostPython()),
      envs: { HOME: DEFAULT_AGENT_WORKDIR, ...this.flagEnvs() },
      stdin: UTF8.encode(JSON.stringify(payload)),
    };
  }

  #runScript(python: string): string {
    const lines = [
      "set -u",
      `mkdir -p ${shellQuote(this.stateDir)}`,
      `exec 9>${shellQuote(AGENT_RUN_LOCK_PATH)}`,
      `if ! flock -n 9; then ${failureLine("busy")}; exit 0; fi`,
      `if ! command -v ${shellQuote(python)} >/dev/null 2>&1 || ` +
        `[ ! -r ${shellQuote(`${AGENT_HOST_DIR}/__main__.py`)} ]; then ` +
        `${failureLine("runtime_missing")}; exit 0; fi`,
      `set -a; . ${shellQuote(this.wiringEnvPath)}; set +a`,
      `exec ${shellQuote(python)} ${shellQuote(AGENT_HOST_DIR)} ${HOST_TASK_MODE} ` +
        shellQuote(this.hostPath),
    ];
    return `${lines.join("\n")}\n`;
  }

  newState(): RuntimeState {
    return newProtocolV1State();
  }

  /** Una línea del protocolo v1 a cero o un evento. */
  parseLine(line: Uint8Array, state: RuntimeState): readonly AgentEvent[] {
    return parseV1Line(line, state as ProtocolV1State);
  }

  /** `Done` sólo con salida 0, el `done` del host y una sesión. */
  finish(state: RuntimeState, exitCode: number): Done | AgentFailed {
    return finishV1(state as ProtocolV1State, exitCode);
  }

  /** Vacío: la imagen se compone con `installSteps()`. */
  templateSteps(): readonly TemplateStep[] {
    return [];
  }

  /** Ninguno por defecto; los runtimes con un import caro lo sobrescriben. */
  warmupSteps(): readonly WarmupStep[] {
    return [];
  }
}
