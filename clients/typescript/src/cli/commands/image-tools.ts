/**
 * `rayito image publish | prune | zip`: publicar una imagen desde un zip ya
 * construido, podar sus versiones antiguas y armar ese zip. Las órdenes sólo
 * validan opciones y delegan en `images/` (dominio puro, servicios sobre
 * puertos y adaptadores de AWS y de ficheros); `zip` nunca toca AWS.
 */

import { statSync } from "node:fs";
import { ImageArtifactError, SIDECAR_DIRECTORY } from "../../images/artifact.js";
import {
  artifactSha256,
  copySidecar,
  fileArtifacts,
  writeImageZip,
} from "../../images/artifact-node.js";
import { DEFAULT_BUILD_TIMEOUT_MS, DEFAULT_MEMORY_MIB } from "../../images/gateway.js";
import {
  DEFAULT_KEEP,
  DEFAULT_PRUNE_IMAGE_NAME,
  DEFAULT_WAIT_TIMEOUT_MS,
  validatePruneSettings,
} from "../../images/prune-domain.js";
import { runPrune } from "../../images/prune-service.js";
import {
  DEFAULT_STACK_NAME,
  defaultImageName,
  EXTRA_SIZE_NAMES,
  ImageSettingsError,
  OS_CAPABILITY_CHOICES,
  type PublishSettings,
  parseEnvironmentAssignments,
  requireEfsCombination,
  sizeEnvVar,
  validateBaselineMemory,
  validateRunId,
  validateSizes,
} from "../../images/publish-domain.js";
import {
  ImagePublishError,
  type PublishDeps,
  publish,
  publishWithSizes,
} from "../../images/publish-service.js";
import { MS_PER_SECOND } from "../../units.js";
import { argument, type CommandContext, defineCommand, flag, option } from "../command.js";
import type { FloatKeys } from "../console.js";
import { EXIT_FAILURE } from "../exit-codes.js";
import { UsageError } from "../failures.js";
import { validateVariant } from "./image.js";
import { failure } from "./template.js";

const BUCKET_ENV_VAR = "RAYITO_BUCKET";
const SEPARATOR = ": ";
const ZIP_SECONDS_DECIMALS = 100;
/** Duraciones que Python redondea y guarda como `float`: `0` se escribe `0.0`. */
const SECONDS_FLOAT_KEYS: FloatKeys = new Set(["waitedSeconds", "buildSeconds", "seconds"]);
const DEFAULT_VARIANT = "full";

const WITH_EFS_HELP =
  "Añade amazon-efs-utils (volumes=, m15-efs-volumes): +~198 MB de imagen; publish exige " +
  "--os-capabilities ALL y publica rayito-base-caps-efs.";
const ARTIFACT_RUN_ID_HELP =
  "Sube el zip a rayito/images/runs/<RUN_ID>/ en vez de la clave compartida, para que la " +
  "limpieza de una ejecución (aceptación, e2e) no borre el artefacto de otra; el resumen JSON " +
  "dice si se subió (artifactUploaded).";

/** Una opción inválida es un error de uso (salida 2) que nombra su opción. */
function usageOf(error: ImageSettingsError): UsageError {
  return new UsageError(
    error.hint === undefined ? error.message : `${error.hint}${SEPARATOR}${error.message}`,
  );
}

function validated<T>(compute: () => T, hint?: string): T {
  try {
    return compute();
  } catch (error) {
    if (error instanceof ImageSettingsError) {
      throw usageOf(
        error.hint === undefined && hint !== undefined
          ? new ImageSettingsError(error.message, hint)
          : error,
      );
    }
    throw error;
  }
}

function validateOsCapabilities(value: string | undefined): string | undefined {
  if (value !== undefined && !OS_CAPABILITY_CHOICES.includes(value)) {
    throw new UsageError(
      `--os-capabilities${SEPARATOR}admitidas: ${OS_CAPABILITY_CHOICES.join(", ")}`,
    );
  }
  return value;
}

function resolveBucket(context: CommandContext): string {
  const resolved = context.values.string("bucket") ?? (context.env[BUCKET_ENV_VAR] || undefined);
  if (resolved === undefined) {
    throw new UsageError(`--bucket${SEPARATOR}pasa --bucket o exporta ${BUCKET_ENV_VAR}`);
  }
  return resolved;
}

/** Los progresos van a stderr con `--json`, para que stdout lleve sólo el documento. */
function progress(context: CommandContext): (message: string) => void {
  return (message) => context.console.echo(message, { err: context.globals.json });
}

