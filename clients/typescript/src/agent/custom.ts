/**
 * `CustomRuntime` y `HttpApp` (ADR-028, D2): el agente que escribes tú, con
 * cualquier framework (CrewAI, OpenAI Agents SDK, Google ADK, Claude Agent
 * SDK, Vercel AI SDK...), como un comando, una función de Python o una app
 * HTTP. Espejo de `rayito._agent._custom`: implementa `AgentRuntime`,
 * `InstallableRuntime` y `ServableRuntime` sobre `HostBackedRuntime`.
 *
 * Exactamente uno de `command`, `entrypoint` o `app`. Con `packageManager`
 * (`pip` o `uv`: Python; `npm`, `pnpm` o `yarn`: Node) y `pythonVersion` o
 * `nodeVersion` el proyecto se instala con el gestor y la versión del
 * intérprete que pidas (`install-plan.ts`):
 *
 * - `command`: tu `argv`, que recibe la petición por stdin y habla el
 *   protocolo v1 por stdout (`protocol: "rayito-v1"`) o escribe texto
 *   (`protocol: "text"`: el prompt llega tal cual por stdin y la sesión, el
 *   modelo y el razonamiento por `RAYITO_SESSION_ID`, `RAYITO_MODEL` y
 *   `RAYITO_REASONING`).
 * - `entrypoint: "modulo:funcion"`: una función de Python que recibe la
 *   petición (`{ prompt, session_id, model, reasoning }`) y devuelve el texto,
 *   un iterable de trozos de texto o un objeto con `text`. Con un gestor de
 *   Node es `ruta.ts#exportacion` y la ejecuta `node_bridge.mjs` con un
 *   `adapter`: `callable` (la misma función, en JavaScript), `langgraph` (un
 *   grafo de LangGraph.js o de deepagents-js) o `vercel-ai` (un agente del AI
 *   SDK).
 * - `app: new HttpApp(...)`: tu servidor HTTP; Rayito lo arranca, espera a su
 *   ruta de salud y traduce `/invocations`. Con `protocol: "json"` (una app con
 *   su propio contrato) también reenvía tus demás rutas; con `protocol: "raw"`
 *   (un servidor MCP por Streamable HTTP, por ejemplo) sólo se expone, sin
 *   traducir; con `protocol: "langgraph-api"` (experimental) la app es
 *   `langgraph dev` y el host habla su LangGraph Platform API.
 *
 * Ese es el contrato mínimo de cualquier framework que no tiene su propio
 * adaptador (CrewAI, OpenAI Agents SDK, Google ADK, Claude Agent SDK...): una app
 * HTTP que contesta a su ruta de salud y a un POST con JSON, una función
 * `(petición) -> texto` o un comando que lee el prompt por stdin y escribe texto
 * por stdout. Los frameworks con adaptador (Strands, deepagents y LangGraph,
 * Hermes, OpenCode, y en JavaScript LangGraph.js, deepagents-js y el AI SDK) no
 * necesitan ninguno de los tres.
 *
 * El modelo llega a tu código por las variables estándar del proveedor y por
 * las `RAYITO_MODEL_*`, apuntadas a la pasarela con un marcador como clave: la
 * clave real no está en el sandbox.
 */

import { createHash } from "node:crypto";
import { InvalidArgumentError } from "../errors.js";
import {
  AGENT_DEFAULT_NODE_MAJOR,
  AGENT_LANGGRAPH_HEALTH_PATH,
  AGENT_LANGGRAPH_INVOKE_PATH,
  AGENT_NODE_BUILD_SHA256,
  AGENT_NODE_BUILD_VERSIONS,
  AGENT_PROTOCOL_VERSION,
  AGENT_SERVICE_HEALTH_PATH,
  AGENT_SERVICE_INVOKE_PATH,
  AGENT_STATE_DIR,
  MODEL_CREDENTIAL_PLACEHOLDER,
  PORT_MAX,
  PORT_MIN,
} from "../limits.js";
import type { AgentSpec } from "./domain.js";
import {
  HostBackedRuntime,
  type HostSetup,
  modelBaseUrl,
  PYTHON_FLAG_ENVS,
  standardModelEnvs,
} from "./host-runtime.js";
import {
  checkNodeVersion,
  checkPythonVersion,
  checkRequirementsPath,
  DEFAULT_PYTHON_INSTALLER,
  ecosystem,
  NODE_PACKAGE_MANAGERS,
  nodeBin,
  nodeDependencySteps,
  nodeRuntimeSteps,
  pythonDependencySteps,
  pythonInterpreter,
  pythonManifest,
  pythonRuntimeSteps,
} from "./install-plan.js";
import {
  AGENT_APP_DIR,
  AGENT_INSTALL_DIR,
  NODE_BRIDGE_NAME,
  NODE_BRIDGE_PATH,
  nodeBridge,
} from "./recipes.js";
import {
  copyStep,
  type InstallableRuntime,
  type InstallTarget,
  installTarget,
  runStep,
  type TemplateStep,
} from "./runtime.js";

