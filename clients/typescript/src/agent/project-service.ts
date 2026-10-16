/**
 * `AgentProject.build()`, `run()` y `deploy()` (ADR-028, D6): la capa de
 * aplicación sobre `AgentTemplate`, `Sandbox` y `AgentDeployment`. Espejo de
 * `rayito._agent._project_base` y `_project_service_async`. Nada aquí se
 * ejecuta al construir o cargar un proyecto; cada operación es la única que
 * crea recursos y `deploy()` mata su sandbox ante cualquier fallo (salvo
 * `keepOnFailure`).
 */

import { posix } from "node:path";
import { InvalidArgumentError, SandboxError, TimeoutError } from "../errors.js";
import {
  AGENT_RUN_SANDBOX_MARGIN_SECONDS,
  AGENT_SERVICE_READY_MARKER_PATH,
  AGENT_SERVICE_READY_TIMEOUT_SECONDS,
  AGENT_SERVICE_SESSION_HEADER,
  AGENT_STARTUP_DIAGNOSTICS_MAX_BYTES,
  DEFAULT_AGENT_WORKDIR,
} from "../limits.js";
import { ACCESS_TOKEN_ENV_VAR } from "../sandbox/launch.js";
import type { SandboxConnectOptions, SandboxCreateOptions } from "../sandbox/sandbox.js";
import type { BuildInfo } from "../templates/build.js";
import { AgentDeployment, type DeploymentSandbox, labelsFor } from "./deployment.js";
import type { AgentEvent, AgentResult } from "./events.js";
import { canonicalJson, shellQuote } from "./opencode.js";
import type { AgentProject } from "./project.js";
import { AGENT_HOST_DIR } from "./recipes.js";
import {
  gatewayWiring,
  type HostConfig,
  type InstallableRuntime,
  requireInstallable,
  requireServable,
  type ServableRuntime,
} from "./runtime.js";
import { resolveRuntime } from "./runtimes.js";
import { type AgentSandbox, gatewayUrlsFor } from "./stream.js";
import { AgentTemplate, type AgentTemplateBuildOptions } from "./template.js";

const MS_PER_SECOND = 1000;
/** Esquema del `host.json` que lee `agent_host` (`rayito.agent-host/1`). */
export const AGENT_HOST_SCHEMA = "rayito.agent-host/1";
/** El directorio del host dentro de `AGENT_STATE_DIR` y su configuración. */
export const AGENT_HOST_STATE_DIR = posix.dirname(AGENT_SERVICE_READY_MARKER_PATH);
export const AGENT_HOST_CONFIG_PATH = `${AGENT_HOST_STATE_DIR}/host.json`;
/** Sufijo del marcador de arranque fallido (`agent_host/serve.py`). */
export const AGENT_HOST_FAILED_SUFFIX = ".failed";
/** Etiqueta del comando del host en `commands.list()`. */
export const AGENT_SERVE_TAG = "rayito-agent-serve";
/** Cada cuánto `deploy()` mira el marcador de listo. */
export const AGENT_SERVICE_READY_POLL_MS = 500;
/** Usuario con el que corre el host del agente (uid 1000): nunca root. */
export const AGENT_SERVE_USER = "user";
/** URL de relleno para calcular la forma del `hostConfig()` sin sandbox. */
export const PLACEHOLDER_GATEWAY_URL = "http://127.0.0.1:0";
const HOST_DOC_MODE = 0o644;

/** Lo que `run()` y `deploy()` pasan a `Sandbox.create()` además de lo que
 * sale del proyecto: dónde está AWS (`region`, credenciales, plano de
 * control). */
export type AgentProjectSandboxOptions = Pick<
  SandboxConnectOptions,
  "region" | "controlPlane" | "client" | "logger" | "signal"
>;

/** Lo de `AgentProjectSandboxOptions` más el access token del sandbox del
 * despliegue (el de `Sandbox.create()`; sin él, `RAYITO_ACCESS_TOKEN` o uno
 * nuevo). Quien quiera reconectarse desde otro proceso
 * (`AgentDeployment.connect`, `rayito agent invoke`) lo guarda (con uno nuevo,
 * el valor está en `AgentDeployment.accessToken`): sin él sólo
 * `Sandbox.kill(sandboxId)` llega al sandbox.
 *
 * `keepOnFailure` (`false` por defecto) deja vivo el sandbox si el despliegue
 * falla, para depurarlo: el error lleva su id en el mensaje, hace falta
 * `accessToken` (o `RAYITO_ACCESS_TOKEN`) para poder conectarse y lo
 * terminas tú con `Sandbox.kill(sandboxId)`, porque sigue facturando. */
