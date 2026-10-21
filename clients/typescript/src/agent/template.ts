/**
 * `AgentTemplate`: la plantilla de imagen con los runtimes de agente de IA
 * (`ai-agent-fast-start`, design.md §5). Misma receta que el SDK de Python
 * (`rayito/_agent/_template.py`; vectores compartidos en
 * `testdata/agent/agent-template/cases.json`; ADR-028): la imagen se compone
 * con los `installSteps()` de cada runtime (el puerto `InstallableRuntime`):
 * OpenCode y ripgrep fijados por versión y sha256 (`limits.json`), el venv de
 * deepagents con `--require-hashes`, todo de root con 0755, el manifiesto
 * `rayito.agent-template/1`, el código del usuario (`app`) en `AGENT_APP_DIR`
 * y, con `prefetch`, el demonio de precarga como `startCmd`.
 *
 * La composición es pura (`toTemplate()`, `manifest()`); sólo `build()`
 * escribe el contexto en un directorio temporal y llama a `Template.build`.
 */

import { statSync } from "node:fs";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { InvalidArgumentError, UnimplementedError } from "../errors.js";
import {
  AGENT_KERNEL_WARMUP_MARKER_PATH,
  AGENT_KERNEL_WARMUP_SLIM_VALUE,
  AGENT_MIN_MEMORY_MIB,
  AGENT_PREFETCH_BUILD_MARKER_PATH,
  AGENT_PREFETCH_BUILD_TIMEOUT_SECONDS,
  AGENT_PREFETCH_INTERVAL_SECONDS,
  AGENT_PREFETCH_RESTORE_JUMP_SECONDS,
  AGENT_PROTOCOL_VERSION,
  AGENT_TEMPLATE_MANIFEST_PATH,
  AGENT_TEMPLATE_MANIFEST_SCHEMA,
  AGENT_TOKEN_FILE_DEFAULT,
} from "../limits.js";
import type { BuildInfo, BuildOptions } from "../templates/build.js";
import { collectContextFiles, collectContextFilesSync } from "../templates/context-node.js";
import { Template } from "../templates/dsl.js";
import { type ReadyCommand, waitForFile } from "../templates/ready-cmds.js";
import { OPENCODE_FLAG_ENVS } from "./opencode.js";
import {
  AGENT_APP_CONTEXT_DIR,
  AGENT_APP_DIR,
  AGENT_BIN_DIR,
  AGENT_HOST_CONTEXT_DIR,
  AGENT_HOST_DIR,
  AGENT_INSTALL_DIR,
  agentHostFiles,
  hostSha256,
  PREFETCH_SCRIPT_NAME,
  PREFETCH_SCRIPT_PATH,
  prefetchScript,
} from "./recipes.js";
import {
  type AgentRuntime,
  type InstallableRuntime,
  installTarget,
  requireInstallable,
  STEP_COPY,
  STEP_RUN,
  type TemplateStep,
} from "./runtime.js";
import { RUNTIME_KINDS, resolveRuntime } from "./runtimes.js";

/** Nombre por defecto de la imagen que construye `AgentTemplate`. */
export const DEFAULT_AGENT_TEMPLATE_NAME = "rayito-agent";
/** Imagen base: la variante con capabilities, la única donde `rayd` aplica
 * el deny-all de egress que el agente necesita. */
export const DEFAULT_AGENT_TEMPLATE_BASE = "rayito-base-caps";
/** Runtimes que la plantilla instala por defecto. */
export const AGENT_TEMPLATE_RUNTIMES = Object.freeze(["opencode", "deepagents"] as const);
/** @deprecated Desde 0.14: `AgentTemplate` acepta nombres del registro de
 * runtimes y objetos `AgentRuntime`; usa `string | AgentRuntime`. Se retira en
 * una versión posterior. */
export type AgentTemplateRuntime = (typeof AGENT_TEMPLATE_RUNTIMES)[number];

const MANIFEST_CONTEXT_NAME = "rayito-agent.json";
/** Las claves del manifiesto que existían antes de ADR-028 y que el demonio
 * de precarga y los SDK anteriores leen. */