export const CUSTOM_PROTOCOLS = ["rayito-v1", "text"] as const;
export type CustomProtocol = (typeof CUSTOM_PROTOCOLS)[number];
/** El LangGraph Platform API de `langgraph dev` (experimental, ADR-033 D5). */
export const LANGGRAPH_API_PROTOCOL = "langgraph-api";
export const HTTP_APP_PROTOCOLS = [
  "agentcore",
  "json",
  "openai-chat",
  "raw",
  LANGGRAPH_API_PROTOCOL,
] as const;
export type HttpAppProtocol = (typeof HTTP_APP_PROTOCOLS)[number];
/** Los protocolos con los que la respuesta se lee de `responseField`. */
const RESPONSE_FIELD_PROTOCOLS: readonly HttpAppProtocol[] = ["json", LANGGRAPH_API_PROTOCOL];
export const CUSTOM_NAME = "custom";
export const DEFAULT_CUSTOM_PROTOCOL: CustomProtocol = "rayito-v1";
export const DEFAULT_HTTP_APP_PROTOCOL: HttpAppProtocol = "agentcore";
export const HTTP_APP_KEYS: readonly string[] = [
  "command",
  "port",
  "health_path",
  "invoke_path",
  "protocol",
  "request_field",
  "response_field",
  "port_env",
  "assistant_id",
];
/** Los adaptadores del puente de Node que un `CustomRuntime` con un gestor de
 * Node puede elegir (el de Strands lo trae `StrandsRuntime`). */
export const CUSTOM_ADAPTERS = ["callable", "langgraph", "vercel-ai"] as const;
export type CustomAdapter = (typeof CUSTOM_ADAPTERS)[number];
export const DEFAULT_CUSTOM_ADAPTER: CustomAdapter = "callable";
/** Carpeta de sesiones (bajo el estado del runtime) del historial que guarda el
 * puente de Node para los marcos sin memoria propia. */
export const CUSTOM_SESSIONS_DIR_NAME = "sessions";
/** El instalador de Python si `requirements` no trae `packageManager`. */
/** El venv que crea `CustomRuntime({ requirements })`. */
export const CUSTOM_VENV_NAME = "custom";
/** Variables con el modelo que ve tu código, además de las estándar. */
export const MODEL_PROVIDER_ENV = "RAYITO_MODEL_PROVIDER";
export const MODEL_ID_ENV = "RAYITO_MODEL_ID";
export const MODEL_REGION_ENV = "RAYITO_MODEL_REGION";
export const MODEL_BASE_URL_ENV = "RAYITO_MODEL_BASE_URL";
export const MODEL_API_KEY_ENV = "RAYITO_MODEL_API_KEY";
/** Variables que el driver `command` pone al hijo con `protocol: "text"`. */
export const SESSION_ID_ENV = "RAYITO_SESSION_ID";
export const REQUEST_MODEL_ENV = "RAYITO_MODEL";
export const REQUEST_REASONING_ENV = "RAYITO_REASONING";

