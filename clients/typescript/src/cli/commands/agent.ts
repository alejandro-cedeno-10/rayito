/**
 * `rayito agent ...`: el agente de IA como proyecto.
 *
 * - `agent template build` (`ai-agent-fast-start`): construye la imagen de
 *   `AgentTemplate` con los runtimes del agente. Mismas banderas que `rayito
 *   template build` más `--no-deepagents`, `--no-prefetch` y
 *   `--kernel-warmup`; sin fichero de spec, porque la receta es la del SDK.
 * - `agent init | build | run | deploy | export | invoke | status | stop`
 *   (`agent-runtimes-byo`): conchas finas sobre `AgentProject` y
 *   `AgentDeployment`. `init` y `export` no llaman a AWS. Códigos de salida: 1
 *   si la operación falla, 2 para uso (fichero de proyecto inválido, ficheros
 *   que `init` no sobrescribe sin `--force`, destino de `export` ocupado).
 * - `agent detect` y `agent deploy [RUTA]` (ADR-029): detectan el proyecto sin
 *   ejecutar su código y lo despliegan como una flota con `deployAgent()`
 *   (`--dry-run` sólo calcula el plan y el coste). `invoke`, `status` y `stop`
 *   aceptan el nombre de la flota además del id de un sandbox.
 */

import { type AgentEvent, type AgentResult, failureMessage } from "../../agent/events.js";
import { projectNameOf } from "../../agent/project-file.js";
import {
  AGENT_TEMPLATE_RUNTIMES,
  AgentTemplate,
  DEFAULT_AGENT_TEMPLATE_BASE,
  DEFAULT_AGENT_TEMPLATE_NAME,
} from "../../agent/template.js";
import {
  AgentError,
  BuildError,
  CapacityError,
  InvalidArgumentError,
  SandboxError,
} from "../../errors.js";
import { DEFAULT_BUILD_TIMEOUT_MS } from "../../images/gateway.js";
import { AGENT_MIN_MEMORY_MIB } from "../../limits.js";
import { MS_PER_SECOND } from "../../units.js";
import {
  CompactBuildLog,
  deployToken,
  detectionLines,
  fleetToken,
  PairFlagError,
  parseFlagPairs,
} from "../agent-deploy.js";
import {
  DEFAULT_PROVIDER,
  LANGUAGES,
  PROVIDER_PRESETS,
  PYTHON_LANGUAGE,
  SCAFFOLD_FALLBACK_REGION,
  SCAFFOLD_RUNTIMES,
  ScaffoldConflictError,
  scaffoldFiles,
  TYPESCRIPT_LANGUAGE,
  writeScaffold,
} from "../agent-scaffold.js";
import {
  argument,
  type CommandContext,
  defineCommand,
  defineGroup,
  flag,
  option,
  toggle,
} from "../command.js";
import { EXIT_USAGE } from "../exit-codes.js";
import { UsageError } from "../failures.js";
import type {
  AgentDeployFlags,
  AgentDeployRequest,
  AgentDetectOverrides,
  DeployedFleetView,
  HiddenAppRoutes,
} from "../ops-ports.js";
import { TokenFileError } from "../tokens.js";
import { publishBaseImage } from "./image-tools.js";
import { tokenFileOption, tokenOrUsage } from "./sandbox.js";
import { buildLogPrinter, failure, printBuildInfo } from "./template.js";

const BUILD_TIMEOUT_SECONDS = DEFAULT_BUILD_TIMEOUT_MS / MS_PER_SECOND;
const DEEPAGENTS_RUNTIME = "deepagents";

export const agentGroup = defineGroup({
  path: ["agent"],
  help: "Agente de IA dentro del sandbox.",
});

export const agentTemplateGroup = defineGroup({
  path: ["agent", "template"],
  help: "Plantilla de imagen con los runtimes del agente (AgentTemplate).",
});