const LEGACY_OPENCODE_RUNTIME = "opencode";
const LEGACY_DEEPAGENTS_RUNTIME = "deepagents";
/** De root y sin escritura para grupo ni otros. */
const PERMISSIONS_CMD = `chown -R root:root ${AGENT_INSTALL_DIR} && chmod -R a+rX,go-w ${AGENT_INSTALL_DIR}`;

export interface AgentTemplateOptions {
  readonly name?: string | undefined;
  readonly base?: string | undefined;
  /** Nombres del registro u objetos que implementen `InstallableRuntime`. */
  readonly runtimes?: readonly (string | AgentRuntime)[] | undefined;
  readonly prefetch?: boolean | undefined;
  readonly kernelWarmup?: boolean | undefined;
  readonly memoryMib?: number | undefined;
  readonly baseVersion?: string | undefined;
  /** Directorio local con el código del agente, copiado a `AGENT_APP_DIR`. */
  readonly app?: string | undefined;
  /** Patrones de `.dockerignore` que se suman a los del propio `app`. */
  readonly appExclude?: readonly string[] | undefined;
}

/** Opciones de `AgentTemplate.build()`: las de `Template.build` salvo
 * `memoryMb` (sale de `memoryMib`) y `contextDir` (lo escribe la plantilla). */
export type AgentTemplateBuildOptions = Omit<BuildOptions, "memoryMb" | "contextDir">;

/** El manifiesto `rayito.agent-template/1` que se hornea en la imagen. */
export interface AgentTemplateManifest {
  readonly schema: string;
  readonly protocol: number;
  readonly opencode: Readonly<Record<string, unknown>> | null;
  readonly deepagents: { readonly requirements_sha256: unknown } | null;
  readonly runner_sha256: unknown;
  readonly prefetch_paths: readonly string[];
  readonly runtimes: Readonly<Record<string, Readonly<Record<string, unknown>>>>;
  readonly host_sha256: string | null;
}

/** Apaga el calentamiento del kernel de `runCode` (el marcador que lee
 * `0004_warmup.py` del sidecar, el mismo que escribe `image_zip.py
 * --variant slim`). */
function slimKernelCmd(): string {
  return (
    `printf '%s\\n' ${AGENT_KERNEL_WARMUP_SLIM_VALUE} > ${AGENT_KERNEL_WARMUP_MARKER_PATH}` +
    ` && chmod 0644 ${AGENT_KERNEL_WARMUP_MARKER_PATH}`
  );
}

/** El `startCmd` del demonio de precarga, con sus cuatro argumentos: el
 * cuarto le hace precargar una vez antes del snapshot del build. */
export function prefetchStartCmd(): string {
  return (
    `${PREFETCH_SCRIPT_PATH} ${AGENT_TEMPLATE_MANIFEST_PATH}` +
    ` ${AGENT_PREFETCH_RESTORE_JUMP_SECONDS} ${AGENT_PREFETCH_INTERVAL_SECONDS}` +
    ` ${AGENT_PREFETCH_BUILD_MARKER_PATH}`
  );
}

/** El `readyCmd` que retiene el snapshot del build hasta que el demonio
 * terminó su precarga inicial. */
export function prefetchReadyCmd(): ReadyCommand {
  return waitForFile(AGENT_PREFETCH_BUILD_MARKER_PATH).timeout(
    AGENT_PREFETCH_BUILD_TIMEOUT_SECONDS,
  );
}

function manifestText(manifest: AgentTemplateManifest): string {
  return `${JSON.stringify(sortKeys(manifest), null, 2)}\n`;
}

function sortKeys(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map(sortKeys);
  }
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(
      Object.keys(value)
        .sort()
        .map((key) => [key, sortKeys((value as Record<string, unknown>)[key])]),
    );
  }
  return value;
}

function installable(item: string | AgentRuntime | InstallableRuntime): InstallableRuntime {
  if (typeof item === "string") {
    let runtime: unknown;
    try {
      runtime = resolveRuntime(item);
    } catch (error) {
      if (
        error instanceof UnimplementedError &&
        !(RUNTIME_KINDS as readonly string[]).includes(item)
      ) {
        throw new InvalidArgumentError(
          `runtime de plantilla desconocido: ${JSON.stringify(item)} ` +
            `(admitidos: ${RUNTIME_KINDS.join(", ")})`,
        );
      }
      throw error;
    }
    return requireInstallable(runtime);
  }
  return requireInstallable(item);
}

