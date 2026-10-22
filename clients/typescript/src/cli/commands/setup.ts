/**
 * `rayito setup` (ADR-030): prepara una cuenta y una región de AWS para Rayito
 * en una orden idempotente. Lee la cuenta, muestra el plan y el coste, pide
 * confirmación, lo aplica y termina con `rayito doctor`. La orden sólo valida
 * opciones, imprime y delega en `AccountSetup`; los textos y el documento JSON
 * son los de `testdata/account-setup/contract.json`, los mismos que la CLI de
 * Python.
 */

import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { ErrorCode, SetupPlan, SetupResult } from "../../account-setup/domain.js";
import { AccountSetupError, BASE_IMAGE_NAME, BUCKET_ENV_VAR } from "../../account-setup/domain.js";
import { AccountSetup } from "../../account-setup/service.js";
import { fileArtifacts } from "../../images/artifact-node.js";
import { VERSION } from "../../version.js";
import { type CommandContext, defineCommand, flag, option } from "../command.js";
import { runDoctor } from "../doctor/checks.js";
import { newDoctorContext } from "../doctor/model.js";
import { doctorDocument, exitCodeFor, QUOTA_FLOAT_KEYS, renderHuman } from "../doctor/render.js";
import { EXIT_FAILURE, EXIT_USAGE } from "../exit-codes.js";
import { translateFailure } from "../failures.js";
import type { CliServices } from "../services.js";
import {
  CONFIRM_QUESTION,
  DOCTOR_FAILED,
  DRY_RUN_FOOTER,
  ERROR_PREFIX,
  finalLines,
  planLines,
  type SetupDocumentInput,
  type SetupFailure,
  setupDocument,
} from "../setup-output.js";
import { pairsOrUsage } from "./sandbox.js";

const WORKDIR_PREFIX = "rayito-setup-";

const HELP =
  "Prepara la cuenta y la región para Rayito en una orden idempotente, como `cdk " +
  "bootstrap`: el bucket de artefactos (el tuyo, el de la pila de IAM o el del bootstrap de" +
  " CDK; si no hay ninguno, lo crea con la pila rayito-m0-artifacts: cifrado, privado, sólo" +
  " TLS y con los zips caducando a los 7 días), la pila de IAM rayito-m0-iam y las imágenes " +
  "rayito-base y rayito-base-caps (la base del agente en MicroVM; --no-caps-image la omite) " +
  "desde el rayito-image.zip de la release de tu versión, descargado una vez y comprobado " +
  "contra su SHA256SUMS. Muestra el plan y el coste, pide confirmación y termina con rayito " +
  "doctor. Coste: cada versión publicada de cada imagen ≈ $0,04 por semana de almacenamiento " +
  "($0,08 por GB-mes, mínimo una semana); la pila de IAM es gratis; el zip en el bucket, " +
  "céntimos; con --dry-run sólo lee.";

/** Un `--artifact` ausente o que no es la variante `full` sin EFS se rechaza antes de cualquier llamada a AWS. */
function requireFullArtifact(artifact: string): void {
  if (!fileArtifacts.exists(artifact)) {
    throw new AccountSetupError("artifact_missing", { artifact });
  }
  const found = fileArtifacts.inspect(artifact);
  if (found.variant !== "full" || found.withEfs) {
    throw new AccountSetupError("artifact_variant", { artifact });
  }
}

/** El documento de `--json` va a stdout y las líneas de progreso a stderr. */
function progress(context: CommandContext): (message: string) => void {
  return (message) => context.console.echo(message, { err: context.globals.json });
}

function emitDocument(
  context: CommandContext,
  input: Omit<SetupDocumentInput, "sdkVersion">,
): void {
  context.console.emitJson(setupDocument({ sdkVersion: VERSION, ...input }), QUOTA_FLOAT_KEYS);
}

/** Pregunta en una terminal; sin ella (o con `--json`) lo que cuesta exige `--yes`. */
async function confirmed(context: CommandContext, plan: SetupPlan, yes: boolean): Promise<boolean> {
  if (!plan.needsConfirmation || yes) {
    return true;
  }
  if (context.globals.json || !context.console.canPrompt) {
    throw new AccountSetupError("needs_confirmation");
  }
  return context.console.confirm(CONFIRM_QUESTION);
}

async function applyPlan(
  context: CommandContext,
  plan: SetupPlan,
  setup: AccountSetup,
  tags: Readonly<Record<string, string>>,
): Promise<SetupResult> {
  const workdir = await mkdtemp(join(tmpdir(), WORKDIR_PREFIX));
  try {
    return await setup.apply(plan, { workdir, onProgress: progress(context), tags });
  } finally {
    await rm(workdir, { recursive: true, force: true });
  }
}

/** `rayito doctor` sin `--launch` sobre `rayito-base` y el bucket elegido; sale 1 con algún `FAIL`. */
async function finish(
  context: CommandContext,
  services: CliServices,
  plan: SetupPlan,
  result: SetupResult,
): Promise<number | undefined> {
  const doctorContext = newDoctorContext({
    template: BASE_IMAGE_NAME,
    bucket: result.bucket,
    launch: false,
  });
  const results = await runDoctor(services.doctor, doctorContext, (message) =>
    context.console.echo(message, { err: true }),
  );
  const exitCode = exitCodeFor(results);
  const failed = exitCode !== 0;
  if (context.globals.json) {
    emitDocument(context, {
      region: services.region,
      dryRun: false,
      ready: !failed,
      plan,
      result,
      doctor: doctorDocument({
        region: services.region,
        context: doctorContext,
        results,
        exitCode,
      }),
      error: undefined,
      exitCode,
    });
  } else {
    renderHuman(context.console, results);
    const lines = failed ? [DOCTOR_FAILED] : finalLines(result, services.region);
    for (const line of lines) {
      context.console.echo(line);
    }
  }
  return failed ? EXIT_FAILURE : undefined;
}

