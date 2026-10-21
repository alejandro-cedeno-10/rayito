/**
 * `AgentProjectTools` sobre el SDK: `AgentProject` y `AgentDeployment`. Se
 * importan al usarlos y nunca antes, así que `rayito agent init` o `export`
 * no cargan nada del agente ni piden la sesión de AWS; `build`, `run`,
 * `deploy` y `connect` la construyen en su primera llamada.
 */

import { CustomRuntime } from "../agent/custom.js";
import type { AgentDeployment } from "../agent/deployment.js";
import type { DetectOverrides } from "../agent/detect-report.js";
import type { AgentProject } from "../agent/project.js";
import { resolveRuntime } from "../agent/runtimes.js";
import { type AwsClientSettings, awsClientSettingsOf } from "../aws/control-plane.js";
import { InvalidArgumentError, SandboxNotFoundError } from "../errors.js";
import type { BuildInfo } from "../templates/build.js";
import { describePlan, poolBackendFileFor } from "./agent-deploy.js";
import { envWarningLine } from "./agent-secret-env.js";
import type {
  AgentBuildRequest,
  AgentDeployRequest,
  AgentDetectOverrides,
  AgentExportRequest,
  AgentExportResult,
  AgentProjectRunRequest,
  AgentProjectTool,
  AgentProjectTools,
  DeployedAgent,
  DeployedFleetView,
  DeployPlanView,
  DetectedProject,
  FleetClient,
  HiddenAppRoutes,
} from "./ops-ports.js";
import type { CliServices } from "./services.js";

type AwsOptions = { region: string; credentials?: NonNullable<AwsClientSettings["credentials"]> };

async function awsOptionsOf(services: () => Promise<CliServices>): Promise<AwsOptions> {
  const session = await services();
  const { credentials } = awsClientSettingsOf(session.controlPlane);
  return { region: session.region, ...(credentials === undefined ? {} : { credentials }) };
}

function deployedAgent(deployment: AgentDeployment): DeployedAgent {
  return {
    sandboxId: deployment.sandboxId,
    url: deployment.url,
    invoke: (prompt, options) => deployment.invoke(prompt, withSession(options)),
    stream: (prompt, options) => deployment.stream(prompt, withSession(options)),
    status: () => deployment.status(),
  };
}

function hiddenRoutesOf(project: AgentProject): HiddenAppRoutes | undefined {
  const runtime = resolveRuntime(project.runtime);
  const app = runtime instanceof CustomRuntime ? runtime.app : undefined;
  return app?.hidesOwnRoutes === true
    ? { protocol: app.protocol, healthPath: app.healthPath, invokePath: app.invokePath }
    : undefined;
}

function withSession(options: { readonly sessionId: string | undefined }): {
  sessionId?: string;
} {
  return options.sessionId === undefined ? {} : { sessionId: options.sessionId };
}

class SdkAgentProjectTool implements AgentProjectTool {
  readonly #project: AgentProject;
  readonly #aws: () => Promise<AwsOptions>;

  constructor(project: AgentProject, aws: () => Promise<AwsOptions>) {
    this.#project = project;
    this.#aws = aws;
  }

  async build(options: AgentBuildRequest): Promise<BuildInfo> {
    return this.#project.build({
      ...(await this.#aws()),
      bucket: options.bucket,
      force: options.force,
      timeoutMs: options.timeoutMs,
      ...(options.onBuildLogs === undefined ? {} : { onBuildLogs: options.onBuildLogs }),
    });
  }

  async run(prompt: string, options: AgentProjectRunRequest) {
    return this.#project.run(prompt, {
      ...(await this.#aws()),
      ...(options.sessionId === undefined ? {} : { sessionId: options.sessionId }),
      ...(options.model === undefined ? {} : { model: options.model }),
      ...(options.onEvent === undefined ? {} : { onEvent: options.onEvent }),
    });
  }

  async export(options: AgentExportRequest): Promise<AgentExportResult> {
    return this.#project.export(options);
  }
}

export class SdkAgentProjectTools implements AgentProjectTools {
  readonly #aws: () => Promise<AwsOptions>;
  readonly #region: () => string | undefined;

  constructor(services: () => Promise<CliServices>, region?: () => string | undefined) {
    this.#aws = () => awsOptionsOf(services);
    this.#region = region ?? (() => undefined);
  }

