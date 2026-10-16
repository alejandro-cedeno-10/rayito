/**
 * `rayito agent template build` (`ai-agent-fast-start`): construye la imagen
 * de `AgentTemplate` con los runtimes del agente de IA. Mismas banderas que
 * `rayito template build` más `--no-deepagents`, `--no-prefetch` y
 * `--kernel-warmup`; sin fichero de spec, porque la receta es la del SDK.
 */

import {
  AGENT_TEMPLATE_RUNTIMES,
  AgentTemplate,
  DEFAULT_AGENT_TEMPLATE_BASE,
  DEFAULT_AGENT_TEMPLATE_NAME,
} from "../../agent/template.js";
import { BuildError } from "../../errors.js";
import { DEFAULT_BUILD_TIMEOUT_MS } from "../../images/gateway.js";
import { AGENT_MIN_MEMORY_MIB } from "../../limits.js";
import { MS_PER_SECOND } from "../../units.js";
import { defineCommand, defineGroup, flag, option, toggle } from "../command.js";
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
