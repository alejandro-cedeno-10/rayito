/**
 * Adaptador de Strands Agents (`strands-agents` y `strands-harness`, Python y
 * TypeScript) de los puertos `AgentRuntime`, `InstallableRuntime` y
 * `ServableRuntime` (ADR-028, D2, D7 y D9). Espejo de `rayito._agent._strands`:
 * escribe la configuración que lee el driver del host (`strands` para Python y
 * el harness, `strands-ts` para el puente de Node), compone la receta de
 * instalación y deja que `HostBackedRuntime` arme el contrato de tareas.
 *
 * - **Strands (Python)**: sin `entrypoint` el host construye `Agent(model,
 *   system_prompt, tools=[])`; con `entrypoint: "modulo:atributo"` importa tu
 *   `Agent` (o la función `fn(ctx) -> Agent`, que recibe el modelo ya cableado
 *   a la pasarela en `ctx.model`) desde `AGENT_APP_DIR`.
 * - **Harness**: `create_harness(...)` con las herramientas integradas que
 *   permiten los `AgentPermissions`; `programmatic_tool_caller` y `web_search`
 *   van siempre apagadas y la sesión y la memoria viven bajo
 *   `AGENT_STATE_DIR/strands`.
 * - **TypeScript**: tu `Agent` de `@strands-agents/sdk` (módulo `entrypoint`,
 *   que exporta `agent` o `createAgent(ctx)`) corre en Node a través de
 *   `strands_bridge.mjs`; las dependencias son las de tu `package-lock.json`.
 *
 * El modelo llega siempre por la pasarela de secretos con un marcador como
 * clave, nunca con credenciales de AWS que el agente pudiera leer.
 */

import { createHash } from "node:crypto";
import { InvalidArgumentError } from "../errors.js";
import {
  AGENT_NODE_SHA256,
  AGENT_NODE_VERSION,
  AGENT_PROTOCOL_VERSION,
  AGENT_STATE_DIR,
  AGENT_STRANDS_HARNESS_REQUIREMENTS_SHA256,
  AGENT_STRANDS_HARNESS_VERSION,
  AGENT_STRANDS_REQUIREMENTS_SHA256,
  AGENT_STRANDS_TS_HARNESS_VERSION,
  AGENT_STRANDS_TS_VERSION,
  AGENT_STRANDS_VERSION,
  MODEL_CREDENTIAL_PLACEHOLDER,
} from "../limits.js";
import {
  STRANDS_BRIDGE,
  STRANDS_HARNESS_REQUIREMENTS,
  STRANDS_REQUIREMENTS,
} from "./assets/template-assets.gen.js";
import type { AgentSpec } from "./domain.js";
import {
  containerPackagesStep,
  HostBackedRuntime,
  type HostSetup,
  type ProviderClient,
  PYTHON_FLAG_ENVS,
  permissionRules,
  providerBaseUrl,
  requireSupportedProvider,
  standardModelEnvs,
  toolAllowed,
  userRequirementsSteps,
} from "./host-runtime.js";
import { shellQuote } from "./opencode.js";
import {
  AGENT_APP_DIR,
  AGENT_INSTALL_DIR,
  AGENT_RAYITO_DIR,
  hashedVenvInstall,
} from "./recipes.js";
import {
  copyStep,
  type InstallableRuntime,
  type InstallTarget,
  runStep,
  type TemplateStep,
  type WarmupStep,
} from "./runtime.js";

