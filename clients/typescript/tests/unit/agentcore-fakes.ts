/**
 * Dobles en memoria de los puertos de `--target agentcore`: nunca AWS. Cada
 * llamada queda registrada en `calls`, y el estado de la cuenta (pila, imagen,
 * runtimes, sesiones) es el que el test le pone.
 */

import type {
  AgentCoreDeployPlan,
  ImageObservation,
  ListedRuntime,
  RuntimeDetails,
} from "../../src/agentcore/models.js";
import {
  type AgentCorePorts,
  type BuildObservation,
  type BuildRequest,
  type InvokeRequest,
  RuntimeConflictError,
  type RuntimeWriteRequest,
  type RuntimeWriteResult,
} from "../../src/agentcore/ports.js";
import { StackError } from "../../src/errors.js";
import type { StackStatus } from "../../src/stacks/model.js";
import type { StackProvisioner, UpdateOutcome } from "../../src/stacks/port.js";

export const ACCOUNT = "123456789012";
export const REGION = "us-east-1";
export const BUCKET = "amzn-s3-demo-bucket";
export const AGENT = "support-bot";
export const RUNTIME_NAME = "rayito_support_bot";
export const RUNTIME_ID = "rayito_support_bot-a1b2c3d4e5";
export const IMAGE_TAG = "src-0123456789abcdef";
export const REPOSITORY_URI = `${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/rayito/agentcore/${AGENT}`;
export const RUNTIME_ROLE = `arn:aws:iam::${ACCOUNT}:role/rayito-agentcore-${AGENT}-RuntimeRole-ABCDEFGHIJKL`;
export const DESCRIPTION = `Rayito agent ${AGENT} (rayito.agentcore/1)`;

export const STACK_OUTPUTS: Readonly<Record<string, string>> = {
  RepositoryName: `rayito/agentcore/${AGENT}`,
  RepositoryUri: REPOSITORY_URI,
  RepositoryArn: `arn:aws:ecr:${REGION}:${ACCOUNT}:repository/rayito/agentcore/${AGENT}`,
  BuildProjectName: `rayito-agentcore-${AGENT}`,
  BuildLogGroupName: `/aws/codebuild/rayito-agentcore-${AGENT}`,
  RuntimeRoleArn: RUNTIME_ROLE,
};

export const SDK_ENVIRONMENT = { AWS_DEFAULT_REGION: REGION, AWS_REGION: REGION };

export function samplePlan(overrides: Partial<AgentCoreDeployPlan> = {}): AgentCoreDeployPlan {
  return {
    target: "agentcore",
    name: AGENT,
    region: REGION,
    runtimeName: RUNTIME_NAME,
    stackName: `rayito-agentcore-${AGENT}`,
    repositoryName: `rayito/agentcore/${AGENT}`,
    buildProjectName: `rayito-agentcore-${AGENT}`,
    imageTag: IMAGE_TAG,
    bucket: BUCKET,
    bucketSource: "argument",
    network: "PUBLIC",
    bedrockModels: ["us.anthropic.claude-haiku-4-5-20251001-v1:0"],
    readableSecrets: [],
    environment: SDK_ENVIRONMENT,
    lifecycle: { idleSessionTimeoutSeconds: 900, maxSessionLifetimeSeconds: 28800 },
    tags: {},
    steps: [],
    security: [],
    warnings: [],
    cost: [],
    ...overrides,
  };
}

export function desiredDetails(overrides: Partial<RuntimeDetails> = {}): RuntimeDetails {
  return {
    runtimeId: RUNTIME_ID,
    runtimeArn: `arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT}:runtime/${RUNTIME_ID}`,
    runtimeName: RUNTIME_NAME,
    runtimeVersion: "2",
    status: "READY",
    description: DESCRIPTION,
    roleArn: RUNTIME_ROLE,
    networkMode: "PUBLIC",
    serverProtocol: "HTTP",
    environmentVariables: SDK_ENVIRONMENT,
    idleRuntimeSessionTimeout: 900,
    maxLifetime: 28800,
    containerUri: `${REPOSITORY_URI}:${IMAGE_TAG}`,
    requireMMDSV2: true,
    ...overrides,
  };
}

export interface Call {
  readonly operation: string;
  readonly input?: unknown;
}

