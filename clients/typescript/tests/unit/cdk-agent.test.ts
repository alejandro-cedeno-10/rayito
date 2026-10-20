/**
 * `RayitoAgent` sintetizado sin AWS (ADR-032 D12.1): el runtime y sus
 * propiedades, la IAM de la pila anidada igual a la de
 * `infra/agentcore-runtime.yaml` salvo la lectura del asset, los ARN de
 * Bedrock y de Secrets Manager, las etiquetas en cada recurso etiquetable,
 * el cableado de MMDSv2, `localDocker()` sin build, la cuenta token y los
 * errores antes de añadir nada.
 */

import { cpSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { App, NestedStack, Stack } from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import { DockerImageAsset } from "aws-cdk-lib/aws-ecr-assets";
import { Role, ServicePrincipal } from "aws-cdk-lib/aws-iam";
import { CfnInclude } from "aws-cdk-lib/cloudformation-include";
import { afterEach, describe, expect, it } from "vitest";
import { bedrockResourceArns } from "../../src/agentcore/arns.js";
import { canonicalAgentName } from "../../src/agentcore/names.js";
import { STACK_PARAMETERS } from "../../src/agentcore/stack.js";
import { runtimeTemplatePath } from "../../src/cdk/handler.js";
import {
  AgentImageBuild,
  RayitoAgent,
  type RayitoAgentProps,
  RayitoCdkError,
} from "../../src/cdk/index.js";
import { AgentCoreError } from "../../src/errors.js";
import {
  AGENTCORE_BUILD_LOG_RETENTION_DAYS,
  AGENTCORE_IMAGE_RETENTION_COUNT,
  CDK_MANAGED_BY_TAG_VALUE,
} from "../../src/limits.js";
import {
  COMPONENT_TAG_KEY,
  MANAGED_BY_TAG_KEY,
  SDK_VERSION_TAG_KEY,
} from "../../src/stacks/model.js";
import { VERSION } from "../../src/version.js";
import { loadContract, TESTDATA_ROOT } from "./cdk-contract-harness.js";

const ACCOUNT = "123456789012";
const REGION = "us-east-1";
const STRANDS = join(TESTDATA_ROOT, "agent-samples", "python", "strands");
const FLASK = join(TESTDATA_ROOT, "agent-samples", "python", "flask-custom");
const HAIKU = "us.anthropic.claude-haiku-4-5-20251001-v1:0";
const PROFILE_ARNS = [
  `arn:aws:bedrock:${REGION}:${ACCOUNT}:inference-profile/${HAIKU}`,
  "arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0",
];
const TAGGABLE_TYPES = new Set([
  "AWS::BedrockAgentCore::Runtime",
  "AWS::CloudFormation::Stack",
  "AWS::CodeBuild::Project",
  "AWS::ECR::Repository",
  "AWS::IAM::Role",
  "AWS::Lambda::Function",
  "AWS::Logs::LogGroup",
  "AWS::StepFunctions::StateMachine",
]);
const scratches: string[] = [];

afterEach(() => {
  for (const directory of scratches.splice(0)) {
    rmSync(directory, { recursive: true, force: true });
  }
});

type Resources = Record<string, { Type: string; Properties?: Record<string, unknown> }>;

function newStack(env?: { account?: string; region?: string }): Stack {
  const app = new App({
    context: JSON.parse(loadContract().caseFormat.appContext) as Record<string, unknown>,
  });
  return new Stack(app, "AgentTest", env === undefined ? {} : { env });
}

function synth(props: Partial<RayitoAgentProps> = {}, env = { account: ACCOUNT, region: REGION }) {
  const stack = newStack(env);
  const agent = new RayitoAgent(stack, "Agent", { project: STRANDS, ...props });
  const nested = stack.node.findAll().find((child) => child instanceof NestedStack) as NestedStack;
  return {
    stack,
    agent,
    root: Template.fromStack(stack),
    nested: Template.fromStack(nested),
  };
}

function resourcesOf(template: Template): Resources {
  return template.toJSON().Resources as Resources;
}

function only<T>(values: Record<string, T>): T {
  const all = Object.values(values);
  expect(all).toHaveLength(1);
  return all[0] as T;
}

function policyStatements(role: { Properties?: Record<string, unknown> }, index = 0): unknown[] {
  const policies = role.Properties?.Policies as Array<{
    PolicyDocument: { Statement: unknown[] };
  }>;
  return policies[index]?.PolicyDocument.Statement ?? [];
}

function documentStatements(
  resource: { Properties?: Record<string, unknown> } | undefined,
): Array<Record<string, unknown>> {
  const document = (resource?.Properties?.PolicyDocument ?? { Statement: [] }) as {
    Statement: Array<Record<string, unknown>>;
  };
  return document.Statement;
}

/** El `Resource` de la sentencia condicional `condition` del rol (`Fn::If`). */
function conditionalResource(
  role: { Properties?: Record<string, unknown> } | undefined,
  condition: string,
): unknown {
  const branch = policyStatements(role ?? {})
    .map((statement) => (statement as Record<string, unknown>)["Fn::If"] as unknown[] | undefined)
    .find((value) => value?.[0] === condition);
  return (branch?.[1] as { Resource: unknown } | undefined)?.Resource;
}

function referenceNested(parameters: Record<string, unknown>): Resources {
  const stack = newStack({ account: ACCOUNT, region: REGION });
  const nested = new NestedStack(stack, "Reference");
  new CfnInclude(nested, "Template", { templateFile: runtimeTemplatePath(), parameters });
  return resourcesOf(Template.fromStack(nested));
}

describe("RayitoAgent: el runtime", () => {
  it("sintetiza un AWS::BedrockAgentCore::Runtime con el plan y la marca +cdk", () => {
    const { root } = synth({ bedrockModels: [HAIKU] });
    const runtime = only(root.findResources("AWS::BedrockAgentCore::Runtime"));
    expect(runtime.Properties).toMatchObject({
      AgentRuntimeName: "rayito_strands",
      Description: "Rayito agent strands (rayito.agentcore/1+cdk)",
      NetworkConfiguration: { NetworkMode: "PUBLIC" },
      ProtocolConfiguration: "HTTP",
      LifecycleConfiguration: { IdleRuntimeSessionTimeout: 900, MaxLifetime: 28800 },
      EnvironmentVariables: { AWS_DEFAULT_REGION: REGION, AWS_REGION: REGION },
    });
    expect(runtime.DependsOn).toEqual([expect.stringMatching(/ImageBuild/)]);
  });

  it("el nombre y los plazos de las props llegan al runtime", () => {
    const { root, agent } = synth({
      name: "web-agent",
      idleSessionTimeoutSeconds: 600,
      maxSessionLifetimeSeconds: 3600,
    });
    root.hasResourceProperties("AWS::BedrockAgentCore::Runtime", {
      AgentRuntimeName: "rayito_web_agent",
      Description: "Rayito agent web-agent (rayito.agentcore/1+cdk)",
      LifecycleConfiguration: { IdleRuntimeSessionTimeout: 600, MaxLifetime: 3600 },
    });
    expect(agent.agentName).toBe("web-agent");
    expect(agent.runtimeName).toBe("rayito_web_agent");
  });

  it("exportOutputs añade sólo las dos salidas", () => {
    expect(Object.keys(synth().root.toJSON().Outputs ?? {})).toEqual([]);
    const outputs = Object.keys(synth({ exportOutputs: true }).root.toJSON().Outputs ?? {});
    expect(outputs).toHaveLength(2);
    expect(outputs.join(" ")).toMatch(/RuntimeArn.*RuntimeId/);
  });

  it("runtime es un IBedrockAgentRuntime y grantInvoke da InvokeAgentRuntime", () => {
    const stack = newStack({ account: ACCOUNT, region: REGION });
    const agent = new RayitoAgent(stack, "Agent", { project: STRANDS });
    expect(agent.runtime.agentRuntimeArn).toBe(agent.runtimeArn);
    const role = new Role(stack, "Caller", {
      assumedBy: new ServicePrincipal("lambda.amazonaws.com"),
    });
    agent.grantInvoke(role);
    const policy = Template.fromStack(stack).findResources("AWS::IAM::Policy", {
      Properties: { Roles: [{ Ref: stack.getLogicalId(role.node.defaultChild as never) }] },
    });
    const statement = documentStatements(only(policy))[0] as {
      Action: string;
      Resource: unknown[];
    };
    expect(statement.Action).toBe("bedrock-agentcore:InvokeAgentRuntime");
    expect(statement.Resource).toHaveLength(2);
  });
});

describe("RayitoAgent: IAM", () => {
  it("la pila anidada tiene la IAM de la plantilla de la CLI más la lectura del asset", () => {
    const { nested, agent } = synth({ bedrockModels: [HAIKU] });
    const resources = resourcesOf(nested);
    const reference = referenceNested({
      [STACK_PARAMETERS.agentName]: canonicalAgentName(agent.agentName),
      [STACK_PARAMETERS.agentDisplayName]: agent.agentName,
      [STACK_PARAMETERS.runtimeName]: agent.runtimeName,
      [STACK_PARAMETERS.artifactBucket]: `cdk-hnb659fds-assets-${ACCOUNT}-${REGION}`,
      [STACK_PARAMETERS.bedrockModelArns]: conditionalResource(
        resources.RuntimeRole,
        "HasBedrockModels",
      ),
      [STACK_PARAMETERS.readableSecretArns]: [],
      [STACK_PARAMETERS.imageRetentionCount]: String(AGENTCORE_IMAGE_RETENTION_COUNT),
      [STACK_PARAMETERS.buildLogRetentionDays]: String(AGENTCORE_BUILD_LOG_RETENTION_DAYS),
      [STACK_PARAMETERS.buildTimeoutMinutes]: "30",
    });
    const strip = (role: { Properties?: Record<string, unknown> } | undefined) => {
      const { Tags: _tags, ...rest } = role?.Properties ?? {};
      return rest;
    };
    expect(strip(resources.RuntimeRole)).toEqual(strip(reference.RuntimeRole));
    const build = strip(resources.BuildRole) as { Policies: unknown[] };
    const referenceBuild = strip(reference.BuildRole) as { Policies: unknown[] };
    expect(build.Policies[0]).toEqual(referenceBuild.Policies[0]);
    expect(build.Policies).toHaveLength(2);
    expect(build.Policies[1]).toMatchObject({
      PolicyName: "source-asset",
      PolicyDocument: {
        Statement: [
          {
            Sid: "ReadSourceAsset",
            Action: ["s3:GetObject", "s3:GetObjectVersion"],
            Effect: "Allow",
          },
        ],
      },
    });
    expect({ ...build, Policies: [] }).toEqual({ ...referenceBuild, Policies: [] });
  });

  it("los ARN de Bedrock son los resueltos y no hay sentencia de secretos sin secretos", () => {
    const { nested } = synth({ bedrockModels: [HAIKU] });
    const role = resourcesOf(nested).RuntimeRole;
    const resolved = conditionalResource(role, "HasBedrockModels") as string[];
    expect(resolved.slice(-PROFILE_ARNS.length)).toEqual(PROFILE_ARNS);
    expect(resolved).toEqual([...new Set(resolved)]);
    expect(conditionalResource(role, "HasReadableSecrets")).toEqual([]);
    expect(nested.toJSON().Conditions.HasReadableSecrets).toEqual({
      "Fn::Not": [{ "Fn::Equals": [{ "Fn::Join": ["", []] }, ""] }],
    });
  });

  it("los secretos legibles se resuelven a sus ARN con el sufijo comodín", () => {
    const { nested } = synth({ readableSecrets: ["rayito/demo-key"] });
    expect(conditionalResource(resourcesOf(nested).RuntimeRole, "HasReadableSecrets")).toEqual([
      `arn:aws:secretsmanager:${REGION}:${ACCOUNT}:secret:rayito/demo-key-??????`,
    ]);
  });

  it("el manejador sólo puede usar su proyecto, su repositorio y su runtime", () => {
    const { root } = synth();
    const policy = only(
      root.findResources("AWS::IAM::Policy", {
        Properties: { PolicyName: Match.stringLikeRegexp("AgentHandlerRole") },
      }),
    );
    const statements = documentStatements(policy);
    expect(statements.map((statement) => statement.Action)).toEqual([
      ["logs:CreateLogStream", "logs:PutLogEvents"],
      ["logs:CreateLogStream", "logs:PutLogEvents"],
      ["codebuild:StartBuild", "codebuild:BatchGetBuilds"],
      "ecr:DescribeImages",
      ["bedrock-agentcore:GetAgentRuntime", "bedrock-agentcore:UpdateAgentRuntime"],
      "iam:PassRole",
    ]);
    expect(JSON.stringify(statements)).not.toContain('"*"');
    expect(JSON.stringify(statements[4]?.Resource)).toContain(
      `:bedrock-agentcore:${REGION}:${ACCOUNT}:runtime/rayito_strands-*`,
    );
  });
});

describe("RayitoAgent: build y MMDSv2", () => {
  it("codeBuild: Custom::RayitoImageBuild con las propiedades del contrato", () => {
    const { root } = synth({ build: AgentImageBuild.codeBuild({ timeoutMinutes: 20 }) });
    const build = only(root.findResources("Custom::RayitoImageBuild"));
    const properties = loadContract().constants.customResourceProperties as Record<
      string,
      string[]
    >;
    expect(
      Object.keys(build.Properties ?? {})
        .filter((key) => key !== "ServiceToken")
        .sort(),
    ).toEqual([...(properties["Custom::RayitoImageBuild"] ?? [])].sort());
    expect(build.Properties?.ImageTag).toMatch(/^src-[0-9a-f]+$/);
    expect(build.Properties?.MaxImageBytes).toBe(2_000_000_000);
  });

  it("Custom::RayitoRuntimeMmds lleva el id y la versión del runtime", () => {
    const { root } = synth();
    const runtimeId = Object.keys(root.findResources("AWS::BedrockAgentCore::Runtime"))[0];
    root.hasResourceProperties("Custom::RayitoRuntimeMmds", {
      AgentRuntimeId: { "Fn::GetAtt": [runtimeId, "AgentRuntimeId"] },
      AgentRuntimeVersion: { "Fn::GetAtt": [runtimeId, "AgentRuntimeVersion"] },
    });
  });

  it("las funciones del manejador usan python3.13 e index.on_event / index.is_complete", () => {
    const { root } = synth();
    const handlers = Object.values(
      root.findResources("AWS::Lambda::Function", { Properties: { Runtime: "python3.13" } }),
    ).map((fn) => fn.Properties?.Handler);
    expect(handlers.sort()).toEqual(["index.is_complete", "index.on_event"]);
  });

  it("localDocker quita el build de la pila anidada y usa una imagen arm64", () => {
    const stack = newStack({ account: ACCOUNT, region: REGION });
    new RayitoAgent(stack, "Agent", { project: FLASK, build: AgentImageBuild.localDocker() });
    const nested = stack.node
      .findAll()
      .find((child) => child instanceof NestedStack) as NestedStack;
    const root = Template.fromStack(stack);
    const inner = Template.fromStack(nested);
    expect(Object.keys(root.findResources("Custom::RayitoImageBuild"))).toEqual([]);
    expect(Object.values(resourcesOf(inner)).map((resource) => resource.Type)).toEqual([
      "AWS::IAM::Role",
    ]);
    expect(Object.keys(inner.toJSON().Outputs ?? {})).not.toContain("RepositoryUri");
    const artifact = stack.node.findAll().find((child) => child.node.id === "Image");
    expect(artifact).toBeInstanceOf(DockerImageAsset);
    const assembly = App.of(stack)?.synth();
    const manifest = readdirSync(assembly?.directory ?? "").find((name) =>
      name.startsWith(`${stack.stackName}.assets`),
    );
    const assets = readFileSync(join(assembly?.directory ?? "", manifest ?? ""), "utf8");
    expect(assets).toContain("linux/arm64");
  });
});

describe("RayitoAgent: etiquetas", () => {
  it("cada recurso etiquetable lleva las tres etiquetas fijas y las del usuario", () => {
    const { root, nested } = synth({ tags: { team: "demo" } });
    const expected = {
      [COMPONENT_TAG_KEY]: "agentcore-runtime",
      [MANAGED_BY_TAG_KEY]: CDK_MANAGED_BY_TAG_VALUE,
      [SDK_VERSION_TAG_KEY]: VERSION,
      team: "demo",
    };
    let checked = 0;
    for (const template of [root, nested]) {
      for (const [logicalId, resource] of Object.entries(resourcesOf(template))) {
        if (!TAGGABLE_TYPES.has(resource.Type)) {
          continue;
        }
        const raw = resource.Properties?.Tags;
        const tags = Array.isArray(raw)
          ? Object.fromEntries(
              (raw as Array<{ Key: string; Value: string }>).map((tag) => [tag.Key, tag.Value]),
            )
          : (raw as Record<string, string> | undefined);
        expect(tags, logicalId).toEqual(expected);
        checked += 1;
      }
    }
    expect(checked).toBeGreaterThan(10);
  });

  it("las etiquetas fijas ganan a las del usuario", () => {
    const { root } = synth({ tags: { [MANAGED_BY_TAG_KEY]: "otro" } });
    root.hasResourceProperties("AWS::BedrockAgentCore::Runtime", {
      Tags: Match.objectLike({ [MANAGED_BY_TAG_KEY]: CDK_MANAGED_BY_TAG_VALUE }),
    });
  });
});

describe("RayitoAgent: entorno y errores", () => {
  it("un stack sin región es region_unresolved y no añade nada", () => {
    const stack = newStack();
    expect(() => new RayitoAgent(stack, "Agent", { project: STRANDS })).toThrow(
      expect.objectContaining({ code: "region_unresolved", fields: { construct: "RayitoAgent" } }),
    );
    expect(stack.node.children).toHaveLength(0);
  });

  it("una cuenta token se acepta y los ARN usan AWS::AccountId", () => {
    const { nested } = synth({ bedrockModels: [HAIKU] }, { region: REGION } as never);
    expect(JSON.stringify(nested.toJSON())).toContain('"Ref":"AWS::AccountId"');
  });

  it("una cuenta que no son 12 dígitos es account_invalid", () => {
    const stack = newStack({ account: "12345", region: REGION });
    expect(() => new RayitoAgent(stack, "Agent", { project: STRANDS })).toThrow(RayitoCdkError);
  });

  it("los errores del plan salen como AgentCoreError", () => {
    const stack = newStack({ account: ACCOUNT, region: REGION });
    expect(() => new RayitoAgent(stack, "Agent", { project: STRANDS, name: "Bad Name" })).toThrow(
      expect.objectContaining({ code: "name_invalid" }),
    );
    expect(
      () => new RayitoAgent(stack, "Agent", { project: STRANDS, idleSessionTimeoutSeconds: 10 }),
    ).toThrow(AgentCoreError);
    expect(stack.node.children).toHaveLength(0);
  });

  it("un valor de entorno de más de 2048 caracteres es env_invalid", () => {
    const project = mkdtempSync(join(tmpdir(), "rayito-cdk-agent-"));
    scratches.push(project);
    cpSync(STRANDS, project, { recursive: true });
    writeFileSync(
      join(project, "rayito-agent.json"),
      JSON.stringify({ name: "long-env", env: { BIG: "x".repeat(2049) } }),
    );
    const stack = newStack({ account: ACCOUNT, region: REGION });
    expect(() => new RayitoAgent(stack, "Agent", { project })).toThrow(
      expect.objectContaining({
        code: "env_invalid",
        fields: expect.objectContaining({ maxChars: "2048" }),
      }),
    );
  });

  it("timeoutMinutes fuera de 5-110 es option_invalid", () => {
    for (const timeoutMinutes of [4, 111, 1.5]) {
      expect(() => AgentImageBuild.codeBuild({ timeoutMinutes })).toThrow(
        expect.objectContaining({ code: "option_invalid" }),
      );
    }
    expect(AgentImageBuild.codeBuild().timeoutMinutes).toBe(30);
    expect(AgentImageBuild.localDocker().kind).toBe("localDocker");
  });

  it("los ARN del rol coinciden con bedrockResourceArns", () => {
    expect(bedrockResourceArns([HAIKU], { modelRegion: REGION, account: ACCOUNT })).toEqual(
      PROFILE_ARNS,
    );
  });
});