export const STRANDS_LANGUAGES = ["python", "typescript"] as const;
export type StrandsLanguage = (typeof STRANDS_LANGUAGES)[number];
/** Nombres con los que el registro y la telemetría conocen al adaptador. */
export const STRANDS_NAME = "strands";
export const STRANDS_HARNESS_NAME = "strands-harness";
/** Estado del runtime dentro de `AGENT_STATE_DIR` (sesiones y memoria). */
export const STRANDS_STATE_NAME = "strands";
export const STRANDS_STATE_DIR = `${AGENT_STATE_DIR}/${STRANDS_STATE_NAME}`;
export const STRANDS_SESSIONS_DIR = `${STRANDS_STATE_DIR}/sessions`;
export const STRANDS_MEMORY_DIR = `${STRANDS_STATE_DIR}/memory`;
/** Venvs de la plantilla (`/opt/agents/strands` y `/opt/agents/strands-harness`). */
export const STRANDS_VENV_NAME = "strands";
export const STRANDS_HARNESS_VENV_NAME = "strands-harness";
export const STRANDS_REQUIREMENTS_NAME = "requirements-strands.txt";
export const STRANDS_HARNESS_REQUIREMENTS_NAME = "requirements-strands-harness.txt";
/** El puente de Node (dato de paquete) y dónde lo instala la plantilla. */
export const STRANDS_BRIDGE_NAME = "strands_bridge.mjs";
export const STRANDS_BRIDGE_PATH = `${AGENT_RAYITO_DIR}/${STRANDS_BRIDGE_NAME}`;
/** Node: tarball oficial linux arm64 (SHASUMS256 de nodejs.org) en `node/`. */
export const NODE_DIR_NAME = "node";
export const NODE_TARBALL_URL = `https://nodejs.org/dist/v${AGENT_NODE_VERSION}/node-v${AGENT_NODE_VERSION}-linux-arm64.tar.gz`;
/** El `package.json` y el lockfile del usuario (`app/` del contexto de build)
 * que `npm ci` instala junto a Node. */
export const NODE_PACKAGE_FILES = ["package.json", "package-lock.json"] as const;
export const STRANDS_SMOKE_MODULES = "strands, boto3, anthropic, openai, google.genai";
export const STRANDS_HARNESS_SMOKE_MODULES = `${STRANDS_SMOKE_MODULES}, strands_harness`;
export const TS_SMOKE_MODULE = "@strands-agents/sdk";
/** Telemetría de Strands apagada: sin exportador no sale nada del sandbox. */
export const STRANDS_TELEMETRY_ENVS: Readonly<Record<string, string>> = Object.freeze({
  OTEL_SDK_DISABLED: "true",
  OTEL_TRACES_EXPORTER: "none",
  OTEL_METRICS_EXPORTER: "none",
  OTEL_LOGS_EXPORTER: "none",
});
/** Herramienta de `AgentPermissions` -> herramientas integradas del harness
 * que la gobiernan (`BUILTIN_TOOL_NAMES`, comprobadas en
 * `testdata/agent/strands-spike.json`). */
export const HARNESS_PERMISSION_TOOLS: Readonly<Record<string, readonly string[]>> = Object.freeze({
  bash: ["shell"],
  read: ["read"],
  edit: ["write", "edit"],
  webfetch: ["web_fetch"],
  task: ["subagent"],
});
/** Siempre apagadas: la primera ejecuta código que escribe el modelo y la
 * segunda manda las consultas a un tercero. */
export const HARNESS_ALWAYS_OFF = ["programmatic_tool_caller", "web_search"] as const;
/** Proveedores por pasarela de Strands (Python y harness), medidos en
 * `testdata/agent/strands-spike.json`. `azure` queda fuera: la pasarela de
 * Azure inyecta `api-key` y estos clientes mandan `Authorization`. */
export const STRANDS_PROVIDERS: Readonly<Record<string, ProviderClient>> = Object.freeze({
  bedrock: { client: "BedrockModel", basePath: "" },
  anthropic: { client: "AnthropicModel", basePath: "" },
  openai: { client: "OpenAIModel", basePath: "/v1" },
  "openai-compatible": { client: "OpenAIModel", basePath: null },
  google: { client: "GeminiModel", basePath: "" },
});
/** Los de `@strands-agents/sdk` (TypeScript), medidos igual. */
export const STRANDS_TS_PROVIDERS: Readonly<Record<string, ProviderClient>> = Object.freeze({
  bedrock: { client: "BedrockModel", basePath: "" },
  anthropic: { client: "AnthropicModel", basePath: "" },
  openai: { client: "OpenAIModel", basePath: "/v1" },
  "openai-compatible": { client: "OpenAIModel", basePath: null },
  google: { client: "GoogleModel", basePath: "" },
});

