/**
 * `AgentTemplate`: la plantilla de imagen con los runtimes de agente de IA
 * (`ai-agent-fast-start`, design.md §5). Misma receta que el SDK de Python
 * (`rayito/_agent/_template.py`; vectores compartidos en
 * `testdata/agent/agent-template/cases.json`): OpenCode y ripgrep fijados por
 * versión y sha256 (`limits.json`), el venv de deepagents con
 * `--require-hashes`, todo de root con 0755, el manifiesto
 * `rayito.agent-template/1` y, con `prefetch`, el demonio de precarga como
 * `startCmd`.
 *
 * La composición es pura (`toTemplate()`, `manifest()`); sólo `build()`
 * escribe el contexto en un directorio temporal y llama a `Template.build`.
 */

import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { InvalidArgumentError } from "../errors.js";
import {
  AGENT_DEEPAGENTS_REQUIREMENTS_SHA256,
  AGENT_MIN_MEMORY_MIB,
  AGENT_OPENCODE_SHA256,
  AGENT_OPENCODE_VERSION,
  AGENT_PREFETCH_INTERVAL_SECONDS,
  AGENT_PREFETCH_RESTORE_JUMP_SECONDS,
  AGENT_PROTOCOL_VERSION,
  AGENT_RIPGREP_SHA256,
  AGENT_RIPGREP_VERSION,
  AGENT_TEMPLATE_MANIFEST_PATH,
  AGENT_TEMPLATE_MANIFEST_SCHEMA,
} from "../limits.js";
import type { BuildInfo, BuildOptions } from "../templates/build.js";
import { Template } from "../templates/dsl.js";
import { DEEPAGENTS_REQUIREMENTS, PREFETCH_SCRIPT } from "./assets/template-assets.gen.js";
import { OPENCODE_FLAG_ENVS } from "./opencode.js";

/** Nombre por defecto de la imagen que construye `AgentTemplate`. */
export const DEFAULT_AGENT_TEMPLATE_NAME = "rayito-agent";
/** Imagen base: la variante con capabilities, la única donde `rayd` aplica
 * el deny-all de egress que el agente necesita. */
export const DEFAULT_AGENT_TEMPLATE_BASE = "rayito-base-caps";
/** Runtimes que la plantilla sabe instalar (y los que instala por defecto). */
export const AGENT_TEMPLATE_RUNTIMES = Object.freeze(["opencode", "deepagents"] as const);
export type AgentTemplateRuntime = (typeof AGENT_TEMPLATE_RUNTIMES)[number];

const AGENT_INSTALL_DIR = "/opt/agents";
const AGENT_BIN_DIR = `${AGENT_INSTALL_DIR}/bin`;
const OPENCODE_BINARY_PATH = `${AGENT_BIN_DIR}/opencode`;
const RIPGREP_BINARY_PATH = `${AGENT_BIN_DIR}/rg`;
const DEEPAGENTS_VENV_DIR = `${AGENT_INSTALL_DIR}/deepagents`;
const DEEPAGENTS_PYTHON_PATH = `${DEEPAGENTS_VENV_DIR}/bin/python`;
const DEEPAGENTS_REQUIREMENTS_NAME = "requirements-deepagents.txt";
const PREFETCH_SCRIPT_NAME = "rayito-agent-prefetch";
const PREFETCH_SCRIPT_PATH = `${AGENT_BIN_DIR}/${PREFETCH_SCRIPT_NAME}`;
const MANIFEST_CONTEXT_NAME = "rayito-agent.json";
const OPENCODE_RELEASE_URL =
  "https://github.com/anomalyco/opencode/releases/download/" +
  `v${AGENT_OPENCODE_VERSION}/opencode-linux-arm64.tar.gz`;
const RIPGREP_RELEASE_DIR = `ripgrep-${AGENT_RIPGREP_VERSION}-aarch64-unknown-linux-gnu`;
const RIPGREP_RELEASE_URL =
  "https://github.com/BurntSushi/ripgrep/releases/download/" +
  `${AGENT_RIPGREP_VERSION}/${RIPGREP_RELEASE_DIR}.tar.gz`;

export interface AgentTemplateOptions {
  readonly name?: string | undefined;
  readonly base?: string | undefined;
  readonly runtimes?: readonly AgentTemplateRuntime[] | undefined;
  readonly prefetch?: boolean | undefined;
  readonly memoryMib?: number | undefined;
  readonly baseVersion?: string | undefined;
}

/** Opciones de `AgentTemplate.build()`: las de `Template.build` salvo
 * `memoryMb` (sale de `memoryMib`) y `contextDir` (lo escribe la plantilla). */
