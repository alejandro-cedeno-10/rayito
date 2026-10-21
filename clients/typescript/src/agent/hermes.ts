/**
 * Adaptador de Hermes Agent v0.22.2 (Nous Research, MIT) de los puertos
 * `AgentRuntime`, `InstallableRuntime` y `ServableRuntime` (ADR-028, D2, D7 y
 * D9). Espejo de `rayito._agent._hermes`: escribe la configuración que lee el
 * driver `hermes` del host, compone la receta de instalación y deja que
 * `HostBackedRuntime` arme el contrato de tareas.
 *
 * Hermes no está en PyPI y su lock sólo cubre CPython 3.14, así que la imagen
 * instala tres cosas bajo `AGENT_INSTALL_DIR/hermes`: un CPython 3.14
 * independiente (python-build-standalone, con su sha256), un venv con los
 * requisitos con hash y el checkout del tag, verificado contra su commit.
 * Corre como `hermes gateway run` con su API HTTP sólo en loopback.
 *
 * Valores seguros (D9): un `HERMES_HOME` propio, `HERMES_IGNORE_USER_CONFIG=1`,
 * `TERMINAL_ENV=local`, sólo los toolsets pedidos (y los que los permisos no
 * deniegan), sin telemetría, sin comprobación de actualizaciones, sin títulos
 * generados por un segundo modelo y la clave de la API generada por el driver
 * en cada arranque (nunca en ficheros, `envs` ni argv).
 */

import { createHash } from "node:crypto";
import { InvalidArgumentError } from "../errors.js";
import {
  AGENT_HERMES_API_PORT,
  AGENT_HERMES_COMMIT,
  AGENT_HERMES_REQUIREMENTS_SHA256,
  AGENT_HERMES_VERSION,
  AGENT_PROTOCOL_VERSION,
  AGENT_PYTHON_BUILD_SHA256,
  AGENT_PYTHON_BUILD_VERSIONS,
  AGENT_STATE_DIR,
  MODEL_CREDENTIAL_PLACEHOLDER,
} from "../limits.js";
import { HERMES_REQUIREMENTS } from "./assets/template-assets.gen.js";
import { type AgentSpec, requiredModelId } from "./domain.js";
import {
  HostBackedRuntime,
  type HostSetup,
  type ProviderClient,
  PYTHON_FLAG_ENVS,
  providerBaseUrl,
  requireSupportedProvider,
  standardModelEnvs,
} from "./host-runtime.js";
import { pythonDownloadCommand } from "./install-plan.js";
import { shellQuote } from "./opencode.js";
import { AGENT_APP_DIR, AGENT_INSTALL_DIR, hashedVenvInstall } from "./recipes.js";
import {
  containerPackagesStep,
  copyStep,
  type InstallableRuntime,
  type InstallTarget,
  runStep,
  type TemplateStep,
  type WarmupStep,
} from "./runtime.js";

export const HERMES_NAME = "hermes";
export const HERMES_STATE_DIR = `${AGENT_STATE_DIR}/hermes`;
/** `HERMES_HOME`: configuración, sesiones y estado de Hermes, propios de
 * Rayito y no el `~/.hermes` del usuario. */
export const HERMES_HOME_DIR = `${HERMES_STATE_DIR}/home`;
/** Lo que se instala (root, 0755): el CPython 3.14, el venv y el checkout. */
export const HERMES_INSTALL_DIR = `${AGENT_INSTALL_DIR}/hermes`;
export const HERMES_VENV_DIR = `${HERMES_INSTALL_DIR}/venv`;
export const HERMES_PYTHON = `${HERMES_VENV_DIR}/bin/python`;
export const HERMES_SOURCE_DIR = `${HERMES_INSTALL_DIR}/src`;
export const HERMES_REQUIREMENTS_NAME = "requirements-hermes.txt";
export const HERMES_REPOSITORY_URL = "https://github.com/NousResearch/hermes-agent";
/** La versión menor de CPython que necesita Hermes; el build es el fijado en
 * `AGENT_PYTHON_BUILD_VERSIONS`. */
export const HERMES_PYTHON_MINOR = "3.14";
export const PYTHON_TARBALL_PATH = "/tmp/hermes-python.tar.gz";
/** Lo que sobra del checkout en una imagen (medido: 379 MiB -> 86 MiB). */
export const HERMES_PRUNED_PATHS = [
  ".git",
  "tests",
  "tests-js",
  "apps",
  "website",
  "ui-tui",
  "web",
  "contributors",
  "evals",
  "docker",
  "nix",
] as const;
/** Toolsets por defecto: terminal y ficheros. */
export const HERMES_DEFAULT_TOOLSETS: readonly string[] = Object.freeze(["terminal", "file"]);
/** Toolset de Hermes -> herramientas de `AgentPermissions` que lo gobiernan:
 * el toolset se quita cuando todas están denegadas. */