const PYTHON_ENTRYPOINT_PATTERN = /^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$/;
/** `ruta/modulo.ts` o `ruta/modulo.ts#exportacion`. */
const NODE_ENTRYPOINT_PATTERN = /^[A-Za-z0-9_][A-Za-z0-9_./-]*(#[A-Za-z_$][A-Za-z0-9_$]*)?$/;
const FIELD_PATTERN = /^[A-Za-z_][A-Za-z0-9_.]*$/;
const ENV_NAME_PATTERN = /^[A-Z_][A-Z0-9_]*$/;
const URL_PATH_PATTERN = /^\/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*$/;
/** Un grafo de `langgraph.json` o el id de un assistant (un UUID cabe). */
const ASSISTANT_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;

function argv(value: unknown, label: string): readonly string[] {
  if (
    typeof value === "string" ||
    !Array.isArray(value) ||
    value.length === 0 ||
    value.some((part) => typeof part !== "string" || part === "")
  ) {
    throw new InvalidArgumentError(`${label} debe ser una lista no vacía de cadenas`);
  }
  return Object.freeze([...(value as string[])]);
}

/** Opciones de `new HttpApp(...)`. */
export interface HttpAppOptions {
  readonly command: readonly string[];
  readonly port: number;
  readonly healthPath?: string | undefined;
  readonly invokePath?: string | undefined;
  readonly protocol?: HttpAppProtocol | undefined;
  /** Con `protocol: "json"`: el campo del cuerpo que lleva el prompt (por
   * defecto `prompt`). */
  readonly requestField?: string | undefined;
  /** Con `protocol: "json"`: de dónde sale el texto (con puntos para anidar); si
   * falta, del primer campo de texto de `answer`, `output`, `text`, `response`,
   * `result` o `content`. */
  readonly responseField?: string | undefined;
  /** La variable de entorno con la que se le dice a tu app en qué `port`
   * escuchar (por ejemplo `PORT`). */
  readonly portEnv?: string | undefined;
  /** Con `protocol: "langgraph-api"` (y obligatorio con él): el grafo de
   * `langgraph.json` que se ejecuta. */
  readonly assistantId?: string | undefined;
}

/**
 * Tu servidor HTTP (`new CustomRuntime({ app: new HttpApp(...) })`): `command`
 * lo arranca, `port` es donde escucha (dentro del sandbox), `healthPath` es la
 * ruta que debe contestar 200 cuando está listo e `invokePath` la que recibe
 * `{"prompt": ...}`. `protocol` es `"agentcore"` (el contrato de AgentCore),
 * `"json"` (una app con su propio contrato JSON), `"openai-chat"` (Chat
 * Completions con streaming) o `"raw"` (sólo se expone).
 *
 * Cuándo cada uno: con `"agentcore"` u `"openai-chat"` el host sirve `/ping` y
 * `/invocations` y traduce a tu `invokePath`, así que `deployment.invoke()`
 * funciona pero tus rutas propias no se alcanzan desde fuera; con `"json"` el
 * host traduce igual (el prompt va en `requestField`, por defecto `prompt`, y
 * el texto sale de `responseField`, o del primer campo de texto de `answer`,
 * `output`, `text`, `response`, `result` o `content`) y además reenvía tus
 * demás rutas, de modo que `invoke()` funciona y tu `/health` o tu `/chat`
 * siguen alcanzables; con `"raw"` el despliegue reenvía tus rutas tal cual (un
 * servidor MCP) y `invoke()` no existe: usa `deployment.url` y
 * `deployment.headers` con tu cliente. `portEnv` es la variable de entorno con
 * la que se le dice a tu app en qué `port` escuchar (por ejemplo `PORT`).
 *
 * Con `"langgraph-api"` (experimental, ADR-033 D5) tu `command` es `langgraph
 * dev` y el host traduce cada invocación a `POST /threads/{thread_id}/runs/wait`
 * del LangGraph Platform API: el hilo sale de la sesión y `assistantId`
 * (obligatorio, y sólo con este protocolo) es el grafo de `langgraph.json` que
 * se ejecuta. La salud es `/ok` si dejas `healthPath` por defecto, la ruta de
 * invocación la fija el API (`invokePath` no se cambia) y el texto sale de
 * `responseField` o del último mensaje. Sólo `/ping` e `/invocations` se
 * exponen: el resto del Platform API no se alcanza desde fuera. Tu `command` se ejecuta con el
 * directorio del código copiado como directorio de trabajo y en su
 * `PYTHONPATH`; con `requirements`, `python` y `pip` son los del venv (sin él,
 * `python` es el intérprete de la imagen).
 */
export class HttpApp {
  readonly command: readonly string[];
  readonly port: number;
  readonly healthPath: string;
  readonly invokePath: string;
  readonly protocol: HttpAppProtocol;
  readonly requestField: string | undefined;
  readonly responseField: string | undefined;
  readonly portEnv: string | undefined;
  readonly assistantId: string | undefined;

  constructor(options: HttpAppOptions) {
    this.command = argv(options.command, "HttpApp.command");
    if (!Number.isInteger(options.port)) {
      throw new InvalidArgumentError("HttpApp.port debe ser un entero");
    }
    if (options.port < PORT_MIN || options.port > PORT_MAX) {
      throw new InvalidArgumentError(
        `HttpApp.port debe estar entre ${PORT_MIN} y ${PORT_MAX}, recibido ${options.port}`,
      );
    }
    const healthPath = options.healthPath ?? AGENT_SERVICE_HEALTH_PATH;
    const invokePath = options.invokePath ?? AGENT_SERVICE_INVOKE_PATH;
    for (const [label, path] of [
      ["healthPath", healthPath],
      ["invokePath", invokePath],
    ] as const) {
      if (typeof path !== "string" || !URL_PATH_PATTERN.test(path)) {
        throw new InvalidArgumentError(`HttpApp.${label} debe ser una ruta que empiece por /`);
      }
    }
    const protocol = options.protocol ?? DEFAULT_HTTP_APP_PROTOCOL;
    if (!(HTTP_APP_PROTOCOLS as readonly string[]).includes(protocol)) {
      throw new InvalidArgumentError(
        `HttpApp.protocol debe ser uno de ${JSON.stringify(HTTP_APP_PROTOCOLS)}`,
      );
    }
    for (const [label, value, protocols] of [
      ["requestField", options.requestField, ["json"]],
      ["responseField", options.responseField, RESPONSE_FIELD_PROTOCOLS],
    ] as const) {
      if (value === undefined) {
        continue;
      }
      if (!(protocols as readonly string[]).includes(protocol)) {
        const allowed = protocols.map((name) => `'${name}'`).join(" o ");
        throw new InvalidArgumentError(`HttpApp.${label} sólo se usa con protocol ${allowed}`);
      }
      if (typeof value !== "string" || !FIELD_PATTERN.test(value)) {
        throw new InvalidArgumentError(
          `HttpApp.${label} debe ser un nombre de campo (con puntos para anidar)`,
        );
      }
    }
    if (options.portEnv !== undefined && !ENV_NAME_PATTERN.test(String(options.portEnv))) {
      throw new InvalidArgumentError(
        "HttpApp.portEnv debe ser un nombre de variable de entorno (MAYÚSCULAS)",
      );
    }
    checkLanggraphFields(protocol, options.assistantId, invokePath);
    this.port = options.port;
    this.healthPath = healthPath;
    this.invokePath = invokePath;
    this.protocol = protocol;
    this.requestField = options.requestField;
    this.responseField = options.responseField;
    this.portEnv = options.portEnv;
    this.assistantId = options.assistantId;
  }

  /** La ruta de salud a la que llama el host: `healthPath`, o `/ok` con
   * `langgraph-api` si se dejó la de por defecto. */
  get servedHealthPath(): string {
    return this.protocol === LANGGRAPH_API_PROTOCOL && this.healthPath === AGENT_SERVICE_HEALTH_PATH
      ? AGENT_LANGGRAPH_HEALTH_PATH
      : this.healthPath;
  }

  /** La ruta de invocación a la que llama el host: `invokePath`, o la de una
   * ejecución de un hilo (`/threads/{thread_id}/runs/wait`) con
   * `langgraph-api`. */
  get servedInvokePath(): string {
    return this.protocol === LANGGRAPH_API_PROTOCOL ? AGENT_LANGGRAPH_INVOKE_PATH : this.invokePath;
  }

  /** Con `protocol: "agentcore"`, `"openai-chat"` o `"langgraph-api"` el
   * despliegue sólo expone `/ping` y `/invocations` (el host traduce a tu
   * `invokePath`): tus rutas propias (`healthPath`, `invokePath` distintas de esas dos) no se alcanzan
   * desde fuera. Con `protocol: "json"` o `"raw"` se exponen tal cual. Con
   * `langgraph-api`, `/ok` (la salud del API, la que escribe la detección) no
   * es una ruta propia. */
  get hidesOwnRoutes(): boolean {
    if (this.protocol === "raw" || this.protocol === "json") {
      return false;
    }
    const servicePaths: readonly string[] =
      this.protocol === LANGGRAPH_API_PROTOCOL
        ? [AGENT_SERVICE_HEALTH_PATH, AGENT_LANGGRAPH_HEALTH_PATH]
        : [AGENT_SERVICE_HEALTH_PATH];
    return !servicePaths.includes(this.healthPath) || this.invokePath !== AGENT_SERVICE_INVOKE_PATH;
  }

  /** La tabla `[runtime.app]` de `rayito-agent.toml`. */
  static fromMapping(table: Readonly<Record<string, unknown>>): HttpApp {
    const unknown = Object.keys(table)
      .filter((key) => !HTTP_APP_KEYS.includes(key))
      .sort();
    if (unknown.length > 0) {
      throw new InvalidArgumentError(`runtime.app.${unknown[0]}: clave desconocida`);
    }
    return new HttpApp({
      command: table.command as string[],
      port: table.port as number,
      healthPath: table.health_path as string | undefined,
      invokePath: table.invoke_path as string | undefined,
      protocol: table.protocol as HttpAppProtocol | undefined,
      requestField: table.request_field as string | undefined,
      responseField: table.response_field as string | undefined,
      portEnv: table.port_env as string | undefined,
      assistantId: table.assistant_id as string | undefined,
    });
  }

  /** La tabla `[runtime.app]` canónica (sólo lo que no es el valor por defecto). */
  toMapping(): Record<string, unknown> {
    const table: Record<string, unknown> = { command: [...this.command], port: this.port };
    if (this.healthPath !== AGENT_SERVICE_HEALTH_PATH) {
      table.health_path = this.healthPath;
    }
    if (this.invokePath !== AGENT_SERVICE_INVOKE_PATH) {
      table.invoke_path = this.invokePath;
    }
    if (this.protocol !== DEFAULT_HTTP_APP_PROTOCOL) {
      table.protocol = this.protocol;
    }
    if (this.requestField !== undefined) {
      table.request_field = this.requestField;
    }
    if (this.responseField !== undefined) {
      table.response_field = this.responseField;
    }
    if (this.portEnv !== undefined) {
      table.port_env = this.portEnv;
    }
    if (this.assistantId !== undefined) {
      table.assistant_id = this.assistantId;
    }
    return table;
  }
}

/** `assistantId` es obligatorio con `langgraph-api` y no se usa con otro
 * protocolo; la ruta de invocación la fija el Platform API. */
function checkLanggraphFields(
  protocol: HttpAppProtocol,
  assistantId: string | undefined,
  invokePath: string,
): void {
  if (protocol !== LANGGRAPH_API_PROTOCOL) {
    if (assistantId !== undefined) {
      throw new InvalidArgumentError(
        `HttpApp.assistantId sólo se usa con protocol '${LANGGRAPH_API_PROTOCOL}'`,
      );
    }
    return;
  }
  if (typeof assistantId !== "string" || !ASSISTANT_ID_PATTERN.test(assistantId)) {
    throw new InvalidArgumentError(
      `HttpApp.assistantId es obligatorio con protocol '${LANGGRAPH_API_PROTOCOL}' y es el nombre de un grafo de langgraph.json`,
    );
  }
  if (invokePath !== AGENT_SERVICE_INVOKE_PATH) {
    throw new InvalidArgumentError(
      `HttpApp.invokePath no se usa con protocol '${LANGGRAPH_API_PROTOCOL}': la ruta la fija el LangGraph Platform API`,
    );
  }
}

/** Opciones de `new CustomRuntime(...)`. */
export interface CustomRuntimeOptions {
  /** Órdenes de shell que se ejecutan al construir la imagen (como root,
   * antes de copiar tu `source`). */
  readonly install?: readonly string[] | undefined;
  /** Un fichero de dependencias de tu `source` (`app/<requirements>`:
   * `requirements.txt` o, con `packageManager: "uv"`, `pyproject.toml` o
   * `uv.lock`): se instala en un venv propio, que pasa a ser el intérprete del
   * host. */
  readonly requirements?: string | undefined;
  readonly command?: readonly string[] | undefined;
  readonly entrypoint?: string | undefined;
  readonly app?: HttpApp | undefined;
  readonly protocol?: CustomProtocol | undefined;
  /** El intérprete con el que corre todo (por defecto `python3`). */
  readonly python?: string | undefined;
  /** `pip` o `uv` (Python) o `npm`, `pnpm` o `yarn` (Node: `source` debe llevar
   * `package.json` y su lockfile). */
  readonly packageManager?: string | undefined;
  /** La versión menor de CPython (`"3.11"`...) si no es la de la imagen. */
  readonly pythonVersion?: string | undefined;
  /** La versión mayor de Node (`"22"`, `"24"`). */
  readonly nodeVersion?: string | undefined;
  /** Cómo el puente de Node ejecuta un `entrypoint` `ruta.ts#exportacion`:
   * `callable`, `langgraph` o `vercel-ai`. */
  readonly adapter?: string | undefined;
}

/**
 * `AgentRuntime` del agente que escribes tú (ver el módulo).
 *
 * `install` son órdenes de shell que se ejecutan al construir la imagen (como
 * root, antes de copiar tu `source`). `requirements` es un fichero de
 * dependencias de tu `source`; `packageManager` elige el gestor (`pip`, `uv`,
 * `npm`, `pnpm` o `yarn`), y `pythonVersion` y `nodeVersion` piden un
 * intérprete distinto del de la imagen (un build con sha256 fijado).
 *
 * Coste y activación
 * -------------------
 * Activa: pasar `new CustomRuntime(...)` a `AgentTemplate`, `AgentProject` o
 *   `sbx.agent.run`.
 * Recursos y llamadas AWS: ninguna por sí solo; la imagen que lo instala se
 *   construye con `AgentTemplate.build` (un build de imagen) y cada ejecución
 *   llama al modelo de la pasarela que elijas.
 * Coste aproximado: el del build de la imagen (ver `AgentTemplate`) más el del
 *   modelo; lo que añada tu `install` y tus requisitos.
 * IAM: la política `RayitoTemplateBuilder` para construir; ninguna para
 *   ejecutar (la pasarela guarda la clave).
 * Cómo apagarla: no incluirlo en `runtimes` ni en el proyecto.
 * Ejemplo:
 *   new CustomRuntime({ entrypoint: "agente:responde", requirements: "requirements.txt" })
 */
export class CustomRuntime extends HostBackedRuntime implements InstallableRuntime {
  readonly name = CUSTOM_NAME;
  readonly stateName = CUSTOM_NAME;
  readonly install: readonly string[];
  readonly requirements: string | undefined;
  readonly commandArgv: readonly string[] | undefined;
  readonly entrypoint: string | undefined;
  readonly app: HttpApp | undefined;
  readonly protocol: CustomProtocol;
  readonly python: string | undefined;
  readonly packageManager: string | undefined;
  readonly pythonVersion: string | undefined;
  readonly nodeVersion: string | undefined;
  readonly adapter: string | undefined;

  constructor(options: CustomRuntimeOptions = {}) {
    super();
    const install = options.install ?? [];
    if (
      typeof install === "string" ||
      !Array.isArray(install) ||
      install.some((cmd) => typeof cmd !== "string" || cmd === "")
    ) {
      throw new InvalidArgumentError("CustomRuntime.install debe ser una lista de órdenes");
    }
    const targets = [options.command, options.entrypoint, options.app];
    if (targets.filter((target) => target !== undefined).length !== 1) {
      throw new InvalidArgumentError(
        "CustomRuntime necesita exactamente uno de command, entrypoint o app",
      );
    }
    const protocol = options.protocol ?? DEFAULT_CUSTOM_PROTOCOL;
    if (!(CUSTOM_PROTOCOLS as readonly string[]).includes(protocol)) {
      throw new InvalidArgumentError(
        `CustomRuntime.protocol debe ser uno de ${JSON.stringify(CUSTOM_PROTOCOLS)}`,
      );
    }
    if (protocol !== DEFAULT_CUSTOM_PROTOCOL && options.command === undefined) {
      throw new InvalidArgumentError("CustomRuntime.protocol sólo se usa con command");
    }
    if (options.app !== undefined && !(options.app instanceof HttpApp)) {
      throw new InvalidArgumentError("CustomRuntime.app debe ser un HttpApp");
    }
    if (
      options.python !== undefined &&
      (typeof options.python !== "string" || options.python === "")
    ) {
      throw new InvalidArgumentError("CustomRuntime.python no puede ser vacío");
    }
    const kind = toolchain(options);
    checkEntrypoint(options.entrypoint, kind);
    checkRequirements(options.requirements, kind, options.packageManager);
    checkAdapter(options.adapter, options.entrypoint, kind);
    this.install = Object.freeze([...install]);
    this.requirements = options.requirements;
    this.commandArgv =
      options.command === undefined ? undefined : argv(options.command, "CustomRuntime.command");
    this.entrypoint = options.entrypoint;
    this.app = options.app;
    this.protocol = protocol;
    this.python = options.python;
    this.packageManager = options.packageManager;
    this.pythonVersion = options.pythonVersion;
    this.nodeVersion = options.nodeVersion;
    const bridged = kind === "node" && options.entrypoint !== undefined;
    this.adapter = options.adapter ?? (bridged ? DEFAULT_CUSTOM_ADAPTER : undefined);
  }

  /** El runtime de la tabla `[runtime]` de `rayito-agent.toml`. */
  static fromOptions(options: Readonly<Record<string, unknown>>): CustomRuntime {
    const text = (key: string): string | undefined => {
      const value = options[key];
      if (value !== undefined && typeof value !== "string") {
        throw new InvalidArgumentError(`runtime.${key} debe ser una cadena`);
      }
      return value;
    };
    const app = options.app;
    if (app !== undefined && (typeof app !== "object" || app === null || Array.isArray(app))) {
      throw new InvalidArgumentError("runtime.app debe ser una tabla");
    }
    const install = options.install ?? [];
    if (!Array.isArray(install)) {
      throw new InvalidArgumentError("runtime.install debe ser una lista");
    }
    const command = options.command;
    return new CustomRuntime({
      install: install as string[],
      requirements: text("requirements"),
      command: command === undefined ? undefined : argv(command, "runtime.command"),
      entrypoint: text("entrypoint"),
      app: app === undefined ? undefined : HttpApp.fromMapping(app as Record<string, unknown>),
      protocol: (text("protocol") ?? DEFAULT_CUSTOM_PROTOCOL) as CustomProtocol,
      python: text("python"),
      packageManager: text("package_manager"),
      pythonVersion: text("python_version"),
      nodeVersion: text("node_version"),
      adapter: text("adapter"),
    });
  }

  /** La tabla `[runtime]` canónica. */
  toOptions(): Record<string, unknown> {
    const options: Record<string, unknown> = { kind: CUSTOM_NAME };
    if (this.install.length > 0) {
      options.install = [...this.install];
    }
    if (this.requirements !== undefined) {
      options.requirements = this.requirements;
    }
    if (this.commandArgv !== undefined) {
      options.command = [...this.commandArgv];
    }
    if (this.entrypoint !== undefined) {
      options.entrypoint = this.entrypoint;
    }
    if (this.app !== undefined) {
      options.app = this.app.toMapping();
    }
    if (this.protocol !== DEFAULT_CUSTOM_PROTOCOL) {
      options.protocol = this.protocol;
    }
    const optional: ReadonlyArray<readonly [string, string | undefined]> = [
      ["python", this.python],
      ["package_manager", this.packageManager],
      ["python_version", this.pythonVersion],
      ["node_version", this.nodeVersion],
    ];
    for (const [key, value] of optional) {
      if (value !== undefined) {
        options[key] = value;
      }
    }
    if (this.adapter !== undefined && this.adapter !== DEFAULT_CUSTOM_ADAPTER) {
      options.adapter = this.adapter;
    }
    return options;
  }

  get #node(): boolean {
    return this.packageManager !== undefined && ecosystem(this.packageManager) === "node";
  }

  /** El `entrypoint` es de JavaScript y lo ejecuta el puente de Node. */
  get #bridged(): boolean {
    return this.#node && this.entrypoint !== undefined;
  }

  /** El `bin` del venv de `requirements`: va el primero en el `PATH` de tu
   * app o tu comando, de modo que `python` y `pip` son los suyos. */
  venvBin(installDir: string = AGENT_INSTALL_DIR): string {
    return `${installDir}/${CUSTOM_VENV_NAME}/bin`;
  }

  venvPython(installDir: string = AGENT_INSTALL_DIR): string {
    return `${this.venvBin(installDir)}/python`;
  }

  #pinnedPython(): string {
    return pythonInterpreter(installTarget(), this.pythonVersion);
  }

  /** Dónde corre tu código: el directorio copiado (directorio de trabajo y
   * `PYTHONPATH`) y, con `requirements`, el `bin` del venv; con un gestor de
   * Node, el `bin` de Node. */
  #environmentOptions(): Record<string, unknown> {
    const options: Record<string, unknown> = { app_dir: AGENT_APP_DIR };
    const prepend: string[] = [];
    if (this.requirements !== undefined) {
      prepend.push(this.venvBin());
    }
    if (this.#node) {
      prepend.push(nodeBin(installTarget()));
    }
    if (prepend.length > 0) {
      options.path_prepend = prepend;
    }
    return options;
  }

  hostPython(): string {
    if (this.requirements !== undefined) {
      return this.venvPython();
    }
    if (this.pythonVersion !== undefined) {
      return this.#pinnedPython();
    }
    return this.python ?? "python3";
  }

  override flagEnvs(): Readonly<Record<string, string>> {
    return PYTHON_FLAG_ENVS;
  }

  /** Las variables estándar del proveedor más las `RAYITO_MODEL_*`
   * (`RAYITO_MODEL_ID` sólo si se conoce el id). */
  override wiringEnvs(
    spec: AgentSpec,
    urls: Readonly<Record<string, string>> | undefined,
  ): Readonly<Record<string, string>> {
    const envs = standardModelEnvs(spec, urls);
    const model = spec.model;
    envs[MODEL_PROVIDER_ENV] = model.provider;
    if (model.id !== undefined) {
      envs[MODEL_ID_ENV] = model.id;
    }
    if (model.region !== undefined) {
      envs[MODEL_REGION_ENV] = model.region;
    }
    if (urls !== undefined) {
      envs[MODEL_BASE_URL_ENV] = modelBaseUrl(spec, urls);
      envs[MODEL_API_KEY_ENV] = MODEL_CREDENTIAL_PLACEHOLDER;
    }
    return envs;
  }

  hostSetup(
    spec: AgentSpec,
    _urls: Readonly<Record<string, string>> | undefined,
    workdir: string,
  ): HostSetup {
    if (this.app !== undefined) {
      return { driver: "http-app", options: this.#appOptions(this.app, spec, workdir) };
    }
    if (this.commandArgv !== undefined) {
      return {
        driver: "command",
        options: {
          command: [...this.commandArgv],
          protocol: this.protocol,
          workdir,
          ...this.#environmentOptions(),
        },
      };
    }
    if (this.#bridged) {
      return { driver: "node-callable", config: this.#bridgeConfig(spec, workdir) };
    }
    return {
      driver: "callable",
      options: { entrypoint: this.entrypoint, app_dir: AGENT_APP_DIR, workdir },
    };
  }

  #appOptions(app: HttpApp, spec: AgentSpec, workdir: string): Record<string, unknown> {
    const options: Record<string, unknown> = {
      command: [...app.command],
      port: app.port,
      health_path: app.servedHealthPath,
      invoke_path: app.servedInvokePath,
      protocol: app.protocol,
      workdir,
      model: spec.model.id ?? null,
      ...this.#environmentOptions(),
    };
    if (app.requestField !== undefined) {
      options.request_field = app.requestField;
    }
    if (app.responseField !== undefined) {
      options.response_field = app.responseField;
    }
    if (app.portEnv !== undefined) {
      options.port_env = app.portEnv;
    }
    if (app.assistantId !== undefined) {
      options.assistant_id = app.assistantId;
    }
    return options;
  }

  #bridgeConfig(spec: AgentSpec, workdir: string): Record<string, unknown> {
    const stateDir = `${AGENT_STATE_DIR}/${this.stateName}`;
    return {
      v: AGENT_PROTOCOL_VERSION,
      bridge: NODE_BRIDGE_PATH,
      adapter: this.adapter,
      entrypoint: this.entrypoint,
      model: spec.model.id ?? null,
      instructions: spec.instructions ?? null,
      workdir,
      app_dir: AGENT_APP_DIR,
      state_dir: stateDir,
      sessions_dir: `${stateDir}/${CUSTOM_SESSIONS_DIR_NAME}`,
    };
  }

  /** Tus órdenes de `install` y el toolchain que pidas: con `requirements`, el
   * venv con tus dependencias; con un gestor de Node, Node y la instalación
   * congelada de tu lockfile (y el puente si hay `entrypoint`). */
  installSteps(target: InstallTarget): readonly TemplateStep[] {
    const steps: TemplateStep[] = this.install.map((command) => runStep(command));
    if (this.#node) {
      steps.push(
        ...nodeRuntimeSteps(target, this.nodeVersion),
        ...nodeDependencySteps(target, this.packageManager as string),
      );
      if (this.#bridged) {
        steps.push(copyStep(NODE_BRIDGE_NAME, NODE_BRIDGE_PATH));
      }
      return steps;
    }
    steps.push(...pythonRuntimeSteps(target, this.pythonVersion));
    if (this.requirements !== undefined) {
      steps.push(...this.#pythonDependencySteps(target, this.requirements));
    }
    return steps;
  }

  #pythonDependencySteps(target: InstallTarget, requirements: string): readonly TemplateStep[] {
    const manifest = pythonManifest(requirements);
    return pythonDependencySteps(target, {
      installer: this.packageManager ?? DEFAULT_PYTHON_INSTALLER,
      manifest,
      path: manifest === "requirements" ? requirements : undefined,
      python: this.python ?? pythonInterpreter(target, this.pythonVersion),
      venvDir: `${target.installDir}/${CUSTOM_VENV_NAME}`,
      slug: CUSTOM_VENV_NAME,
    });
  }

  /** El puente de Node si hay un `entrypoint` de JavaScript; si no, ninguno:
   * tu código y tus requisitos van en `source`. */
  contextFiles(): Readonly<Record<string, string>> {
    return this.#bridged ? { [NODE_BRIDGE_NAME]: nodeBridge() } : {};
  }

  /** Qué se instaló: el driver, el hash de tus órdenes y tus requisitos, y el
   * gestor y las versiones de intérprete que pediste. */
  manifestEntry(): Readonly<Record<string, unknown>> {
    const entry: Record<string, unknown> = {
      driver: this.hostSetupDriver(),
      install_sha256: createHash("sha256").update(this.install.join("\n"), "utf8").digest("hex"),
      requirements: this.requirements ?? null,
    };
    if (this.packageManager !== undefined) {
      entry.package_manager = this.packageManager;
    }
    if (this.pythonVersion !== undefined) {
      entry.python_version = this.pythonVersion;
    }
    if (this.#node) {
      const major = this.nodeVersion ?? AGENT_DEFAULT_NODE_MAJOR;
      entry.node_version = AGENT_NODE_BUILD_VERSIONS[major];
      entry.node_sha256 = AGENT_NODE_BUILD_SHA256[major];
    }
    return entry;
  }

  hostSetupDriver(): string {
    if (this.app !== undefined) {
      return "http-app";
    }
    if (this.commandArgv !== undefined) {
      return "command";
    }
    return this.#bridged ? "node-callable" : "callable";
  }

  /** Node o el CPython fijado, si los pediste; si no, ninguna: no sabemos qué
   * instalas. */
  smokeChecks(): readonly string[] {
    if (this.#node) {
      return ["su user -c 'node --version'"];
    }
    if (this.pythonVersion !== undefined) {
      return [`su user -c '${this.#pinnedPython()} --version'`];
    }
    return [];
  }

  prefetchPaths(): readonly string[] {
    return [];
  }
}

