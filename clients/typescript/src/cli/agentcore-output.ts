/**
 * La salida de `rayito agent ... --target agentcore` (ADR-031): las líneas del
 * plan, de los pasos, del estado, del listado y del borrado, y los documentos
 * de `--json`. Los textos son los de `agentcore-messages.ts` (los de
 * `testdata/agentcore-deploy/contract.json`) y las claves JSON las de
 * `contract.json.json`, las mismas que la CLI de Python. Sólo da forma al texto:
 * no lee AWS ni escribe en la terminal.
 */

import {
  SECURITY_MESSAGES,
  type SecurityKey,
  WARNING_MESSAGES,
  type WarningKey,
} from "../agentcore/messages.js";
import type {
  AgentCoreDeleteResult,
  AgentCoreDeployPlan,
  AgentCoreListItem,
  AgentCoreStatus,
  AgentCoreStep,
} from "../agentcore/models.js";
import { formatTemplate } from "../template-text.js";
import {
  ACTION_TEMPLATES,
  COST_HEADER,
  COST_LINE_TEMPLATE,
  DELETE_HEADER_TEMPLATE,
  DELETE_LOG_GROUPS_LINE,
  DELETE_RUNTIME_LINE,
  DELETE_STACK_LINE,
  DELETED_TEMPLATE,
  DEPLOYED_LINES,
  HEADER_TEMPLATE,
  LIST_EMPTY_TEMPLATE,
  LIST_LINE_TEMPLATE,
  SECURITY_HEADER,
  SECURITY_LINE_TEMPLATE,
  STACK_CONTENTS_LINE,
  STATUS_LINES,
  STEP_LABELS,
  STEP_TEMPLATE,
  WARNING_LINE_TEMPLATE,
  WARNINGS_HEADER,
} from "./agentcore-messages.js";
import type { AgentCoreDeletePreview, AgentCoreDeployedView } from "./ops-ports.js";

export const TARGET_LABEL = "agentcore";
const MODEL_SEPARATOR = ", ";
const PLANNED_ACTIONS = {
  stack: ACTION_TEMPLATES.stackPlanned,
  image: ACTION_TEMPLATES.imagePlanned,
  runtime: ACTION_TEMPLATES.runtimePlanned,
} as const;

function securityText(key: SecurityKey, plan: AgentCoreDeployPlan): string {
  return formatTemplate(SECURITY_MESSAGES[key], {
    models: plan.bedrockModels.join(MODEL_SEPARATOR),
    secrets: plan.readableSecrets.join(MODEL_SEPARATOR),
  });
}

function stepLine(step: AgentCoreStep, action: string): string {
  return formatTemplate(STEP_TEMPLATE, {
    label: STEP_LABELS[step.resource],
    name: step.name,
    action,
  });
}

/** El plan en líneas: encabezado, tres pasos, seguridad, avisos (si hay) y coste. */
export function planLines(plan: AgentCoreDeployPlan): string[] {
  const lines = [formatTemplate(HEADER_TEMPLATE, { name: plan.name, region: plan.region })];
  for (const step of plan.steps) {
    lines.push(stepLine(step, PLANNED_ACTIONS[step.resource]));
    if (step.resource === "stack") {
      lines.push(STACK_CONTENTS_LINE);
    }
  }
  lines.push(SECURITY_HEADER);
  for (const key of plan.security) {
    lines.push(
      formatTemplate(SECURITY_LINE_TEMPLATE, { text: securityText(key as SecurityKey, plan) }),
    );
  }
  if (plan.warnings.length > 0) {
    lines.push(WARNINGS_HEADER);
    for (const key of plan.warnings) {
      lines.push(
        formatTemplate(WARNING_LINE_TEMPLATE, { text: WARNING_MESSAGES[key as WarningKey] }),
      );
    }
  }
  lines.push(COST_HEADER, ...plan.cost.map((text) => formatTemplate(COST_LINE_TEMPLATE, { text })));
  return lines;
}

function actionText(step: AgentCoreStep, version: string): string {
  const status = step.status ?? "";
  const key = `${step.resource}:${step.action}`;
  const templates: Readonly<Record<string, string>> = {
    "stack:create": ACTION_TEMPLATES.stackCreate,
    "stack:update": ACTION_TEMPLATES.stackUpdate,
    "stack:reuse": ACTION_TEMPLATES.stackReuse,
    "stack:wait": ACTION_TEMPLATES.stackWait,
    "image:build": ACTION_TEMPLATES.imageBuild,
    "image:reuse": ACTION_TEMPLATES.imageReuse,
    "runtime:create": ACTION_TEMPLATES.runtimeCreate,
    "runtime:update": ACTION_TEMPLATES.runtimeUpdate,
    "runtime:reuse": ACTION_TEMPLATES.runtimeReuse,
    "runtime:wait": ACTION_TEMPLATES.runtimeWait,
  };
  return formatTemplate(templates[key] ?? step.action, { status, version });
}

/** Los pasos de un despliegue ya hecho, con lo que se hizo en cada uno. */
export function appliedStepLines(deployed: AgentCoreDeployedView): string[] {
  return deployed.steps.map((step) => stepLine(step, actionText(step, deployed.runtimeVersion)));
}

/** El plan como documento de `--json`, con las claves de `json.planKeys`;
 * `externalModel` sólo aparece si el modelo está fuera de Bedrock. */
