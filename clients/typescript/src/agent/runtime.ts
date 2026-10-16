/**
 * Puerto `AgentRuntime` (`ai-agent-core`, ADR-025, design.md §3). Espejo de
 * `rayito._agent._runtime`: lo que un adaptador de runtime (OpenCode,
 * deepagents) traduce de un `AgentSpec` a ficheros de configuración, a un
 * comando y de vuelta a eventos. Puro: sin `grpc`/Connect, `@aws-sdk/*` ni
 * reloj.
 *
 * El adaptador de OpenCode vive en `opencode.ts`; un llamante también puede
 * pasar su propio objeto que cumpla `AgentRuntime` como `runtime`.
 *
 * Junto a `AgentRuntime`, ADR-028 añade dos puertos opcionales:
 * `InstallableRuntime` (qué entra en la imagen) y `ServableRuntime` (cómo se
 * sirve). La firma de `AgentRuntime` no cambia, así que un objeto escrito
 * para 0.13 sigue funcionando.
 */

import { InvalidArgumentError } from "../errors.js";
import type { AgentSpec } from "./domain.js";
import type { AgentEvent, AgentFailed, Done } from "./events.js";

/** Un fichero de configuración del runtime. */
export interface RuntimeFile {
  readonly path: string;
  readonly data: Uint8Array;
  readonly mode?: number | undefined;
}

/** Lo que `buildConfig()` devuelve: los ficheros a escribir y un sha256 de
 * su contenido conjunto, para que el servicio de aplicación se salte
 * `filesWrite` cuando ya está aplicado (mismo sha que la última vez). */
export interface RuntimeFiles {
  readonly files: readonly RuntimeFile[];
  readonly configSha256: string;
}

export interface RunRequest {
  readonly spec: AgentSpec;
  readonly prompt: string;
  readonly workdir: string;
  readonly sessionId?: string | undefined;
  readonly model?: string | undefined;
  readonly reasoning?: boolean | undefined;
}

/** El comando que ejecuta la petición: texto de script de bash (nunca un
 * secreto), sus variables de entorno y el stdin a mandarle (el prompt). */
export interface RunCommand {
  readonly script: string;
  readonly envs?: Readonly<Record<string, string>> | undefined;
  readonly stdin: Uint8Array;
}

/** Operaciones que un `TemplateStep` puede pedir a `AgentTemplate`. */
export const STEP_RUN = "run";
export const STEP_COPY = "copy";

/**
 * Un paso DSL que `AgentTemplate` añade a la plantilla. `op` es `STEP_RUN`
 * (`args: { cmd }`, un `RUN`) o `STEP_COPY` (`args: { src, dst }` más
 * `executable: true` si el destino debe quedar 0755, un fichero o directorio
 * del contexto de build).
 */
export interface TemplateStep {
  readonly op: string;
  readonly args?: Readonly<Record<string, unknown>> | undefined;
}

/** Un paso `RUN`. */
export function runStep(cmd: string): TemplateStep {
  return Object.freeze({ op: STEP_RUN, args: Object.freeze({ cmd }) });
}

/** Herramientas de descarga que una imagen Debian de contenedor no trae. */
export const DOWNLOAD_PACKAGES = ["curl", "ca-certificates"] as const;

/** El `RUN` de `apt-get` con las herramientas de build que una imagen Debian
 * de un contenedor exportado no trae; vacío en un MicroVM. */
export function containerPackagesStep(
  target: InstallTarget,
  packages: readonly string[],
): readonly TemplateStep[] {
  if (target.platform !== "container") {
    return [];
  }
  return [
    runStep(
      "apt-get update && apt-get install -y --no-install-recommends " +
        `${packages.join(" ")} && rm -rf /var/lib/apt/lists/*`,
    ),
  ];
}

/** Un paso `COPY` de un fichero o directorio del contexto de build. */
export function copyStep(
  src: string,
  dst: string,
  options: { readonly executable?: boolean | undefined } = {},
): TemplateStep {
  const args: Record<string, unknown> = { src, dst };
  if (options.executable === true) {
    args.executable = true;
  }
  return Object.freeze({ op: STEP_COPY, args: Object.freeze(args) });
}