/**
 * Los códigos del contrato más cercanos a un fallo que `setup` no traduce (un
 * error de AWS crudo, una excepción de una pila): `contract.json` no tiene un
 * código para ellos y su salida es la de siempre, 1 (la operación falló) o 2
 * (credenciales o región). Cada uno lleva el código del contrato con la misma
 * salida: `stack_blocked` (1) y `bucket_unavailable` (2). El mensaje es la línea
 * ya traducida de `failures.ts`.
 */
const UNTRANSLATED_FAILURE_CODE: ErrorCode = "stack_blocked";
const UNTRANSLATED_USAGE_CODE: ErrorCode = "bucket_unavailable";

function reportFailure(
  context: CommandContext,
  services: CliServices,
  plan: SetupPlan | undefined,
  error: SetupFailure,
  exitCode: number,
): number {
  context.console.echo(`${ERROR_PREFIX}${error.message}`, { err: true });
  if (context.globals.json) {
    emitDocument(context, {
      region: services.region,
      dryRun: context.values.flag("dry_run"),
      ready: false,
      plan,
      result: undefined,
      doctor: undefined,
      error,
      exitCode,
    });
  }
  return exitCode;
}

/** Con `--json` un fallo ajeno a `setup` también deja un único documento; en modo humano sigue su camino de siempre. */
function reportUntranslated(
  context: CommandContext,
  services: CliServices,
  plan: SetupPlan | undefined,
  error: unknown,
): number {
  const failure = translateFailure(error, context.globals.verbose);
  const message = failure.line.startsWith(ERROR_PREFIX)
    ? failure.line.slice(ERROR_PREFIX.length)
    : failure.line;
  const code = failure.code === EXIT_USAGE ? UNTRANSLATED_USAGE_CODE : UNTRANSLATED_FAILURE_CODE;
  return reportFailure(context, services, plan, { code, message }, failure.code);
}

async function runSetup(
  context: CommandContext,
  services: CliServices,
  state: { plan: SetupPlan | undefined },
): Promise<number | undefined> {
  const { values, console } = context;
  const artifact = values.string("artifact");
  const dryRun = values.flag("dry_run");
  const tags = pairsOrUsage(values.strings("tag"), "--tag");
  if (artifact !== undefined) {
    requireFullArtifact(artifact);
  }
  const setup = new AccountSetup(services.accountSetup);
  const plan = await setup.plan({
    region: services.region,
    bucket: values.string("bucket"),
    environmentBucket: context.env[BUCKET_ENV_VAR] || undefined,
    artifact,
    sdkVersion: VERSION,
    capsImage: !values.flag("no_caps_image"),
  });
  state.plan = plan;
  if (!context.globals.json) {
    for (const line of planLines(plan)) {
      console.echo(line);
    }
  }
  if (dryRun) {
    if (context.globals.json) {
      emitDocument(context, {
        region: services.region,
        dryRun,
        ready: plan.ready,
        plan,
        result: undefined,
        doctor: undefined,
        error: undefined,
        exitCode: 0,
      });
    } else {
      console.echo(DRY_RUN_FOOTER);
    }
    return undefined;
  }
  if (!(await confirmed(context, plan, values.flag("yes")))) {
    return EXIT_FAILURE;
  }
  return finish(context, services, plan, await applyPlan(context, plan, setup, tags));
}

export const setupCommand = defineCommand({
  path: ["setup"],
  help: HELP,
  parameters: [
    option(["--bucket"], {
      help:
        "Bucket de artefactos en la misma región; por defecto RAYITO_BUCKET, el de la pila de " +
        "IAM, el del bootstrap de CDK o uno nuevo rayito-artifacts-<cuenta>-<región>. Si no " +
        "existe, se crea con ese nombre (pila rayito-m0-artifacts).",
    }),
    option(["--artifact"], {
      type: "path",
      help:
        "Zip de la imagen ya verificado (p. ej. con cosign); sin él se descarga " +
        "rayito-image.zip de la release rayd-v<versión del SDK> y se comprueba contra su SHA256SUMS.",
    }),
    option(["--tag"], {
      multiple: true,
      default: [],
      help:
        "Etiqueta K=V; repetible. Se aplica a las pilas que crea setup (rayito-m0-artifacts y " +
        "rayito-m0-iam) y CloudFormation la propaga a sus recursos; útil si la organización " +
        "exige etiquetas.",
    }),
    flag(["--yes"], {
      help: "No pedir confirmación (hace falta sin terminal o con --json si hay algo que crear).",
    }),
    flag(["--dry-run"], {
      help: "Sólo lee la cuenta y muestra el plan; no crea ni cambia nada.",
    }),
    flag(["--no-caps-image"], {
      help:
        "No publicar rayito-base-caps, la base del agente en MicroVM (la misma imagen con " +
        "additionalOsCapabilities ALL); por defecto se publica desde el mismo zip.",
    }),
  ],
  run: async (context) => {
    const services = await context.services();
    const state: { plan: SetupPlan | undefined } = { plan: undefined };
    try {
      return await runSetup(context, services, state);
    } catch (error) {
      if (error instanceof AccountSetupError) {
        return reportFailure(context, services, state.plan, error, error.exitCode);
      }
      if (context.globals.json) {
        return reportUntranslated(context, services, state.plan, error);
      }
      throw error;
    }
  },
});