export interface AgentProjectDeployOptions extends AgentProjectSandboxOptions {
  readonly accessToken?: string | undefined;
  readonly keepOnFailure?: boolean | undefined;
}

export interface AgentProjectRunOptions extends AgentProjectSandboxOptions {
  readonly sessionId?: string | undefined;
  readonly model?: string | undefined;
  readonly reasoning?: boolean | undefined;
  /** Recibe cada evento del agente a medida que llega (`--stream` de la CLI). */
  readonly onEvent?: ((event: AgentEvent) => void) | undefined;
}

/** Lo que el servicio usa de un `AgentStream`. */
export interface ServiceAgentStream extends AsyncIterable<AgentEvent> {
  result(): Promise<AgentResult>;
  close(): Promise<void>;
}

/** El `Sandbox` mínimo que usa el servicio; el real y los dobles de prueba lo
 * cumplen. */
export interface ServiceSandbox extends DeploymentSandbox {
  readonly agent: {
    stream(prompt: string, options: Record<string, unknown>): Promise<ServiceAgentStream>;
  };
  readonly commands: {
    run(cmd: string, options: Record<string, unknown>): Promise<{ disconnect(): void }>;
  };
  readonly files: {
    writeFiles(
      files: readonly { path: string; data: Uint8Array; mode?: number | undefined }[],
    ): Promise<unknown>;
    exists(path: string): Promise<boolean>;
    read(path: string): Promise<string>;
  };
  readonly gateways: ReturnType<AgentSandbox["gateways"]>;
}

/** `Sandbox.create()` de un doble o el real. */
export interface SandboxFactory {
  create(options: SandboxCreateOptions): Promise<ServiceSandbox>;
}

export interface ProjectServiceOptions {
  readonly sandboxFactory?: SandboxFactory | undefined;
  readonly fetch?: typeof fetch | undefined;
  readonly sleep?: ((ms: number) => Promise<void>) | undefined;
  readonly now?: (() => number) | undefined;
}

/** La imagen del proyecto: `template` o, por defecto, `name`. */
export function templateName(project: AgentProject): string {
  return project.template ?? project.name;
}

export function installableRuntime(project: AgentProject): InstallableRuntime {
  return requireInstallable(resolveRuntime(project.runtime));
}

export function servableRuntime(project: AgentProject): ServableRuntime {
  return requireServable(resolveRuntime(project.runtime));
}

/** El `AgentTemplate` de `project`: su runtime (ya comprobado como
 * instalable), su código y la memoria pedida. */
export function projectTemplate(project: AgentProject): AgentTemplate {
  installableRuntime(project);
  return new AgentTemplate({
    name: templateName(project),
    base: project.base,
    runtimes: [project.runtime],
    memoryMib: project.memoryMib,
    app: project.source,
    appExclude: project.exclude,
  });
}

/** `Sandbox.create()` mide la vida en segundos enteros: se redondea hacia
 * arriba, como `math.ceil` de la superficie de Python. */
export function ceilToSeconds(ms: number): number {
  return Math.ceil(ms / MS_PER_SECOND) * MS_PER_SECOND;
}

/** Vida del sandbox de `run()`: el plazo de la ejecución más un margen
 * (`AGENT_RUN_SANDBOX_MARGIN_SECONDS`). */
export function runTimeoutMs(project: AgentProject): number {
  return project.limits.timeoutMs + AGENT_RUN_SANDBOX_MARGIN_SECONDS * MS_PER_SECOND;
}

/** Plazo de una petición a un despliegue: el de una ejecución más el margen
 * del sandbox. */
export function requestTimeoutMs(project: AgentProject): number {
  return runTimeoutMs(project);
}

function commonCreateOptions(project: AgentProject): Partial<Mutable<SandboxCreateOptions>> {
  return {
    template: templateName(project),
    gateways: { ...project.gateways },
    allowInternetAccess: project.allowInternetAccess,
    ...(project.egress === undefined ? {} : { egress: [...project.egress] }),
  };
}

