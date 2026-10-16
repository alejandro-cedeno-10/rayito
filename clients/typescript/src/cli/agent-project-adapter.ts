/**
 * `AgentProjectTools` sobre el SDK: `AgentProject` y `AgentDeployment`. Se
 * importan al usarlos y nunca antes, así que `rayito agent init` o `export`
 * no cargan nada del agente ni piden la sesión de AWS; `build`, `run`,
 * `deploy` y `connect` la construyen en su primera llamada.
 */

import { CustomRuntime } from "../agent/custom.js";
import type { AgentDeployment } from "../agent/deployment.js";
import type { AgentProject } from "../agent/project.js";
import { resolveRuntime } from "../agent/runtimes.js";
import { type AwsClientSettings, awsClientSettingsOf } from "../aws/control-plane.js";
import { SandboxNotFoundError } from "../errors.js";
import type { BuildInfo } from "../templates/build.js";
import type {
  AgentBuildRequest,
  AgentDeployOverrides,
  AgentExportRequest,
  AgentExportResult,
  AgentProjectRunRequest,
  AgentProjectTool,
  AgentProjectTools,
  DeployedAgent,
  DeployedService,
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

  async deploy(overrides: AgentDeployOverrides): Promise<DeployedService> {
    const { serve } = this.#project;
    const effective = {
      ...serve,
      ...(overrides.port === undefined ? {} : { port: overrides.port }),
      ...(overrides.timeoutMs === undefined ? {} : { timeoutMs: overrides.timeoutMs }),
    };
    const { AgentProject } = await import("../agent/project.js");
    const project = new AgentProject({ ...this.#project, serve: effective });
    const deployment = await project.deploy({
      ...(await this.#aws()),
      accessToken: overrides.accessToken,
      keepOnFailure: overrides.keepOnFailure,
    });
    return {
      agent: deployedAgent(deployment),
      port: effective.port,
      hiddenRoutes: hiddenRoutesOf(this.#project),
    };
  }

  async export(options: AgentExportRequest): Promise<AgentExportResult> {
    return this.#project.export(options);
  }
}

export class SdkAgentProjectTools implements AgentProjectTools {
  readonly #aws: () => Promise<AwsOptions>;

  constructor(services: () => Promise<CliServices>) {
    this.#aws = () => awsOptionsOf(services);
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
