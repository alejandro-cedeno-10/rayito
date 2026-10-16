/**
 * Dobles de `AgentProjectTools` para probar `rayito agent ...` sin AWS ni SDK
 * del agente: registran cada llamada con sus argumentos.
 */

import type { AgentEvent, AgentResult } from "../../../src/agent/events.js";
import { TokenUsage } from "../../../src/agent/events.js";
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
} from "../../../src/cli/ops-ports.js";
import type { BuildInfo } from "../../../src/templates/build.js";
import type { Call } from "./ops-fakes.js";

export const SANDBOX_ID = "microvm-00000000-0000-0000-0000-000000000001";
export const DEPLOYMENT_URL = "https://abc.lambda-microvm.us-east-1.on.aws";
export const USAGE = new TokenUsage({ input: 10, output: 5 });
export const DEFAULT_SERVICE_PORT = 8088;

export const RESULT: AgentResult = {
  sessionId: "ses_1",
  text: "hola",
  steps: 2,
  usage: USAGE,
  exitCode: 0,
  toolCalls: [],
  droppedLines: 0,
};

const BUILD_INFO: BuildInfo = { templateId: "tpl-1", buildId: "bld-1", alias: "support-bot" };

export class FakeDeployed implements DeployedAgent {
  readonly sandboxId = SANDBOX_ID;
  readonly url = DEPLOYMENT_URL;
  readonly calls: Call[] = [];
  events: AgentEvent[] = [];

  async invoke(prompt: string, options: { sessionId: string | undefined }): Promise<AgentResult> {
    this.calls.push(["invoke", prompt, options.sessionId]);
    return RESULT;
  }

  async *stream(
    prompt: string,
    options: { sessionId: string | undefined },
  ): AsyncIterable<AgentEvent> {
    this.calls.push(["stream", prompt, options.sessionId]);
    yield* this.events;
  }

  async status() {
    return {
      state: "RUNNING",
      ready: true,
      busy: false,
      port: DEFAULT_SERVICE_PORT,
      project: "support-bot",
    };
  }
}

export class FakeProject implements AgentProjectTool {
  readonly calls: Call[] = [];
  readonly deployed = new FakeDeployed();
  runEvents: AgentEvent[] = [];
  runFailure: Error | undefined;
  runResult: AgentResult = RESULT;
  hiddenRoutes: HiddenAppRoutes | undefined;
  deployFailure: Error | undefined;

  async build(options: AgentBuildRequest): Promise<BuildInfo> {
    this.calls.push(["build", options.bucket, options.force, options.timeoutMs]);
    return BUILD_INFO;
  }

  async run(prompt: string, options: AgentProjectRunRequest): Promise<AgentResult> {
    this.calls.push([
      "run",
      prompt,
      options.sessionId,
      options.model,
      options.onEvent !== undefined,
    ]);
    if (this.runFailure !== undefined) {
      throw this.runFailure;
    }
    for (const event of this.runEvents) {
      options.onEvent?.(event);
    }
    return this.runResult;
  }

  async deploy(overrides: AgentDeployOverrides): Promise<DeployedService> {
    this.calls.push([
      "deploy",
      overrides.port,
      overrides.timeoutMs,
      overrides.accessToken,
      overrides.keepOnFailure,
    ]);
    if (this.deployFailure !== undefined) {
      throw this.deployFailure;
    }
    return {
      agent: this.deployed,
      port: overrides.port ?? DEFAULT_SERVICE_PORT,
      hiddenRoutes: this.hiddenRoutes,
    };
  }

  async export(options: AgentExportRequest): Promise<AgentExportResult> {
    this.calls.push(["export", options.target, options.outDir, options.overwrite]);
    return {
      path: options.outDir,
      files: ["Dockerfile", "README.md"],
      port: 8080,
      platform: "linux/arm64",
    };
  }
}

export class FakeAgentProjects implements AgentProjectTools {
  readonly calls: Call[] = [];
  readonly project = new FakeProject();
  loadFailure: Error | undefined;

  async load(file: string): Promise<AgentProjectTool> {
    this.calls.push(["load", file]);
    if (this.loadFailure !== undefined) {
      throw this.loadFailure;
    }
    return this.project;
  }

  stopResult = true;

  async connect(sandboxId: string, accessToken: string): Promise<DeployedAgent> {
    this.calls.push(["connect", sandboxId, accessToken]);
    return this.project.deployed;
  }

  async stop(sandboxId: string): Promise<boolean> {
    this.calls.push(["stop", sandboxId]);
    return this.stopResult;
  }
}