export const agentTemplateBuildCommand = defineCommand({
  path: ["agent", "template", "build"],
  help:
    "Construye la imagen --name con OpenCode, ripgrep y (salvo --no-deepagents) deepagents, " +
    "todo fijado por hash. Cada versión nueva cuesta almacenamiento de snapshot " +
    "(≈ 3,0 GB, mínimo una semana: ≈ $0,056/semana); las versiones se borran con `rayito image`.",
  parameters: [
    option(["--bucket"], { required: true, help: "Bucket S3 del artefacto de build." }),
    option(["--name"], {
      default: DEFAULT_AGENT_TEMPLATE_NAME,
      help: "Nombre de la imagen a crear/actualizar.",
    }),
    option(["--base"], {
      default: DEFAULT_AGENT_TEMPLATE_BASE,
      help: "Imagen base (publicada con rayito image publish).",
    }),
    option(["--memory-mb"], {
      type: "int",
      default: AGENT_MIN_MEMORY_MIB,
      help: "Memoria mínima de la imagen.",
    }),
    toggle(["--deepagents", "--no-deepagents"], {
      default: true,
      help: "Instala el venv de deepagents.",
    }),
    toggle(["--prefetch", "--no-prefetch"], {
      default: true,
      help: "Hornea el demonio de precarga.",
    }),
    toggle(["--kernel-warmup", "--no-kernel-warmup"], {
      default: false,
      help:
        "Mantiene el calentamiento del kernel de run_code (numpy, pandas...). Apagado por " +
        "defecto: create() vuelve antes y el snapshot pesa menos.",
    }),
    flag(["--force"], { help: "Reconstruye aunque nada cambiara." }),
    option(["--timeout"], {
      type: "float",
      default: BUILD_TIMEOUT_SECONDS,
      help: "Segundos de espera del build.",
    }),
  ],
  run: async (context) => {
    const { values } = context;
    const { templates } = await context.services();
    try {
      const template = new AgentTemplate({
        name: values.string("name") ?? DEFAULT_AGENT_TEMPLATE_NAME,
        base: values.string("base") ?? DEFAULT_AGENT_TEMPLATE_BASE,
        runtimes: AGENT_TEMPLATE_RUNTIMES.filter(
          (runtime) => values.flag("deepagents") || runtime !== DEEPAGENTS_RUNTIME,
        ),
        prefetch: values.flag("prefetch"),
        kernelWarmup: values.flag("kernel_warmup"),
        memoryMib: values.number("memory_mb"),
      });
      const info = await templates.buildAgent(template, {
        bucket: values.string("bucket") ?? "",
        force: values.flag("force"),
        timeoutMs: values.number("timeout") * MS_PER_SECOND,
        onBuildLogs: buildLogPrinter(context),
      });
      printBuildInfo(context, info);
      return undefined;
    } catch (error) {
      if (error instanceof BuildError) {
        return failure(context, `build fallido: ${error.message}`);
      }
      throw error;
    }
  },
});

const PROJECT_FILE_DEFAULT = ".";
const PROJECT_FILE_HELP = "Fichero o directorio del proyecto (rayito-agent.toml o .json).";
const STREAM_HELP =
  "Imprime los eventos a medida que llegan (en --json, una línea JSON por evento).";
const EXPORT_TARGETS = ["docker", "agentcore"] as const;
const SESSION_HELP = "Continúa una sesión anterior.";

function usageFailure(context: CommandContext, message: string): number {
  context.console.echo(`rayito: ${message}`, { err: true });
  return EXIT_USAGE;
}

function badChoice(context: CommandContext, flagName: string, allowed: readonly string[]): number {
  return usageFailure(context, `${flagName}: admitidas: ${allowed.join(", ")}`);
}

function isOneOf(value: string, allowed: readonly string[]): boolean {
  return allowed.includes(value);
}

const USAGE_UNKNOWN_LABEL = "desconocido";
const CUSTOM_INIT_HINT =
  'la app usa protocol = "raw": el despliegue reenvía tus rutas (/health, /invoke) con ' +
  'deployment.url y deployment.headers; deployment.invoke() es para protocol = "agentcore"';

function rawRoutesHint(routes: HiddenAppRoutes): string {
  return (
    `aviso: con protocol = "${routes.protocol}" el despliegue sólo expone /ping y /invocations; ` +
    `tus rutas (${routes.healthPath} y ${routes.invokePath}) no se alcanzan desde fuera. ` +
    'Usa protocol = "raw" en [runtime.app] para exponerlas.'
  );
}

function resultDocument(result: AgentResult): Record<string, unknown> {
  const { usage } = result;
  return {
    sessionId: result.sessionId,
    text: result.text,
    steps: result.steps,
    usage: {
      input: usage.input,
      output: usage.output,
      reasoning: usage.reasoning,
      cacheRead: usage.cacheRead,
      cacheWrite: usage.cacheWrite,
      total: usage.total,
    },
    usageReported: result.usageReported !== false,
    exitCode: result.exitCode,
  };
}

function printResult(context: CommandContext, result: AgentResult): void {
  if (context.globals.json) {
    context.console.emitJson(resultDocument(result));
    return;
  }
  context.console.echo(result.text);
  context.console.echo(`session_id=${result.sessionId}`, { err: true });
  const tokens = result.usageReported === false ? USAGE_UNKNOWN_LABEL : result.usage.total;
  context.console.echo(`steps=${result.steps} tokens=${tokens}`, { err: true });
}

/**
 * Escribe los eventos de un stream: en `--json`, un objeto por línea; en
 * texto, los `text_delta` tal cual (el `text` entero sólo si no hubo deltas)
 * y las herramientas en stderr. Un `agent_failed` termina la orden con 1.
 */
