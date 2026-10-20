/**
 * Lo común a `agent-project*.e2e.test.ts`: los casos por runtime, las dos
 * imágenes desechables y el `AgentProject` de cada caso. Espejo de
 * `clients/python/tests/e2e/agent_project_support.py`; las variables de
 * entorno y el coste están en la cabecera de `agent-project.e2e.test.ts`.
 */

import { join } from "node:path";
import { AGENT_APP_DIR } from "../../src/agent/recipes.js";
import {
  AgentLimits,
  AgentModel,
  AgentProject,
  type AgentRuntime,
  AgentSpec,
  AgentTemplate,
  anthropicGateway,
  bedrockGateway,
  CommandExitError,
  CustomRuntime,
  DeepAgents,
  HermesRuntime,
  InvalidArgumentError,
  type Sandbox,
  SecretCache,
  SecretStore,
  StrandsRuntime,
} from "../../src/index.js";

export const REPO_ROOT = join(import.meta.dirname, "../../../..");
export const FIXTURES = join(REPO_ROOT, "testdata", "agent-project-e2e");
export const BEDROCK_SECRET = process.env.RAYITO_E2E_BEDROCK_SECRET ?? "";
export const ANTHROPIC_SECRET = process.env.RAYITO_E2E_ANTHROPIC_SECRET ?? "";
export const PREFIX = process.env.RAYITO_E2E_AGENT_PROJECT_PREFIX ?? "";
export const BASE = process.env.RAYITO_E2E_AGENT_PROJECT_BASE ?? "";
export const BUCKET = process.env.RAYITO_E2E_AGENT_PROJECT_BUCKET ?? "";
export const DEFAULT_BEDROCK_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0";
export const DEFAULT_BEDROCK_REGION = "us-east-1";
export const ANTHROPIC_MODEL = "claude-haiku-4-5";
export const BEDROCK_MODEL = process.env.RAYITO_E2E_BEDROCK_MODEL || DEFAULT_BEDROCK_MODEL;
export const BEDROCK_REGION = process.env.RAYITO_E2E_BEDROCK_REGION || DEFAULT_BEDROCK_REGION;
export const GATEWAY_NAME = "model";
/** El comando propio corre con el directorio de trabajo del proyecto, no con el
 * del código horneado en la imagen: el script se nombra con su ruta absoluta. */
export const CUSTOM_SCRIPT = `${AGENT_APP_DIR}/run.sh`;
export const PROMPT = "Reply with exactly one short sentence.";
export const MS_PER_SECOND = 1000;
export const RUN_TIMEOUT_MS = 300 * MS_PER_SECOND;
export const DEPLOY_TIMEOUT_MS = 1200 * MS_PER_SECOND;
export const TEST_TIMEOUT_MS = 900 * MS_PER_SECOND;
export const BUILD_TIMEOUT_MS = 1800 * MS_PER_SECOND;
export const MAX_STEPS = 8;

export type ImageKind = "py" | "cli";

export interface RuntimeCase {
  readonly id: string;
  readonly image: ImageKind;
  readonly runtime: () => AgentRuntime | string;
  readonly callsTheModel: boolean;
  /** Las líneas de `[runtime]` del `rayito-agent.toml` del caso. */
  readonly runtimeToml: string;
}

export const CASES: readonly RuntimeCase[] = [
  {
    id: "deepagents",
    image: "py",
    runtime: () => "deepagents",
    callsTheModel: true,
    runtimeToml: 'kind = "deepagents"',
  },
  {
    id: "langgraph",
    image: "py",
    runtime: () => new DeepAgents({ entrypoint: "graph:build" }),
    callsTheModel: true,
    runtimeToml: 'kind = "langgraph"\nentrypoint = "graph:build"',
  },
  {
    id: "strands",
    image: "py",
    runtime: () => new StrandsRuntime({ entrypoint: "agent:agent" }),
    callsTheModel: true,
    runtimeToml: 'kind = "strands"\nentrypoint = "agent:agent"',
  },
  {
    id: "strands-harness",
    image: "py",
    runtime: () => new StrandsRuntime({ harness: true }),
    callsTheModel: true,
    runtimeToml: 'kind = "strands-harness"',
  },
  {
    id: "custom",
    image: "py",
    runtime: () => new CustomRuntime({ command: ["bash", CUSTOM_SCRIPT], protocol: "text" }),
    callsTheModel: false,
    runtimeToml: `kind = "custom"\ncommand = ["bash", "${CUSTOM_SCRIPT}"]\nprotocol = "text"`,
  },
  {
    id: "opencode",
    image: "cli",
    runtime: () => "opencode",
    callsTheModel: true,
    runtimeToml: 'kind = "opencode"',
  },
  {
    id: "hermes",
    image: "cli",
    runtime: () => new HermesRuntime(),
    callsTheModel: true,
    runtimeToml: 'kind = "hermes"',
  },
  {
    id: "strands-node",
    image: "cli",
    runtime: () => new StrandsRuntime({ entrypoint: "agent.ts", language: "typescript" }),
    callsTheModel: true,
    runtimeToml: 'kind = "strands"\nlanguage = "typescript"\nentrypoint = "agent.ts"',
  },
];