/** El ecosistema (`python`, `node` o `undefined`) que piden las opciones del
 * toolchain, validando que no se contradigan. */
function toolchain(options: CustomRuntimeOptions): "python" | "node" | undefined {
  const manager = options.packageManager;
  if (manager !== undefined && typeof manager !== "string") {
    throw new InvalidArgumentError("CustomRuntime.packageManager debe ser una cadena");
  }
  const kind = manager === undefined ? undefined : ecosystem(manager);
  if (options.pythonVersion !== undefined) {
    if (kind === "node") {
      throw new InvalidArgumentError(
        "CustomRuntime.pythonVersion no se combina con un gestor de Node",
      );
    }
    if (options.python !== undefined) {
      throw new InvalidArgumentError(
        "CustomRuntime.python y pythonVersion no se combinan: usa uno",
      );
    }
    checkPythonVersion(options.pythonVersion);
  }
  if (options.nodeVersion !== undefined) {
    if (kind !== "node") {
      throw new InvalidArgumentError(
        `CustomRuntime.nodeVersion necesita packageManager en ${JSON.stringify(NODE_PACKAGE_MANAGERS)}`,
      );
    }
    checkNodeVersion(options.nodeVersion);
  }
  return kind;
}

function checkEntrypoint(entrypoint: string | undefined, kind: string | undefined): void {
  if (entrypoint === undefined) {
    return;
  }
  const pattern = kind === "node" ? NODE_ENTRYPOINT_PATTERN : PYTHON_ENTRYPOINT_PATTERN;
  if (!pattern.test(entrypoint) || entrypoint.split("/").includes("..")) {
    const shape = kind === "node" ? "'ruta/modulo.ts#exportacion'" : "'paquete.modulo:funcion'";
    throw new InvalidArgumentError(`CustomRuntime.entrypoint debe tener la forma ${shape}`);
  }
}

function checkRequirements(
  requirements: string | undefined,
  kind: string | undefined,
  manager: string | undefined,
): void {
  if (requirements === undefined) {
    return;
  }
  if (kind === "node") {
    throw new InvalidArgumentError(
      "CustomRuntime.requirements es de Python: con un gestor de Node las " +
        "dependencias son las de package.json",
    );
  }
  checkRequirementsPath("CustomRuntime", requirements, manager);
}

function checkAdapter(
  adapter: string | undefined,
  entrypoint: string | undefined,
  kind: string | undefined,
): void {
  if (adapter === undefined) {
    return;
  }
  if (!(CUSTOM_ADAPTERS as readonly string[]).includes(adapter)) {
    throw new InvalidArgumentError(
      `CustomRuntime.adapter debe ser uno de ${JSON.stringify(CUSTOM_ADAPTERS)}`,
    );
  }
  if (kind !== "node" || entrypoint === undefined) {
    throw new InvalidArgumentError(
      "CustomRuntime.adapter sólo se usa con entrypoint y un gestor de Node",
    );
  }
}