/**
 * Plantilla de imagen con los runtimes del agente de IA.
 *
 * `runtimes` elige qué se instala: nombres del registro (`"opencode"`,
 * `"deepagents"`, `"langgraph"`...) u objetos que implementen
 * `InstallableRuntime`; la imagen se compone con los `installSteps()` de cada
 * uno. `app` es un directorio local con el código del agente: se copia a
 * `AGENT_APP_DIR` (de root, sin escritura para `user`) con las reglas de su
 * `.dockerignore` más `appExclude` (patrones de `.dockerignore`).
 * `prefetch: true` hornea el demonio que precarga los binarios en la caché
 * de páginas tras cada restauración del snapshot. `kernelWarmup: false`
 * (por defecto) apaga el calentamiento del kernel de `runCode` (numpy,
 * pandas, matplotlib, scipy y scikit-learn importados en cada arranque del
 * kernel, también en la rotación que `Sandbox.create()` espera): medido en
 * AWS (`AWS_API_NOTES.md` Q155), `create()` vuelve 4,4 s antes (p50) y el
 * snapshot de memoria pesa 248 MB menos, con el mismo primer token del
 * agente; a cambio, la primera celda de `runCode` que importe ese stack paga
 * la importación. `kernelWarmup: true` deja el kernel como en la imagen
 * base. `memoryMib` por debajo de `AGENT_MIN_MEMORY_MIB` (2048) es
 * `InvalidArgumentError`.
 *
 * Coste y activación
 * -------------------
 * Activa: `new AgentTemplate(...).build({ bucket })`.
 * Recursos y llamadas AWS: los de `Template.build` (un build de imagen,
 *   `s3:PutObject` del contexto, una versión de imagen nueva); nada si no se
 *   construye.
 * Coste aproximado: build de 260–320 s (medido en AWS, 2026-10-07 y
 *   2026-10-09); la versión almacenada ≈ 3,0 GB (código 2,10 + memoria 0,86 +
 *   disco 0,03; memoria 0,92 con `kernelWarmup: true`) por $0,08/GB-mes, con
 *   el mínimo de una semana ≈ $0,056/semana (≈ $0,24/mes) por versión, y cada
 *   lanzamiento lee el snapshot de memoria (≈ 0,86 GB por $0,00155/GB ≈
 *   $0,0013). Estimación con precios de lista de Lambda
 *   MicroVMs, us-east-1, consultados 2026-10-06
 *   (https://aws.amazon.com/lambda/pricing/).
 * IAM: la política `RayitoTemplateBuilder` (la misma que `Template.build`).
 * Cómo apagarla: no la construyas; borra sus versiones con `rayito image`.
 * Ejemplo:
 *   await new AgentTemplate({ runtimes: ["opencode"] }).build({ bucket: "amzn-s3-demo-bucket" });
 */
export class AgentTemplate {
  readonly name: string;
  readonly base: string;
  readonly runtimes: readonly (string | AgentRuntime)[];
  readonly prefetch: boolean;
  readonly kernelWarmup: boolean;
  readonly memoryMib: number;
  readonly baseVersion: string | undefined;
  readonly app: string | undefined;
  readonly appExclude: readonly string[];

  constructor(options: AgentTemplateOptions = {}) {
    const memoryMib = options.memoryMib ?? AGENT_MIN_MEMORY_MIB;
    if (typeof memoryMib !== "number" || !Number.isInteger(memoryMib)) {
      throw new InvalidArgumentError("memoryMib debe ser un entero");
    }
    if (memoryMib < AGENT_MIN_MEMORY_MIB) {
      throw new InvalidArgumentError(
        `memoryMib debe ser >= ${AGENT_MIN_MEMORY_MIB} para el agente de IA, recibido ${memoryMib}`,
      );
    }
    const runtimes = [...(options.runtimes ?? AGENT_TEMPLATE_RUNTIMES)];
    if (runtimes.length === 0) {
      throw new InvalidArgumentError("runtimes no puede ser vacío");
    }
    const names = runtimes.map((item) => installable(item).name);
    if (new Set(names).size !== names.length) {
      throw new InvalidArgumentError("runtimes no puede repetir un nombre");
    }
    const name = options.name ?? DEFAULT_AGENT_TEMPLATE_NAME;
    const base = options.base ?? DEFAULT_AGENT_TEMPLATE_BASE;
    if (!name || !base) {
      throw new InvalidArgumentError("name y base no pueden ser vacíos");
    }
    if (options.app !== undefined && !isDirectory(options.app)) {
      throw new InvalidArgumentError("app debe ser un directorio existente");
    }
    this.name = name;
    this.base = base;
    this.runtimes = Object.freeze(runtimes);
    this.prefetch = options.prefetch ?? true;
    this.kernelWarmup = options.kernelWarmup ?? false;
    this.memoryMib = memoryMib;
    this.baseVersion = options.baseVersion;
    this.app = options.app;
    this.appExclude = Object.freeze([...(options.appExclude ?? [])]);
  }

