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
import type { SetupPlan, SetupResult } from "../../account-setup/domain.js";
import { AccountSetupError, BASE_IMAGE_NAME, BUCKET_ENV_VAR } from "../../account-setup/domain.js";
import { AccountSetup } from "../../account-setup/service.js";
import { fileArtifacts } from "../../images/artifact-node.js";
import { VERSION } from "../../version.js";
import { type CommandContext, defineCommand, flag, option } from "../command.js";
import { runDoctor } from "../doctor/checks.js";
import { newDoctorContext } from "../doctor/model.js";
import { doctorDocument, exitCodeFor, QUOTA_FLOAT_KEYS, renderHuman } from "../doctor/render.js";
import { EXIT_FAILURE } from "../exit-codes.js";
import type { CliServices } from "../services.js";
import {
  CONFIRM_QUESTION,
  DOCTOR_FAILED,
  DRY_RUN_FOOTER,
  ERROR_PREFIX,
  finalLines,
  planLines,
  type SetupDocumentInput,
  setupDocument,
} from "../setup-output.js";

const WORKDIR_PREFIX = "rayito-setup-";

const HELP =
  "Prepara la cuenta y la región para Rayito en una orden idempotente, como `cdk bootstrap`: " +
  "la pila de IAM rayito-m0-iam, el bucket de artefactos (el tuyo, el de la pila o el del " +
  "bootstrap de CDK; Rayito no crea buckets) y la imagen rayito-base desde el " +
  "rayito-image.zip de la release de tu versión, comprobado contra su SHA256SUMS. Muestra el " +
  "plan y el coste, pide confirmación y termina con rayito doctor. Coste: cada versión " +
  "publicada de la imagen ≈ $0,04 por semana de almacenamiento (mínimo una semana); la pila " +
  "de IAM es gratis; con --dry-run sólo lee.";

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
): Promise<SetupResult> {
  const workdir = await mkdtemp(join(tmpdir(), WORKDIR_PREFIX));
  try {
    return await setup.apply(plan, { workdir, onProgress: progress(context) });
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

function reportFailure(
  context: CommandContext,
  services: CliServices,
  plan: SetupPlan | undefined,
  error: AccountSetupError,
  dryRun: boolean,
): number {
  context.console.echo(`${ERROR_PREFIX}${error.message}`, { err: true });
  if (context.globals.json) {
    emitDocument(context, {
      region: services.region,
      dryRun,
      ready: false,
      plan,
      result: undefined,
      doctor: undefined,
      error,
      exitCode: error.exitCode,
    });
  }
  return error.exitCode;
}

async function runSetup(
  context: CommandContext,
  services: CliServices,
  state: { plan: SetupPlan | undefined },
): Promise<number | undefined> {
  const { values, console } = context;
  const artifact = values.string("artifact");
  const dryRun = values.flag("dry_run");
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
  return finish(context, services, plan, await applyPlan(context, plan, setup));
}

export const setupCommand = defineCommand({
  path: ["setup"],
  help: HELP,
  parameters: [
    option(["--bucket"], {
      help:
        "Bucket de artefactos en la misma región; por defecto RAYITO_BUCKET, el de la pila de " +
        "IAM o el del bootstrap de CDK. Rayito no crea buckets.",
    }),
    option(["--artifact"], {
      type: "path",
      help:
        "Zip de la imagen ya verificado (p. ej. con cosign); sin él se descarga " +
        "rayito-image.zip de la release rayd-v<versión del SDK> y se comprueba contra su SHA256SUMS.",
    }),
    flag(["--yes"], {
      help: "No pedir confirmación (hace falta sin terminal o con --json si hay algo que crear).",
    }),
    flag(["--dry-run"], {
      help: "Sólo lee la cuenta y muestra el plan; no crea ni cambia nada.",
    }),
  ],
  run: async (context) => {
    const services = await context.services();
    const state: { plan: SetupPlan | undefined } = { plan: undefined };
    try {
      return await runSetup(context, services, state);
    } catch (error) {
      if (error instanceof AccountSetupError) {
        return reportFailure(context, services, state.plan, error, context.values.flag("dry_run"));
      }
      throw error;
    }
  },
});
