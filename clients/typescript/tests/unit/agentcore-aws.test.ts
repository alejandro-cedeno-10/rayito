/**
 * Los adaptadores de AWS de `--target agentcore` (ADR-031, D8) con los clientes
 * del SDK sustituidos por un doble que graba cada comando: los parámetros que
 * salen son los de `AWS_API_NOTES.md` («Despliegue en AgentCore Runtime») y los
 * errores se traducen por código, sin el mensaje de AWS. Nunca AWS.
 */

import { createHash } from "node:crypto";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { SdkRuntimeControl, SdkRuntimeInvoker } from "../../src/agentcore/aws.js";
import {
  SdkAccountIdentity,
  SdkImageBuilder,
  SdkImageRegistry,
  SdkRuntimeLogs,
  SdkSourceStore,
} from "../../src/agentcore/aws-build.js";
import { clientTokenFor } from "../../src/agentcore/identifiers.js";
import { MissingResourceError, RuntimeConflictError } from "../../src/agentcore/ports.js";
import {
  AgentCoreError,
  AgentError,
  QuotaExceededError,
  RateLimitError,
} from "../../src/errors.js";

const sdk = vi.hoisted(() => {
  interface Sent {
    readonly client: string;
    readonly command: string;
    readonly input: Record<string, unknown>;
    readonly config: Record<string, unknown>;
  }
  const state = {
    sent: [] as Sent[],
    clients: [] as { name: string; config: Record<string, unknown> }[],
    responses: new Map<string, unknown[]>(),
  };
  function module(clientName: string, commands: string[]): Record<string, unknown> {
    const exports: Record<string, unknown> = {};
    class Client {
      readonly config: Record<string, unknown>;
      constructor(config: Record<string, unknown>) {
        this.config = config;
        state.clients.push({ name: clientName, config });
      }
      async send(command: { constructor: { name: string }; input: Record<string, unknown> }) {
        const name = command.constructor.name;
        state.sent.push({
          client: clientName,
          command: name,
          input: command.input,
          config: this.config,
        });
        const queue = state.responses.get(name) ?? [];
        const next = queue.length > 1 ? queue.shift() : queue[0];
        if (next instanceof Error) {
          throw next;
        }
        return next ?? {};
      }
    }
    Object.defineProperty(Client, "name", { value: clientName });
    exports[clientName] = Client;
    for (const name of commands) {
      const Command = class {
        readonly input: Record<string, unknown>;
        constructor(input: Record<string, unknown>) {
          this.input = input;
        }
      };
      Object.defineProperty(Command, "name", { value: name });
      exports[name] = Command;
    }
    return exports;
  }
  return { state, module };
});

vi.mock("@aws-sdk/client-bedrock-agentcore-control", () =>
  sdk.module("BedrockAgentCoreControlClient", [
    "CreateAgentRuntimeCommand",
    "UpdateAgentRuntimeCommand",
    "GetAgentRuntimeCommand",
    "ListAgentRuntimesCommand",
    "DeleteAgentRuntimeCommand",
  ]),
);
vi.mock("@aws-sdk/client-bedrock-agentcore", () =>
  sdk.module("BedrockAgentCoreClient", ["InvokeAgentRuntimeCommand", "StopRuntimeSessionCommand"]),
);
vi.mock("@aws-sdk/client-codebuild", () =>
  sdk.module("CodeBuildClient", ["StartBuildCommand", "BatchGetBuildsCommand"]),
);
vi.mock("@aws-sdk/client-ecr", () => sdk.module("ECRClient", ["DescribeImagesCommand"]));
vi.mock("@aws-sdk/client-s3", () =>
  sdk.module("S3Client", ["PutObjectCommand", "DeleteObjectCommand", "HeadBucketCommand"]),
);
vi.mock("@aws-sdk/client-cloudwatch-logs", () =>
  sdk.module("CloudWatchLogsClient", ["DeleteLogGroupCommand"]),
);
vi.mock("@aws-sdk/client-sts", () => sdk.module("STSClient", ["GetCallerIdentityCommand"]));

const SETTINGS = { region: "us-east-1" };
const FAULT_KEY = "$fault";
const METADATA_KEY = "$metadata";
const RESPONSE_KEY = "$response";
const RUNTIME_ARN =
  "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/rayito_demo-abc1234567";
const RAW_AWS_TEXT = "El mensaje crudo de AWS con arn:aws:iam::123456789012:role/secreto";

