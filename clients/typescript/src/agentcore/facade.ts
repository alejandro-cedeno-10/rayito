/**
 * `planDeploy()` y `deployAgent()` con `target: "agentcore"` (ADR-031, D1):
 * detecta el proyecto como el despliegue de MicroVMs, calcula el plan sin
 * llamar a AWS y, sólo `deployAgentCore`, lo aplica con `AgentCoreDeployer` y
 * devuelve el `AgentCoreFleet`. Este módulo se importa sólo con ese destino:
 * sin él ningún cliente de AgentCore, CodeBuild o ECR se construye ni se carga.
 */

import { type Environ, NO_REGION_MESSAGE } from "../agent/aws-resolve.js";
import type { DeployAgentOptions } from "../agent/deploy.js";
import {
  type AgentDeployOptions,
  deployOptionsOf,
  projectFor,
  resolvedRegion,
} from "../agent/deploy-plan.js";
import { renderExport, usesRawProtocol } from "../agent/export.js";
import type { AgentProject } from "../agent/project.js";
import { AgentCoreError, InvalidArgumentError } from "../errors.js";
import { sdkAgentCorePorts } from "./aws-ports.js";
import type { ServiceSettings } from "./clock.js";
import { type CallFlags, planWithoutImageTag } from "./domain.js";
import { AgentCoreFleet } from "./fleet.js";
import { imageTagFor } from "./identifiers.js";
import type { AgentCoreDeployPlan, AgentCoreDeployResult } from "./models.js";
import type { AgentCorePorts } from "./ports.js";
import { AgentCoreDeployer } from "./service.js";

/** Las dependencias de `deployAgent()` con AgentCore que los tests sustituyen: AWS, el entorno y los relojes. En producción es el valor por defecto y no se pasa. */
export interface AgentCoreDeployPorts {
  readonly aws?: AgentCorePorts | undefined;
  readonly environ?: Environ | undefined;
  readonly settings?: ServiceSettings | undefined;
}

export interface PlannedAgentCore {
  readonly plan: AgentCoreDeployPlan;
  /** El árbol de `renderExport(project, "agentcore")`, que el zip del build empaqueta. */
  readonly tree: ReadonlyMap<string, string | Uint8Array>;
  readonly project: AgentProject;
  readonly report: unknown;
}

/**
 * El plan de AgentCore de `path` sin llamar a AWS: detecta el proyecto (lee
 * ficheros locales, no ejecuta su código), valida lo que no necesita AWS en el
 * orden del contrato y calcula la etiqueta de la imagen del árbol que se
 * construiría. `flags` son las banderas de la CLI que ponen valores en el
 * proyecto (`--memory`, `--port`, `--timeout`, `--idle`) o `--token-file`.
 */
export async function planAgentCoreWith(
  ports: AgentCoreDeployPorts,
  path: string,
  args: DeployAgentOptions<"agentcore">,
  flags?: CallFlags,
): Promise<PlannedAgentCore> {
  const options = deployOptionsOf(args.options);
  const detected = await projectFor(path, args.project, args.overrides, args.region);
  const { project } = detected;
  const environ = ports.environ ?? process.env;
  const region = resolvedRegion(args.region, project, environ);
  if (region === undefined) {
    throw new InvalidArgumentError(NO_REGION_MESSAGE);
  }
  const partial = planWithoutImageTag({
    project,
    options,
    agentcore: args.agentcore,
    region,
    environ,
    flags,
  });
  if (usesRawProtocol(project)) {
    throw new AgentCoreError("raw_protocol");
  }
  const tree = await renderExport(project, "agentcore");
  return {
    plan: { ...partial, imageTag: imageTagFor(tree) },
    tree,
    project,
    report: detected.report,
  };
}

/** Lo que un despliegue en AgentCore devuelve: el `AgentCoreFleet` y lo que se hizo (la CLI imprime sus pasos). */
export interface AppliedAgentCore {
  readonly fleet: AgentCoreFleet;
  readonly result: AgentCoreDeployResult;
}

/** Aplica un plan ya calculado (`planAgentCoreWith`) sin volver a detectar el proyecto: es lo que la CLI hace tras mostrarlo y confirmarlo. */
export async function applyPlannedAgentCore(
  ports: AgentCoreDeployPorts,
  planned: PlannedAgentCore,
  args: DeployAgentOptions<"agentcore">,
): Promise<AppliedAgentCore> {
  const options: AgentDeployOptions = deployOptionsOf(args.options);
  const { plan, project } = planned;
  const aws =
    ports.aws ??
    sdkAgentCorePorts({
      region: plan.region,
      ...(args.credentials === undefined ? {} : { credentials: args.credentials }),
      ...(args.proxy === undefined ? {} : { proxy: args.proxy }),
    });
  const result = await new AgentCoreDeployer(aws, ports.settings).apply({
    plan,
    tree: planned.tree,
    modelRegion: project.spec.model.region ?? plan.region,
    buildImage: options.buildImage,
    buildTimeoutMs: options.buildTimeoutMs,
    onStep: options.onStep,
    onBuildLogs: options.onBuildLogs,
  });
  const fleet = new AgentCoreFleet({
    name: result.name,
    region: result.region,
    runtimeId: result.runtimeId,
    runtimeArn: result.runtimeArn,
    runtimeVersion: result.runtimeVersion,
    imageUri: result.imageUri,
    ports: aws,
  });
  return { fleet, result };
}

/** `deployAgent()` con `target: "agentcore"`: el `AgentCoreFleet` del agente desplegado. */
export async function deployAgentCoreWith(
  ports: AgentCoreDeployPorts,
  path: string,
  args: DeployAgentOptions<"agentcore">,
): Promise<AgentCoreFleet> {
  const planned = await planAgentCoreWith(ports, path, args);
  return (await applyPlannedAgentCore(ports, planned, args)).fleet;
}