export const HERMES_PERMISSION_TOOLSETS: Readonly<Record<string, readonly string[]>> =
  Object.freeze({
    terminal: ["bash"],
    file: ["read", "edit"],
    web: ["webfetch", "websearch"],
  });
/** Variables de Hermes que dependen de la configuración del SDK. Las fijas
 * (`HERMES_IGNORE_USER_CONFIG`, `TERMINAL_ENV`, `API_SERVER_ENABLED`,
 * `API_SERVER_HOST`) las define sólo el driver del host (`STATIC_ENVS`), y
 * `API_SERVER_KEY` la genera el driver en cada arranque. */
export const HERMES_ENVS: Readonly<Record<string, string>> = Object.freeze({
  HERMES_HOME: HERMES_HOME_DIR,
  API_SERVER_PORT: String(AGENT_HERMES_API_PORT),
});
/** Cliente de modelo de Hermes por proveedor, medido en el spike. */
export const HERMES_PROVIDERS: Readonly<Record<string, ProviderClient>> = Object.freeze({
  bedrock: { client: "bedrock", basePath: "" },
  anthropic: { client: "anthropic", basePath: "" },
  openai: { client: "custom", basePath: "/v1" },
  "openai-compatible": { client: "custom", basePath: null },
});
/** Proveedores que Hermes lee de variables de entorno en vez de la
 * configuración. */
export const HERMES_ENV_PROVIDERS = ["bedrock", "anthropic"] as const;
/** Sin pasarela (contenedor exportado), `openai` usa el proveedor nativo. */
export const HERMES_DIRECT_OPENAI_CLIENT = "openai-api";
export const HERMES_SMOKE_COMMAND =
  `cd ${HERMES_SOURCE_DIR} && PYTHONPATH=${HERMES_SOURCE_DIR} ${HERMES_PYTHON} -P -c ` +
  "'import aiohttp, boto3, anthropic, hermes_cli.main, gateway.platforms.api_server'";

const TOOLSET_PATTERN = /^[a-z][a-z0-9_-]{0,63}$/;
const SKILLS_DIR_PATTERN = /^[A-Za-z0-9_][A-Za-z0-9_./-]*$/;

/** Los requisitos con hash del venv de Hermes (dato de paquete). */
export function hermesRequirements(): string {
  return HERMES_REQUIREMENTS;
}

/** Los toolsets pedidos menos los que `AgentPermissions` deniega del todo.
 * Hermes decide por toolset, no por comando: un patrón en una herramienta que
 * gobierna un toolset es `InvalidArgumentError`. */
export function effectiveToolsets(spec: AgentSpec, toolsets: readonly string[]): string[] {
  const effective = spec.permissions.effectiveTools();
  for (const tools of Object.values(HERMES_PERMISSION_TOOLSETS)) {
    for (const tool of tools) {
      if (typeof (effective[tool] ?? "allow") !== "string") {
        throw new InvalidArgumentError(
          `Hermes no admite patrones en AgentPermissions.tools[${JSON.stringify(tool)}]: usa 'allow' o 'deny'`,
        );
      }
    }
  }
  const fallback = spec.permissions.default;
  return toolsets.filter((toolset) => {
    const governing = HERMES_PERMISSION_TOOLSETS[toolset];
    const denied = governing?.every((tool) => (effective[tool] ?? fallback) === "deny");
    return denied !== true;
  });
}

/** Opciones de `new HermesRuntime(...)`. */
export interface HermesRuntimeOptions {
  /** Los toolsets de Hermes que se activan (por defecto terminal y ficheros);
   * los que `AgentPermissions` deniega del todo se quitan. */
  readonly toolsets?: readonly string[] | undefined;
  /** Una carpeta de skills de tu `source` (`app/<skillsDir>`) que Hermes lee
   * como directorio externo, de sólo lectura. */
  readonly skillsDir?: string | undefined;
}