function awsError(name: string, extra: Record<string, unknown> = {}): Error {
  const error = new Error(RAW_AWS_TEXT);
  error.name = name;
  Object.assign(error, { [FAULT_KEY]: "client", [METADATA_KEY]: { httpStatusCode: 400 } }, extra);
  return error;
}

function respond(command: string, ...responses: unknown[]): void {
  sdk.state.responses.set(command, responses);
}

function sentOf(command: string): Record<string, unknown>[] {
  return sdk.state.sent.filter((entry) => entry.command === command).map((entry) => entry.input);
}

beforeEach(() => {
  sdk.state.sent.length = 0;
  sdk.state.clients.length = 0;
  sdk.state.responses.clear();
});

describe("plano de control", () => {
  const control = new SdkRuntimeControl(SETTINGS);

  it("lista todas las páginas con maxResults 100", async () => {
    respond(
      "ListAgentRuntimesCommand",
      {
        agentRuntimes: [{ agentRuntimeId: "a", agentRuntimeName: "rayito_a", status: "READY" }],
        nextToken: "t1",
      },
      { agentRuntimes: [{ agentRuntimeId: "b", agentRuntimeName: "rayito_b", description: "d" }] },
    );
    const runtimes = await control.listRuntimes();
    expect(runtimes.map((runtime) => runtime.runtimeId)).toEqual(["a", "b"]);
    expect(sentOf("ListAgentRuntimesCommand")).toEqual([
      { maxResults: 100 },
      { maxResults: 100, nextToken: "t1" },
    ]);
  });

  it("crea el runtime sólo con los parámetros documentados", async () => {
    respond("CreateAgentRuntimeCommand", {
      agentRuntimeId: "rayito_demo-abc1234567",
      agentRuntimeArn: RUNTIME_ARN,
      agentRuntimeVersion: "1",
      status: "CREATING",
    });
    const written = await control.createRuntime({
      runtimeName: "rayito_demo",
      containerUri:
        "123456789012.dkr.ecr.us-east-1.amazonaws.com/rayito/agentcore/demo:src-0123456789abcdef",
      roleArn: "arn:aws:iam::123456789012:role/runtime",
      environmentVariables: { AWS_REGION: "us-east-1" },
      idleRuntimeSessionTimeout: 900,
      maxLifetime: 28800,
      description: "Rayito agent demo (rayito.agentcore/1)",
      tags: { "rayito:component": "agentcore-runtime" },
    });
    expect(written).toMatchObject({
      runtimeId: "rayito_demo-abc1234567",
      runtimeVersion: "1",
      status: "CREATING",
    });
    const [input] = sentOf("CreateAgentRuntimeCommand");
    expect(Object.keys(input ?? {}).sort()).toEqual(
      [
        "agentRuntimeArtifact",
        "agentRuntimeName",
        "clientToken",
        "description",
        "environmentVariables",
        "lifecycleConfiguration",
        "networkConfiguration",
        "protocolConfiguration",
        "roleArn",
        "tags",
      ].sort(),
    );
    const { clientToken, ...unsigned } = input ?? {};
    expect(clientToken).toBe(clientTokenFor(unsigned));
    expect(input).toMatchObject({
      agentRuntimeArtifact: {
        containerConfiguration: { containerUri: expect.stringContaining(":src-") },
      },
      networkConfiguration: { networkMode: "PUBLIC" },
      protocolConfiguration: { serverProtocol: "HTTP" },
      lifecycleConfiguration: { idleRuntimeSessionTimeout: 900, maxLifetime: 28800 },
    });
  });

  it("un ConflictException al crear es RuntimeConflictError", async () => {
    respond("CreateAgentRuntimeCommand", awsError("ConflictException"));
    await expect(
      control.createRuntime({
        runtimeName: "rayito_demo",
        containerUri: "x",
        roleArn: "y",
        environmentVariables: {},
        idleRuntimeSessionTimeout: 900,
        maxLifetime: 28800,
        description: "d",
      }),
    ).rejects.toBeInstanceOf(RuntimeConflictError);
  });

  it("actualiza con la configuración completa y requireMMDSV2", async () => {
    respond("UpdateAgentRuntimeCommand", { agentRuntimeVersion: "2", status: "UPDATING" });
    await control.updateRuntime({
      runtimeId: "rayito_demo-abc1234567",
      containerUri: "uri",
      roleArn: "role",
      environmentVariables: {},
      idleRuntimeSessionTimeout: 900,
      maxLifetime: 28800,
      description: "d",
      requireMMDSV2: true,
      baseVersion: "2",
    });
    const [input] = sentOf("UpdateAgentRuntimeCommand");
    expect(input).toMatchObject({
      agentRuntimeId: "rayito_demo-abc1234567",
      metadataConfiguration: { requireMMDSV2: true },
      networkConfiguration: { networkMode: "PUBLIC" },
    });
    expect(input).not.toHaveProperty("platformVersion");
    const { clientToken, ...unsigned } = input ?? {};
    expect(clientToken).toBe(clientTokenFor(unsigned, "2"));
  });

  it("lee los campos que se comparan y no failureReason", async () => {
    respond("GetAgentRuntimeCommand", {
      agentRuntimeId: "rayito_demo-abc1234567",
      agentRuntimeArn: RUNTIME_ARN,
      agentRuntimeName: "rayito_demo",
      agentRuntimeVersion: "3",
      status: "READY",
      description: "d",
      roleArn: "role",
      networkConfiguration: { networkMode: "PUBLIC" },
      protocolConfiguration: { serverProtocol: "HTTP" },
      lifecycleConfiguration: { idleRuntimeSessionTimeout: 600, maxLifetime: 7200 },
      environmentVariables: { A: "1" },
      agentRuntimeArtifact: { containerConfiguration: { containerUri: "uri" } },
      metadataConfiguration: { requireMMDSV2: true },
      failureReason: RAW_AWS_TEXT,
    });
    const details = await control.getRuntime("rayito_demo-abc1234567");
    expect(details).toMatchObject({
      runtimeVersion: "3",
      idleRuntimeSessionTimeout: 600,
      maxLifetime: 7200,
      containerUri: "uri",
      requireMMDSV2: true,
    });
    expect(JSON.stringify(details)).not.toContain("El mensaje crudo");
  });

  it("un runtime que no existe es undefined al leer y no es un error al borrar", async () => {
    respond("GetAgentRuntimeCommand", awsError("ResourceNotFoundException"));
    expect(await control.getRuntime("x")).toBeUndefined();
    respond("DeleteAgentRuntimeCommand", awsError("ResourceNotFoundException"));
    await expect(control.deleteRuntime("x")).resolves.toBeUndefined();
  });

  it("AccessDenied es access_denied y cualquier otro error del servicio aws_error, sin el texto de AWS", async () => {
    respond("ListAgentRuntimesCommand", awsError("AccessDeniedException"));
    const denied = await control.listRuntimes().catch((error: unknown) => error);
    expect(denied).toBeInstanceOf(AgentCoreError);
    expect((denied as AgentCoreError).code).toBe("access_denied");
    expect((denied as AgentCoreError).message).toContain("ListAgentRuntimes");
    expect((denied as AgentCoreError).message).not.toContain("El mensaje crudo");
    respond("ListAgentRuntimesCommand", awsError("ThrottlingException"));
    const other = await control.listRuntimes().catch((error: unknown) => error);
    expect((other as AgentCoreError).code).toBe("aws_error");
    expect((other as AgentCoreError).message).toContain("ThrottlingException");
    expect(String((other as AgentCoreError).message)).not.toContain("secreto");
    expect(String((other as AgentCoreError).cause)).not.toContain("secreto");
  });

  it("construirlo no carga ningún cliente", () => {
    new SdkRuntimeControl(SETTINGS);
    expect(sdk.state.clients).toEqual([]);
  });
});