  async load(file: string): Promise<AgentProjectTool> {
    const { AgentProject } = await import("../agent/project.js");
    return new SdkAgentProjectTool(await AgentProject.load(file), this.#aws);
  }

  async connect(sandboxId: string, accessToken: string): Promise<DeployedAgent> {
    const { AgentDeployment } = await import("../agent/deployment.js");
    return deployedAgent(
      await AgentDeployment.connect(sandboxId, { ...(await this.#aws()), accessToken }),
    );
  }

  async detect(path: string, overrides: AgentDetectOverrides): Promise<DetectedProject> {
    const { projectFor } = await import("../agent/deploy-plan.js");
    const { project, report } = await projectFor(
      path,
      undefined,
      overridesOf(overrides),
      this.#region(),
    );
    return { report, document: project.toObject() };
  }

  async writeDetected(path: string, overrides: AgentDetectOverrides): Promise<string> {
    const { writeFileSync, existsSync } = await import("node:fs");
    const { join } = await import("node:path");
    const { AGENT_PROJECT_FILE_NAMES } = await import("../limits.js");
    for (const name of AGENT_PROJECT_FILE_NAMES) {
      if (existsSync(join(path, name))) {
        throw new InvalidArgumentError(`ya existe ${name}; bórralo o edítalo a mano`);
      }
    }
    const { document } = await this.detect(path, overrides);
    const target = AGENT_PROJECT_FILE_NAMES[AGENT_PROJECT_FILE_NAMES.length - 1] ?? "";
    writeFileSync(join(path, target), `${JSON.stringify(document, null, 2)}\n`);
    return target;
  }

  async planDeploy(request: AgentDeployRequest): Promise<DeployPlanView> {
    const { planDeploy } = await import("../agent/deploy.js");
    const { project, report, options } = await prepared(request, false, this.#region());
    const plan = await planDeploy(request.path, {
      project,
      options,
      region: this.#region(),
    });
    const { detection: _detection, ...rest } = plan;
    const { prepareSecretEnv } = await import("../agent/secret-env-deploy.js");
    const { warnings } = prepareSecretEnv(project, options.createSecretsFrom, options.checkEnv);
    return {
      report,
      document: { ...rest, scaling: { ...plan.scaling } },
      lines: [...warnings.map(envWarningLine), ...describePlan(plan)],
    };
  }

  async deployFleet(request: AgentDeployRequest): Promise<DeployedFleetView> {
    const { deployAgent } = await import("../agent/deploy.js");
    const { project, options } = await prepared(request, true, this.#region());
    const fleet = await deployAgent(request.path, { project, options, ...(await this.#aws()) });
    await fleet.detach();
    const [first] = fleet.deployments;
    return {
      name: fleet.name,
      sandboxIds: fleet.sandboxIds,
      url: first?.url ?? "",
      port: project.serve.port,
      hiddenRoutes: hiddenRoutesOf(project),
    };
  }

  async connectFleet(name: string, accessToken: string | undefined): Promise<FleetClient> {
    const { AgentFleet } = await import("../agent/fleet.js");
    const aws = await this.#aws();
    const fleet = await AgentFleet.connect(name, {
      ...(accessToken === undefined ? {} : { accessToken }),
      connection: { region: aws.region },
    });
    return {
      invoke: (prompt, options) => fleet.invoke(prompt, withSession(options)),
      stream: (prompt, options) => fleet.stream(prompt, withSession(options)),
      status: async () => {
        const status = await fleet.status();
        return {
          name: status.name,
          minVms: status.minVms,
          maxVms: status.maxVms,
          members: status.members.map(({ sandboxId, state }) => ({ sandboxId, state })),
        };
      },
      stop: async () => {
        const count = fleet.sandboxIds.length;
        await fleet.stop();
        return count;
      },
    };
  }

  async stop(sandboxId: string): Promise<boolean> {
    const { Sandbox } = await import("../sandbox/sandbox.js");
    try {
      return await Sandbox.kill(sandboxId, await this.#aws());
    } catch (error) {
      if (error instanceof SandboxNotFoundError) {
        return false;
      }
      throw error;
    }
  }
}

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

/** El proyecto detectado con las banderas aplicadas, su informe y las
 * `DeployOptions` de la petición. */
async function prepared(request: AgentDeployRequest, park: boolean, region: string | undefined) {
  const { projectFor, withExcludedFile } = await import("../agent/deploy-plan.js");
  const { AgentDeployOptions } = await import("../agent/deploy-plan.js");
  const { applyProjectFlags, applyServeFlags, scalingOf } = await import(
    "./agent-project-flags.js"
  );
  const detected = await projectFor(
    request.path,
    undefined,
    overridesOf(request.overrides),
    region,
  );
  const { settings } = request;
  const project = withExcludedFile(
    applyProjectFlags(applyServeFlags(detected.project, request.flags), request.flags),
    settings.tokenFile,
  );
  const scaling = scalingOf(project, request.flags);
  const { fleetSettingsOf } = await import("../agent/fleet-base.js");
  const backendFile = park
    ? poolBackendFileFor(fleetSettingsOf(project, scaling).scaling.warmVms)
    : undefined;
  const poolBackend =
    backendFile === undefined
      ? undefined
      : new (await import("../pool/backend.js")).JsonFilePoolBackend(backendFile);
  const options = new AgentDeployOptions({
    bucket: settings.bucket,
    secretNames: settings.secretNames,
    createSecretFromEnv: settings.createSecretFromEnv,
    basePublisher: settings.basePublisher,
    accessToken: settings.accessToken,
    keepOnFailure: settings.keepOnFailure,
    buildImage: settings.buildImage,
    scaling,
    poolBackend,
    onBuildLogs: settings.onBuildLogs,
    onStep: settings.onStep,
    createSecretsFrom: settings.createSecretsFrom,
    checkEnv: settings.checkEnv,
  });
  return { project, report: detected.report, options };
}
