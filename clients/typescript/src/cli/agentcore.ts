/**
 * `rayito agent deploy|invoke|status|stop|list|delete --target agentcore`
 * (ADR-031): las órdenes sólo validan opciones, imprimen y delegan en
 * `AgentCoreTools`. Los textos y los documentos JSON son los de
 * `testdata/agentcore-deploy/contract.json`, los mismos que la CLI de Python. En
 * `--json` un fallo deja un único objeto `{ error: { code, message }, exitCode }`
 * por stdout y la línea `rayito: <mensaje>` por stderr, como `rayito setup`.
 */

import { REASON_IAM_AUTH } from "../agentcore/domain.js";
import { AGENT_TARGETS, type AgentTarget, TARGET_MICROVM } from "../agentcore/models.js";
import { AgentCoreError } from "../errors.js";
import { formatTemplate } from "../template-text.js";
import { MS_PER_SECOND } from "../units.js";
import { detectionLines } from "./agent-deploy.js";
import { EventPrinter, printResult } from "./agent-output.js";
import {
  CONFIRM_QUESTION,
  DELETE_CONFIRM_QUESTION,
  DELETE_NOTHING_TEMPLATE,
  DRY_RUN_FOOTER,
  ERROR_PREFIX,
  STOPPED_TEMPLATE,
} from "./agentcore-messages.js";
import {
  appliedStepLines,
  deleteDocument,
  deletedLine,
  deletePreviewLines,
  deployDocument,
  deployedLines,
  dryRunDocument,
  listDocument,
  listLines,
  planLines,
  statusDocument,
  statusLines,
  stopDocument,
} from "./agentcore-output.js";
import type { CommandContext } from "./command.js";
import { pairsOrUsage } from "./commands/sandbox.js";
import { EXIT_FAILURE } from "./exit-codes.js";
import type {
  AgentCoreDeployRequest,
  AgentDeployFlags,
  AgentDeploySettings,
  AgentDetectOverrides,
} from "./ops-ports.js";

const TOKEN_FILE_OPTION = "--token-file";

/** El valor de `--target`, o `undefined` si no es uno de los admitidos (el llamante da el error de uso). */
export function targetOf(context: CommandContext): AgentTarget | undefined {
  const value = context.values.string("target") ?? TARGET_MICROVM;
  return (AGENT_TARGETS as readonly string[]).includes(value) ? (value as AgentTarget) : undefined;
}

/** Imprime un fallo de AgentCore: la línea por stderr y, con `--json`, el documento de error por stdout. Devuelve su código de salida. */
export function reportFailure(context: CommandContext, error: AgentCoreError): number {
  context.console.echo(`${ERROR_PREFIX}${error.message}`, { err: true });
  if (context.globals.json) {
    context.console.emitJson({
      error: { code: error.code, message: error.message },
      exitCode: error.exitCode,
    });
  }
  return error.exitCode;
}

/** Ejecuta `action`; un fallo de AgentCore sale con su documento y su código de salida, y cualquier otro sigue su camino de siempre. */
export async function guardedAgentCore(
  context: CommandContext,
  action: () => Promise<number | undefined>,
): Promise<number | undefined> {
  try {
    return await action();
  } catch (error) {
    if (error instanceof AgentCoreError) {
      return reportFailure(context, error);
    }
    throw error;
  }
}

/** La primera opción de AgentCore (`--tag`, `--bedrock-model`...) que se pasó con otro destino, o `undefined`. */
export function agentCoreOptionGiven(context: CommandContext): string | undefined {
  const { values } = context;
  const given: [string, boolean][] = [
    ["--tag", values.strings("tag").length > 0],
    ["--bedrock-model", values.strings("bedrock_model").length > 0],
    ["--readable-secret", values.strings("readable_secret").length > 0],
    ["--idle-session-timeout", values.optionalNumber("idle_session_timeout") !== undefined],
    ["--max-session-lifetime", values.optionalNumber("max_session_lifetime") !== undefined],
  ];
  return given.find(([, isGiven]) => isGiven)?.[0];
}

function secondsToMs(seconds: number | undefined): number | undefined {
  return seconds === undefined ? undefined : seconds * MS_PER_SECOND;
}

export interface DeployInput {
  readonly path: string;
  readonly overrides: AgentDetectOverrides;
  readonly flags: AgentDeployFlags;
  readonly settings: AgentDeploySettings;
}

/**
 * La región del despliegue: `--region`, `AWS_REGION` o `AWS_DEFAULT_REGION`. Con
 * `--dry-run` no hay más fuentes (el plan no llama a AWS); sin él, y sin ninguna,
 * la de la sesión de AWS (perfil o configuración).
 */
async function regionOf(context: CommandContext, dryRun: boolean): Promise<string | undefined> {
  const explicit =
    context.globals.region ?? context.env.AWS_REGION ?? context.env.AWS_DEFAULT_REGION;
  if (explicit || dryRun) {
    return explicit || undefined;
  }
  return (await context.services()).region;
}

function deployRequest(
  context: CommandContext,
  input: DeployInput,
  region: string | undefined,
): AgentCoreDeployRequest {
  const { values } = context;
  const idle = secondsToMs(values.optionalNumber("idle_session_timeout"));
  const max = secondsToMs(values.optionalNumber("max_session_lifetime"));
  return {
    path: input.path,
    overrides: input.overrides,
    flags: input.flags,
    settings: input.settings,
    agentcore: {
      bedrockModels: [...values.strings("bedrock_model")],
      readableSecrets: [...values.strings("readable_secret")],
      ...(idle === undefined ? {} : { idleSessionTimeoutMs: idle }),
      ...(max === undefined ? {} : { maxSessionLifetimeMs: max }),
      tags: pairsOrUsage(values.strings("tag"), "--tag"),
    },
    tokenFile: values.string("token_file"),
    region,
    environ: context.env,
  };
}