export const TOOL_CASES: readonly string[] = [
  "deepagents",
  "langgraph",
  "strands-harness",
  "opencode",
  "hermes",
];
export const SANDBOX_WORKDIR = "/home/user";
export const CREDENTIAL_PROBE_TIMEOUT_MS = 600 * MS_PER_SECOND;
/** Donde un runtime escribiría una configuración o un registro; el código
 * instalado (paquetes de Python y de Node) no se recorre: son cientos de miles
 * de ficheros que el agente no genera. */
export const CREDENTIAL_SEARCH_ROOTS = [
  "/home",
  "/root",
  "/tmp",
  "/var",
  "/etc",
  "/run",
  "/srv",
  "/opt",
];
export const CREDENTIAL_SEARCH_SKIPPED = ["site-packages", "node_modules"];
export const TELEMETRY_OFF_SWITCHES: Readonly<Record<string, string>> = {
  OTEL_SDK_DISABLED: "true",
  OTEL_TRACES_EXPORTER: "none",
  OTEL_METRICS_EXPORTER: "none",
  OTEL_LOGS_EXPORTER: "none",
};
export const TELEMETRY_ENV_PREFIXES = [
  "OTEL_",
  "LANGSMITH_",
  "LANGCHAIN_TRACING",
  "LANGCHAIN_API_KEY",
];

export function imageRuntimes(kind: ImageKind): (AgentRuntime | string)[] {
  return kind === "py"
    ? ["deepagents", "strands", "strands-harness"]
    : [
        "opencode",
        "hermes",
        new StrandsRuntime({ entrypoint: "agent.ts", language: "typescript" }),
      ];
}

export function explicitImage(kind: ImageKind): string | undefined {
  const name = process.env[`RAYITO_E2E_AGENT_PROJECT_${kind.toUpperCase()}_TEMPLATE`];
  return name || (PREFIX ? `${PREFIX}-${kind}` : undefined);
}

export function providerFor(item: RuntimeCase): {
  provider: "bedrock" | "anthropic";
  model: string;
  secret: string;
} {
  if (item.id === "hermes" && ANTHROPIC_SECRET) {
    return { provider: "anthropic", model: ANTHROPIC_MODEL, secret: ANTHROPIC_SECRET };
  }
  return { provider: "bedrock", model: BEDROCK_MODEL, secret: BEDROCK_SECRET };
}

export function projectFor(item: RuntimeCase, template: string): AgentProject {
  const { provider, model, secret } = providerFor(item);
  const gateway =
    provider === "bedrock"
      ? bedrockGateway(secret, { region: BEDROCK_REGION, models: [model] })
      : anthropicGateway(secret);
  return new AgentProject({
    name: `e2e-${item.id}`,
    runtime: item.runtime(),
    spec: new AgentSpec({
      model: new AgentModel({
        provider,
        id: model,
        gateway: GATEWAY_NAME,
        ...(provider === "bedrock" ? { region: BEDROCK_REGION } : {}),
      }),
      instructions: "Answer with one short sentence.",
    }),
    gateways: { [GATEWAY_NAME]: gateway },
    template,
    limits: new AgentLimits({ maxSteps: MAX_STEPS, timeoutMs: RUN_TIMEOUT_MS }),
    serve: { timeoutMs: DEPLOY_TIMEOUT_MS, idle: null },
  });
}

export function unsupportedReason(item: RuntimeCase, template: string): string | undefined {
  try {
    projectFor(item, template);
  } catch (error) {
    if (error instanceof InvalidArgumentError) {
      return error.message;
    }
    throw error;
  }
  return undefined;
}

const builtImages: Partial<Record<ImageKind, Promise<string>>> = {};

/** El código de prueba del tipo de imagen: `py` o, para `cli`, `ts`. */
export function appFixture(kind: ImageKind): string {
  return join(FIXTURES, kind === "py" ? "py" : "ts");
}

/** El nombre de la imagen del tipo; la construye una vez por proceso si el
 * entorno trae el prefijo y el bucket. */
export function agentImage(kind: ImageKind, region: string): Promise<string> {
  const cached = builtImages[kind];
  if (cached !== undefined) {
    return cached;
  }
  const name = explicitImage(kind);
  if (name === undefined) {
    return Promise.reject(
      new Error("faltan las imágenes: exporta el prefijo y el bucket, o los nombres"),
    );
  }
  const ready = BUCKET
    ? new AgentTemplate({
        name,
        runtimes: imageRuntimes(kind),
        app: appFixture(kind),
        ...(BASE ? { base: BASE } : {}),
      })
        .build({ bucket: BUCKET, region, timeoutMs: BUILD_TIMEOUT_MS })
        .then(() => name)
    : Promise.resolve(name);
  builtImages[kind] = ready;
  return ready;
}

