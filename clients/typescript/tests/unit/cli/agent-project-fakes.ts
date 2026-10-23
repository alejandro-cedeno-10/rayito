/**
 * Dobles de `AgentProjectTools` para probar `rayito agent ...` sin AWS ni SDK
 * del agente: registran cada llamada con sus argumentos.
 */

import type { AgentEvent, AgentResult } from "../../../src/agent/events.js";
import { TokenUsage } from "../../../src/agent/events.js";
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
  FleetConnectFlags,
  FleetStatusView,
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
  runEvents: AgentEvent[] = [];
  runFailure: Error | undefined;
  runResult: AgentResult = RESULT;

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

export const FLEET_NAME = "support-bot";
export const SECOND_SANDBOX_ID = "microvm-00000000-0000-0000-0000-000000000002";

export class FakeFleetClient implements FleetClient {
  readonly calls: Call[] = [];
  events: AgentEvent[] = [];
  busy: Error | undefined;

  async invoke(prompt: string, options: { sessionId: string | undefined }): Promise<AgentResult> {
    this.calls.push(["invoke", prompt, options.sessionId]);
    if (this.busy !== undefined) {
      throw this.busy;
    }
    return { ...RESULT, sessionId: `rs1.${SANDBOX_ID}.ses_1` };
  }

  async stream(
    prompt: string,
    options: { sessionId: string | undefined },
  ): Promise<AsyncIterable<AgentEvent>> {
    this.calls.push(["stream", prompt, options.sessionId]);
    const events = this.events;
    return (async function* () {
      yield* events;
    })();
  }

  async status(): Promise<FleetStatusView> {
    return {
      name: FLEET_NAME,
      minVms: 1,
      maxVms: 2,
      members: [
        { sandboxId: SANDBOX_ID, state: "unknown" },
        { sandboxId: SECOND_SANDBOX_ID, state: "healthy" },
      ],
    };
  }

  async stop(): Promise<number> {
    this.calls.push(["stop"]);
    return 2;
  }
}

export class FakeAgentProjects implements AgentProjectTools {
  readonly calls: Call[] = [];
  readonly project = new FakeProject();
  readonly deployed = new FakeDeployed();
  readonly fleet = new FakeFleetClient();
  readonly deployRequests: AgentDeployRequest[] = [];
  readonly planRequests: AgentDeployRequest[] = [];
  loadFailure: Error | undefined;
  detectionReport: unknown;
  deployFailure: Error | undefined;
  hiddenRoutes: HiddenAppRoutes | undefined;
  /** Los sandboxes de la flota que `deployFleet` devuelve. */
  sandboxIds: readonly string[] = [SANDBOX_ID];

  async detect(path: string, overrides: AgentDetectOverrides): Promise<DetectedProject> {
    this.calls.push(["detect", path, overrides.framework, overrides.entry]);
    if (this.loadFailure !== undefined) {
      throw this.loadFailure;
    }
    return { report: this.detectionReport, document: { name: FLEET_NAME } };
  }

  async writeDetected(path: string, overrides: AgentDetectOverrides): Promise<string> {
    this.calls.push(["writeDetected", path, overrides.language]);
    return "rayito-agent.json";
  }

  async planDeploy(request: AgentDeployRequest): Promise<DeployPlanView> {
    this.planRequests.push(request);
    return {
      report: this.detectionReport,
      document: { name: FLEET_NAME, scaling: { maxVms: request.flags.maxVms ?? 1 } },
      lines: [`VMs: máximo ${request.flags.maxVms ?? 1}`, "coste de lista: $0,1261/h"],
    };
  }

  async deployFleet(request: AgentDeployRequest): Promise<DeployedFleetView> {
    this.deployRequests.push(request);
    request.settings.onStep?.({ kind: "image", detail: "3.0" });
    if (this.deployFailure !== undefined) {
      throw this.deployFailure;
    }
    return {
      name: FLEET_NAME,
      sandboxIds: this.sandboxIds,
      url: DEPLOYMENT_URL,
      port: request.flags.port ?? DEFAULT_SERVICE_PORT,
      hiddenRoutes: this.hiddenRoutes,
    };
  }

  async connectFleet(
    name: string,
    accessToken: string | undefined,
    options: FleetConnectFlags = {},
  ): Promise<FleetClient> {
    this.calls.push(
      options.renewGatewayKeys === true
        ? ["connectFleet", name, accessToken, options]
        : ["connectFleet", name, accessToken],
    );
    return this.fleet;
  }

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
    return this.deployed;
  }

  async stop(sandboxId: string): Promise<boolean> {
    this.calls.push(["stop", sandboxId]);
    return this.stopResult;
  }
}