/** Pregunta en una terminal; sin ella, o con `--json`, lo que cuesta exige `--yes`. */
async function confirmed(
  context: CommandContext,
  question: string,
  refusal: "needs_confirmation" | "delete_needs_confirmation",
): Promise<boolean> {
  if (context.values.flag("yes")) {
    return true;
  }
  if (context.globals.json || !context.console.canPrompt) {
    throw new AgentCoreError(refusal);
  }
  return context.console.confirm(question);
}

/** `rayito agent deploy --target agentcore`: plan, coste, confirmación y despliegue. */
export async function runAgentCoreDeploy(
  context: CommandContext,
  input: DeployInput & { readonly dryRun: boolean },
): Promise<number | undefined> {
  const { console, globals } = context;
  const region = await regionOf(context, input.dryRun);
  const view = await context.agentcore.plan(deployRequest(context, input, region));
  if (input.dryRun && globals.json) {
    console.emitJson(dryRunDocument(view.plan, view.report));
    return undefined;
  }
  if (!globals.json) {
    for (const line of detectionLines(view.report)) {
      console.echo(line, { err: !input.dryRun });
    }
    for (const line of planLines(view.plan)) {
      console.echo(line);
    }
  }
  if (input.dryRun) {
    console.echo(DRY_RUN_FOOTER);
    return undefined;
  }
  if (!(await confirmed(context, CONFIRM_QUESTION, "needs_confirmation"))) {
    return EXIT_FAILURE;
  }
  const deployed = await view.apply();
  if (globals.json) {
    console.emitJson(deployDocument(deployed));
    return undefined;
  }
  for (const line of appliedStepLines(deployed)) {
    console.echo(line, { err: true });
  }
  for (const line of deployedLines(deployed)) {
    console.echo(line);
  }
  return undefined;
}

function rejectTokenFile(context: CommandContext): void {
  if (context.values.string("token_file") !== undefined) {
    throw new AgentCoreError("option_not_supported", {
      option: TOKEN_FILE_OPTION,
      reason: REASON_IAM_AUTH,
    });
  }
}

/** `rayito agent invoke <nombre> <prompt> --target agentcore`. */
export async function runAgentCoreInvoke(context: CommandContext): Promise<number | undefined> {
  const { values } = context;
  rejectTokenFile(context);
  const client = await context.agentcore.connect(values.string("sandbox_id") ?? "");
  const prompt = values.string("prompt") ?? "";
  const options = { sessionId: values.string("session_id") };
  if (values.flag("stream")) {
    const printer = new EventPrinter(context);
    for await (const event of await client.stream(prompt, options)) {
      printer.print(event);
    }
    return printer.finish();
  }
  printResult(context, await client.invoke(prompt, options));
  return undefined;
}

/** `rayito agent status <nombre> --target agentcore`. */
export async function runAgentCoreStatus(context: CommandContext): Promise<number | undefined> {
  rejectTokenFile(context);
  const client = await context.agentcore.connect(context.values.string("sandbox_id") ?? "");
  const status = await client.status();
  if (context.globals.json) {
    context.console.emitJson(statusDocument(status));
    return undefined;
  }
  for (const line of statusLines(status)) {
    context.console.echo(line);
  }
  return undefined;
}

/** `rayito agent stop <nombre> --session-id <id> --target agentcore`. */
export async function runAgentCoreStop(context: CommandContext): Promise<number | undefined> {
  const { values, console } = context;
  const name = values.string("sandbox_id") ?? "";
  const sessionId = values.string("session_id");
  if (sessionId === undefined) {
    throw new AgentCoreError("session_required");
  }
  const client = await context.agentcore.connect(name);
  const stopped = await client.stopSession(sessionId);
  if (context.globals.json) {
    console.emitJson(stopDocument(name, sessionId, stopped));
    return undefined;
  }
  if (stopped) {
    console.echo(formatTemplate(STOPPED_TEMPLATE, { sessionId, name }));
  }
  return undefined;
}

/** `rayito agent list --target agentcore`. */
export async function runAgentCoreList(context: CommandContext): Promise<number | undefined> {
  const { region, agents } = await context.agentcore.list();
  if (context.globals.json) {
    context.console.emitJson(listDocument(region, agents));
    return undefined;
  }
  for (const line of listLines(region, agents)) {
    context.console.echo(line);
  }
  return undefined;
}

/** `rayito agent delete <nombre> --target agentcore`: muestra lo que borra, confirma y borra. */
export async function runAgentCoreDelete(context: CommandContext): Promise<number | undefined> {
  const { console, globals, values } = context;
  const name = values.string("name") ?? "";
  const preview = await context.agentcore.previewDelete(name);
  if (preview.runtimeId === undefined && !preview.stackExists) {
    if (globals.json) {
      console.emitJson(
        deleteDocument({
          name,
          region: preview.region,
          runtimeDeleted: false,
          stackDeleted: false,
          logGroupDeleted: false,
        }),
      );
    } else {
      console.echo(formatTemplate(DELETE_NOTHING_TEMPLATE, { name, region: preview.region }));
    }
    return undefined;
  }
  if (!globals.json) {
    for (const line of deletePreviewLines(name, preview)) {
      console.echo(line);
    }
  }
  if (!(await confirmed(context, DELETE_CONFIRM_QUESTION, "delete_needs_confirmation"))) {
    return EXIT_FAILURE;
  }
  const result = await context.agentcore.delete(name, (step) =>
    console.echo(step.detail, { err: true }),
  );
  if (globals.json) {
    console.emitJson(deleteDocument(result));
    return undefined;
  }
  console.echo(deletedLine(result));
  return undefined;
}