/** El `rayito-agent.toml` del caso: el mismo proyecto que `projectFor`, sobre
 * la imagen ya construida, para las pruebas de la CLI. Sólo Bedrock.
 * `[source]` apunta al propio directorio, que lleva el código del caso
 * (`appFixture`): `deploy` reconstruye la imagen desde ahí y, sin código, el
 * punto de entrada no existiría. */
export function projectFileText(item: RuntimeCase, template: string): string {
  const { model, secret } = providerFor(item);
  return [
    'schema = "rayito.agent-project/1"',
    `name = "e2e-${item.id}"`,
    "",
    `[runtime]\n${item.runtimeToml}`,
    "",
    '[source]\npath = "."',
    "",
    `[model]\nprovider = "bedrock"\nid = "${model}"\nregion = "${BEDROCK_REGION}"\ngateway = "${GATEWAY_NAME}"`,
    "",
    `[gateways.${GATEWAY_NAME}]\npreset = "bedrock"\nsecret = "${secret}"\nregion = "${BEDROCK_REGION}"\nmodels = ["${model}"]`,
    "",
    '[agent]\ninstructions = "Answer with one short sentence."',
    "",
    `[limits]\nmax_steps = ${MAX_STEPS}\ntimeout_seconds = ${RUN_TIMEOUT_MS / MS_PER_SECOND}`,
    "",
    `[serve]\ntimeout_seconds = ${DEPLOY_TIMEOUT_MS / MS_PER_SECOND}\nidle_seconds = 0`,
    "",
    `[image]\ntemplate = "${template}"`,
    "",
  ].join("\n");
}

/** El prompt que pide al agente volcar su entorno con su herramienta de shell. */
export function envDumpPrompt(path: string): string {
  return `Run exactly \`env > ${path}\` with your shell tool, then reply with the single word done.`;
}

/** La clave de Bedrock del secreto del e2e, sin el prefijo `Bearer `. Sólo para
 * comprobar que el agente no puede leerla: nunca se imprime. */
export async function modelCredential(region: string): Promise<string> {
  const cache = new SecretCache({ store: new SecretStore({ region }) });
  return (await cache.get(BEDROCK_SECRET)).replace(/^Bearer /, "");
}

/** Los sitios del sandbox donde se lee `key`: su entorno, el de `rayd`
 * (`/proc/1/environ`, `/proc/1/mem`) y cualquier fichero legible. Vacío si no
 * se lee en ninguno. Devuelve nombres de sitio, nunca la clave. */
export async function sandboxExposesKey(sandbox: Sandbox, key: string): Promise<string[]> {
  async function sh(cmd: string): Promise<{ exitCode: number; stdout: string }> {
    try {
      return await sandbox.commands.run(cmd, { timeoutMs: CREDENTIAL_PROBE_TIMEOUT_MS });
    } catch (error) {
      if (error instanceof CommandExitError) {
        return error;
      }
      throw error;
    }
  }
  const found: string[] = [];
  if ((await sh("env")).stdout.includes(key)) {
    found.push("env");
  }
  const environ = await sh("cat /proc/1/environ 2>&1");
  if (environ.exitCode === 0 || environ.stdout.includes(key)) {
    found.push("proc-1-environ");
  }
  if ((await sh("head -c 16 /proc/1/mem")).exitCode === 0) {
    found.push("proc-1-mem");
  }
  const needle = key.slice(0, Math.floor(key.length / 2));
  const roots = CREDENTIAL_SEARCH_ROOTS.join(" ");
  const skipped = CREDENTIAL_SEARCH_SKIPPED.map((name) => `--exclude-dir=${name}`).join(" ");
  const files = await sh(`grep -rIlsF '${needle}' ${roots} ${skipped} 2>/dev/null | head -5`);
  if (files.stdout.split(/\s+/).some(Boolean)) {
    found.push("readable-file");
  }
  return found;
}

/** Los nombres de variable del volcado que encenderían trazas hacia fuera. Los
 * interruptores que la apagan (`OTEL_SDK_DISABLED=true`, los `OTEL_*_EXPORTER=none`
 * que pone Strands) no cuentan. */
export function telemetryEnvNames(envDump: string): string[] {
  return envDump
    .split("\n")
    .filter((line) => line.includes("="))
    .map((line) => [line.slice(0, line.indexOf("=")), line.slice(line.indexOf("=") + 1)] as const)
    .filter(
      ([name, value]) =>
        TELEMETRY_ENV_PREFIXES.some((prefix) => name.startsWith(prefix)) &&
        TELEMETRY_OFF_SWITCHES[name] !== value,
    )
    .map(([name]) => name);
}