class EventPrinter {
  readonly #context: CommandContext;
  #sawDelta = false;
  #wroteText = false;
  failed: AgentEvent | undefined;

  constructor(context: CommandContext) {
    this.#context = context;
  }

  readonly print = (event: AgentEvent): void => {
    const { console, globals } = this.#context;
    if (globals.json) {
      console.emitJsonLine(event);
    } else if (event.type === "text_delta") {
      this.#sawDelta = true;
      this.#write(event.text);
    } else if (event.type === "text" && !this.#sawDelta) {
      this.#write(event.text);
    } else if (event.type === "tool_call") {
      console.echo(`tool=${event.name} status=${event.status}`, { err: true });
    }
    if (event.type === "agent_failed") {
      this.failed = event;
    }
  };

  finish(): number | undefined {
    if (this.#wroteText) {
      this.#context.console.writeRaw("stdout", "\n");
    }
    if (this.failed?.type === "agent_failed") {
      return failure(this.#context, failureMessage(this.failed.reason, this.failed.detailCode));
    }
    return undefined;
  }

  #write(text: string): void {
    this.#wroteText = true;
    this.#context.console.write(text);
  }
}

async function loadProject(context: CommandContext) {
  return context.agentProjects.load(context.values.string("file") ?? PROJECT_FILE_DEFAULT);
}

async function guarded(
  context: CommandContext,
  action: () => Promise<number | undefined>,
): Promise<number | undefined> {
  try {
    return await action();
  } catch (error) {
    if (error instanceof InvalidArgumentError) {
      return usageFailure(context, error.message);
    }
    if (error instanceof AgentError) {
      return failure(context, error.message);
    }
    if (error instanceof BuildError) {
      return failure(context, `build fallido: ${error.message}`);
    }
    throw error;
  }
}

const fileOption = option(["--file"], { default: PROJECT_FILE_DEFAULT, help: PROJECT_FILE_HELP });

export const agentInitCommand = defineCommand({
  path: ["agent", "init"],
  help:
    "Escribe rayito-agent.toml y un andamiaje mínimo para el runtime. No llama a AWS. " +
    "Sin --force no sobrescribe nada: si algún fichero existe, no escribe ninguno y sale con 2.",
  parameters: [
    argument("directory", {
      type: "path",
      default: ".",
      help: "Directorio del proyecto (se crea si no existe).",
    }),
    option(["--runtime"], { default: "strands", help: "Runtime del agente." }),
    option(["--language"], {
      default: PYTHON_LANGUAGE,
      help: "Lenguaje del agente (typescript sólo con strands).",
    }),
    option(["--provider"], { default: DEFAULT_PROVIDER, help: "Proveedor del modelo." }),
    option(["--model"], { help: "Id del modelo (obligatorio con openai)." }),
    option(["--region"], { help: "Región de Bedrock (por defecto, la de AWS)." }),
    flag(["--force"], { help: "Sobrescribe los ficheros que ya existan." }),
  ],
  run: async (context) => {
    const { values } = context;
    const directory = values.string("directory") ?? ".";
    const runtime = values.string("runtime") ?? "";
    const language = values.string("language") ?? "";
    const provider = values.string("provider") ?? "";
    if (!isOneOf(runtime, SCAFFOLD_RUNTIMES)) {
      return badChoice(context, "--runtime", SCAFFOLD_RUNTIMES);
    }
    if (!isOneOf(language, LANGUAGES)) {
      return badChoice(context, "--language", LANGUAGES);
    }
    const presets = Object.keys(PROVIDER_PRESETS);
    if (!isOneOf(provider, presets)) {
      return badChoice(context, "--provider", presets);
    }
    const modelId = values.string("model") ?? PROVIDER_PRESETS[provider]?.defaultModel;
    if (modelId === undefined) {
      return usageFailure(context, `--model es obligatorio con --provider ${provider}`);
    }
    if (language === TYPESCRIPT_LANGUAGE && runtime !== "strands") {
      return usageFailure(context, "--language typescript sólo existe con --runtime strands");
    }
    const region =
      values.string("region") ??
      context.globals.region ??
      context.env.AWS_REGION ??
      SCAFFOLD_FALLBACK_REGION;
    const files = scaffoldFiles({
      name: projectNameOf(directory),
      runtime,
      language,
      provider,
      modelId,
      region,
    });
    try {
      const written = writeScaffold(directory, files, { force: values.flag("force") });
      if (context.globals.json) {
        context.console.emitJson({ directory, files: written });
        return undefined;
      }
      for (const name of written) {
        context.console.echo(`escrito ${name}`);
      }
      if (runtime === "custom") {
        context.console.echo(CUSTOM_INIT_HINT);
      }
      context.console.echo(
        `siguiente paso: rayito agent build --bucket <bucket> --file ${directory}`,
      );
      return undefined;
    } catch (error) {
      if (error instanceof ScaffoldConflictError) {
        return usageFailure(context, `ya existe ${error.paths.join(", ")}; --force lo sobrescribe`);
      }
      throw error;
    }
  },
});