/** Un paso de calentamiento: `agent.prepare()` y el `warmup` de un pool
 * corren la misma lista. `background: true` no espera a que termine (un
 * proceso que sigue vivo); si no, se espera su salida con `timeoutMs`. */
export interface WarmupStep {
  readonly cmd: string;
  readonly background?: boolean | undefined;
  readonly timeoutMs?: number | undefined;
  readonly tag?: string | undefined;
}

/** Estado mutable que un adaptador acumula entre llamadas a `parseLine()`;
 * opaco para el servicio de aplicación. */
// biome-ignore lint/suspicious/noEmptyInterface: marcador opaco, como RuntimeState en Python.
export interface RuntimeState {}

/** Lo que un adaptador de runtime implementa. `name` identifica el
 * adaptador en los eventos de telemetría (`gen_ai.agent.name`). */
export interface AgentRuntime {
  readonly name: string;
  buildConfig(
    spec: AgentSpec,
    options: { readonly gatewayUrls: Readonly<Record<string, string>>; readonly workdir: string },
  ): RuntimeFiles;
  command(request: RunRequest): RunCommand;
  newState(): RuntimeState;
  parseLine(line: Uint8Array, state: RuntimeState): readonly AgentEvent[];
  finish(state: RuntimeState, exitCode: number): Done | AgentFailed;
  templateSteps(): readonly TemplateStep[];
  warmupSteps(): readonly WarmupStep[];
}

/**
 * Los drivers del host del agente (ADR-028, design.md D3): lista cerrada.
 * `strands-ts` es el nombre antiguo de `node-callable` con el adaptador
 * `strands` y sólo se mantiene para leer una configuración ya escrita.
 */
export const HOST_DRIVERS = [
  "deepagents",
  "opencode",
  "strands",
  "strands-ts",
  "node-callable",
  "hermes",
  "callable",
  "command",
  "http-app",
] as const;
export type HostDriver = (typeof HOST_DRIVERS)[number];
/** Dónde se instala: un MicroVM de Rayito o un contenedor exportado. */
export const INSTALL_PLATFORMS = ["microvm", "container"] as const;
export type InstallPlatform = (typeof INSTALL_PLATFORMS)[number];
/** El intérprete con el que `InstallTarget` crea los venvs por defecto. */
export const DEFAULT_INSTALL_PYTHON = "python3";
/** Raíz de todo lo que una plantilla instala (root, 0755). */
export const DEFAULT_INSTALL_DIR = "/opt/agents";

/** Dónde instala un `InstallableRuntime`: descripción pura, sin E/S. */
export interface InstallTarget {
  readonly platform: InstallPlatform;
  readonly python: string;
  readonly installDir: string;
}

/** Un `InstallTarget`, validado: por defecto un MicroVM con `python3` y
 * `/opt/agents`. */
export function installTarget(options: Partial<InstallTarget> = {}): InstallTarget {
  const target: InstallTarget = {
    platform: options.platform ?? "microvm",
    python: options.python ?? DEFAULT_INSTALL_PYTHON,
    installDir: options.installDir ?? DEFAULT_INSTALL_DIR,
  };
  if (!(INSTALL_PLATFORMS as readonly string[]).includes(target.platform)) {
    throw new InvalidArgumentError(
      `InstallTarget.platform debe ser uno de ${JSON.stringify(INSTALL_PLATFORMS)}`,
    );
  }
  if (!target.python || !target.installDir.startsWith("/")) {
    throw new InvalidArgumentError(
      "InstallTarget.python no puede ser vacío e installDir debe ser una ruta absoluta",
    );
  }
  return Object.freeze(target);
}

/** El modelo llega por las pasarelas del sandbox: `urls` por nombre de
 * pasarela (MicroVM). El agente sólo ve un marcador como clave. */
export interface GatewayWiring {
  readonly kind: "gateway";
  readonly urls: Readonly<Record<string, string>>;
}

/** Sin pasarela (contenedor exportado): el host lee la credencial de la
 * fuente estándar del proveedor. La garantía de la pasarela no aplica. */
export interface DirectWiring {
  readonly kind: "direct";
}

export type ModelWiring = GatewayWiring | DirectWiring;

