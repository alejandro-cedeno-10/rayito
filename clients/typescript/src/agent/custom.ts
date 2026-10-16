/**
 * `CustomRuntime` y `HttpApp` (ADR-028, D2): el agente que escribes tú, con
 * cualquier framework (CrewAI, OpenAI Agents SDK, Google ADK, Claude Agent
 * SDK, Vercel AI SDK...), como un comando, una función de Python o una app
 * HTTP. Espejo de `rayito._agent._custom`: implementa `AgentRuntime`,
 * `InstallableRuntime` y `ServableRuntime` sobre `HostBackedRuntime`.
 *
 * Exactamente uno de `command`, `entrypoint` o `app`:
 *
 * - `command`: tu `argv`, que recibe la petición por stdin y habla el
 *   protocolo v1 por stdout (`protocol: "rayito-v1"`) o escribe texto
 *   (`protocol: "text"`: el prompt llega tal cual por stdin y la sesión, el
 *   modelo y el razonamiento por `RAYITO_SESSION_ID`, `RAYITO_MODEL` y
 *   `RAYITO_REASONING`).
 * - `entrypoint: "modulo:funcion"`: una función de Python que recibe la
 *   petición (`{ prompt, session_id, model, reasoning }`) y devuelve el texto,
 *   un iterable de trozos de texto o un objeto con `text`.
 * - `app: new HttpApp(...)`: tu servidor HTTP; Rayito lo arranca, espera a su
 *   ruta de salud y traduce `/invocations`. Con `protocol: "raw"` (un servidor
 *   MCP por Streamable HTTP, por ejemplo) sólo se expone, sin traducir.
 *
 * El modelo llega a tu código por las variables estándar del proveedor y por
 * las `RAYITO_MODEL_*`, apuntadas a la pasarela con un marcador como clave: la
 * clave real no está en el sandbox.
 */

import { createHash } from "node:crypto";
import { InvalidArgumentError } from "../errors.js";
import {
  AGENT_SERVICE_HEALTH_PATH,
  AGENT_SERVICE_INVOKE_PATH,
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
  userRequirementsSteps,
} from "./host-runtime.js";
import { AGENT_APP_DIR, AGENT_INSTALL_DIR } from "./recipes.js";
import {
  type InstallableRuntime,
  type InstallTarget,
  runStep,
  type TemplateStep,
} from "./runtime.js";

export const CUSTOM_PROTOCOLS = ["rayito-v1", "text"] as const;
export type CustomProtocol = (typeof CUSTOM_PROTOCOLS)[number];
export const HTTP_APP_PROTOCOLS = ["agentcore", "openai-chat", "raw"] as const;
export type HttpAppProtocol = (typeof HTTP_APP_PROTOCOLS)[number];
export const CUSTOM_NAME = "custom";
export const DEFAULT_CUSTOM_PROTOCOL: CustomProtocol = "rayito-v1";
export const DEFAULT_HTTP_APP_PROTOCOL: HttpAppProtocol = "agentcore";
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
const REQUIREMENTS_PATTERN = /^[A-Za-z0-9_][A-Za-z0-9_./-]*$/;
const URL_PATH_PATTERN = /^\/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*$/;

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
}

/**
 * Tu servidor HTTP (`new CustomRuntime({ app: new HttpApp(...) })`): `command`
 * lo arranca, `port` es donde escucha (dentro del sandbox), `healthPath` es la
 * ruta que debe contestar 200 cuando está listo e `invokePath` la que recibe
 * `{"prompt": ...}`. `protocol` es `"agentcore"` (el contrato de AgentCore),
 * `"openai-chat"` (Chat Completions con streaming) o `"raw"` (sólo se
 * expone).
 */
export class HttpApp {
  readonly command: readonly string[];
  readonly port: number;
  readonly healthPath: string;
  readonly invokePath: string;
  readonly protocol: HttpAppProtocol;

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
    this.port = options.port;
    this.healthPath = healthPath;
    this.invokePath = invokePath;
    this.protocol = protocol;
  }

  /** La tabla `[runtime.app]` de `rayito-agent.toml`. */
  static fromMapping(table: Readonly<Record<string, unknown>>): HttpApp {
    const known = ["command", "port", "health_path", "invoke_path", "protocol"];
    const unknown = Object.keys(table)
      .filter((key) => !known.includes(key))
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
    return table;
  }
}

/** Opciones de `new CustomRuntime(...)`. */
export interface CustomRuntimeOptions {
  /** Órdenes de shell que se ejecutan al construir la imagen (como root,
   * antes de copiar tu `source`). */
  readonly install?: readonly string[] | undefined;
  /** Un fichero de requisitos de tu `source` (`app/<requirements>`): se
   * instala en un venv propio, que pasa a ser el intérprete del host. */
  readonly requirements?: string | undefined;
  readonly command?: readonly string[] | undefined;
  readonly entrypoint?: string | undefined;
  readonly app?: HttpApp | undefined;
  readonly protocol?: CustomProtocol | undefined;
  /** El intérprete con el que corre todo (por defecto `python3`). */
  readonly python?: string | undefined;
}