type Mutable<T> = { -readonly [K in keyof T]: T[K] };

/** Los argumentos de `Sandbox.create()` de `run()`: la imagen del proyecto,
 * sus pasarelas y su red. Sin `idle` ni etiquetas: el sandbox vive lo que
 * dura la ejecución. */
export function runCreateOptions(project: AgentProject): SandboxCreateOptions {
  return { timeoutMs: ceilToSeconds(runTimeoutMs(project)), ...commonCreateOptions(project) };
}

/** Los de `deploy()`: la vida y la política de `idle` de `project.serve` y
 * las etiquetas con las que `connect()` reconoce el despliegue. */
export function deployCreateOptions(
  project: AgentProject,
  options: { readonly raw: boolean },
): SandboxCreateOptions {
  const serve = project.serve;
  return {
    timeoutMs: ceilToSeconds(serve.timeoutMs),
    idle: serve.idle,
    metadata: { ...serve.metadata, ...labelsFor(project.name, serve.port, options) },
    ...commonCreateOptions(project),
  };
}

/** Una app `raw` (`HttpApp(protocol: "raw")`): sólo se expone. */
export function isRawApp(host: HostConfig): boolean {
  return host.driver === "http-app" && host.options.protocol === "raw";
}

/** Si el runtime del proyecto es una app `raw`, antes de crear el sandbox
 * (las etiquetas del despliegue lo dicen para `connect()`): `hostConfig()` es
 * puro, y con URLs de pasarela de relleno devuelve el mismo driver y las
 * mismas opciones. */
export function isRawProject(project: AgentProject): boolean {
  const urls = Object.fromEntries(
    [...project.spec.gatewayNames()].map((name) => [name, PLACEHOLDER_GATEWAY_URL]),
  );
  const host = servableRuntime(project).hostConfig(project.spec, {
    wiring: gatewayWiring(urls),
    workdir: DEFAULT_AGENT_WORKDIR,
  });
  return isRawApp(host);
}

/** La `HostConfig` del runtime con las URLs de las pasarelas del sandbox
 * recién creado; falla con `InvalidArgumentError` si falta una. */
export function hostConfigFor(project: AgentProject, sandbox: ServiceSandbox): HostConfig {
  const urls = gatewayUrlsFor(project.spec, sandbox.gateways);
  return servableRuntime(project).hostConfig(project.spec, {
    wiring: gatewayWiring(urls),
    workdir: DEFAULT_AGENT_WORKDIR,
  });
}

/** El `host.json`: el driver, sus opciones, los topes de `AgentLimits`, la
 * concurrencia y dónde dejar el marcador de listo. */
export function hostDocument(project: AgentProject, host: HostConfig): Record<string, unknown> {
  const limits = project.limits;
  return {
    schema: AGENT_HOST_SCHEMA,
    v: 1,
    driver: host.driver,
    options: { ...host.options },
    limits: {
      max_steps: limits.maxSteps,
      timeout_seconds: limits.timeoutMs / MS_PER_SECOND,
      max_output_bytes: limits.maxOutputBytes,
      max_total_tokens: limits.maxTotalTokens,
    },
    max_concurrency: project.serve.maxConcurrency,
    session_header: AGENT_SERVICE_SESSION_HEADER,
    ready_marker: AGENT_SERVICE_READY_MARKER_PATH,
  };
}

/** Los ficheros que `deploy()` escribe antes de arrancar el host: los del
 * runtime (con sus modos) y el `host.json`. */
export function hostEntries(
  project: AgentProject,
  host: HostConfig,
): { path: string; data: Uint8Array; mode?: number | undefined }[] {
  const entries: { path: string; data: Uint8Array; mode?: number | undefined }[] =
    host.files.files.map((file) => ({ path: file.path, data: file.data, mode: file.mode }));
  entries.push({
    path: AGENT_HOST_CONFIG_PATH,
    data: canonicalJson(hostDocument(project, host)),
    mode: HOST_DOC_MODE,
  });
  return entries;
}

/** El comando que arranca el host en modo servicio con el intérprete del
 * runtime. */