export const agentBuildCommand = defineCommand({
  path: ["agent", "build"],
  help:
    "Construye la imagen del proyecto: el runtime y tu código, fijados por hash. Cada versión " +
    "nueva cuesta almacenamiento de snapshot (mínimo una semana); las versiones se borran " +
    "con `rayito image`.",
  parameters: [
    option(["--bucket"], { required: true, help: "Bucket S3 del artefacto de build." }),
    fileOption,
    flag(["--force"], { help: "Reconstruye aunque nada cambiara." }),
    option(["--timeout"], {
      type: "float",
      default: BUILD_TIMEOUT_SECONDS,
      help: "Segundos de espera del build.",
    }),
  ],
  run: (context) =>
    guarded(context, async () => {
      const project = await loadProject(context);
      const info = await project.build({
        bucket: context.values.string("bucket") ?? "",
        force: context.values.flag("force"),
        timeoutMs: context.values.number("timeout") * MS_PER_SECOND,
        onBuildLogs: buildLogPrinter(context),
      });
      printBuildInfo(context, info);
      return undefined;
    }),
});

export const agentRunCommand = defineCommand({
  path: ["agent", "run"],
  help:
    "Crea un sandbox del proyecto, ejecuta el agente una vez y lo destruye. Cuesta lo que dure " +
    "la VM y los tokens del modelo.",
  parameters: [
    argument("prompt", { required: true, help: "Lo que se le pide al agente." }),
    fileOption,
    option(["--session-id"], { help: SESSION_HELP }),
    option(["--model"], { help: "Otro id de modelo para esta ejecución." }),
    flag(["--stream"], { help: STREAM_HELP }),
  ],
  run: (context) =>
    guarded(context, async () => {
      const { values } = context;
      const project = await loadProject(context);
      const printer = values.flag("stream") ? new EventPrinter(context) : undefined;
      const result = await project.run(values.string("prompt") ?? "", {
        sessionId: values.string("session_id"),
        model: values.string("model"),
        onEvent: printer?.print,
      });
      if (printer !== undefined) {
        return printer.finish();
      }
      printResult(context, result);
      return undefined;
    }),
});

const DETECT_FLAGS_HELP = "Fija a mano lo que la detección decidiría.";
const SESSION_MODE_CHOICES = ["shared", "dedicated"] as const;
const MICROVM_ID_PREFIX = "microvm-";
const BASE_NEEDS_CONFIRMATION =
  "publicar la imagen base cuesta dinero: confírmalo con --yes (o desde una terminal)";
const STEP_MESSAGES: Readonly<Record<string, string>> = {
  base_image: "imagen base publicada",
  secret_created:
    "secreto creado para la pasarela {detail} (cuesta $0,40 al mes hasta que lo borres)",
  image: "imagen lista: versión {detail}",
  fleet: "{detail} VM(s) listos",
};

const detectOverrideOptions = [
  option(["--framework"], { help: DETECT_FLAGS_HELP }),
  option(["--language"], { help: DETECT_FLAGS_HELP }),
  option(["--entry"], { help: DETECT_FLAGS_HELP }),
  option(["--protocol"], { help: DETECT_FLAGS_HELP }),
];

function detectOverridesOf(context: CommandContext): AgentDetectOverrides {
  const { values } = context;
  return {
    framework: values.string("framework"),
    language: values.string("language"),
    entry: values.string("entry"),
    protocol: values.string("protocol"),
  };
}

/** Un paso de `deployAgent()` por línea en stderr (stdout queda para el
 * resultado). El bucket se nombra porque quien despliega tiene que saber
 * dónde fue el artefacto; nunca se nombra un secreto. */
function stepPrinter(
  context: CommandContext,
): (step: { readonly kind: string; readonly detail: string }) => void {
  return (step) => {
    const { console } = context;
    if (step.kind === "bucket") {
      const separator = step.detail.indexOf(":");
      const origin = step.detail.slice(0, separator);
      console.echo(`bucket del artefacto: ${step.detail.slice(separator + 1)} (${origin})`, {
        err: true,
      });
    } else if (step.kind === "warm") {
      console.echo(
        step.detail === "ready"
          ? "plazas calientes aparcadas"
          : "las plazas calientes siguen creándose; el despliegue ya sirve",
        { err: true },
      );
    } else {
      const template = STEP_MESSAGES[step.kind] ?? step.kind;
      console.echo(template.replace("{detail}", step.detail), { err: true });
    }
  };
}