/**
 * `AgentRuntime` del agente que escribes tú (ver el módulo).
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
    if (options.entrypoint !== undefined && !PYTHON_ENTRYPOINT_PATTERN.test(options.entrypoint)) {
      throw new InvalidArgumentError(
        "CustomRuntime.entrypoint debe tener la forma 'paquete.modulo:funcion'",
      );
    }
    if (options.app !== undefined && !(options.app instanceof HttpApp)) {
      throw new InvalidArgumentError("CustomRuntime.app debe ser un HttpApp");
    }
    const requirements = options.requirements;
    if (
      requirements !== undefined &&
      (!REQUIREMENTS_PATTERN.test(requirements) || requirements.split("/").includes(".."))
    ) {
      throw new InvalidArgumentError(
        "CustomRuntime.requirements es una ruta relativa dentro de source",
      );
    }
    if (
      options.python !== undefined &&
      (typeof options.python !== "string" || options.python === "")
    ) {
      throw new InvalidArgumentError("CustomRuntime.python no puede ser vacío");
    }
    this.install = Object.freeze([...install]);
    this.requirements = requirements;
    this.commandArgv =
      options.command === undefined ? undefined : argv(options.command, "CustomRuntime.command");
    this.entrypoint = options.entrypoint;
    this.app = options.app;
    this.protocol = protocol;
    this.python = options.python;
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
    if (this.python !== undefined) {
      options.python = this.python;
    }
    return options;
  }

  venvPython(installDir: string = AGENT_INSTALL_DIR): string {
    return `${installDir}/${CUSTOM_VENV_NAME}/bin/python`;
  }

  hostPython(): string {
    if (this.requirements !== undefined) {
      return this.venvPython();
    }
    return this.python ?? "python3";
  }

  override flagEnvs(): Readonly<Record<string, string>> {
    return PYTHON_FLAG_ENVS;
  }

  /** Las variables estándar del proveedor más las `RAYITO_MODEL_*`. */
  override wiringEnvs(
    spec: AgentSpec,
    urls: Readonly<Record<string, string>> | undefined,
  ): Readonly<Record<string, string>> {
    const envs = standardModelEnvs(spec, urls);
    const model = spec.model;
    envs[MODEL_PROVIDER_ENV] = model.provider;
    envs[MODEL_ID_ENV] = model.id;
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
      return {
        driver: "http-app",
        options: {
          command: [...this.app.command],
          port: this.app.port,
          health_path: this.app.healthPath,
          invoke_path: this.app.invokePath,
          protocol: this.app.protocol,
          workdir,
          model: spec.model.id,
        },
      };
    }
    if (this.commandArgv !== undefined) {
      return {
        driver: "command",
        options: { command: [...this.commandArgv], protocol: this.protocol, workdir },
      };
    }
    return {
      driver: "callable",
      options: { entrypoint: this.entrypoint, app_dir: AGENT_APP_DIR, workdir },
    };
  }

  /** Tus órdenes de `install` y, con `requirements`, el venv con tus
   * requisitos. */
  installSteps(target: InstallTarget): readonly TemplateStep[] {
    const steps: TemplateStep[] = this.install.map((command) => runStep(command));
    if (this.requirements !== undefined) {
      const venvDir = `${target.installDir}/${CUSTOM_VENV_NAME}`;
      const creator = this.python ?? target.python;
      steps.push(runStep(`${creator} -m venv ${venvDir}`));
      steps.push(
        ...userRequirementsSteps(target, {
          venvDir,
          requirements: this.requirements,
          slug: CUSTOM_VENV_NAME,
        }),
      );
    }
    return steps;
  }

  /** Ninguno: tu código y tus requisitos van en `source`. */
  contextFiles(): Readonly<Record<string, string>> {
    return {};
  }

  /** Qué se instaló: el driver, el hash de tus órdenes y tus requisitos. */
  manifestEntry(): Readonly<Record<string, unknown>> {
    return {
      driver: this.hostSetupDriver(),
      install_sha256: createHash("sha256").update(this.install.join("\n"), "utf8").digest("hex"),
      requirements: this.requirements ?? null,
    };
  }

  hostSetupDriver(): string {
    if (this.app !== undefined) {
      return "http-app";
    }
    return this.commandArgv !== undefined ? "command" : "callable";
  }

  /** Ninguna: no sabemos qué instalas. */
  smokeChecks(): readonly string[] {
    return [];
  }

  prefetchPaths(): readonly string[] {
    return [];
  }
}