export interface StackState {
  status: string | undefined;
  outputs: Record<string, string>;
  parameters: Record<string, string>;
  /** El estado al que pasa la pila cuando alguien espera a que termine lo que está en curso. */
  settlesTo?: string | undefined;
}

/** `CloudFormation` en memoria. */
export class FakeStacks implements StackProvisioner {
  readonly state: StackState;
  readonly calls: Call[];
  unchanged = false;
  failWait = false;

  constructor(state: StackState, calls: Call[]) {
    this.state = state;
    this.calls = calls;
  }

  async describe(): Promise<StackStatus | undefined> {
    this.calls.push({ operation: "DescribeStacks" });
    return this.state.status === undefined
      ? undefined
      : {
          name: "rayito-agentcore",
          state: this.state.status,
          outputs: this.state.outputs,
          parameters: this.state.parameters,
        };
  }

  async create(
    _component: unknown,
    options: { parameters: Record<string, string>; tags: Record<string, string> },
  ): Promise<void> {
    this.calls.push({ operation: "CreateStack", input: options });
    this.state.status = "CREATE_IN_PROGRESS";
    this.state.settlesTo = "CREATE_COMPLETE";
    this.state.parameters = options.parameters;
  }

  async update(
    _component: unknown,
    options: { parameters: Record<string, string>; tags: Record<string, string> },
  ): Promise<UpdateOutcome> {
    this.calls.push({ operation: "UpdateStack", input: options });
    if (this.unchanged) {
      return "no_changes";
    }
    this.state.status = "UPDATE_IN_PROGRESS";
    this.state.settlesTo = "UPDATE_COMPLETE";
    this.state.parameters = options.parameters;
    return "changed";
  }

  async delete(): Promise<void> {
    this.calls.push({ operation: "DeleteStack" });
    this.state.status = "DELETE_IN_PROGRESS";
    this.state.settlesTo = undefined;
  }

  async wait(_name: string, target: "deployed" | "deleted"): Promise<void> {
    this.calls.push({ operation: "WaitStack", input: target });
    if (this.failWait) {
      throw new StackError("la pila falló", { code: "failed" });
    }
    if (target === "deleted") {
      this.state.status = undefined;
      this.state.outputs = {};
      return;
    }
    if (this.state.settlesTo !== undefined) {
      this.state.status = this.state.settlesTo;
      if (this.state.status === "CREATE_COMPLETE") {
        this.state.outputs = { ...STACK_OUTPUTS };
      }
    }
  }

  async putArtifact(): Promise<void> {}

  async failureReason(): Promise<string | undefined> {
    return undefined;
  }
}

export interface RuntimeState {
  listed: ListedRuntime | undefined;
  details: RuntimeDetails | undefined;
  /** El estado en el que se queda tras una espera (`wait`, `create`, `update`). */
  settled: RuntimeDetails | undefined;
  conflictOnCreate: boolean;
  failOnSettle: string | undefined;
}

const NEW_VERSION = "9";

/** `bedrock-agentcore-control` en memoria; las escrituras dejan el runtime en `READY` tras una espera. */
export class FakeControl {
  readonly runtime: RuntimeState;
  readonly calls: Call[];
  readonly writes: RuntimeWriteRequest[] = [];
  #pendingStatus: string | undefined;

  constructor(runtime: RuntimeState, calls: Call[]) {
    this.runtime = runtime;
    this.calls = calls;
  }

  async listRuntimes(): Promise<ListedRuntime[]> {
    this.calls.push({ operation: "ListAgentRuntimes" });
    return this.runtime.listed === undefined ? [] : [this.runtime.listed];
  }

  async getRuntime(runtimeId: string): Promise<RuntimeDetails | undefined> {
    this.calls.push({ operation: "GetAgentRuntime", input: runtimeId });
    const current = this.runtime.details ?? this.runtime.listed;
    if (current === undefined) {
      return undefined;
    }
    if (this.#pendingStatus !== undefined) {
      const settled = this.runtime.settled ?? (this.runtime.details as RuntimeDetails);
      const status = this.runtime.failOnSettle ?? "READY";
      this.#pendingStatus = undefined;
      this.runtime.details = { ...settled, status };
      this.runtime.listed = { ...(this.runtime.listed as ListedRuntime), status };
    }
    return (this.runtime.details ?? ({ ...current } as RuntimeDetails)) as RuntimeDetails;
  }