export const publishCommand = defineCommand({
  path: ["image", "publish"],
  help:
    "Publica la imagen: sube el zip a S3 (clave por sha256), crea o actualiza la imagen y " +
    "espera al gate de tres estados; reutiliza una versión igual. Con `--sizes` publica, " +
    "además, una imagen por tamaño desde el mismo artefacto y lo informa todo en un único " +
    "bloque de salida: un documento JSON, o un `RAYITO_TEMPLATE=` del baseline seguido de un " +
    "`RAYITO_TEMPLATE_<SIZE>=` por tamaño. Sin `--sizes`, sólo el baseline. Con " +
    "`--artifact-run-id`, la clave del zip es de esa ejecución.",
  parameters: [
    option(["--artifact"], {
      type: "path",
      required: true,
      help: "Zip con el Dockerfile en la raíz.",
    }),
    option(["--base-image-version"], {
      required: true,
      help: "baseImageVersion de la base gestionada (imageVersion de list-managed-microvm-image-versions).",
    }),
    option(["--bucket"], { help: `Bucket de artefactos; por defecto ${BUCKET_ENV_VAR}.` }),
    option(["--image-name"], { help: "Sustituye el nombre por defecto de la variante." }),
    option(["--variant"], {
      default: DEFAULT_VARIANT,
      help: "full (rayito-base), slim (rayito-base-slim) o poly (rayito-base-poly).",
    }),
    option(["--os-capabilities"], { help: "additionalOsCapabilities de la imagen (ALL)." }),
    option(["--build-role-arn"], { help: "Sustituye la salida del stack." }),
    option(["--stack-name"], {
      default: DEFAULT_STACK_NAME,
      help: "Stack de CloudFormation con la salida BuildRoleArn.",
    }),
    option(["--memory-mib"], {
      type: "int",
      default: DEFAULT_MEMORY_MIB,
      help: "Memoria mínima de la imagen.",
    }),
    option(["--timeout-seconds"], {
      type: "float",
      default: DEFAULT_BUILD_TIMEOUT_MS / MS_PER_SECOND,
      help: "Segundos de espera del build.",
    }),
    flag(["--force"], { help: "Construye una versión nueva aunque una coincida." }),
    option(["--sizes"], {
      help:
        `Tamaños extra separados por comas (${EXTRA_SIZE_NAMES.join(",")}), publicados además ` +
        "del baseline desde el mismo artefacto.",
    }),
    option(["--env"], {
      multiple: true,
      default: [],
      help:
        "KEY=VALUE horneado en environmentVariables (repetible). Nunca secretos: legible por " +
        "GetMicrovmImageVersion y por todo proceso del guest; usa SecretStore/secrets= para eso.",
    }),
    flag(["--with-efs"], { help: WITH_EFS_HELP }),
    option(["--artifact-run-id"], { help: ARTIFACT_RUN_ID_HELP }),
  ],
  run: async (context) => {
    const { values } = context;
    const rawRunId = values.string("artifact_run_id");
    const artifactRunId =
      rawRunId === undefined
        ? undefined
        : validated(() => validateRunId(rawRunId), "--artifact-run-id");
    const sizeNames = validated(() => validateSizes(values.string("sizes")));
    validated(() => validateBaselineMemory(values.number("memory_mib"), sizeNames));
    const environmentVariables = validated(() =>
      parseEnvironmentAssignments(values.strings("env")),
    );
    const osCapabilities = validateOsCapabilities(values.string("os_capabilities"));
    const variant = validateVariant(values.string("variant") ?? DEFAULT_VARIANT);
    const withEfs = values.flag("with_efs");
    if (withEfs) {
      validated(() => requireEfsCombination(variant, osCapabilities), "--with-efs");
    }
    const settings: PublishSettings = {
      artifact: values.string("artifact") ?? "",
      imageName: values.string("image_name") ?? defaultImageName(variant, withEfs),
      variant,
      bucket: resolveBucket(context),
      stackName: values.string("stack_name") ?? DEFAULT_STACK_NAME,
      buildRoleArn: values.string("build_role_arn"),
      baseImageVersion: values.string("base_image_version") ?? "",
      memoryMib: values.number("memory_mib"),
      force: values.flag("force"),
      timeoutMs: values.number("timeout_seconds") * MS_PER_SECOND,
      osCapabilities,
      environmentVariables,
      withEfs,
      artifactRunId,
    };
    const services = await context.services();
    const deps: PublishDeps = {
      ports: services.publishPorts,
      artifacts: fileArtifacts,
      sleep: context.sleep,
      now: () => context.now().getTime(),
      emit: progress(context),
      utcNow: context.now,
    };
    try {
      if (sizeNames.length > 0) {
        const outcome = await publishWithSizes(deps, settings, sizeNames);
        if (context.globals.json) {
          context.console.emitJson(outcome.document, SECONDS_FLOAT_KEYS);
        } else if (!outcome.failed) {
          context.console.echo(`RAYITO_TEMPLATE=${String(outcome.baseline.imageArn)}`);
          for (const [sizeName, summary] of Object.entries(outcome.sized)) {
            context.console.echo(`${sizeEnvVar(sizeName)}=${String(summary.imageArn)}`);
          }
        }
        return outcome.failed ? EXIT_FAILURE : undefined;
      }
      const summary = await publish(deps, settings);
      if (!summary.launchable) {
        if (context.globals.json) {
          context.console.emitJson(summary, SECONDS_FLOAT_KEYS);
        }
        return EXIT_FAILURE;
      }
      context.console.emitJson(summary, SECONDS_FLOAT_KEYS);
      if (!context.globals.json) {
        context.console.echo(`RAYITO_TEMPLATE=${String(summary.imageArn)}`);
      }
      return undefined;
    } catch (error) {
      if (error instanceof ImagePublishError) {
        return failure(context, error.message);
      }
      throw error;
    }
  },
});