const INVOKE = {
  agentName: "demo",
  runtimeArn: RUNTIME_ARN,
  sessionId: "rayito-0123456789abcdef0123456789abcdef",
  accept: "application/json",
  payload: new TextEncoder().encode('{"prompt":"hola"}'),
  requestTimeoutMs: 900_000,
};

async function textOf(chunks: AsyncIterable<Uint8Array>): Promise<string> {
  let text = "";
  for await (const chunk of chunks) {
    text += new TextDecoder().decode(chunk);
  }
  return text;
}

describe("plano de datos", () => {
  it("InvokeAgentRuntime lleva el ARN, la sesión, los tipos y el payload", async () => {
    respond("InvokeAgentRuntimeCommand", {
      response: (async function* () {
        yield new TextEncoder().encode("hola");
      })(),
    });
    const invoker = new SdkRuntimeInvoker(SETTINGS);
    expect(await textOf(await invoker.invoke(INVOKE))).toBe("hola");
    expect(sentOf("InvokeAgentRuntimeCommand")).toEqual([
      {
        agentRuntimeArn: RUNTIME_ARN,
        runtimeSessionId: INVOKE.sessionId,
        contentType: "application/json",
        accept: "application/json",
        payload: INVOKE.payload,
      },
    ]);
  });

  it("usa un cliente por plazo de lectura (15 y 60 minutos) y los reutiliza", async () => {
    respond("InvokeAgentRuntimeCommand", { response: (async function* () {})() });
    const invoker = new SdkRuntimeInvoker(SETTINGS);
    await invoker.invoke(INVOKE);
    await invoker.invoke({ ...INVOKE, requestTimeoutMs: 3_600_000 });
    await invoker.invoke(INVOKE);
    expect(
      sdk.state.clients.filter((client) => client.name === "BedrockAgentCoreClient"),
    ).toHaveLength(2);
  });

  it.each([
    ["ThrottlingException", RateLimitError],
    ["ServiceQuotaExceededException", QuotaExceededError],
    ["RuntimeClientError", AgentError],
  ])("%s se traduce a su error propio sin el texto de AWS", async (name, type) => {
    respond("InvokeAgentRuntimeCommand", awsError(name));
    const error = await new SdkRuntimeInvoker(SETTINGS)
      .invoke(INVOKE)
      .catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(type);
    expect((error as Error).message).not.toContain("El mensaje crudo");
    expect((error as Error).message).not.toMatch(/arn:/);
    if (error instanceof AgentError) {
      expect(error.reason).toBe("runtime_error");
    }
  });

  it("ResourceNotFoundException nombra el agente y AccessDenied es access_denied", async () => {
    respond("InvokeAgentRuntimeCommand", awsError("ResourceNotFoundException"));
    const missing = await new SdkRuntimeInvoker(SETTINGS)
      .invoke(INVOKE)
      .catch((caught: unknown) => caught);
    expect(missing).toMatchObject({ code: "runtime_not_found" });
    expect((missing as Error).message).toContain("demo");
    respond("InvokeAgentRuntimeCommand", awsError("AccessDeniedException"));
    const denied = await new SdkRuntimeInvoker(SETTINGS)
      .invoke(INVOKE)
      .catch((caught: unknown) => caught);
    expect(denied).toMatchObject({ code: "access_denied" });
  });

  it("StopRuntimeSession lleva el ARN y la sesión; una sesión ausente es false", async () => {
    const invoker = new SdkRuntimeInvoker(SETTINGS);
    expect(await invoker.stopSession(RUNTIME_ARN, "s".repeat(33))).toBe(true);
    expect(sentOf("StopRuntimeSessionCommand")).toEqual([
      { agentRuntimeArn: RUNTIME_ARN, runtimeSessionId: "s".repeat(33) },
    ]);
    respond("StopRuntimeSessionCommand", awsError("ResourceNotFoundException"));
    expect(await invoker.stopSession(RUNTIME_ARN, "s".repeat(33))).toBe(false);
  });
});