  async createRuntime(request: RuntimeWriteRequest): Promise<RuntimeWriteResult> {
    this.calls.push({ operation: "CreateAgentRuntime", input: request });
    if (this.runtime.conflictOnCreate) {
      this.runtime.conflictOnCreate = false;
      this.runtime.listed = {
        runtimeId: RUNTIME_ID,
        runtimeArn: desiredDetails().runtimeArn,
        runtimeName: RUNTIME_NAME,
        runtimeVersion: "1",
        status: "CREATING",
        description: DESCRIPTION,
      };
      this.runtime.details = desiredDetails({
        runtimeVersion: "1",
        status: "CREATING",
        requireMMDSV2: false,
      });
      this.runtime.settled = desiredDetails({ runtimeVersion: "1", requireMMDSV2: false });
      this.#pendingStatus = "READY";
      throw new RuntimeConflictError();
    }
    this.writes.push(request);
    const details = desiredDetails({
      runtimeVersion: "1",
      status: "CREATING",
      containerUri: request.containerUri,
      environmentVariables: request.environmentVariables,
      idleRuntimeSessionTimeout: request.idleRuntimeSessionTimeout,
      maxLifetime: request.maxLifetime,
      requireMMDSV2: false,
    });
    this.runtime.listed = details;
    this.runtime.details = details;
    this.runtime.settled = { ...details, status: "READY" };
    this.#pendingStatus = "READY";
    return {
      runtimeId: RUNTIME_ID,
      runtimeArn: details.runtimeArn,
      runtimeVersion: "1",
      status: "CREATING",
    };
  }

  async updateRuntime(request: RuntimeWriteRequest): Promise<RuntimeWriteResult> {
    this.calls.push({ operation: "UpdateAgentRuntime", input: request });
    if (this.#pendingStatus !== undefined) {
      throw new Error("UpdateAgentRuntime sobre un runtime que no está READY");
    }
    this.writes.push(request);
    const current = (this.runtime.details ?? this.runtime.listed) as RuntimeDetails;
    const details: RuntimeDetails = {
      ...current,
      runtimeVersion: NEW_VERSION,
      status: "UPDATING",
      containerUri: request.containerUri,
      roleArn: request.roleArn,
      environmentVariables: request.environmentVariables,
      idleRuntimeSessionTimeout: request.idleRuntimeSessionTimeout,
      maxLifetime: request.maxLifetime,
      requireMMDSV2: request.requireMMDSV2 === true,
    };
    this.runtime.details = details;
    this.runtime.listed = {
      ...(this.runtime.listed ?? details),
      status: "UPDATING",
      runtimeVersion: NEW_VERSION,
    };
    this.runtime.settled = { ...details, status: "READY" };
    this.#pendingStatus = "READY";
    return {
      runtimeId: request.runtimeId ?? RUNTIME_ID,
      runtimeArn: details.runtimeArn,
      runtimeVersion: NEW_VERSION,
      status: "UPDATING",
    };
  }

  async deleteRuntime(runtimeId: string): Promise<void> {
    this.calls.push({ operation: "DeleteAgentRuntime", input: runtimeId });
    this.runtime.listed = undefined;
    this.runtime.details = undefined;
  }

  /** Lo que una espera posterior a `wait` ve: el runtime ya terminó lo que tenía en curso. */
  settle(): void {
    this.#pendingStatus = "READY";
  }
}

export interface ImageState {
  exists: boolean;
  sizeBytes: number | undefined;
  buildStatus: string;
}

export interface FakeAccount {
  readonly ports: AgentCorePorts;
  readonly calls: Call[];
  readonly stacks: FakeStacks;
  readonly control: FakeControl;
  readonly image: ImageState;
  readonly builds: BuildRequest[];
  readonly invokes: InvokeRequest[];
  readonly source: { puts: string[]; deletes: string[]; region: string | undefined };
  readonly logs: { deleted: string[]; exists: boolean };
  clock: { now: number; sleeps: number[] };
  response: Uint8Array[];
  invokeError: unknown;
  discovered: string | undefined;
}

