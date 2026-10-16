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
 */

import { type AgentEvent, type AgentResult, failureMessage } from "../../agent/events.js";
import {
  AGENT_TEMPLATE_RUNTIMES,
  AgentTemplate,
  DEFAULT_AGENT_TEMPLATE_BASE,
  DEFAULT_AGENT_TEMPLATE_NAME,
} from "../../agent/template.js";
import { AgentError, BuildError, InvalidArgumentError } from "../../errors.js";
import { DEFAULT_BUILD_TIMEOUT_MS } from "../../images/gateway.js";
import { AGENT_MIN_MEMORY_MIB } from "../../limits.js";
import { generateAccessToken } from "../../payload.js";
import { MS_PER_SECOND } from "../../units.js";
import {
  DEFAULT_PROVIDER,
  LANGUAGES,
  PROVIDER_PRESETS,
  PYTHON_LANGUAGE,
  projectNameOf,
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
import { TOKEN_ENV_VAR, TokenFileError, writeTokenFile } from "../tokens.js";
import { tokenFileOption, tokenOrUsage } from "./sandbox.js";
import { buildLogPrinter, failure, printBuildInfo } from "./template.js";

const BUILD_TIMEOUT_SECONDS = DEFAULT_BUILD_TIMEOUT_MS / MS_PER_SECOND;
const DEPLOY_NEEDS_TOKEN_FILE_MESSAGE = `sin ${TOKEN_ENV_VAR}, deploy necesita --token-file para poder volver a conectarte (invoke y status)`;
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
  context.console.echo(`steps=${result.steps} tokens=${result.usage.total}`, { err: true });
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

/** El token del despliegue: `RAYITO_ACCESS_TOKEN` o uno nuevo. Se guarda en
 * `--token-file` antes de crear el sandbox, así un fallo al escribirlo nunca
 * deja un VM huérfano. Sin ninguno de los dos no habría forma de volver a
 * conectarse, y `deploy` no empieza. */
function deployToken(context: CommandContext): string {
  const existing = context.env[TOKEN_ENV_VAR] || undefined;
  const tokenFile = context.values.string("token_file");
  if (existing === undefined && tokenFile === undefined) {
    throw new UsageError(DEPLOY_NEEDS_TOKEN_FILE_MESSAGE);
  }
  const token = existing ?? generateAccessToken();
  if (tokenFile !== undefined) {
    try {
      writeTokenFile(tokenFile, token);
    } catch (error) {
      if (error instanceof TokenFileError) {
        throw new UsageError(error.message);
      }
      throw error;
    }
  }
  return token;
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

export const agentDeployCommand = defineCommand({
  path: ["agent", "deploy"],
  help:
    "Despliega el agente como servicio HTTP en un sandbox y deja la VM viva. Imprime el id del " +
    "sandbox y la URL; las cabeceras de acceso no se imprimen nunca (se piden con " +
    "AgentDeployment.headers). El access token del sandbox se guarda en --token-file (o es el " +
    "de RAYITO_ACCESS_TOKEN): `invoke` y `status` lo necesitan. Vive como mucho 8 h: para " +
    "pararlo, `rayito agent stop`.",
  parameters: [
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
  ],
  run: (context) =>
    guarded(context, async () => {
      const { values } = context;
      const accessToken = deployToken(context);
      const project = await loadProject(context);
      const timeout = values.optionalNumber("timeout");
      const deployed = await project.deploy({
        port: values.optionalNumber("port"),
        timeoutMs: timeout === undefined ? undefined : timeout * MS_PER_SECOND,
        accessToken,
      });
      const { agent, port } = deployed;
      if (context.globals.json) {
        context.console.emitJson({ sandboxId: agent.sandboxId, url: agent.url, port });
        return undefined;
      }
      context.console.echo(`sandbox_id=${agent.sandboxId}`);
      context.console.echo(`url=${agent.url}`);
      context.console.echo(`port=${port}`);
      return undefined;
    }),
});

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

export const agentInvokeCommand = defineCommand({
  path: ["agent", "invoke"],
  help: "Llama a un agente desplegado con `rayito agent deploy`.",
  parameters: [
    argument("sandbox_id", { required: true, help: "Id del sandbox desplegado." }),
    argument("prompt", { required: true, help: "Lo que se le pide al agente." }),
    option(["--session-id"], { help: SESSION_HELP }),
    flag(["--stream"], { help: STREAM_HELP }),
    tokenFileOption,
  ],
  run: (context) =>
    guarded(context, async () => {
      const { values } = context;
      const deployment = await context.agentProjects.connect(
        values.string("sandbox_id") ?? "",
        tokenOrUsage(context),
      );
      const prompt = values.string("prompt") ?? "";
      const options = { sessionId: values.string("session_id") };
      if (values.flag("stream")) {
        const printer = new EventPrinter(context);
        for await (const event of deployment.stream(prompt, options)) {
          printer.print(event);
        }
        return printer.finish();
      }
      printResult(context, await deployment.invoke(prompt, options));
      return undefined;
    }),
});

export const agentStatusCommand = defineCommand({
  path: ["agent", "status"],
  help: "Estado de un agente desplegado: si está listo y si está ocupado.",
  parameters: [
    argument("sandbox_id", { required: true, help: "Id del sandbox desplegado." }),
    tokenFileOption,
  ],
  run: (context) =>
    guarded(context, async () => {
      const sandboxId = context.values.string("sandbox_id") ?? "";
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
  help: "Para un agente desplegado: destruye su sandbox. No necesita el access token.",
  parameters: [argument("sandbox_id", { required: true, help: "Id del sandbox desplegado." })],
  run: (context) =>
    guarded(context, async () => {
      const sandboxId = context.values.string("sandbox_id") ?? "";
      const stopped = await context.agentProjects.stop(sandboxId);
      if (context.globals.json) {
        context.console.emitJson({ sandboxId, stopped });
        return undefined;
      }
      context.console.echo(`${stopped ? "parado" : "ya no existe"} sandbox_id=${sandboxId}`);
      return undefined;
    }),
});