export const agentDetectCommand = defineCommand({
  path: ["agent", "detect"],
  help:
    "Detecta el proyecto de un agente sin ejecutar su código y lo describe. Lee el fichero de " +
    "proyecto (si lo hay) y los manifiestos del directorio; lo que detecta se puede fijar con " +
    "--framework, --language, --entry y --protocol. No llama a AWS. Con --write guarda el " +
    "resultado como rayito-agent.json (no sobrescribe un fichero de proyecto existente).",
  parameters: [
    argument("path", {
      type: "path",
      default: PROJECT_FILE_DEFAULT,
      help: "Directorio del proyecto (por defecto, el actual).",
    }),
    ...detectOverrideOptions,
    flag(["--write"], { help: "Guarda el proyecto resuelto como rayito-agent.json." }),
  ],
  run: (context) =>
    guarded(context, async () => {
      const { values } = context;
      const path = values.string("path") ?? PROJECT_FILE_DEFAULT;
      const overrides = detectOverridesOf(context);
      const detected = await context.agentProjects.detect(path, overrides);
      const written = values.flag("write")
        ? await context.agentProjects.writeDetected(path, overrides)
        : undefined;
      if (context.globals.json) {
        context.console.emitJson({
          detection: detected.report ?? null,
          project: detected.document,
          written: written ?? null,
        });
        return undefined;
      }
      const lines = detectionLines(detected.report);
      for (const line of lines.length > 0
        ? lines
        : [`proyecto: ${String(detected.document.name)} (fichero de proyecto)`]) {
        context.console.echo(line);
      }
      if (written !== undefined) {
        context.console.echo(`escrito ${written}`);
      }
      return undefined;
    }),
});

function deployFlagsOf(context: CommandContext): AgentDeployFlags {
  const { values } = context;
  const queueTimeout = values.optionalNumber("queue_timeout");
  const timeout = values.optionalNumber("timeout");
  return {
    port: values.optionalNumber("port"),
    timeoutMs: timeout === undefined ? undefined : timeout * MS_PER_SECOND,
    memory: values.optionalNumber("memory"),
    maxSessions: values.optionalNumber("max_sessions"),
    idleSeconds: values.optionalNumber("idle"),
    queue: values.optionalNumber("queue"),
    queueTimeoutMs: queueTimeout === undefined ? undefined : queueTimeout * MS_PER_SECOND,
    env: parseFlagPairs(values.strings("env"), "--env"),
    allowOut: values.strings("allow_out"),
    minVms: values.optionalNumber("min_vms"),
    maxVms: values.optionalNumber("max_vms"),
    warm: values.optionalNumber("warm"),
    sessions: values.string("sessions"),
  };
}

function deployTarget(context: CommandContext): string {
  const { values } = context;
  const path = values.string("path");
  const file = values.string("file") ?? PROJECT_FILE_DEFAULT;
  if (path === undefined) {
    return file;
  }
  if (file !== PROJECT_FILE_DEFAULT && file !== path) {
    throw new UsageError("pasa la ruta del proyecto como argumento o con --file, no las dos");
  }
  return path;
}

/** Quién publica la imagen base si falta: sólo con `--base-artifact`, y sólo
 * tras confirmar el coste (`--yes`). */
function basePublisherOf(
  context: CommandContext,
  bucket: string | undefined,
): (() => Promise<void>) | undefined {
  const artifact = context.values.string("base_artifact");
  if (artifact === undefined) {
    return undefined;
  }
  return async () => {
    if (!context.values.flag("yes")) {
      throw new UsageError(BASE_NEEDS_CONFIRMATION);
    }
    const resolved = bucket ?? (context.env.RAYITO_BUCKET || undefined);
    if (resolved === undefined) {
      throw new UsageError("--bucket: pasa --bucket o exporta RAYITO_BUCKET");
    }
    await publishBaseImage(context, {
      artifact,
      bucket: resolved,
      imageName: DEFAULT_AGENT_TEMPLATE_BASE,
    });
  };
}