export type AgentTemplateBuildOptions = Omit<BuildOptions, "memoryMb" | "contextDir">;

/** El manifiesto `rayito.agent-template/1` que se hornea en la imagen. */
export interface AgentTemplateManifest {
  readonly schema: string;
  readonly protocol: number;
  readonly opencode: { readonly version: string; readonly sha256: string } | null;
  readonly deepagents: { readonly requirements_sha256: string } | null;
  readonly runner_sha256: string | null;
  readonly prefetch_paths: readonly string[];
}

function opencodeInstall(): string {
  return (
    `curl -fsSL --retry 3 -o /tmp/opencode.tar.gz ${OPENCODE_RELEASE_URL}` +
    ` && echo '${AGENT_OPENCODE_SHA256}  /tmp/opencode.tar.gz' | sha256sum -c -` +
    ` && curl -fsSL --retry 3 -o /tmp/ripgrep.tar.gz ${RIPGREP_RELEASE_URL}` +
    ` && echo '${AGENT_RIPGREP_SHA256}  /tmp/ripgrep.tar.gz' | sha256sum -c -` +
    ` && mkdir -p ${AGENT_BIN_DIR}` +
    ` && tar -xzf /tmp/opencode.tar.gz -C ${AGENT_BIN_DIR} opencode` +
    " && tar -xzf /tmp/ripgrep.tar.gz -C /tmp" +
    ` && mv /tmp/${RIPGREP_RELEASE_DIR}/rg ${RIPGREP_BINARY_PATH}` +
    ` && rm -rf /tmp/opencode.tar.gz /tmp/ripgrep.tar.gz /tmp/${RIPGREP_RELEASE_DIR}` +
    ` && chown -R root:root ${AGENT_INSTALL_DIR}` +
    ` && chmod 0755 ${AGENT_INSTALL_DIR} ${AGENT_BIN_DIR} ${OPENCODE_BINARY_PATH}` +
    ` ${RIPGREP_BINARY_PATH}` +
    ` && ln -sf ${OPENCODE_BINARY_PATH} /usr/local/bin/opencode` +
    ` && ln -sf ${RIPGREP_BINARY_PATH} /usr/local/bin/rg`
  );
}

function deepagentsInstall(): string {
  const requirements = `${AGENT_INSTALL_DIR}/${DEEPAGENTS_REQUIREMENTS_NAME}`;
  return (
    `echo '${AGENT_DEEPAGENTS_REQUIREMENTS_SHA256}  ${requirements}' | sha256sum -c -` +
    ` && python3 -m venv ${DEEPAGENTS_VENV_DIR}` +
    ` && ${DEEPAGENTS_PYTHON_PATH} -m pip install --no-cache-dir` +
    " --require-hashes --no-deps --only-binary=:all:" +
    ` -r ${requirements}` +
    ` && ${DEEPAGENTS_PYTHON_PATH} -m pip check`
  );
}

function smokeTest(runtimes: readonly AgentTemplateRuntime[]): string {
  const checks: string[] = [];
  if (runtimes.includes("opencode")) {
    checks.push("su user -c 'opencode --version'", "su user -c 'rg --version'");
  }
  if (runtimes.includes("deepagents")) {
    checks.push(`su user -c "${DEEPAGENTS_PYTHON_PATH} -c 'import deepagents, langchain_aws'"`);
  }
  return checks.join(" && ");
}