const PYTHON_ENTRYPOINT_PATTERN = /^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$/;
const TS_ENTRYPOINT_PATTERN = /^[A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:ts|mts|js|mjs)$/;
const REQUIREMENTS_PATTERN = /^[A-Za-z0-9_][A-Za-z0-9_./-]*$/;

/** Los requisitos con hash del venv de Strands (dato de paquete). */
export function strandsRequirements(): string {
  return STRANDS_REQUIREMENTS;
}

/** Los requisitos con hash del venv del harness de Strands. */
export function strandsHarnessRequirements(): string {
  return STRANDS_HARNESS_REQUIREMENTS;
}

/** El puente de Node que importa tu agente y habla el protocolo v1. */
export function strandsBridge(): string {
  return STRANDS_BRIDGE;
}

/** Nombre de herramienta integrada del harness -> herramienta de
 * `AgentPermissions` que la gobierna (la inversa de `HARNESS_PERMISSION_TOOLS`). */
export function harnessPermissionAliases(): Record<string, string> {
  const aliases: Record<string, string> = {};
  for (const [permission, names] of Object.entries(HARNESS_PERMISSION_TOOLS)) {
    for (const name of names) {
      aliases[name] = permission;
    }
  }
  return aliases;
}

/** Las herramientas integradas del harness que `AgentPermissions` permite:
 * parte de cero (`"*": false`), enciende las que gobierna una herramienta no
 * denegada y apaga siempre `HARNESS_ALWAYS_OFF`. */
export function harnessBuiltinTools(spec: AgentSpec): Record<string, boolean> {
  const tools: Record<string, boolean> = { "*": false };
  for (const [permission, names] of Object.entries(HARNESS_PERMISSION_TOOLS)) {
    const allowed = toolAllowed(spec.permissions, permission);
    for (const name of names) {
      tools[name] = allowed;
    }
  }
  for (const name of HARNESS_ALWAYS_OFF) {
    tools[name] = false;
  }
  return tools;
}

/** Opciones de `new StrandsRuntime(...)`. */
export interface StrandsRuntimeOptions {
  /** `modulo:atributo` en Python (un `Agent` o una función `fn(ctx) -> Agent`)
   * y la ruta de un módulo bajo `AGENT_APP_DIR` en TypeScript (obligatoria; y
   * exporta `agent` o `createAgent(ctx)`). */
  readonly entrypoint?: string | undefined;
  /** `true` usa `create_harness` de `strands-harness` (sólo con
   * `language: "python"`). */
  readonly harness?: boolean | undefined;
  readonly language?: StrandsLanguage | undefined;
  /** Un fichero de requisitos de tu `source` (`app/<requirements>`) que se
   * instala en el venv después de los pines de Rayito (sólo Python). */
  readonly requirements?: string | undefined;
}

/**
 * `AgentRuntime` de Strands Agents (`runtime: new StrandsRuntime(...)`;
 * `"strands"` y `"strands-harness"` en el registro). Con `language:
 * "typescript"` el `source` debe llevar `package.json` y `package-lock.json`:
 * el build ejecuta `npm ci --ignore-scripts` con ellos y falla si faltan.
 *
 * Coste y activación
 * -------------------
 * Activa: pasar `new StrandsRuntime(...)` o `runtime: "strands"` /
 *   `"strands-harness"` a `AgentTemplate`, `AgentProject` o `sbx.agent.run`.
 * Recursos y llamadas AWS: ninguna por sí solo; la imagen que lo instala se
 *   construye con `AgentTemplate.build` (un build de imagen) y cada ejecución
 *   llama al modelo de la pasarela que elijas.
 * Coste aproximado: el del build de la imagen (ver `AgentTemplate`) más el del
 *   modelo; el venv de Strands añade unos 0,3 GB al snapshot (se mide en
 *   `AWS_API_NOTES.md` al publicar).
 * IAM: la política `RayitoTemplateBuilder` para construir; ninguna para
 *   ejecutar (la pasarela guarda la clave).
 * Cómo apagarla: no incluirlo en `runtimes` ni en el proyecto.
 * Ejemplo:
 *   new AgentTemplate({ runtimes: [new StrandsRuntime({ entrypoint: "agent:agent" })] })
 */