export const pruneCommand = defineCommand({
  path: ["image", "prune"],
  help: "Borra versiones antiguas de una en una; primero con --dry-run.",
  parameters: [
    option(["--image-name"], { default: DEFAULT_PRUNE_IMAGE_NAME, help: "Imagen a podar." }),
    option(["--keep"], {
      type: "int",
      default: DEFAULT_KEEP,
      help: "Versiones lanzables más nuevas que se conservan.",
    }),
    flag(["--dry-run"], { help: "Imprime el plan sin borrar nada." }),
    option(["--wait-timeout"], {
      type: "float",
      default: DEFAULT_WAIT_TIMEOUT_MS / MS_PER_SECOND,
      help: "Segundos de espera a que la imagen se asiente.",
    }),
  ],
  run: async (context) => {
    const { values } = context;
    const settings = validated(() =>
      validatePruneSettings({
        imageName: values.string("image_name") ?? DEFAULT_PRUNE_IMAGE_NAME,
        keep: values.number("keep"),
        dryRun: values.flag("dry_run"),
        waitTimeoutMs: values.number("wait_timeout") * MS_PER_SECOND,
      }),
    );
    const services = await context.services();
    const result = await runPrune(
      {
        ports: services.prunePorts,
        sleep: context.sleep,
        now: () => context.now().getTime(),
        emit: progress(context),
        utcNow: context.now,
      },
      settings,
    );
    context.console.emitJson(result.summary, SECONDS_FLOAT_KEYS);
    return result.exitCode === 0 ? undefined : result.exitCode;
  },
});

export const zipCommand = defineCommand({
  path: ["image", "zip"],
  help: "Zip determinista del directorio de la imagen (sin tests, cachés ni locks).",
  parameters: [
    argument("image_dir", {
      type: "path",
      required: true,
      help: "Directorio con el Dockerfile en la raíz.",
    }),
    argument("destination", { type: "path", required: true, help: "Zip de salida." }),
    option(["--variant"], { default: DEFAULT_VARIANT, help: "full, slim o poly." }),
    option(["--sidecar"], {
      type: "path",
      help: "Copia primero este kernel-sidecar a IMAGE_DIR/kernel-sidecar.",
    }),
    flag(["--with-efs"], { help: WITH_EFS_HELP }),
    option(["--artifact-run-id"], {
      help: "Se acepta para pasar los mismos argumentos que a `publish`; el zip no depende de ella.",
    }),
  ],
  run: async (context) => {
    const { values } = context;
    const variant = validateVariant(values.string("variant") ?? DEFAULT_VARIANT);
    const imageDir = values.string("image_dir") ?? "";
    const destination = values.string("destination") ?? "";
    const sidecar = values.string("sidecar");
    const withEfs = values.flag("with_efs");
    const started = context.now().getTime();
    try {
      const sidecarTarget = `${imageDir}/${SIDECAR_DIRECTORY}`;
      const copied = sidecar === undefined ? undefined : copySidecar(sidecar, sidecarTarget);
      const count = writeImageZip(imageDir, destination, variant, { withEfs });
      const summary = zipSummary({
        destination,
        count,
        variant,
        withEfs,
        copied,
        started,
        now: context.now,
      });
      if (context.globals.json) {
        context.console.emitJson(summary, SECONDS_FLOAT_KEYS);
        return undefined;
      }
      if (copied !== undefined) {
        context.console.echo(`${sidecarTarget}: ${copied} files`);
      }
      context.console.echo(
        `${destination}: ${count} files, ${summary.bytes} bytes (variant ${variant}` +
          `${withEfs ? ", with efs" : ""}) sha256=${summary.sha256}`,
      );
      return undefined;
    } catch (error) {
      if (error instanceof ImageArtifactError) {
        return failure(context, error.message);
      }
      throw error;
    }
  },
});

function zipSummary(input: {
  readonly destination: string;
  readonly count: number;
  readonly variant: string;
  readonly withEfs: boolean;
  readonly copied: number | undefined;
  readonly started: number;
  readonly now: () => Date;
}): { bytes: number; sha256: string } & Record<string, unknown> {
  const bytes = statSync(input.destination).size;
  const sha256 = artifactSha256(input.destination);
  const seconds = (input.now().getTime() - input.started) / MS_PER_SECOND;
  return {
    destination: input.destination,
    files: input.count,
    bytes,
    variant: input.variant,
    withEfs: input.withEfs,
    sha256,
    sidecarFiles: input.copied ?? null,
    seconds: Math.round(seconds * ZIP_SECONDS_DECIMALS) / ZIP_SECONDS_DECIMALS,
  };
}