export function serveCommand(host: HostConfig, port: number): string {
  return (
    `exec ${shellQuote(host.python)} ${shellQuote(AGENT_HOST_DIR)} serve ` +
    `${shellQuote(AGENT_HOST_CONFIG_PATH)} --port ${port}`
  );
}

/** El mensaje de un arranque fallido a partir del contenido del marcador de
 * fallo: la primera línea es el código y, si hay, el resto es el final de la
 * salida de error de la app que el host ya saneó. Se vuelve a acotar aquí por
 * si el marcador viene de un host más viejo o de otro escritor. */
export function startupFailureMessage(marker: string): string {
  const trimmed = marker.trim();
  const newline = trimmed.indexOf("\n");
  const code = (newline < 0 ? trimmed : trimmed.slice(0, newline)).trim();
  const detail = newline < 0 ? "" : trimmed.slice(newline + 1).trim();
  const message = `el host del agente no pudo arrancar (${code === "" ? AGENT_HOST_FAILED_SUFFIX : code})`;
  const bytes = new TextEncoder().encode(detail);
  const tail = new TextDecoder("utf-8", { fatal: false })
    .decode(bytes.slice(-AGENT_STARTUP_DIAGNOSTICS_MAX_BYTES))
    .replaceAll("\uFFFD", "");
  return tail === "" ? message : `${message}. Final de la salida de error de la app:\n${tail}`;
}

/** `keepOnFailure` conserva un sandbox al que sólo se llega con su access
 * token: sin uno que el llamador conozca (opción o `RAYITO_ACCESS_TOKEN`),
 * conservarlo no sirve de nada. */
export function requireReconnectableToken(accessToken: string | undefined): void {
  if (!accessToken && !process.env[ACCESS_TOKEN_ENV_VAR]) {
    throw new InvalidArgumentError(
      "deploy({ keepOnFailure: true }) necesita accessToken (o " +
        `${ACCESS_TOKEN_ENV_VAR}) para poder conectarte al sandbox que se conserva`,
    );
  }
}

/** La nota que lleva el error de un despliegue con `keepOnFailure`: qué
 * sandbox quedó vivo y cómo terminarlo. Sólo el id, nunca el token. */
export function keptSandboxNote(sandbox: { readonly sandboxId: string }): string {
  return (
    `keepOnFailure: el sandbox ${sandbox.sandboxId} sigue vivo; conéctate con ` +
    "Sandbox.connect(sandboxId, { accessToken }) para depurarlo y termínalo con " +
    "Sandbox.kill(sandboxId) al acabar."
  );
}

export function failedMarkerPath(): string {
  return AGENT_SERVICE_READY_MARKER_PATH + AGENT_HOST_FAILED_SUFFIX;
}

/** Borra los marcadores de un host anterior antes de arrancar el nuevo: un
 * `ready` que sobreviva (una imagen o un guest reutilizado) haría que
 * `deploy()` diera por listo un host que aún no escucha. */
export function clearMarkersCommand(): string {
  return `rm -f ${shellQuote(AGENT_SERVICE_READY_MARKER_PATH)} ${shellQuote(failedMarkerPath())}`;
}