export function planDocument(plan: AgentCoreDeployPlan): Record<string, unknown> {
  const document: Record<string, unknown> = {
    target: plan.target,
    name: plan.name,
    region: plan.region,
    runtimeName: plan.runtimeName,
    stackName: plan.stackName,
    repositoryName: plan.repositoryName,
    buildProjectName: plan.buildProjectName,
    imageTag: plan.imageTag,
    bucket: plan.bucket,
    bucketSource: plan.bucketSource,
    network: plan.network,
    bedrockModels: [...plan.bedrockModels],
    readableSecrets: [...plan.readableSecrets],
    environment: { ...plan.environment },
    lifecycle: { ...plan.lifecycle },
    tags: { ...plan.tags },
    steps: plan.steps.map(stepDocument),
    security: [...plan.security],
    warnings: [...plan.warnings],
    cost: [...plan.cost],
  };
  if (plan.externalModel === true) {
    document.externalModel = true;
  }
  return document;
}

function stepDocument(step: AgentCoreStep): Record<string, unknown> {
  return { resource: step.resource, name: step.name, action: step.action, status: step.status };
}

/** El documento de `--dry-run --json`, con las claves de `json.planDocumentKeys`. */
export function dryRunDocument(
  plan: AgentCoreDeployPlan,
  report: unknown,
): Record<string, unknown> {
  return {
    dryRun: true,
    target: TARGET_LABEL,
    detection: report ?? null,
    plan: planDocument(plan),
  };
}

export function deployDocument(deployed: AgentCoreDeployedView): Record<string, unknown> {
  return {
    target: TARGET_LABEL,
    name: deployed.name,
    region: deployed.region,
    runtimeName: deployed.runtimeName,
    runtimeId: deployed.runtimeId,
    runtimeArn: deployed.runtimeArn,
    runtimeVersion: deployed.runtimeVersion,
    status: deployed.status,
    imageUri: deployed.imageUri,
    stackName: deployed.stackName,
    logGroup: deployed.logGroup,
    steps: deployed.steps.map(stepDocument),
  };
}

/** Las líneas `clave=valor` con las que termina un despliegue. */
export function deployedLines(deployed: AgentCoreDeployedView): string[] {
  return DEPLOYED_LINES.map((line) =>
    formatTemplate(line, {
      name: deployed.name,
      runtimeId: deployed.runtimeId,
      runtimeArn: deployed.runtimeArn,
      version: deployed.runtimeVersion,
      region: deployed.region,
      logGroup: deployed.logGroup,
    }),
  );
}

export function statusLines(status: AgentCoreStatus): string[] {
  return STATUS_LINES.map((line) =>
    formatTemplate(line, {
      name: status.name,
      runtimeId: status.runtimeId,
      version: status.runtimeVersion,
      status: status.status,
      imageUri: status.imageUri,
      region: status.region,
      logGroup: status.logGroup,
    }),
  );
}

export function statusDocument(status: AgentCoreStatus): Record<string, unknown> {
  return {
    target: TARGET_LABEL,
    name: status.name,
    region: status.region,
    runtimeName: status.runtimeName,
    runtimeId: status.runtimeId,
    runtimeArn: status.runtimeArn,
    runtimeVersion: status.runtimeVersion,
    status: status.status,
    imageUri: status.imageUri,
    logGroup: status.logGroup,
  };
}

export function listLines(region: string, agents: readonly AgentCoreListItem[]): string[] {
  if (agents.length === 0) {
    return [formatTemplate(LIST_EMPTY_TEMPLATE, { region })];
  }
  return agents.map((agent) =>
    formatTemplate(LIST_LINE_TEMPLATE, {
      name: agent.name,
      runtimeName: agent.runtimeName,
      runtimeId: agent.runtimeId,
      version: agent.runtimeVersion,
      status: agent.status,
    }),
  );
}

export function listDocument(
  region: string,
  agents: readonly AgentCoreListItem[],
): Record<string, unknown> {
  return {
    target: TARGET_LABEL,
    region,
    agents: agents.map((agent) => ({
      name: agent.name,
      runtimeName: agent.runtimeName,
      runtimeId: agent.runtimeId,
      runtimeVersion: agent.runtimeVersion,
      status: agent.status,
    })),
  };
}

/** Lo que `rayito agent delete` va a borrar, antes de pedir confirmación. */
export function deletePreviewLines(name: string, preview: AgentCoreDeletePreview): string[] {
  const lines = [formatTemplate(DELETE_HEADER_TEMPLATE, { name, region: preview.region })];
  if (preview.runtimeId !== undefined) {
    lines.push(
      formatTemplate(DELETE_RUNTIME_LINE, {
        runtimeName: preview.runtimeName,
        runtimeId: preview.runtimeId,
      }),
    );
  }
  if (preview.stackExists) {
    lines.push(formatTemplate(DELETE_STACK_LINE, { stackName: preview.stackName }));
  }
  if (preview.logGroup !== undefined) {
    lines.push(formatTemplate(DELETE_LOG_GROUPS_LINE, { logGroup: preview.logGroup }));
  }
  return lines;
}

export function deletedLine(result: AgentCoreDeleteResult): string {
  return formatTemplate(DELETED_TEMPLATE, {
    name: result.name,
    runtimeDeleted: String(result.runtimeDeleted),
    stackDeleted: String(result.stackDeleted),
    logGroupDeleted: String(result.logGroupDeleted),
  });
}

export function deleteDocument(result: AgentCoreDeleteResult): Record<string, unknown> {
  return {
    target: TARGET_LABEL,
    name: result.name,
    region: result.region,
    runtimeDeleted: result.runtimeDeleted,
    stackDeleted: result.stackDeleted,
    logGroupDeleted: result.logGroupDeleted,
  };
}

export function stopDocument(
  name: string,
  sessionId: string,
  stopped: boolean,
): Record<string, unknown> {
  return { target: TARGET_LABEL, name, sessionId, stopped };
}