export class StrandsRuntime extends HostBackedRuntime implements InstallableRuntime {
  readonly name: string;
  readonly stateName = STRANDS_STATE_NAME;
  readonly entrypoint: string | undefined;
  readonly harness: boolean;
  readonly language: StrandsLanguage;
  readonly requirements: string | undefined;

  constructor(options: StrandsRuntimeOptions = {}) {
    super();
    const language = options.language ?? "python";
    const harness = options.harness ?? false;
    if (!(STRANDS_LANGUAGES as readonly string[]).includes(language)) {
      throw new InvalidArgumentError(
        `StrandsRuntime.language debe ser uno de ${JSON.stringify(STRANDS_LANGUAGES)}`,
      );
    }
    if (typeof harness !== "boolean") {
      throw new InvalidArgumentError("StrandsRuntime.harness debe ser un boolean");
    }
    if (language === "typescript" && harness) {
      throw new InvalidArgumentError(
        "con language 'typescript' el harness se importa en tu código " +
          "(@strands-agents/harness); harness: true es sólo de Python",
      );
    }
    checkEntrypoint(options.entrypoint, language);
    const requirements = options.requirements;
    if (
      requirements !== undefined &&
      (language === "typescript" ||
        !REQUIREMENTS_PATTERN.test(requirements) ||
        requirements.split("/").includes(".."))
    ) {
      throw new InvalidArgumentError(
        "StrandsRuntime.requirements es una ruta relativa dentro de source " +
          "y sólo existe con language 'python'",
      );
    }
    this.entrypoint = options.entrypoint;
    this.harness = harness;
    this.language = language;
    this.requirements = requirements;
    this.name = harness ? STRANDS_HARNESS_NAME : STRANDS_NAME;
  }

  /** El runtime de la tabla `[runtime]` de `rayito-agent.toml` (`kind` es
   * `"strands"` o `"strands-harness"`). */
  static fromOptions(kind: string, options: Readonly<Record<string, unknown>>): StrandsRuntime {
    const text = (key: string): string | undefined => {
      const value = options[key];
      if (value !== undefined && typeof value !== "string") {
        throw new InvalidArgumentError(`runtime.${key} debe ser una cadena`);
      }
      return value;
    };
    return new StrandsRuntime({
      entrypoint: text("entrypoint"),
      harness: kind === STRANDS_HARNESS_NAME,
      language: (text("language") ?? "python") as StrandsLanguage,
      requirements: text("requirements"),
    });
  }

  /** La tabla `[runtime]` canónica: `kind` y sus opciones. */
  toOptions(): Record<string, unknown> {
    const options: Record<string, unknown> = { kind: this.name };
    if (this.entrypoint !== undefined) {
      options.entrypoint = this.entrypoint;
    }
    if (this.language !== "python") {
      options.language = this.language;
    }
    if (this.requirements !== undefined) {
      options.requirements = this.requirements;
    }
    return options;
  }