export const agentDeployCommand = defineCommand({
  path: ["agent", "deploy"],
  help:
    "Despliega el agente de un directorio como una flota de MicroVMs. Detecta el lenguaje, el " +
    "framework y la entrada de tu proyecto sin ejecutar su código (o usa su rayito-agent.toml), " +
    "construye la imagen sólo si cambió, resuelve la región, la imagen base, el secreto del " +
    "modelo y el bucket, y deja `--min-vms` VMs vivos y `--warm` plazas aparcadas. Cada valor " +
    "sale de la bandera, del fichero, del entorno o del descubrimiento, en ese orden; nada se " +
    "crea en silencio (ni buckets, ni secretos sin --create-secret, ni la imagen base sin " +
    "--base-artifact y --yes). Imprime el nombre de la flota, el sandbox y la URL; las " +
    "cabeceras de acceso no se imprimen nunca. El access token se guarda en --token-file (o es " +
    "el de RAYITO_ACCESS_TOKEN; si no hay ninguno, en .rayito/agent-token con modo 0600, que " +
    "se reutiliza al volver a desplegar). Vive como mucho 8 h: `rayito agent stop <nombre>` lo " +
    "termina. Con --dry-run no llama a AWS.",
  parameters: [
    argument("path", {
      type: "path",
      help: "Directorio del proyecto (por defecto, el actual).",
    }),
    fileOption,
    option(["--timeout"], {
      type: "float",
      help: "Segundos de vida del despliegue (por defecto, el del fichero).",
    }),
    option(["--port"], {
      type: "int",
      help: "Puerto del servicio (por defecto, el del fichero).",
    }),
    tokenFileOption,
    flag(["--keep-on-failure"], {
      help:
        "Si el arranque falla, deja el sandbox vivo para depurarlo (cuesta hasta que lo " +
        "termines con `rayito agent stop`).",
    }),
    ...detectOverrideOptions,
    option(["--memory"], {
      type: "int",
      help: "MiB de cada VM. Se fija en la imagen: cambiarla construye una versión nueva.",
    }),
    option(["--max-sessions"], { type: "int", help: "Sesiones simultáneas por VM." }),
    option(["--sessions"], {
      help: "Sesiones compartidas entre VMs (shared) o un MicroVM por sesión (dedicated).",
    }),
    option(["--min-vms"], { type: "int", help: "VMs siempre vivos." }),
    option(["--max-vms"], { type: "int", help: "VMs como máximo." }),
    option(["--warm"], { type: "int", help: "Plazas calientes (VMs aparcados)." }),
    option(["--idle"], {
      type: "int",
      help: "Segundos sin uso tras los que un VM se suspende.",
    }),
    option(["--queue"], { type: "int", help: "Peticiones que esperan en la cola del host." }),
    option(["--queue-timeout"], {
      type: "float",
      help: "Segundos máximos de espera en la cola.",
    }),
    option(["--env"], { multiple: true, help: "CLAVE=VALOR no secreto; repetible." }),
    option(["--allow-out"], { multiple: true, help: "Destino de salida permitido; repetible." }),
    option(["--bucket"], {
      help: "Bucket del artefacto (por defecto, RAYITO_BUCKET, el de CDK o el de rayito setup).",
    }),
    option(["--secret"], {
      multiple: true,
      help: "proveedor=nombre de un secreto que ya existe; repetible.",
    }),
    flag(["--create-secret"], {
      help: "Crea el secreto que falte desde la variable del proveedor (cuesta $0,40/mes).",
    }),
    option(["--base-artifact"], {
      type: "path",
      help: "Zip de la imagen base: se publica si no existe (pide --yes).",
    }),
    flag(["--yes"], { help: "Confirma lo que cuesta sin preguntar." }),
    flag(["--dry-run"], { help: "Sólo detecta y calcula el plan y el coste, sin AWS." }),
    toggle(["--build", "--no-build"], {
      help: "Construye la imagen (por defecto, si hay bucket) o reutiliza la existente.",
    }),
    flag(["--verbose"], { help: "Imprime todos los logs del build." }),
  ],
  run: (context) =>
    guarded(context, async () => {
      const { values } = context;
      const target = deployTarget(context);
      const sessions = values.string("sessions");
      if (sessions !== undefined && !isOneOf(sessions, SESSION_MODE_CHOICES)) {
        return badChoice(context, "--sessions", SESSION_MODE_CHOICES);
      }
      let flags: AgentDeployFlags;
      let secretNames: Record<string, string>;
      try {
        flags = deployFlagsOf(context);
        secretNames = parseFlagPairs(values.strings("secret"), "--secret");
      } catch (error) {
        if (error instanceof PairFlagError) {
          return usageFailure(context, error.message);
        }
        throw error;
      }
      const bucket = values.string("bucket");
      const verbose = values.flag("verbose");
      const compact = verbose
        ? undefined
        : new CompactBuildLog((line) => context.console.echo(line, { err: true }));
      const request = (token: string | undefined): AgentDeployRequest => ({
        path: target,
        overrides: detectOverridesOf(context),
        flags,
        settings: {
          bucket,
          secretNames,
          createSecretFromEnv: values.flag("create_secret"),
          basePublisher: basePublisherOf(context, bucket),
          accessToken: token,
          keepOnFailure: values.flag("keep_on_failure"),
          buildImage: values.optionalFlag("build"),
          onBuildLogs: verbose ? (line) => context.console.echo(line, { err: true }) : compact?.log,
          onStep: stepPrinter(context),
        },
      });
      if (values.flag("dry_run")) {
        const plan = await context.agentProjects.planDeploy(request(undefined));
        if (context.globals.json) {
          context.console.emitJson({
            dryRun: true,
            detection: plan.report ?? null,
            plan: plan.document,
          });
          return undefined;
        }
        for (const line of [...detectionLines(plan.report), ...plan.lines]) {
          context.console.echo(line);
        }
        context.console.echo("no se llamó a AWS (--dry-run)");
        return undefined;
      }
      let token: { token: string | undefined; file: string | undefined };
      try {
        token = deployToken(values.string("token_file"), context.env);
      } catch (error) {
        if (error instanceof TokenFileError) {
          return usageFailure(context, error.message);
        }
        throw error;
      }
      const deployed = await deployRequest(context, request(token.token), compact);
      if (typeof deployed === "number") {
        return deployed;
      }
      printDeployed(context, deployed, token.file);
      return undefined;
    }),
});