export function newAccount(
  init: {
    stack?: Partial<StackState> & { status: string | undefined };
    runtime?: Partial<RuntimeState>;
    image?: Partial<ImageState>;
  } = {},
): FakeAccount {
  const calls: Call[] = [];
  const stacks = new FakeStacks(
    {
      status: init.stack?.status,
      outputs:
        init.stack?.outputs ?? (init.stack?.status === undefined ? {} : { ...STACK_OUTPUTS }),
      parameters: init.stack?.parameters ?? {},
      ...(init.stack?.settlesTo === undefined ? {} : { settlesTo: init.stack.settlesTo }),
    },
    calls,
  );
  const control = new FakeControl(
    {
      listed: init.runtime?.listed,
      details: init.runtime?.details,
      settled: init.runtime?.settled,
      conflictOnCreate: init.runtime?.conflictOnCreate ?? false,
      failOnSettle: init.runtime?.failOnSettle,
    },
    calls,
  );
  const image: ImageState = {
    exists: false,
    sizeBytes: 800_000_000,
    buildStatus: "SUCCEEDED",
    ...init.image,
  };
  const account: FakeAccount = {
    calls,
    stacks,
    control,
    image,
    builds: [],
    invokes: [],
    source: { puts: [], deletes: [], region: REGION },
    logs: { deleted: [], exists: true },
    clock: { now: 0, sleeps: [] },
    response: [],
    invokeError: undefined,
    discovered: undefined,
    ports: undefined as unknown as AgentCorePorts,
  };
  let polls = 0;
  const ports: AgentCorePorts = {
    control,
    stacks,
    identity: {
      accountId: async () => {
        calls.push({ operation: "GetCallerIdentity" });
        return ACCOUNT;
      },
    },
    buckets: { discover: async () => account.discovered },
    source: {
      putSource: async (_bucket, key) => {
        calls.push({ operation: "PutObject", input: key });
        account.source.puts.push(key);
      },
      deleteSource: async (_bucket, key) => {
        calls.push({ operation: "DeleteObject", input: key });
        account.source.deletes.push(key);
      },
      bucketRegion: async () => account.source.region,
    },
    registry: {
      describeImage: async (): Promise<ImageObservation> => {
        calls.push({ operation: "DescribeImages" });
        return { exists: image.exists, sizeBytes: image.exists ? image.sizeBytes : undefined };
      },
    },
    builder: {
      startBuild: async (request) => {
        calls.push({ operation: "StartBuild", input: request });
        account.builds.push(request);
        polls = 0;
        return { buildId: "rayito-agentcore-support-bot:build-1" };
      },
      getBuild: async (): Promise<BuildObservation> => {
        calls.push({ operation: "BatchGetBuilds" });
        polls += 1;
        if (polls < 2) {
          return {
            status: "IN_PROGRESS",
            phase: "BUILD",
            logGroup: STACK_OUTPUTS.BuildLogGroupName,
          };
        }
        image.exists = image.buildStatus === "SUCCEEDED";
        return {
          status: image.buildStatus,
          phase: "COMPLETED",
          logGroup: STACK_OUTPUTS.BuildLogGroupName,
        };
      },
    },
    logs: {
      deleteLogGroup: async (name) => {
        calls.push({ operation: "DeleteLogGroup", input: name });
        account.logs.deleted.push(name);
        return account.logs.exists;
      },
    },
    invoker: {
      invoke: async (request) => {
        calls.push({ operation: "InvokeAgentRuntime", input: request });
        account.invokes.push(request);
        if (account.invokeError !== undefined) {
          throw account.invokeError;
        }
        const chunks = account.response;
        return (async function* () {
          yield* chunks;
        })();
      },
      stopSession: async (_arn, sessionId) => {
        calls.push({ operation: "StopRuntimeSession", input: sessionId });
        return true;
      },
    },
  };
  Object.assign(account, { ports });
  return account;
}

export function clockOf(account: FakeAccount): {
  sleep: (ms: number) => Promise<void>;
  now: () => number;
} {
  return {
    sleep: async (ms) => {
      account.clock.sleeps.push(ms);
      account.clock.now += ms;
    },
    now: () => account.clock.now,
  };
}

export function operations(account: FakeAccount): string[] {
  return account.calls.map((call) => call.operation);
}
