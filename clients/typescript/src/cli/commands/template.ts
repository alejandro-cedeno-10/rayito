/**
 * `rayito template build | status | logs` (m15-templates): construye, sondea y
 * explica un build de `Template`. `build` imprime cada línea del log del
 * build (salvo en `--json`); `status` y `logs` sin `--version` usan la versión
 * más reciente de la imagen, sin importar si terminó bien o mal.
 *
 * El fichero de spec es un módulo ESM que exporta `template` (una instancia
 * de `Template`); se importa tal cual, con el mismo nivel de confianza que
 * ejecutar cualquier script local: la CLI no lo aísla, igual que `rayito
 * image publish` no aísla su Dockerfile.
 */

import { BuildError } from "../../errors.js";
import { DEFAULT_BUILD_TIMEOUT_MS, DEFAULT_MEMORY_MIB } from "../../images/gateway.js";
import { BUILD_LOG_LINES } from "../../templates/build.js";
import { MS_PER_SECOND } from "../../units.js";
import {
  argument,
  type CommandContext,
  defineCommand,
  defineGroup,
  flag,
  option,
} from "../command.js";
import { EXIT_FAILURE } from "../exit-codes.js";
import { TemplateSpecError } from "../ops-ports.js";

const BUILD_TIMEOUT_SECONDS = DEFAULT_BUILD_TIMEOUT_MS / MS_PER_SECOND;

export const templateGroup = defineGroup({
  path: ["template"],
  help: "Templates declarativos (Template.build): build, status, logs.",
});

/** Un fallo de la orden que Python imprime como `rayito: <mensaje>` y sale con 1. */
export function failure(context: CommandContext, message: string): number {
  context.console.echo(`rayito: ${message}`, { err: true });
  return EXIT_FAILURE;
}

/** La orden de un build que termina con `template_id=` y `build_id=` (o el JSON de `BuildInfo`). */
export function printBuildInfo(
  context: CommandContext,
  info: { readonly templateId: string; readonly buildId: string; readonly alias: string },
): void {
  if (context.globals.json) {
    context.console.emitJson({
      templateId: info.templateId,
      buildId: info.buildId,
      alias: info.alias,
    });
    return;
  }
  context.console.echo(`template_id=${info.templateId}`);
  context.console.echo(`build_id=${info.buildId}`);
}

/** Cada línea del log del build, salvo en `--json`, donde stdout es un solo documento. */
export function buildLogPrinter(context: CommandContext): ((line: string) => void) | undefined {
  return context.globals.json ? undefined : (line) => context.console.echo(line);
}

export const buildCommand = defineCommand({
  path: ["template", "build"],
  help:
    "Construye la imagen del `template` definido en SPEC con Template.build. Sube el " +
    "contexto a BUCKET (bajo rayito/templates/) y crea o actualiza la imagen NAME; cada " +
    "versión nueva cuesta almacenamiento de snapshot, igual que `rayito image publish`. " +
    "Las versiones se borran con `rayito image`.",
  parameters: [
    argument("spec", {
      type: "path",
      required: true,
      help: "Módulo ESM que exporta una constante `template`.",
    }),
    option(["--name"], { required: true, help: "Nombre de la imagen a crear/actualizar." }),
    option(["--bucket"], { required: true, help: "Bucket S3 del artefacto de build." }),
    option(["--memory-mb"], { type: "int", default: DEFAULT_MEMORY_MIB, help: "Memoria mínima." }),
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
      const template = await templates.loadSpec(values.string("spec") ?? "");
      const info = await templates.build(template, values.string("name") ?? "", {
        bucket: values.string("bucket") ?? "",
        memoryMb: values.number("memory_mb"),
        force: values.flag("force"),
        timeoutMs: values.number("timeout") * MS_PER_SECOND,
        onBuildLogs: buildLogPrinter(context),
      });
      printBuildInfo(context, info);
      return undefined;
    } catch (error) {
      if (error instanceof TemplateSpecError) {
        return failure(context, error.message);
      }
      if (error instanceof BuildError) {
        return failure(context, `build fallido: ${error.message}`);
      }
      throw error;
    }
  },
});

export const statusCommand = defineCommand({
  path: ["template", "status"],
  help: "Estado del build de una versión de la imagen NAME (sólo lectura).",
  parameters: [
    argument("name", { required: true, help: "Nombre de la imagen del template." }),
    option(["--version"], { help: "Versión a consultar; por defecto la más reciente." }),
  ],
  run: async (context) => {
    const { templates } = await context.services();
    const status = await templates.status(
      context.values.string("name") ?? "",
      context.values.string("version"),
    );
    if (context.globals.json) {
      context.console.emitJson({
        state: status.state,
        templateId: status.info?.templateId ?? null,
        buildId: status.info?.buildId ?? null,
        errorMessage: status.errorMessage ?? null,
      });
      return undefined;
    }
    context.console.echo(`state=${status.state}`);
    if (status.info !== undefined) {
      context.console.echo(`template_id=${status.info.templateId}`);
      context.console.echo(`build_id=${status.info.buildId}`);
    }
    if (status.errorMessage !== undefined) {
      context.console.echo(`error=${status.errorMessage}`);
    }
    return undefined;
  },
});

export const logsCommand = defineCommand({
  path: ["template", "logs"],
  help: "Últimas líneas del log de build de la imagen NAME (sólo lectura).",
  parameters: [
    argument("name", { required: true, help: "Nombre de la imagen del template." }),
    option(["--limit"], {
      type: "int",
      default: BUILD_LOG_LINES,
      help: "Máximo de líneas a leer.",
    }),
  ],
  run: async (context) => {
    const { templates } = await context.services();
    const lines = await templates.logs(
      context.values.string("name") ?? "",
      context.values.number("limit"),
    );
    if (context.globals.json) {
      context.console.emitJson({ lines });
      return undefined;
    }
    for (const line of lines) {
      context.console.echo(line);
    }
    return undefined;
  },
});
