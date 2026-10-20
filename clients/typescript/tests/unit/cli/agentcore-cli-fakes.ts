/**
 * Un `AgentCoreTools` en memoria para las pruebas de `rayito agent ... --target
 * agentcore`: registra lo que la orden le pide y devuelve lo que el test le
 * pone. Nunca AWS.
 */

import type { AgentEvent, AgentResult } from "../../../src/agent/events.js";
import type {
  AgentCoreDeleteResult,
  AgentCoreDeployPlan,
  AgentCoreListItem,
  AgentCoreStatus,
} from "../../../src/agentcore/models.js";
import type {
  AgentCoreClient,
  AgentCoreDeletePreview,
  AgentCoreDeployedView,
  AgentCoreDeployRequest,
  AgentCorePlanView,
  AgentCoreTools,
} from "../../../src/cli/ops-ports.js";
import { samplePlan } from "../agentcore-fakes.js";
import { RESULT } from "./agent-project-fakes.js";

export const FAKE_REGION = "us-east-1";
export const FAKE_ACCOUNT_RUNTIME_ID = "rayito_support_bot-a1b2c3d4e5";

export const DEPLOYED: AgentCoreDeployedView = {
  name: "support-bot",
  region: FAKE_REGION,
  runtimeName: "rayito_support_bot",
  runtimeId: FAKE_ACCOUNT_RUNTIME_ID,
  runtimeArn: `arn:aws:bedrock-agentcore:${FAKE_REGION}:123456789012:runtime/${FAKE_ACCOUNT_RUNTIME_ID}`,
  runtimeVersion: "1",
  status: "READY",
  imageUri:
    "123456789012.dkr.ecr.us-east-1.amazonaws.com/rayito/agentcore/support-bot:src-0123456789abcdef",
  stackName: "rayito-agentcore-support-bot",
  logGroup: `/aws/bedrock-agentcore/runtimes/${FAKE_ACCOUNT_RUNTIME_ID}-DEFAULT`,
  steps: [
    {
      resource: "stack",
      name: "rayito-agentcore-support-bot",
      action: "create",
      status: "CREATE_COMPLETE",
    },
    { resource: "image", name: "rayito/agentcore/support-bot", action: "build", status: null },
    { resource: "runtime", name: "rayito_support_bot", action: "create", status: "READY" },
  ],
};

export const STATUS: AgentCoreStatus = {
  name: DEPLOYED.name,
  region: DEPLOYED.region,
  runtimeName: DEPLOYED.runtimeName,
  runtimeId: DEPLOYED.runtimeId,
  runtimeArn: DEPLOYED.runtimeArn,
  runtimeVersion: DEPLOYED.runtimeVersion,
  status: DEPLOYED.status,
  imageUri: DEPLOYED.imageUri,
  logGroup: DEPLOYED.logGroup,
};

export const LISTED: AgentCoreListItem = {
  name: DEPLOYED.name,
  runtimeName: DEPLOYED.runtimeName,
  runtimeId: DEPLOYED.runtimeId,
  runtimeVersion: "1",
  status: "READY",
};

export const PREVIEW: AgentCoreDeletePreview = {
  region: FAKE_REGION,
  runtimeName: DEPLOYED.runtimeName,
  runtimeId: DEPLOYED.runtimeId,
  stackName: DEPLOYED.stackName,
  stackExists: true,
  logGroup: DEPLOYED.logGroup,
};

type Call = readonly unknown[];

export class FakeAgentCoreClient implements AgentCoreClient {
  readonly calls: Call[];
  stopped = true;

  constructor(calls: Call[]) {
    this.calls = calls;
  }

  async invoke(
    prompt: string,
    options: { readonly sessionId: string | undefined },
  ): Promise<AgentResult> {
    this.calls.push(["invoke", prompt, options.sessionId]);
    return RESULT;
  }

  async stream(
    prompt: string,
    options: { readonly sessionId: string | undefined },
  ): Promise<AsyncIterable<AgentEvent>> {
    this.calls.push(["stream", prompt, options.sessionId]);
    return (async function* (): AsyncGenerator<AgentEvent> {
      yield { type: "text", text: "42" } as AgentEvent;
      yield {
        type: "done",
        sessionId: RESULT.sessionId,
        exitCode: 0,
        usage: RESULT.usage,
        usageReported: true,
      } as AgentEvent;
    })();
  }

  async status(): Promise<AgentCoreStatus> {
    this.calls.push(["status"]);
    return STATUS;
  }

  async stopSession(sessionId: string): Promise<boolean> {
    this.calls.push(["stopSession", sessionId]);
    return this.stopped;
  }
}

export class FakeAgentCoreTools implements AgentCoreTools {
  readonly calls: Call[] = [];
  readonly client = new FakeAgentCoreClient(this.calls);
  plan_: AgentCoreDeployPlan = samplePlan({
    steps: [
      { resource: "stack", name: "rayito-agentcore-support-bot", action: "planned", status: null },
      { resource: "image", name: "rayito/agentcore/support-bot", action: "planned", status: null },
      { resource: "runtime", name: "rayito_support_bot", action: "planned", status: null },
    ],
    security: ["noGateway", "bedrockModels", "publicNetwork", "sessionIsolation"],
    warnings: ["fileMemory"],
    cost: ["runtime: ejemplo", "build: ejemplo"],
  });
  applyError: unknown;
  preview: AgentCoreDeletePreview = PREVIEW;
  agents: AgentCoreListItem[] = [LISTED];
  deleteResult: AgentCoreDeleteResult = {
    name: "support-bot",
    region: FAKE_REGION,
    runtimeDeleted: true,
    stackDeleted: true,
    logGroupDeleted: true,
  };

  async plan(request: AgentCoreDeployRequest): Promise<AgentCorePlanView> {
    this.calls.push(["plan", request]);
    return {
      report: { framework: "deepagents" },
      plan: this.plan_,
      apply: async () => {
        this.calls.push(["apply"]);
        if (this.applyError !== undefined) {
          throw this.applyError;
        }
        return DEPLOYED;
      },
    };
  }

  async connect(name: string): Promise<AgentCoreClient> {
    this.calls.push(["connect", name]);
    return this.client;
  }

  async list() {
    this.calls.push(["list"]);
    return { region: FAKE_REGION, agents: this.agents };
  }

  async previewDelete(name: string): Promise<AgentCoreDeletePreview> {
    this.calls.push(["previewDelete", name]);
    return this.preview;
  }

  async delete(
    name: string,
    onStep: (step: { kind: string; detail: string }) => void,
  ): Promise<AgentCoreDeleteResult> {
    this.calls.push(["delete", name]);
    onStep({ kind: "runtime", detail: "borrando el runtime rayito_support_bot" });
    return this.deleteResult;
  }
}