  get #typescript(): boolean {
    return this.language === "typescript";
  }

  get #venvName(): string {
    return this.harness ? STRANDS_HARNESS_VENV_NAME : STRANDS_VENV_NAME;
  }

  venvPython(installDir: string = AGENT_INSTALL_DIR): string {
    return `${installDir}/${this.#venvName}/bin/python`;
  }

  hostPython(): string {
    return this.#typescript ? "python3" : this.venvPython();
  }

  override flagEnvs(): Readonly<Record<string, string>> {
    return { ...PYTHON_FLAG_ENVS, ...STRANDS_TELEMETRY_ENVS };
  }

  /** Las variables estándar del proveedor apuntadas a la pasarela. */
  override wiringEnvs(
    spec: AgentSpec,
    urls: Readonly<Record<string, string>> | undefined,
  ): Readonly<Record<string, string>> {
    return standardModelEnvs(spec, urls);
  }

  hostSetup(
    spec: AgentSpec,
    urls: Readonly<Record<string, string>> | undefined,
    workdir: string,
  ): HostSetup {
    const providers = this.#typescript ? STRANDS_TS_PROVIDERS : STRANDS_PROVIDERS;
    const entry = requireSupportedProvider(
      spec,
      providers,
      `el runtime ${JSON.stringify(this.name)}`,
    );
    const baseUrl = providerBaseUrl(spec, entry, urls);
    const config: Record<string, unknown> = {
      v: AGENT_PROTOCOL_VERSION,
      provider: spec.model.provider,
      client: entry.client,
      model: spec.model.id,
      region: spec.model.region ?? null,
      base_url: baseUrl ?? null,
      credential_placeholder: baseUrl === undefined ? null : MODEL_CREDENTIAL_PLACEHOLDER,
      prompt_caching: spec.model.promptCaching,
      instructions: spec.instructions ?? null,
      workdir,
      app_dir: AGENT_APP_DIR,
      state_dir: STRANDS_STATE_DIR,
      sessions_dir: STRANDS_SESSIONS_DIR,
      entrypoint: this.entrypoint ?? null,
      permissions: permissionRules(spec.permissions),
    };
    if (this.#typescript) {
      config.bridge = STRANDS_BRIDGE_PATH;
      return { driver: "strands-ts", config };
    }
    config.harness = this.harness;
    config.memory_dir = STRANDS_MEMORY_DIR;
    config.builtin_tools = this.harness ? harnessBuiltinTools(spec) : null;
    config.permission_aliases = this.harness ? harnessPermissionAliases() : {};
    return { driver: "strands", config };
  }

  /** Python: los requisitos con hash, el venv (`--require-hashes`) y, si hay,
   * los del usuario. TypeScript: Node (sha256 fijado) y `npm ci
   * --ignore-scripts` de tu lockfile, más el puente. */
  installSteps(target: InstallTarget): readonly TemplateStep[] {
    if (this.#typescript) {
      return this.#typescriptSteps(target);
    }
    const name = this.harness ? STRANDS_HARNESS_REQUIREMENTS_NAME : STRANDS_REQUIREMENTS_NAME;
    const sha256 = this.harness
      ? AGENT_STRANDS_HARNESS_REQUIREMENTS_SHA256
      : AGENT_STRANDS_REQUIREMENTS_SHA256;
    const requirements = `${target.installDir}/${name}`;
    const venvDir = `${target.installDir}/${this.#venvName}`;
    const steps: TemplateStep[] = [
      copyStep(name, requirements),
      runStep(
        hashedVenvInstall(target, {
          venvDir,
          requirementsPath: requirements,
          requirementsSha256: sha256,
        }),
      ),
    ];
    if (this.requirements !== undefined) {
      steps.push(
        ...userRequirementsSteps(target, {
          venvDir,
          requirements: this.requirements,
          slug: this.#venvName,
        }),
      );
    }
    return steps;
  }

  #typescriptSteps(target: InstallTarget): TemplateStep[] {
    const nodeDir = `${target.installDir}/${NODE_DIR_NAME}`;
    const nodeBin = `${nodeDir}/bin`;
    const steps: TemplateStep[] = [...containerPackagesStep(target, ["curl", "ca-certificates"])];
    steps.push(
      runStep(
        `curl -fsSL --retry 3 -o /tmp/node.tar.gz ${NODE_TARBALL_URL}` +
          ` && echo '${AGENT_NODE_SHA256}  /tmp/node.tar.gz' | sha256sum -c -` +
          ` && mkdir -p ${nodeDir}` +
          ` && tar -xzf /tmp/node.tar.gz -C ${nodeDir} --strip-components=1` +
          " && rm -f /tmp/node.tar.gz" +
          ` && ln -sf ${nodeBin}/node /usr/local/bin/node`,
      ),
    );
    for (const name of NODE_PACKAGE_FILES) {
      steps.push(copyStep(`app/${name}`, `${target.installDir}/${name}`));
    }
    steps.push(
      runStep(
        `cd ${target.installDir} && PATH=${nodeBin}:$PATH` +
          " npm ci --ignore-scripts --omit=dev --no-audit --no-fund",
      ),
    );
    steps.push(copyStep(STRANDS_BRIDGE_NAME, STRANDS_BRIDGE_PATH));
    return steps;
  }

  contextFiles(): Readonly<Record<string, string>> {
    if (this.#typescript) {
      return { [STRANDS_BRIDGE_NAME]: strandsBridge() };
    }
    if (this.harness) {
      return { [STRANDS_HARNESS_REQUIREMENTS_NAME]: strandsHarnessRequirements() };
    }
    return { [STRANDS_REQUIREMENTS_NAME]: strandsRequirements() };
  }

  /** Versiones y hashes de lo instalado, para el manifiesto. */
  manifestEntry(): Readonly<Record<string, unknown>> {
    if (this.#typescript) {
      return {
        language: "typescript",
        node_version: AGENT_NODE_VERSION,
        node_sha256: AGENT_NODE_SHA256,
        tested_sdk_version: AGENT_STRANDS_TS_VERSION,
        tested_harness_version: AGENT_STRANDS_TS_HARNESS_VERSION,
        bridge_sha256: createHash("sha256").update(strandsBridge(), "utf8").digest("hex"),
      };
    }
    if (this.harness) {
      return {
        language: "python",
        version: AGENT_STRANDS_VERSION,
        harness_version: AGENT_STRANDS_HARNESS_VERSION,
        requirements_sha256: AGENT_STRANDS_HARNESS_REQUIREMENTS_SHA256,
      };
    }
    return {
      language: "python",
      version: AGENT_STRANDS_VERSION,
      requirements_sha256: AGENT_STRANDS_REQUIREMENTS_SHA256,
    };
  }

  /** Importa el SDK (y el harness) como `user`. */
  smokeChecks(): readonly string[] {
    if (this.#typescript) {
      return [
        "su user -c 'node --version'",
        `su user -c "cd ${AGENT_INSTALL_DIR} && node --input-type=module -e ` +
          `'import \\"${TS_SMOKE_MODULE}\\"'"`,
        `su user -c 'test -r ${STRANDS_BRIDGE_PATH}'`,
      ];
    }
    const modules = this.harness ? STRANDS_HARNESS_SMOKE_MODULES : STRANDS_SMOKE_MODULES;
    return [`su user -c "${this.venvPython()} -c 'import ${modules}'"`];
  }

  /** El binario de Node (TypeScript); el venv de Python se carga con
   * `warmupSteps()`. */
  prefetchPaths(): readonly string[] {
    return this.#typescript ? [`${AGENT_INSTALL_DIR}/${NODE_DIR_NAME}/bin/node`] : [];
  }

  /** Carga Strands (o Node con el SDK) en la caché de páginas. */
  override warmupSteps(): readonly WarmupStep[] {
    if (this.#typescript) {
      return [
        {
          cmd:
            `cd ${AGENT_INSTALL_DIR} && node --input-type=module ` +
            `-e 'import "${TS_SMOKE_MODULE}"' >/dev/null 2>&1 || true`,
        },
      ];
    }
    return [
      {
        cmd: `${shellQuote(this.venvPython())} -c 'import strands, boto3' >/dev/null 2>&1 || true`,
      },
    ];
  }
}

function checkEntrypoint(entrypoint: string | undefined, language: string): void {
  if (language === "typescript") {
    if (entrypoint === undefined || !TS_ENTRYPOINT_PATTERN.test(entrypoint)) {
      throw new InvalidArgumentError(
        "StrandsRuntime con language 'typescript' necesita un entrypoint con la " +
          "ruta de un módulo .ts/.js dentro de source, por ejemplo 'agent.ts'",
      );
    }
    if (entrypoint.split("/").includes("..")) {
      throw new InvalidArgumentError("StrandsRuntime.entrypoint no puede salir de source");
    }
    return;
  }
  if (entrypoint !== undefined && !PYTHON_ENTRYPOINT_PATTERN.test(entrypoint)) {
    throw new InvalidArgumentError(
      "StrandsRuntime.entrypoint debe tener la forma 'paquete.modulo:atributo'",
    );
  }
}