describe("build de la imagen", () => {
  it("StartBuild lleva el proyecto, el origen, las dos variables y la etiqueta como token de idempotencia", async () => {
    respond("StartBuildCommand", { build: { id: "rayito-agentcore-demo:1" } });
    const started = await new SdkImageBuilder(SETTINGS).startBuild({
      projectName: "rayito-agentcore-demo",
      sourceLocation: "amzn-s3-demo-bucket/rayito/images/agentcore/demo/src-0123456789abcdef.zip",
      imageTag: "src-0123456789abcdef",
      baseImage: "public.ecr.aws/docker/library/python:3.12-slim-bookworm@sha256:abc",
    });
    expect(started.buildId).toBe("rayito-agentcore-demo:1");
    expect(sentOf("StartBuildCommand")).toEqual([
      {
        projectName: "rayito-agentcore-demo",
        sourceLocationOverride:
          "amzn-s3-demo-bucket/rayito/images/agentcore/demo/src-0123456789abcdef.zip",
        environmentVariablesOverride: [
          { name: "IMAGE_TAG", value: "src-0123456789abcdef", type: "PLAINTEXT" },
          {
            name: "AGENT_BASE_IMAGE",
            value: "public.ecr.aws/docker/library/python:3.12-slim-bookworm@sha256:abc",
            type: "PLAINTEXT",
          },
        ],
        idempotencyToken: "src-0123456789abcdef",
      },
    ]);
  });

  it("AccountLimitExceeded es build_quota y un proyecto ausente MissingResourceError", async () => {
    const builder = new SdkImageBuilder(SETTINGS);
    const request = { projectName: "p", sourceLocation: "b/k", imageTag: "t", baseImage: "i" };
    respond("StartBuildCommand", awsError("AccountLimitExceededException"));
    await expect(builder.startBuild(request)).rejects.toMatchObject({ code: "build_quota" });
    respond("StartBuildCommand", awsError("ResourceNotFoundException"));
    await expect(builder.startBuild(request)).rejects.toBeInstanceOf(MissingResourceError);
  });

  it("BatchGetBuilds lee el estado, la fase y el grupo de logs", async () => {
    respond("BatchGetBuildsCommand", {
      builds: [
        {
          buildStatus: "IN_PROGRESS",
          currentPhase: "BUILD",
          logs: { groupName: "/aws/codebuild/x" },
        },
      ],
    });
    expect(await new SdkImageBuilder(SETTINGS).getBuild("id")).toEqual({
      status: "IN_PROGRESS",
      phase: "BUILD",
      logGroup: "/aws/codebuild/x",
    });
    expect(sentOf("BatchGetBuildsCommand")).toEqual([{ ids: ["id"] }]);
  });

  it("DescribeImages pregunta por la etiqueta y lee el tamaño", async () => {
    respond("DescribeImagesCommand", { imageDetails: [{ imageSizeInBytes: 800_000_000 }] });
    const registry = new SdkImageRegistry(SETTINGS);
    expect(await registry.describeImage("rayito/agentcore/demo", "src-1")).toEqual({
      exists: true,
      sizeBytes: 800_000_000,
    });
    expect(sentOf("DescribeImagesCommand")).toEqual([
      { repositoryName: "rayito/agentcore/demo", imageIds: [{ imageTag: "src-1" }] },
    ]);
    respond("DescribeImagesCommand", awsError("ImageNotFoundException"));
    expect(await registry.describeImage("r", "t")).toEqual({ exists: false, sizeBytes: undefined });
    respond("DescribeImagesCommand", awsError("RepositoryNotFoundException"));
    await expect(registry.describeImage("r", "t")).rejects.toBeInstanceOf(MissingResourceError);
  });
});