async function deployRequest(
  context: CommandContext,
  request: AgentDeployRequest,
  compact: CompactBuildLog | undefined,
): Promise<DeployedFleetView | number> {
  try {
    const detected = await context.agentProjects.detect(request.path, request.overrides);
    for (const line of detectionLines(detected.report)) {
      context.console.echo(line, { err: true });
    }
    return await context.agentProjects.deployFleet(request);
  } catch (error) {
    if (error instanceof SandboxError && !(error instanceof InvalidArgumentError)) {
      return failure(context, error.message);
    }
    throw error;
  } finally {
    compact?.finish();
  }
}

function printDeployed(
  context: CommandContext,
  fleet: DeployedFleetView,
  tokenFile: string | undefined,
): void {
  if (fleet.hiddenRoutes !== undefined) {
    context.console.echo(rawRoutesHint(fleet.hiddenRoutes), { err: true });
  }
  const [first] = fleet.sandboxIds;
  if (context.globals.json) {
    context.console.emitJson({
      name: fleet.name,
      sandboxId: first,
      sandboxIds: [...fleet.sandboxIds],
      url: fleet.url,
      port: fleet.port,
      tokenFile: tokenFile ?? null,
    });
    return;
  }
  context.console.echo(`name=${fleet.name}`);
  context.console.echo(`sandbox_id=${first}`);
  context.console.echo(`url=${fleet.url}`);
  context.console.echo(`port=${fleet.port}`);
  context.console.echo(`members=${fleet.sandboxIds.length}`);
  if (tokenFile !== undefined) {
    context.console.echo(`token_file=${tokenFile}`);
  }
}

export const agentExportCommand = defineCommand({
  path: ["agent", "export"],
  help:
    "Escribe un contexto de build de contenedor con el agente. No llama a AWS. En el contenedor " +
    "la credencial del modelo la pone la plataforma: la garantía de la pasarela de secretos no " +
    "aplica fuera de Rayito.",
  parameters: [
    option(["--out"], { type: "path", required: true, help: "Directorio de salida." }),
    option(["--target"], {
      required: true,
      help: "docker o agentcore (contexto de build del contenedor).",
    }),
    fileOption,
    flag(["--overwrite"], { help: "Escribe aunque el directorio no esté vacío." }),
  ],
  run: (context) =>
    guarded(context, async () => {
      const { values } = context;
      const target = values.string("target") ?? "";
      if (!isOneOf(target, EXPORT_TARGETS)) {
        return badChoice(context, "--target", EXPORT_TARGETS);
      }
      const project = await loadProject(context);
      const exported = await project.export({
        target,
        outDir: values.string("out") ?? "",
        overwrite: values.flag("overwrite"),
      });
      if (context.globals.json) {
        context.console.emitJson({
          path: exported.path,
          files: [...exported.files],
          port: exported.port,
          platform: exported.platform,
        });
        return undefined;
      }
      context.console.echo(`path=${exported.path}`);
      context.console.echo(
        `files=${exported.files.length} port=${exported.port} platform=${exported.platform}`,
      );
      return undefined;
    }),
});

/** Un id de sandbox empieza por `microvm-`; cualquier otra cosa es el nombre
 * de una flota (el de su imagen). */
function isFleetName(target: string): boolean {
  return !target.startsWith(MICROVM_ID_PREFIX);
}

const TARGET_HELP = "Id del sandbox desplegado o nombre de la flota.";