function sleepMs(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** Las tres operaciones que cuestan de un `AgentProject`. El proyecto, la
 * fábrica de sandboxes y el transporte llegan por constructor, así que los
 * tests usan dobles y nunca AWS. */
export class ProjectService {
  readonly #project: AgentProject;
  readonly #options: ProjectServiceOptions;

  constructor(project: AgentProject, options: ProjectServiceOptions = {}) {
    this.#project = project;
    this.#options = options;
  }

  /** Construye la imagen del proyecto con `AgentTemplate.build()`. */
  async build(options: AgentTemplateBuildOptions): Promise<BuildInfo> {
    return projectTemplate(this.#project).build(options);
  }

  /** Un sandbox desde la imagen del proyecto, una ejecución con
   * `sbx.agent.run()` y `kill()` siempre, también ante un error. */
  async run(prompt: string, options: AgentProjectRunOptions = {}): Promise<AgentResult> {
    const project = this.#project;
    const sandbox = await this.#create(runCreateOptions(project), options);
    try {
      const stream = await sandbox.agent.stream(prompt, {
        spec: project.spec,
        runtime: project.runtime,
        sessionId: options.sessionId,
        model: options.model,
        limits: project.limits,
        reasoning: options.reasoning ?? false,
      });
      try {
        if (options.onEvent !== undefined) {
          for await (const event of stream) {
            options.onEvent(event);
          }
        }
        return await stream.result();
      } finally {
        await stream.close();
      }
    } finally {
      await this.#kill(sandbox);
    }
  }

  /** Sirve el agente desde un MicroVM y devuelve su `AgentDeployment`. Si
   * cualquier paso falla, mata el sandbox antes de propagar (salvo con
   * `keepOnFailure`, que lo deja vivo y lo anota en el mensaje del error). */
  async deploy(options: AgentProjectDeployOptions = {}): Promise<AgentDeployment> {
    const project = this.#project;
    const { keepOnFailure = false, ...sandboxOptions } = options;
    if (keepOnFailure) {
      requireReconnectableToken(options.accessToken);
    }
    const sandbox = await this.#create(
      deployCreateOptions(project, { raw: isRawProject(project) }),
      sandboxOptions,
    );
    try {
      const host = hostConfigFor(project, sandbox);
      await sandbox.files.writeFiles(hostEntries(project, host));
      await sandbox.commands.run(clearMarkersCommand(), { user: AGENT_SERVE_USER });
      const handle = await sandbox.commands.run(serveCommand(host, project.serve.port), {
        background: true,
        user: AGENT_SERVE_USER,
        envs: { ...host.envs },
        timeoutMs: 0,
        tag: AGENT_SERVE_TAG,
        killTree: true,
      });
      handle.disconnect();
      await this.#waitReady(sandbox);
      return new AgentDeployment({
        sandbox,
        port: project.serve.port,
        hostAccess: await sandbox.getHost(project.serve.port),
        project: project.name,
        raw: isRawApp(host),
        requestTimeoutMs: requestTimeoutMs(project),
        fetch: this.#options.fetch,
      });
    } catch (error) {
      if (keepOnFailure && error instanceof Error) {
        error.message = `${error.message}\n${keptSandboxNote(sandbox)}`;
      } else {
        await this.#kill(sandbox);
      }
      throw error;
    }
  }

  async #create(
    createOptions: SandboxCreateOptions,
    options: AgentProjectSandboxOptions,
  ): Promise<ServiceSandbox> {
    const merged: SandboxCreateOptions = { ...createOptions, ...definedOnly(options) };
    const factory = this.#options.sandboxFactory;
    if (factory !== undefined) {
      return factory.create(merged);
    }
    const { Sandbox } = await import("../sandbox/sandbox.js");
    return (await Sandbox.create(merged)) as unknown as ServiceSandbox;
  }

  /** Espera al marcador de listo del host: `TimeoutError` al agotar
   * `AGENT_SERVICE_READY_TIMEOUT_SECONDS` y `SandboxError` si el host dejó un
   * marcador de arranque fallido. */
  async #waitReady(sandbox: ServiceSandbox): Promise<void> {
    const now = this.#options.now ?? Date.now;
    const sleep = this.#options.sleep ?? sleepMs;
    const deadline = now() + AGENT_SERVICE_READY_TIMEOUT_SECONDS * MS_PER_SECOND;
    for (;;) {
      if (await sandbox.files.exists(AGENT_SERVICE_READY_MARKER_PATH)) {
        return;
      }
      if (await sandbox.files.exists(failedMarkerPath())) {
        throw new SandboxError(startupFailureMessage(await sandbox.files.read(failedMarkerPath())));
      }
      if (now() >= deadline) {
        throw new TimeoutError("el host del agente no estuvo listo a tiempo");
      }
      await sleep(AGENT_SERVICE_READY_POLL_MS);
    }
  }

  /** Mata el sandbox sin tapar la excepción original; un fallo del propio
   * `kill()` se descarta (sin datos del sandbox). */
  async #kill(sandbox: ServiceSandbox): Promise<void> {
    try {
      await sandbox.kill();
    } catch {
      return;
    }
  }
}

function definedOnly<T extends object>(value: T): Partial<T> {
  return Object.fromEntries(
    Object.entries(value).filter(([, entry]) => entry !== undefined),
  ) as Partial<T>;
}