/** El `startCmd` del demonio de precarga, con sus tres argumentos. */
export function prefetchStartCmd(): string {
  return (
    `${PREFETCH_SCRIPT_PATH} ${AGENT_TEMPLATE_MANIFEST_PATH}` +
    ` ${AGENT_PREFETCH_RESTORE_JUMP_SECONDS} ${AGENT_PREFETCH_INTERVAL_SECONDS}`
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

/**
 * Plantilla de imagen con los runtimes del agente de IA.
 *
 * `runtimes` elige qué se instala (`"opencode"`, `"deepagents"`);
 * `prefetch: true` hornea el demonio que precarga los binarios en la caché
 * de páginas tras cada restauración del snapshot. `memoryMib` por debajo de
 * `AGENT_MIN_MEMORY_MIB` (2048) es `InvalidArgumentError`.
 *
 * Coste y activación
 * -------------------
 * Activa: `new AgentTemplate(...).build({ bucket })`.
 * Recursos y llamadas AWS: los de `Template.build` (un build de imagen,
 *   `s3:PutObject` del contexto, una versión de imagen nueva); nada si no se
 *   construye.
 * Coste aproximado: build ≈ 277 s (spike); la versión almacenada ≈ 3,0 GB por
 *   $0,08/GB-mes, con el mínimo de una semana ≈ $0,056/semana (≈ $0,24/mes)
 *   por versión, y cada lanzamiento lee el snapshot de memoria (≈ 0,91 GB por
 *   $0,00155/GB ≈ $0,0014). Estimación con precios de lista de Lambda
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
  readonly runtimes: readonly AgentTemplateRuntime[];
  readonly prefetch: boolean;
  readonly memoryMib: number;
  readonly baseVersion: string | undefined;

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
    const unknown = runtimes.find(
      (name) => !(AGENT_TEMPLATE_RUNTIMES as readonly string[]).includes(name),
    );
    if (unknown !== undefined) {
      throw new InvalidArgumentError(
        `runtime de plantilla desconocido: ${JSON.stringify(unknown)} ` +
          `(admitidos: ${AGENT_TEMPLATE_RUNTIMES.join(", ")})`,
      );
    }
    if (new Set(runtimes).size !== runtimes.length) {
      throw new InvalidArgumentError("runtimes no puede repetir un nombre");
    }
    const name = options.name ?? DEFAULT_AGENT_TEMPLATE_NAME;
    const base = options.base ?? DEFAULT_AGENT_TEMPLATE_BASE;
    if (!name || !base) {
      throw new InvalidArgumentError("name y base no pueden ser vacíos");
    }
    this.name = name;
    this.base = base;
    this.runtimes = Object.freeze(runtimes);
    this.prefetch = options.prefetch ?? true;
    this.memoryMib = memoryMib;
    this.baseVersion = options.baseVersion;
  }

  /** El manifiesto que se hornea en `AGENT_TEMPLATE_MANIFEST_PATH`. */
  manifest(): AgentTemplateManifest {
    const hasOpencode = this.runtimes.includes("opencode");
    return {
      schema: AGENT_TEMPLATE_MANIFEST_SCHEMA,
      protocol: AGENT_PROTOCOL_VERSION,
      opencode: hasOpencode
        ? { version: AGENT_OPENCODE_VERSION, sha256: AGENT_OPENCODE_SHA256 }
        : null,
      deepagents: this.runtimes.includes("deepagents")
        ? { requirements_sha256: AGENT_DEEPAGENTS_REQUIREMENTS_SHA256 }
        : null,
      runner_sha256: null,
      prefetch_paths: hasOpencode ? [OPENCODE_BINARY_PATH, RIPGREP_BINARY_PATH] : [],
    };
  }

  /** Los ficheros del contexto de build (nombre relativo -> texto). */
  contextFiles(): Record<string, string> {
    const files: Record<string, string> = {
      [MANIFEST_CONTEXT_NAME]: manifestText(this.manifest()),
    };
    if (this.runtimes.includes("deepagents")) {
      files[DEEPAGENTS_REQUIREMENTS_NAME] = DEEPAGENTS_REQUIREMENTS;
    }
    if (this.prefetch) {
      files[PREFETCH_SCRIPT_NAME] = PREFETCH_SCRIPT;
    }
    return files;
  }

  /** El `Template` equivalente (puro: no lee ni escribe nada). */
  toTemplate(): Template {
    let tpl = new Template().fromBaseImage(this.base, this.baseVersion);
    if (this.runtimes.includes("opencode")) {
      tpl = tpl.runCmd(opencodeInstall());
    }
    if (this.runtimes.includes("deepagents")) {
      tpl = tpl
        .copy(DEEPAGENTS_REQUIREMENTS_NAME, `${AGENT_INSTALL_DIR}/${DEEPAGENTS_REQUIREMENTS_NAME}`)
        .runCmd(deepagentsInstall());
    }
    tpl = tpl.copy(MANIFEST_CONTEXT_NAME, AGENT_TEMPLATE_MANIFEST_PATH);
    if (this.prefetch) {
      tpl = tpl.copy(PREFETCH_SCRIPT_NAME, PREFETCH_SCRIPT_PATH);
    }
    tpl = tpl.runCmd(
      `mkdir -p ${AGENT_BIN_DIR} && chown -R root:root ${AGENT_INSTALL_DIR}` +
        ` && chmod -R a+rX,go-w ${AGENT_INSTALL_DIR}` +
        (this.prefetch ? ` && chmod 0755 ${PREFETCH_SCRIPT_PATH}` : ""),
    );
    tpl = tpl.setEnvs({ ...OPENCODE_FLAG_ENVS });
    tpl = tpl.runCmd(smokeTest(this.runtimes));
    if (this.prefetch) {
      tpl = tpl.setStartCmd(prefetchStartCmd());
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
      for (const [name, text] of Object.entries(this.contextFiles())) {
        await writeFile(join(contextDir, name), text, "utf8");
      }
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