describe("zip, logs y cuenta", () => {
  it("PutObject lleva el dueño esperado y el sha256 en base64; DeleteObject el mismo dueño", async () => {
    const store = new SdkSourceStore(SETTINGS);
    const body = new TextEncoder().encode("zip");
    await store.putSource("amzn-s3-demo-bucket", "k.zip", body, "123456789012");
    await store.deleteSource("amzn-s3-demo-bucket", "k.zip", "123456789012");
    expect(sentOf("PutObjectCommand")).toEqual([
      {
        Bucket: "amzn-s3-demo-bucket",
        Key: "k.zip",
        ExpectedBucketOwner: "123456789012",
        Body: body,
        ChecksumSHA256: createHash("sha256").update(body).digest("base64"),
      },
    ]);
    expect(sentOf("DeleteObjectCommand")).toEqual([
      { Bucket: "amzn-s3-demo-bucket", Key: "k.zip", ExpectedBucketOwner: "123456789012" },
    ]);
  });

  it("HeadBucket da la región, la de una redirección 301 y undefined si no existe", async () => {
    const store = new SdkSourceStore(SETTINGS);
    respond("HeadBucketCommand", { BucketRegion: "us-east-1" });
    expect(await store.bucketRegion("b")).toBe("us-east-1");
    respond(
      "HeadBucketCommand",
      awsError("PermanentRedirect", {
        [METADATA_KEY]: { httpStatusCode: 301 },
        [RESPONSE_KEY]: { headers: { "x-amz-bucket-region": "eu-west-1" } },
      }),
    );
    expect(await store.bucketRegion("b")).toBe("eu-west-1");
    respond("HeadBucketCommand", awsError("NotFound"));
    expect(await store.bucketRegion("b")).toBeUndefined();
  });

  it("DeleteLogGroup borra el grupo y uno ausente es false", async () => {
    const logs = new SdkRuntimeLogs(SETTINGS);
    expect(await logs.deleteLogGroup("/aws/bedrock-agentcore/runtimes/x-DEFAULT")).toBe(true);
    expect(sentOf("DeleteLogGroupCommand")).toEqual([
      { logGroupName: "/aws/bedrock-agentcore/runtimes/x-DEFAULT" },
    ]);
    respond("DeleteLogGroupCommand", awsError("ResourceNotFoundException"));
    expect(await logs.deleteLogGroup("y")).toBe(false);
  });

  it("GetCallerIdentity se lee una sola vez", async () => {
    respond("GetCallerIdentityCommand", { Account: "123456789012" });
    const identity = new SdkAccountIdentity(SETTINGS);
    expect(await identity.accountId()).toBe("123456789012");
    expect(await identity.accountId()).toBe("123456789012");
    expect(sentOf("GetCallerIdentityCommand")).toHaveLength(1);
  });
});