  /** Los runtimes de la plantilla, ya resueltos. */
  installables(): readonly InstallableRuntime[] {
    return this.runtimes.map(installable);
  }

  /** El manifiesto que se hornea en `AGENT_TEMPLATE_MANIFEST_PATH`: versiones
   * y hashes de lo instalado, protocolo, las rutas que precarga el demonio y,
   * desde ADR-028, la entrada de cada runtime (`runtimes`) y el sha256 del
   * host del agente (`host_sha256`). */
  manifest(): AgentTemplateManifest {
    const runtimes = this.installables();
    const entries = Object.fromEntries(
      runtimes.map((runtime) => [runtime.name, runtime.manifestEntry()]),
    );
    const deepagents = entries[LEGACY_DEEPAGENTS_RUNTIME];
    return {
      schema: AGENT_TEMPLATE_MANIFEST_SCHEMA,
      protocol: AGENT_PROTOCOL_VERSION,
      opencode: entries[LEGACY_OPENCODE_RUNTIME] ?? null,
      deepagents:
        deepagents === undefined ? null : { requirements_sha256: deepagents.requirements_sha256 },
      runner_sha256: deepagents === undefined ? null : deepagents.runner_sha256,
      prefetch_paths: runtimes.flatMap((runtime) => [...runtime.prefetchPaths()]),
      runtimes: entries,
      host_sha256: hostSha256(agentHostFiles()) ?? null,
    };
  }

  /** Los ficheros del contexto de build (nombre relativo -> texto). El
   * código del usuario (`app`) no entra aquí: lo escribe `build()`. */
  contextFiles(): Record<string, string> {
    const files: Record<string, string> = {
      [MANIFEST_CONTEXT_NAME]: manifestText(this.manifest()),
    };
    for (const runtime of this.installables()) {
      Object.assign(files, runtime.contextFiles());
    }
    for (const [relative, text] of Object.entries(agentHostFiles())) {
      files[`${AGENT_HOST_CONTEXT_DIR}/${relative}`] = text;
    }
    if (this.prefetch) {
      files[PREFETCH_SCRIPT_NAME] = prefetchScript();
    }
    return files;
  }

  /** El `Template` equivalente (puro: no lee ni escribe nada). */
  toTemplate(): Template {
    let tpl = new Template().fromBaseImage(this.base, this.baseVersion);
    const executables: string[] = [];
    const target = installTarget();
    for (const runtime of this.installables()) {
      const applied = applySteps(tpl, runtime.installSteps(target));
      tpl = applied.template;
      executables.push(...applied.executables);
    }
    tpl = tpl.copy(MANIFEST_CONTEXT_NAME, AGENT_TEMPLATE_MANIFEST_PATH);
    if (this.prefetch) {
      tpl = tpl.copy(PREFETCH_SCRIPT_NAME, PREFETCH_SCRIPT_PATH);
      executables.push(PREFETCH_SCRIPT_PATH);
    }
    if (Object.keys(agentHostFiles()).length > 0) {
      tpl = tpl.copy(AGENT_HOST_CONTEXT_DIR, AGENT_HOST_DIR);
    }
    if (this.app !== undefined) {
      tpl = tpl.copy(AGENT_APP_CONTEXT_DIR, AGENT_APP_DIR);
    }
    tpl = tpl.runCmd(
      `mkdir -p ${AGENT_BIN_DIR} && ${PERMISSIONS_CMD}` +
        (executables.length > 0 ? ` && chmod 0755 ${executables.join(" ")}` : ""),
    );
    if (!this.kernelWarmup) {
      tpl = tpl.runCmd(slimKernelCmd());
    }
    tpl = tpl.setEnvs({ ...OPENCODE_FLAG_ENVS });
    const smoke = this.installables()
      .flatMap((runtime) => [...runtime.smokeChecks()])
      .join(" && ");
    if (smoke !== "") {
      tpl = tpl.runCmd(smoke);
    }
    if (this.prefetch) {
      tpl = tpl.setStartCmd(prefetchStartCmd(), prefetchReadyCmd());
    }
    return tpl;
  }