/**
 * `AgentRuntime` de Hermes Agent (`runtime: new HermesRuntime(...)` o
 * `runtime: "hermes"`).
 *
 * Coste y activación
 * -------------------
 * Activa: pasar `new HermesRuntime()` o `runtime: "hermes"` a `AgentTemplate`,
 *   `AgentProject` o `sbx.agent.run`.
 * Recursos y llamadas AWS: ninguna por sí solo; la imagen que lo instala se
 *   construye con `AgentTemplate.build` (un build de imagen) y cada ejecución
 *   llama al modelo de la pasarela que elijas.
 * Coste aproximado: el del build de la imagen (ver `AgentTemplate`) más el del
 *   modelo; Hermes suma unos 0,5 GB (CPython 3.14, venv y fuente), así que la
 *   imagen es más grande y arranca más despacio que la de Strands (se mide en
 *   `AWS_API_NOTES.md` al publicar).
 * IAM: la política `RayitoTemplateBuilder` para construir; ninguna para
 *   ejecutar (la pasarela guarda la clave).
 * Cómo apagarla: no incluirlo en `runtimes` ni en el proyecto.
 * Ejemplo:
 *   new AgentTemplate({ runtimes: [new HermesRuntime()] })
 */
export class HermesRuntime extends HostBackedRuntime implements InstallableRuntime {
  readonly name = HERMES_NAME;
  readonly stateName = HERMES_NAME;
  readonly toolsets: readonly string[];
  readonly skillsDir: string | undefined;

  constructor(options: HermesRuntimeOptions = {}) {
    super();
    const toolsets = options.toolsets ?? HERMES_DEFAULT_TOOLSETS;
    if (typeof toolsets === "string" || !Array.isArray(toolsets)) {
      throw new InvalidArgumentError("HermesRuntime.toolsets debe ser una lista");
    }
    if (toolsets.length === 0 || new Set(toolsets).size !== toolsets.length) {
      throw new InvalidArgumentError(
        "HermesRuntime.toolsets no puede ser vacío ni repetir un nombre",
      );
    }
    for (const toolset of toolsets) {
      if (typeof toolset !== "string" || !TOOLSET_PATTERN.test(toolset)) {
        throw new InvalidArgumentError(
          "cada toolset de HermesRuntime debe ser un nombre [a-z][a-z0-9_-]*",
        );
      }
    }
    const skillsDir = options.skillsDir;
    if (
      skillsDir !== undefined &&
      (typeof skillsDir !== "string" ||
        !SKILLS_DIR_PATTERN.test(skillsDir) ||
        skillsDir.split("/").includes(".."))
    ) {
      throw new InvalidArgumentError(
        "HermesRuntime.skillsDir es una ruta relativa dentro de source",
      );
    }
    this.toolsets = Object.freeze([...toolsets]);
    this.skillsDir = skillsDir;
  }

  /** El runtime de la tabla `[runtime]` de `rayito-agent.toml`. */
  static fromOptions(options: Readonly<Record<string, unknown>>): HermesRuntime {
    const toolsets = options.toolsets ?? HERMES_DEFAULT_TOOLSETS;
    const skillsDir = options.skills_dir;
    if (!Array.isArray(toolsets) || toolsets.some((toolset) => typeof toolset !== "string")) {
      throw new InvalidArgumentError("runtime.toolsets debe ser una lista de cadenas");
    }
    if (skillsDir !== undefined && typeof skillsDir !== "string") {
      throw new InvalidArgumentError("runtime.skills_dir debe ser una cadena");
    }
    return new HermesRuntime({ toolsets: toolsets as string[], skillsDir });
  }

  /** La tabla `[runtime]` canónica. */
  toOptions(): Record<string, unknown> {
    const options: Record<string, unknown> = { kind: HERMES_NAME };
    if (this.toolsets.join("\0") !== HERMES_DEFAULT_TOOLSETS.join("\0")) {
      options.toolsets = [...this.toolsets];
    }
    if (this.skillsDir !== undefined) {
      options.skills_dir = this.skillsDir;
    }
    return options;
  }

  /** El host (stdlib) corre con el Python de Hermes: el único intérprete
   * garantizado en la imagen que ya lo trae. */
  hostPython(): string {
    return HERMES_PYTHON;
  }

  override flagEnvs(): Readonly<Record<string, string>> {
    return { ...PYTHON_FLAG_ENVS, ...HERMES_ENVS };
  }

  /** Bedrock y Anthropic por sus variables estándar; `provider: custom` lo
   * lleva la configuración, y las variables de OpenAI no se exportan para no
   * confundir la resolución de proveedor de Hermes. */
  override wiringEnvs(
    spec: AgentSpec,
    urls: Readonly<Record<string, string>> | undefined,
  ): Readonly<Record<string, string>> {
    if ((HERMES_ENV_PROVIDERS as readonly string[]).includes(spec.model.provider)) {
      return standardModelEnvs(spec, urls);
    }
    return {};
  }