/** El cableado por pasarela de un MicroVM. */
export function gatewayWiring(urls: Readonly<Record<string, string>>): GatewayWiring {
  return Object.freeze({ kind: "gateway", urls: Object.freeze({ ...urls }) });
}

/** El cableado sin pasarela de un contenedor exportado. */
export function directWiring(): DirectWiring {
  return Object.freeze({ kind: "direct" });
}

/** Las URLs de pasarela de `wiring`, o `undefined` con `DirectWiring`. */
export function wiringUrls(wiring: ModelWiring): Readonly<Record<string, string>> | undefined {
  return wiring.kind === "gateway" ? wiring.urls : undefined;
}

/**
 * Lo que el host del agente necesita para servir un runtime: el `driver`
 * (lista cerrada `HOST_DRIVERS`), el intérprete `python` que ejecuta el host,
 * los ficheros a escribir antes de arrancar, las variables de entorno y las
 * opciones propias del driver.
 */
export interface HostConfig {
  readonly driver: HostDriver;
  readonly python: string;
  readonly files: RuntimeFiles;
  readonly envs: Readonly<Record<string, string>>;
  readonly options: Readonly<Record<string, unknown>>;
}

/** Un `HostConfig`, validado. */
export function hostConfig(
  value: Omit<HostConfig, "envs" | "options"> & Partial<Pick<HostConfig, "envs" | "options">>,
): HostConfig {
  if (!(HOST_DRIVERS as readonly string[]).includes(value.driver)) {
    throw new InvalidArgumentError(
      `HostConfig.driver debe ser uno de ${JSON.stringify(HOST_DRIVERS)}, recibido ${JSON.stringify(value.driver)}`,
    );
  }
  return Object.freeze({
    driver: value.driver,
    python: value.python,
    files: value.files,
    envs: Object.freeze({ ...value.envs }),
    options: Object.freeze({ ...value.options }),
  });
}

/** Puerto opcional: lo que un runtime pone en la imagen. `AgentTemplate`
 * compone la imagen con los `installSteps()` de cada runtime. */
export interface InstallableRuntime {
  readonly name: string;
  installSteps(target: InstallTarget): readonly TemplateStep[];
  contextFiles(): Readonly<Record<string, string>>;
  manifestEntry(): Readonly<Record<string, unknown>>;
  smokeChecks(): readonly string[];
  prefetchPaths(): readonly string[];
}

/** Puerto opcional: cómo el host sirve un runtime (`hostConfig`). */
export interface ServableRuntime {
  readonly name: string;
  hostConfig(
    spec: AgentSpec,
    options: { readonly wiring: ModelWiring; readonly workdir: string },
  ): HostConfig;
}

function hasMethods(candidate: unknown, methods: readonly string[]): boolean {
  if (typeof candidate !== "object" || candidate === null) {
    return false;
  }
  const record = candidate as Record<string, unknown>;
  return methods.every((method) => typeof record[method] === "function");
}

const INSTALLABLE_METHODS = [
  "installSteps",
  "contextFiles",
  "manifestEntry",
  "smokeChecks",
  "prefetchPaths",
] as const;

/** `runtime` implementa `InstallableRuntime`. */
export function isInstallable(runtime: unknown): runtime is InstallableRuntime {
  return hasMethods(runtime, INSTALLABLE_METHODS);
}

/** `runtime` implementa `ServableRuntime`. */
export function isServable(runtime: unknown): runtime is ServableRuntime {
  return hasMethods(runtime, ["hostConfig"]);
}

/** Falla con `InvalidArgumentError` nombrando el puerto que falta
 * (`AgentTemplate`, `AgentProject.build`). */
export function requireInstallable(runtime: unknown): InstallableRuntime {
  if (!isInstallable(runtime)) {
    throw new InvalidArgumentError(
      "el runtime no implementa InstallableRuntime: no puede entrar en una plantilla",
    );
  }
  return runtime;
}

/** Falla con `InvalidArgumentError` nombrando el puerto que falta
 * (`AgentProject.deploy`, `AgentProject.export`). */
export function requireServable(runtime: unknown): ServableRuntime {
  if (!isServable(runtime)) {
    throw new InvalidArgumentError(
      "el runtime no implementa ServableRuntime: no puede desplegarse ni exportarse",
    );
  }
  return runtime;
}