  toDockerfile(): string {
    return this.toTemplate().toDockerfile();
  }

  /** Construye la imagen `name` con `Template.build` (ver el bloque de coste
   * de la clase). */
  async build(options: AgentTemplateBuildOptions): Promise<BuildInfo> {
    const contextDir = await mkdtemp(join(tmpdir(), "rayito-agent-"));
    try {
      await writeContext(contextDir, this.contextFiles(), this.app, this.appExclude);
      return await Template.build(this.toTemplate(), this.name, {
        ...options,
        memoryMb: this.memoryMib,
        contextDir,
      });
    } finally {
      await rm(contextDir, { recursive: true, force: true });
    }
  }
}

function isDirectory(path: string): boolean {
  try {
    return statSync(path).isDirectory();
  } catch {
    return false;
  }
}

/** Aplica los pasos de un runtime al `Template`; devuelve también los
 * destinos que deben quedar 0755. */
function applySteps(
  template: Template,
  steps: readonly TemplateStep[],
): { readonly template: Template; readonly executables: string[] } {
  let tpl = template;
  const executables: string[] = [];
  for (const step of steps) {
    const args = step.args ?? {};
    if (step.op === STEP_RUN) {
      tpl = tpl.runCmd(String(args.cmd));
    } else if (step.op === STEP_COPY) {
      const destination = String(args.dst);
      tpl = tpl.copy(String(args.src), destination);
      if (args.executable === true) {
        executables.push(destination);
      }
    }
  }
  return { template: tpl, executables };
}

const APP_COPY_STEPS = [{ kind: "copy", src: ".", dst: AGENT_APP_DIR }] as const;

/** El directorio de estado de la CLI (`.rayito/`: el access token de la flota
 * y las plazas calientes), a cualquier profundidad. Se excluye siempre del
 * código del usuario y va el último, así que ni su `.dockerignore` ni
 * `exclude` pueden volver a meterlo en la imagen ni en su hash. */
export const AGENT_STATE_DIR_EXCLUDE = `**/${AGENT_TOKEN_FILE_DEFAULT.split("/")[0]}`;

/** Los ficheros del código del usuario, sin lo que ignoran su `.dockerignore`,
 * `exclude` y `AGENT_STATE_DIR_EXCLUDE`. */
export async function appFiles(
  app: string,
  exclude: readonly string[],
): Promise<Array<[string, Uint8Array]>> {
  return collectContextFiles(app, APP_COPY_STEPS, [...exclude, AGENT_STATE_DIR_EXCLUDE]);
}

/** `appFiles` con lecturas síncronas (los constructos CDK, ADR-032 D7). */
export function appFilesSync(app: string, exclude: readonly string[]): Array<[string, Uint8Array]> {
  return collectContextFilesSync(app, APP_COPY_STEPS, [...exclude, AGENT_STATE_DIR_EXCLUDE]);
}

async function writeContext(
  directory: string,
  files: Readonly<Record<string, string>>,
  app: string | undefined,
  exclude: readonly string[],
): Promise<void> {
  for (const [name, text] of Object.entries(files)) {
    const target = join(directory, name);
    await mkdir(dirname(target), { recursive: true });
    await writeFile(target, text, "utf8");
  }
  if (app !== undefined) {
    await mkdir(join(directory, AGENT_APP_CONTEXT_DIR), { recursive: true });
    for (const [relative, data] of await appFiles(app, exclude)) {
      const target = join(directory, AGENT_APP_CONTEXT_DIR, relative);
      await mkdir(dirname(target), { recursive: true });
      await writeFile(target, data);
    }
  }
}
