/**
 * `AgentCoreTools` sobre el SDK (ADR-031): el plan de `rayito agent deploy
 * --target agentcore`, `AgentCoreFleet`, el listado y el borrado. Todo se importa
 * al usarlo y nunca antes: `rayito agent deploy --dry-run` no construye la sesión
 * de AWS ni carga ningún cliente de AgentCore, CodeBuild o ECR.
 */

import { AgentDeployOptions } from "../agent/deploy-plan.js";
import type { DetectOverrides } from "../agent/detect-report.js";
import type { AgentCorePorts } from "../agentcore/ports.js";
import { type AwsClientSettings, awsClientSettingsOf } from "../aws/control-plane.js";
import { applyProjectFlags, applyServeFlags } from "./agent-project-flags.js";
import type {
  AgentCoreClient,
  AgentCoreDeletePreview,
  AgentCoreDeployedView,
  AgentCoreDeployRequest,
  AgentCorePlanView,
  AgentCoreTools,
  AgentDetectOverrides,
} from "./ops-ports.js";
import type { CliServices } from "./services.js";

type Connection = { region: string; credentials?: NonNullable<AwsClientSettings["credentials"]> };

function overridesOf(overrides: AgentDetectOverrides): DetectOverrides | undefined {
  const none = Object.values(overrides).every((value) => value === undefined);
  return none
    ? undefined
    : {
        framework: overrides.framework,
        language: overrides.language,
        entrypoint: overrides.entry,
        protocol: overrides.protocol,
      };
}

function withSession(options: { readonly sessionId: string | undefined }): { sessionId?: string } {
  return options.sessionId === undefined ? {} : { sessionId: options.sessionId };
}

export class SdkAgentCoreTools implements AgentCoreTools {
  readonly #services: () => Promise<CliServices>;

  constructor(services: () => Promise<CliServices>) {
    this.#services = services;
  }

  async #connection(): Promise<Connection> {
    const session = await this.#services();
    const { credentials } = awsClientSettingsOf(session.controlPlane);
    return { region: session.region, ...(credentials === undefined ? {} : { credentials }) };
  }

  async #ports(): Promise<{ readonly ports: AgentCorePorts; readonly region: string }> {
    const connection = await this.#connection();
    const { sdkAgentCorePorts } = await import("../agentcore/aws-ports.js");
    return { ports: sdkAgentCorePorts(connection), region: connection.region };
  }

  async plan(request: AgentCoreDeployRequest): Promise<AgentCorePlanView> {
    const { planAgentCoreWith, applyPlannedAgentCore } = await import("../agentcore/facade.js");
    const { projectFor } = await import("../agent/deploy-plan.js");
    const { settings, flags } = request;
    const detected = await projectFor(
      request.path,
      undefined,
      overridesOf(request.overrides),
      request.region,
    );
    const project = applyProjectFlags(applyServeFlags(detected.project, flags), flags);
    const options = new AgentDeployOptions({
      bucket: settings.bucket,
      secretNames: settings.secretNames,
      createSecretFromEnv: settings.createSecretFromEnv,
      basePublisher: settings.basePublisher,
      accessToken: settings.accessToken,
      keepOnFailure: settings.keepOnFailure,
      buildImage: settings.buildImage,
      onBuildLogs: settings.onBuildLogs,
      onStep: settings.onStep,
    });
    const args = {
      project,
      options,
      target: "agentcore" as const,
      agentcore: request.agentcore,
      ...(request.region === undefined ? {} : { region: request.region }),
    };
    const ports = { environ: request.environ };
    const planned = await planAgentCoreWith(ports, request.path, args, {
      memoryMib: flags.memory,
      port: flags.port,
      timeoutMs: flags.timeoutMs,
      idleSeconds: flags.idleSeconds,
      minVms: flags.minVms,
      maxVms: flags.maxVms,
      warmVms: flags.warm,
      tokenFile: request.tokenFile,
    });
    return {
      report: detected.report,
      plan: planned.plan,
      apply: async (): Promise<AgentCoreDeployedView> => {
        const connection = await this.#connection();
        const applied = await applyPlannedAgentCore(ports, planned, { ...args, ...connection });
        return applied.result;
      },
    };
  }

  async connect(name: string): Promise<AgentCoreClient> {
    const { AgentCoreFleet } = await import("../agentcore/fleet.js");
    const fleet = await AgentCoreFleet.connect(name, await this.#connection());
    return {
      invoke: (prompt, options) => fleet.invoke(prompt, withSession(options)),
      stream: (prompt, options) => fleet.stream(prompt, withSession(options)),
      status: () => fleet.status(),
      stopSession: (sessionId) => fleet.stopSession(sessionId),
    };
  }

  async list() {
    const { listAgents } = await import("../agentcore/lifecycle.js");
    const { ports, region } = await this.#ports();
    return { region, agents: await listAgents(ports) };
  }

  async previewDelete(name: string): Promise<AgentCoreDeletePreview> {
    const { inspectDeletion } = await import("../agentcore/lifecycle.js");
    const names = await import("../agentcore/names.js");
    const { ports, region } = await this.#ports();
    const target = {
      name: names.validateAgentName(name),
      runtimeName: names.runtimeNameFor(name),
      stackName: names.stackNameFor(name),
    };
    const { listed, stackExists } = await inspectDeletion(ports, target);
    return {
      region,
      runtimeName: target.runtimeName,
      runtimeId: listed?.runtimeId,
      stackName: target.stackName,
      stackExists,
      logGroup: listed === undefined ? undefined : names.runtimeLogGroupFor(listed.runtimeId),
    };
  }

  async delete(
    name: string,
    onStep: (step: { readonly kind: string; readonly detail: string }) => void,
  ) {
    const { AgentCoreDeleter } = await import("../agentcore/lifecycle.js");
    const names = await import("../agentcore/names.js");
    const { ports, region } = await this.#ports();
    return new AgentCoreDeleter(ports).delete({
      name: names.validateAgentName(name),
      region,
      runtimeName: names.runtimeNameFor(name),
      stackName: names.stackNameFor(name),
      onStep,
    });
  }
}