  hostSetup(
    spec: AgentSpec,
    urls: Readonly<Record<string, string>> | undefined,
    workdir: string,
  ): HostSetup {
    const entry = requireSupportedProvider(spec, HERMES_PROVIDERS, "el runtime 'hermes'");
    let client = entry.client;
    if (urls === undefined && spec.model.provider === "openai-compatible") {
      throw new InvalidArgumentError(
        "el proveedor 'openai-compatible' no tiene un endpoint estándar: " +
          "sin pasarela necesita una URL explícita",
      );
    }
    if (urls === undefined && spec.model.provider === "openai") {
      client = HERMES_DIRECT_OPENAI_CLIENT;
    }
    const baseUrl = client === "custom" ? providerBaseUrl(spec, entry, urls) : undefined;
    const config = {
      v: AGENT_PROTOCOL_VERSION,
      provider: spec.model.provider,
      client,
      model: requiredModelId(spec.model, this.name),
      region: spec.model.region ?? null,
      base_url: baseUrl ?? null,
      credential_placeholder: baseUrl === undefined ? null : MODEL_CREDENTIAL_PLACEHOLDER,
      workdir,
      state_dir: HERMES_STATE_DIR,
      home_dir: HERMES_HOME_DIR,
      toolsets: effectiveToolsets(spec, this.toolsets),
      skills_dir: this.skillsDir === undefined ? null : `${AGENT_APP_DIR}/${this.skillsDir}`,
      instructions: spec.instructions ?? null,
      python: HERMES_PYTHON,
      source_dir: HERMES_SOURCE_DIR,
      api_port: AGENT_HERMES_API_PORT,
    };
    return { driver: "hermes", config };
  }

  /** CPython 3.14 independiente (sha256 fijado), venv con `--require-hashes`
   * y el checkout del tag verificado contra su commit y recortado. */
  installSteps(target: InstallTarget): readonly TemplateStep[] {
    const hermesDir = `${target.installDir}/${HERMES_NAME}`;
    const requirements = `${target.installDir}/${HERMES_REQUIREMENTS_NAME}`;
    const interpreter: InstallTarget = { ...target, python: `${hermesDir}/python/bin/python3` };
    return [
      ...containerPackagesStep(target, ["curl", "ca-certificates", "git"]),
      runStep(pythonDownloadCommand(HERMES_PYTHON_MINOR, hermesDir, PYTHON_TARBALL_PATH)),
      copyStep(HERMES_REQUIREMENTS_NAME, requirements),
      runStep(
        hashedVenvInstall(interpreter, {
          venvDir: `${hermesDir}/venv`,
          requirementsPath: requirements,
          requirementsSha256: AGENT_HERMES_REQUIREMENTS_SHA256,
        }),
      ),
      runStep(checkoutCommand(hermesDir)),
    ];
  }

  contextFiles(): Readonly<Record<string, string>> {
    return { [HERMES_REQUIREMENTS_NAME]: hermesRequirements() };
  }

  /** Versión, commit, CPython y hashes de lo instalado. */
  manifestEntry(): Readonly<Record<string, unknown>> {
    return {
      version: AGENT_HERMES_VERSION,
      commit: AGENT_HERMES_COMMIT,
      python_version: AGENT_PYTHON_BUILD_VERSIONS[HERMES_PYTHON_MINOR],
      python_sha256: AGENT_PYTHON_BUILD_SHA256[HERMES_PYTHON_MINOR],
      requirements_sha256: AGENT_HERMES_REQUIREMENTS_SHA256,
      requirements_file_sha256: createHash("sha256")
        .update(hermesRequirements(), "utf8")
        .digest("hex"),
    };
  }

  /** Importa Hermes y su API HTTP como `user`. */
  smokeChecks(): readonly string[] {
    return [`su user -c "${HERMES_SMOKE_COMMAND}"`];
  }

  /** Ninguna: se carga con `warmupSteps()`. */
  prefetchPaths(): readonly string[] {
    return [];
  }

  /** Carga Hermes en la caché de páginas. */
  override warmupSteps(): readonly WarmupStep[] {
    const source = shellQuote(HERMES_SOURCE_DIR);
    return [
      {
        cmd:
          `cd ${source} && PYTHONPATH=${source} ${shellQuote(HERMES_PYTHON)} -P -c ` +
          "'import hermes_cli.main' >/dev/null 2>&1 || true",
      },
    ];
  }
}

function checkoutCommand(hermesDir: string): string {
  const source = `${hermesDir}/src`;
  return (
    `git clone --depth 1 --branch v${AGENT_HERMES_VERSION} ${HERMES_REPOSITORY_URL} ${source}` +
    ` && test "$(git -C ${source} rev-parse HEAD)" = ${AGENT_HERMES_COMMIT}` +
    ` && cd ${source} && rm -rf ${HERMES_PRUNED_PATHS.join(" ")}`
  );
}