export const agentInvokeCommand = defineCommand({
  path: ["agent", "invoke"],
  help:
    "Llama a un agente desplegado con `rayito agent deploy`. Con el id de un sandbox habla " +
    "con ese VM; con el nombre de la flota reparte la sesión entre sus VMs (el `session_id` " +
    "que devuelve identifica el VM: pásalo con --session-id para seguir la conversación).",
  parameters: [
    argument("sandbox_id", { required: true, help: TARGET_HELP }),
    argument("prompt", { required: true, help: "Lo que se le pide al agente." }),
    option(["--session-id"], { help: SESSION_HELP }),
    flag(["--stream"], { help: STREAM_HELP }),
    tokenFileOption,
  ],
  run: (context) =>
    guarded(context, async () => {
      const { values } = context;
      const target = values.string("sandbox_id") ?? "";
      const client = isFleetName(target)
        ? await context.agentProjects.connectFleet(target, fleetTokenOrUsage(context))
        : await context.agentProjects.connect(target, tokenOrUsage(context));
      const prompt = values.string("prompt") ?? "";
      const options = { sessionId: values.string("session_id") };
      try {
        if (values.flag("stream")) {
          const printer = new EventPrinter(context);
          const events = await client.stream(prompt, options);
          for await (const event of events) {
            printer.print(event);
          }
          return printer.finish();
        }
        printResult(context, await client.invoke(prompt, options));
        return undefined;
      } catch (error) {
        if (error instanceof CapacityError) {
          return failure(context, error.message);
        }
        throw error;
      }
    }),
});

/** El token de una flota: `--token-file`, `RAYITO_ACCESS_TOKEN` o el fichero
 * por defecto de `deploy`; sin ninguno, error de uso. */
function fleetTokenOrUsage(context: CommandContext): string {
  try {
    const token = fleetToken(context.values.string("token_file"), context.env);
    if (token === undefined) {
      return tokenOrUsage(context);
    }
    return token;
  } catch (error) {
    if (error instanceof TokenFileError) {
      throw new UsageError(error.message);
    }
    throw error;
  }
}

export const agentStatusCommand = defineCommand({
  path: ["agent", "status"],
  help:
    "Estado de un agente desplegado: si está listo y si está ocupado. Con el nombre de una " +
    "flota, el de cada uno de sus VMs (uno que este proceso no ha abierto figura como " +
    "`unknown`: preguntarle lo reanudaría).",
  parameters: [argument("sandbox_id", { required: true, help: TARGET_HELP }), tokenFileOption],
  run: (context) =>
    guarded(context, async () => {
      const sandboxId = context.values.string("sandbox_id") ?? "";
      if (isFleetName(sandboxId)) {
        const fleet = await context.agentProjects.connectFleet(
          sandboxId,
          fleetTokenOrUsage(context),
        );
        const status = await fleet.status();
        if (context.globals.json) {
          context.console.emitJson(status);
          return undefined;
        }
        context.console.echo(`name: ${status.name}`);
        context.console.echo(
          `members: ${status.members.length} (min ${status.minVms}, max ${status.maxVms})`,
        );
        for (const member of status.members) {
          context.console.echo(`sandbox_id=${member.sandboxId} state=${member.state}`);
        }
        return undefined;
      }
      const deployment = await context.agentProjects.connect(sandboxId, tokenOrUsage(context));
      const status = await deployment.status();
      const document = {
        sandboxId,
        state: status.state,
        ready: status.ready,
        busy: status.busy,
        port: status.port,
        project: status.project,
      };
      if (context.globals.json) {
        context.console.emitJson(document);
        return undefined;
      }
      for (const [key, value] of Object.entries(document)) {
        context.console.echo(`${key}: ${value}`);
      }
      return undefined;
    }),
});

export const agentStopCommand = defineCommand({
  path: ["agent", "stop"],
  help:
    "Para un agente desplegado: destruye su sandbox, o todos los de la flota si le pasas su " +
    "nombre. No necesita el access token.",
  parameters: [argument("sandbox_id", { required: true, help: TARGET_HELP })],
  run: (context) =>
    guarded(context, async () => {
      const sandboxId = context.values.string("sandbox_id") ?? "";
      if (isFleetName(sandboxId)) {
        const fleet = await context.agentProjects.connectFleet(sandboxId, undefined);
        const count = await fleet.stop();
        if (context.globals.json) {
          context.console.emitJson({ name: sandboxId, stopped: count });
          return undefined;
        }
        context.console.echo(`parados ${count} VM(s) de la flota name=${sandboxId}`);
        return undefined;
      }
      const stopped = await context.agentProjects.stop(sandboxId);
      if (context.globals.json) {
        context.console.emitJson({ sandboxId, stopped });
        return undefined;
      }
      context.console.echo(`${stopped ? "parado" : "ya no existe"} sandbox_id=${sandboxId}`);
      return undefined;
    }),
});
