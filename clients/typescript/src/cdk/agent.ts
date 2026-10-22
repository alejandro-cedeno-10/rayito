/**
 * `RayitoAgent` y `AgentImageBuild` (agentcore-cdk, ADR-032 D3-D6): el mismo
 * agente que `rayito agent deploy --target agentcore`, desplegado con
 * `cdk deploy`. Es un adaptador sobre el plan puro del SDK
 * (`planAgentCoreSync`): el proyecto se detecta y valida igual, la pila
 * anidada es `infra/agentcore-runtime.yaml` con los parámetros de la CLI
 * (IAM idéntico por construcción) y el runtime lleva la marca `+cdk`, que la
 * CLI lee pero no despliega ni borra.
 */

import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { Aws, CfnOutput, CustomResource, NestedStack, Stack } from "aws-cdk-lib";
import {
  Runtime as AgentRuntime,
  CfnRuntime,
  type IBedrockAgentRuntime,
} from "aws-cdk-lib/aws-bedrockagentcore";
import type { CfnProject } from "aws-cdk-lib/aws-codebuild";
import type { CfnRepository } from "aws-cdk-lib/aws-ecr";
import { DockerImageAsset, Platform } from "aws-cdk-lib/aws-ecr-assets";
import { type CfnRole, Grant, type IGrantable, PolicyStatement } from "aws-cdk-lib/aws-iam";
import { Asset } from "aws-cdk-lib/aws-s3-assets";
import { CfnInclude } from "aws-cdk-lib/cloudformation-include";
import { Construct } from "constructs";
import { AGENT_BASE_IMAGE_ARG } from "../agent/export.js";
import { copyProject } from "../agent/project-copy.js";
import { detectProjectSync } from "../agent/project-file.js";
import { bedrockResourceArns, secretResourceArns } from "../agentcore/arns.js";
import { type PlannedAgentCore, planAgentCoreSync } from "../agentcore/facade.js";
import { baseImageReference } from "../agentcore/identifiers.js";
import type { AgentCoreDeployPlan } from "../agentcore/models.js";
import { canonicalAgentName, runtimeDescriptionFor } from "../agentcore/names.js";
import { sourceZip } from "../agentcore/source-zip.js";
import { STACK_PARAMETERS } from "../agentcore/stack.js";
import { AgentCoreError } from "../errors.js";
import {
  AGENTCORE_BUILD_LOG_RETENTION_DAYS,
  AGENTCORE_BUILD_TIMEOUT_MINUTES,
  AGENTCORE_IMAGE_RETENTION_COUNT,
  AGENTCORE_MAX_ENVIRONMENT_VARIABLES,
  AGENTCORE_MAX_IMAGE_BYTES,
  AGENTCORE_NETWORK_MODE,
  AGENTCORE_SERVER_PROTOCOL,
  CDK_BUILD_TOTAL_TIMEOUT_MARGIN_MINUTES,
  CDK_RUNTIME_ENVIRONMENT_VALUE_MAX_CHARS,
} from "../limits.js";
import { MS_PER_SECOND } from "../units.js";
import { applyTags, type StackEnvironment, stackEnvironment } from "./environment.js";
import { RayitoCdkError } from "./errors.js";
import { AgentCoreHandler, buildProvider, runtimeTemplatePath, stagingPath } from "./handler.js";

/** `Custom::RayitoImageBuild` y `Custom::RayitoRuntimeMmds` (`contract.constants.customResourceTypes`). */
export const IMAGE_BUILD_RESOURCE_TYPE = "Custom::RayitoImageBuild";
export const RUNTIME_MMDS_RESOURCE_TYPE = "Custom::RayitoRuntimeMmds";
/** Atributo de `Mmds`: la versión viva del runtime que ya exige MMDSv2. */
const RUNTIME_VERSION_ATTRIBUTE = "AgentRuntimeVersion";
/**
 * Plazo de un build: `MinValue` de `BuildTimeoutMinutes` en
 * `infra/agentcore-runtime.yaml` y, con el margen, el `totalTimeout` máximo
 * del `Provider` de `custom_resources` (2 horas).
 */
export const BUILD_TIMEOUT_MIN_MINUTES = 5;
export const PROVIDER_MAX_TOTAL_TIMEOUT_MINUTES = 120;
export const BUILD_TIMEOUT_MAX_MINUTES =
  PROVIDER_MAX_TOTAL_TIMEOUT_MINUTES - CDK_BUILD_TOTAL_TIMEOUT_MARGIN_MINUTES;
/** Lo que `localDocker()` quita de la plantilla incluida: la imagen va al repositorio del bootstrap de CDK. */
export const LOCAL_DOCKER_REMOVED_RESOURCES: readonly string[] = [
  "Repository",
  "BuildLogGroup",
  "BuildRole",
  "BuildProject",
];
export const LOCAL_DOCKER_REMOVED_OUTPUTS: readonly string[] = [
  "RepositoryName",
  "RepositoryUri",
  "RepositoryArn",
  "BuildProjectName",
  "BuildLogGroupName",
];
const OUTPUTS_CHILD = "$Outputs";
/** `PullImage` es la primera sentencia de la política del rol del runtime. */
const PULL_IMAGE_RESOURCE_OVERRIDE = "Policies.0.PolicyDocument.Statement.0.Resource";
/** La política extra del rol de build: leer sólo el objeto del asset. */
const SOURCE_POLICY_OVERRIDE = "Policies.1";
const SOURCE_POLICY_NAME = "source-asset";
const SOURCE_STATEMENT_SID = "ReadSourceAsset";
/**
 * Las mismas acciones que `ReadSource` de `infra/agentcore-runtime.yaml`: el
 * bucket de assets del bootstrap tiene versionado y CodeBuild puede leer la
 * fuente por versión.
 */
const SOURCE_ACTIONS = ["s3:GetObject", "s3:GetObjectVersion"];
const POLICY_VERSION = "2012-10-17";
/** Con `localDocker()` no hay build, así que `ArtifactBucket` no se usa. */
const UNUSED_BUCKET = "";
const INVOKE_ACTIONS = ["bedrock-agentcore:InvokeAgentRuntime"];
const MMDS_ACTIONS = ["bedrock-agentcore:GetAgentRuntime", "bedrock-agentcore:UpdateAgentRuntime"];
const BUILD_ACTIONS = ["codebuild:StartBuild", "codebuild:BatchGetBuilds"];
const DESCRIBE_IMAGES_ACTIONS = ["ecr:DescribeImages"];
const PASS_ROLE_ACTIONS = ["iam:PassRole"];
const ENDPOINTS_SUFFIX = "/*";
const AGENTCORE_SERVICE = "bedrock-agentcore";
const RUNTIME_RESOURCE = "runtime";
const ECR_SERVICE = "ecr";
const REPOSITORY_RESOURCE = "repository";

/** Las opciones de `AgentImageBuild.codeBuild()`. */
export interface CodeBuildOptions {
  /** Minutos tras los que CodeBuild para el build (5-110; 30 por defecto); el recurso se rinde 10 minutos después. */
  readonly timeoutMinutes?: number | undefined;
}

/**
 * Cómo se construye la imagen del agente.
 *
 * Coste y activación
 * -------------------
 * Activa: pasarlo como `build` a `new RayitoAgent(...)` y `cdk deploy`.
 * Recursos y llamadas AWS: `codeBuild()` (por defecto) construye en el
 *     proyecto de CodeBuild de la pila del agente (`StartBuild`,
 *     `BatchGetBuilds`); `localDocker()` construye con Docker o Finch en tu
 *     máquina (arm64) y sube la imagen al repositorio del bootstrap de CDK.
 * Coste aproximado: `codeBuild()`, $0,0034 por minuto de build arm1.small
 *     (consultado 2026-10-19 en https://aws.amazon.com/codebuild/pricing/);
 *     `localDocker()`, nada en CodeBuild.
 * IAM: el del despliegue de CDK.
 * Cómo apagarla: no hay nada encendido fuera de un `cdk deploy`.
 * Ejemplo:
 *   new RayitoAgent(stack, "Agent", { project: "./agent", build: AgentImageBuild.localDocker() });
 */
export class AgentImageBuild {
  /** `codeBuild` o `localDocker`. */
  readonly kind: "codeBuild" | "localDocker";
  /** Minutos de build de CodeBuild (sólo `codeBuild`). */
  readonly timeoutMinutes: number;

  private constructor(kind: "codeBuild" | "localDocker", timeoutMinutes: number) {
    this.kind = kind;
    this.timeoutMinutes = timeoutMinutes;
  }

  /** La imagen se construye en AWS con CodeBuild, como la CLI (por defecto; sin Docker local). */
  static codeBuild(options: CodeBuildOptions = {}): AgentImageBuild {
    const minutes = options.timeoutMinutes ?? AGENTCORE_BUILD_TIMEOUT_MINUTES;
    if (
      !Number.isInteger(minutes) ||
      minutes < BUILD_TIMEOUT_MIN_MINUTES ||
      minutes > BUILD_TIMEOUT_MAX_MINUTES
    ) {
      throw new RayitoCdkError("option_invalid", {
        option: "timeoutMinutes",
        reason: `un número entero de minutos entre ${BUILD_TIMEOUT_MIN_MINUTES} y ${BUILD_TIMEOUT_MAX_MINUTES}`,
      });
    }
    return new AgentImageBuild("codeBuild", minutes);
  }

  /** La imagen se construye con Docker local (arm64) al hacer `cdk deploy`; sin CodeBuild. */
  static localDocker(): AgentImageBuild {
    return new AgentImageBuild("localDocker", AGENTCORE_BUILD_TIMEOUT_MINUTES);
  }
}

/** Las props de `RayitoAgent` (en Python, los mismos nombres en snake_case). */
export interface RayitoAgentProps {
  /** El directorio del agente o su fichero de proyecto (`rayito-agent.toml` / `.json`). */
  readonly project: string;
  /** El nombre del agente; por defecto el del proyecto. */
  readonly name?: string | undefined;
  /** Modelos o perfiles de inferencia de Bedrock que el agente puede invocar, además del del proyecto. */
  readonly bedrockModels?: readonly string[] | undefined;
  /** Secretos de Secrets Manager que el rol puede leer (nunca van al entorno). */
  readonly readableSecrets?: readonly string[] | undefined;
  /** Segundos sin actividad tras los que AgentCore cierra la sesión (900 por defecto). */
  readonly idleSessionTimeoutSeconds?: number | undefined;
  /** Vida máxima de una sesión en segundos (28800 por defecto). */
  readonly maxSessionLifetimeSeconds?: number | undefined;
  /** Etiquetas del usuario; las tres `rayito:*` las pone el constructo. */
  readonly tags?: Readonly<Record<string, string>> | undefined;
  /** Cómo se construye la imagen; por defecto `AgentImageBuild.codeBuild()`. */
  readonly build?: AgentImageBuild | undefined;
  /** Añade `CfnOutput` con el ARN y el id del runtime (apagado por defecto). */
  readonly exportOutputs?: boolean | undefined;
  /** Admite un modelo fuera de Bedrock sin dar al rol permisos de Bedrock que
   * no pidas; la clave la obtiene tu código (apagado por defecto). */
  readonly externalModel?: boolean | undefined;
}

function secondsToMs(seconds: number | undefined): number | undefined {
  return seconds === undefined ? undefined : seconds * MS_PER_SECOND;
}

function planAgent(props: RayitoAgentProps, region: string): PlannedAgentCore {
  const [detected] = detectProjectSync(props.project, undefined, { region });
  const project =
    props.name === undefined ? detected : copyProject(detected, { template: props.name });
  return planAgentCoreSync(
    props.project,
    {
      target: "agentcore",
      project,
      region,
      agentcore: {
        bedrockModels: props.bedrockModels,
        readableSecrets: props.readableSecrets,
        idleSessionTimeoutMs: secondsToMs(props.idleSessionTimeoutSeconds),
        maxSessionLifetimeMs: secondsToMs(props.maxSessionLifetimeSeconds),
        tags: props.tags,
        externalModel: props.externalModel,
      },
    },
    { environ: {} },
  );
}

/**
 * El esquema de `AWS::BedrockAgentCore::Runtime` admite valores de hasta
 * 2048 caracteres (la API, 5000; Q181): uno más largo es el `env_invalid`
 * del plan, en la síntesis.
 */
export function checkRuntimeEnvironment(environment: Readonly<Record<string, string>>): void {
  if (
    Object.values(environment).some(
      (value) => value.length > CDK_RUNTIME_ENVIRONMENT_VALUE_MAX_CHARS,
    )
  ) {
    throw new AgentCoreError("env_invalid", {
      maxVariables: AGENTCORE_MAX_ENVIRONMENT_VARIABLES,
      maxChars: CDK_RUNTIME_ENVIRONMENT_VALUE_MAX_CHARS,
    });
  }
}

interface ArnLists {
  readonly models: string[];
  readonly secrets: string[];
}

/** `BedrockModelArns` y `ReadableSecretArns` como en la CLI; con una cuenta token, con el token. */
function arnLists(planned: PlannedAgentCore, environment: StackEnvironment): ArnLists {
  const { plan, project } = planned;
  const context = {
    account: environment.account,
    partition: environment.partition,
    accountUnresolved: environment.accountUnresolved,
  };
  return {
    models: bedrockResourceArns(plan.bedrockModels, {
      ...context,
      modelRegion: project.spec.model.region ?? plan.region,
    }),
    secrets: secretResourceArns(plan.readableSecrets, { ...context, region: plan.region }),
  };
}

function writeTree(directory: string, tree: ReadonlyMap<string, string | Uint8Array>): string {
  for (const [name, content] of tree) {
    const file = join(directory, ...name.split("/"));
    mkdirSync(dirname(file), { recursive: true });
    writeFileSync(file, content);
  }
  return directory;
}

interface BuiltImage {
  readonly repositoryUri: string;
  readonly imageUri: string;
}

/**
 * Un agente de Rayito en Amazon Bedrock AgentCore Runtime, gestionado por
 * CloudFormation: el mismo proyecto que `rayito agent deploy --target
 * agentcore` da el mismo agente. Sintetizar no llama a AWS; la región del
 * stack debe ser concreta. `rayito agent list/status/invoke` y
 * `AgentCoreFleet.connect` lo ven; `rayito agent deploy/delete` lo rechazan
 * (`name_conflict`): se cambia con `cdk deploy` y se borra con `cdk destroy`.
 *
 * Coste y activación
 * -------------------
 * Activa: `cdk deploy` de un stack que lo contenga; sintetizar es gratis.
 * Recursos y llamadas AWS: una pila anidada con el repositorio ECR (guarda 5
 *     imágenes), el proyecto de CodeBuild, su grupo de logs y los roles de
 *     build y del runtime; el asset del código en el bucket del bootstrap;
 *     dos funciones Lambda y una máquina de estados del `Provider` de CDK
 *     (recursos personalizados del build y de MMDSv2); el
 *     `AWS::BedrockAgentCore::Runtime`. Sin trazas, VPC ni entrega de logs.
 * Coste aproximado: minutos de CodeBuild ($0,0034 por minuto arm1.small),
 *     ECR ($0,10 por GB-mes), logs (14 días), céntimos de Lambda y Step
 *     Functions por despliegue, y AgentCore por uso real ($0,0895 por
 *     vCPU-hora y $0,00945 por GB-hora; consultado 2026-10-19 en
 *     https://aws.amazon.com/bedrock/agentcore/pricing/); el modelo lo cobra
 *     su proveedor. El asset queda en el bootstrap hasta `cdk gc`.
 * IAM: el del despliegue de CDK. El manejador sólo puede `StartBuild` y
 *     `BatchGetBuilds` en su proyecto, `DescribeImages` en su repositorio,
 *     `GetAgentRuntime`/`UpdateAgentRuntime` en su runtime e `iam:PassRole`
 *     sobre el rol del runtime.
 * Cómo apagarla: `cdk destroy` (o quitar el constructo y `cdk deploy`); sin
 *     sesiones no hay cómputo.
 * Ejemplo:
 *   const agent = new RayitoAgent(stack, "Agent", {
 *     project: "./support-bot",
 *     bedrockModels: ["us.anthropic.claude-haiku-4-5-20251001-v1:0"],
 *   });
 *   agent.grantInvoke(myFunction);
 */
export class RayitoAgent extends Construct {
  /** El nombre del agente. */
  readonly agentName: string;
  /** `rayito_<nombre>`. */
  readonly runtimeName: string;
  readonly runtimeArn: string;
  readonly runtimeId: string;
  /**
   * La versión viva del runtime, la que se puede fijar en un endpoint.
   * Normalmente la de CloudFormation (AgentCore ya la crea con MMDSv2); el
   * recurso `Mmds` sólo publica otra si una llegara sin MMDSv2.
   */
  readonly runtimeVersion: string;
  /** El rol que asume AgentCore (el `RuntimeRole` de la pila anidada). */
  readonly roleArn: string;
  /** El repositorio de la imagen (el del bootstrap de CDK con `localDocker()`). */
  readonly repositoryUri: string;
  /** `<repositorio>:<etiqueta>`. */
  readonly imageUri: string;
  /** El runtime como `IBedrockAgentRuntime` de la L2 de AgentCore, para usarlo con ella. */
  readonly runtime: IBedrockAgentRuntime;
  readonly #plan: AgentCoreDeployPlan;

  constructor(scope: Construct, id: string, props: RayitoAgentProps) {
    const environment = stackEnvironment(scope, "RayitoAgent");
    const build = props.build ?? AgentImageBuild.codeBuild();
    const planned = planAgent(props, environment.region);
    const { plan } = planned;
    checkRuntimeEnvironment(plan.environment);
    const arns = arnLists(planned, environment);
    super(scope, id);
    this.#plan = plan;
    const nested = new NestedStack(this, "Build");
    const source =
      build.kind === "codeBuild"
        ? new Asset(this, "Source", {
            path: stagingPath(`${plan.imageTag}.zip`, sourceZip(planned.tree)),
          })
        : undefined;
    const template = this.#include(nested, source?.s3BucketName ?? UNUSED_BUCKET, arns, build);
    const runtimeRole = template.getResource("RuntimeRole") as CfnRole;
    const handler = new AgentCoreHandler(this, "Handler");
    const image =
      source === undefined
        ? this.#localDockerImage(nested, template, planned.tree)
        : this.#codeBuildImage(template, source, handler);
    handler.addToPolicy(
      new PolicyStatement({
        actions: MMDS_ACTIONS,
        resources: [
          Stack.of(this).formatArn({
            service: AGENTCORE_SERVICE,
            resource: RUNTIME_RESOURCE,
            resourceName: `${plan.runtimeName}-*`,
          }),
        ],
      }),
    );
    handler.addToPolicy(
      new PolicyStatement({ actions: PASS_ROLE_ACTIONS, resources: [runtimeRole.attrArn] }),
    );
    const provider = buildProvider(this, handler, build.timeoutMinutes);
    const runtime = new CfnRuntime(this, "Runtime", {
      agentRuntimeName: plan.runtimeName,
      agentRuntimeArtifact: { containerConfiguration: { containerUri: image.imageUri } },
      roleArn: runtimeRole.attrArn,
      networkConfiguration: { networkMode: AGENTCORE_NETWORK_MODE },
      protocolConfiguration: AGENTCORE_SERVER_PROTOCOL,
      lifecycleConfiguration: {
        idleRuntimeSessionTimeout: plan.lifecycle.idleSessionTimeoutSeconds,
        maxLifetime: plan.lifecycle.maxSessionLifetimeSeconds,
      },
      environmentVariables: { ...plan.environment },
      description: runtimeDescriptionFor(plan.name, { managedByCdk: true }),
    });
    if (source !== undefined) {
      const imageBuild = new CustomResource(this, "ImageBuild", {
        serviceToken: provider.serviceToken,
        resourceType: IMAGE_BUILD_RESOURCE_TYPE,
        properties: this.#imageBuildProperties(template, source),
      });
      runtime.node.addDependency(imageBuild);
    }
    const mmds = new CustomResource(this, "Mmds", {
      serviceToken: provider.serviceToken,
      resourceType: RUNTIME_MMDS_RESOURCE_TYPE,
      properties: {
        AgentRuntimeId: runtime.attrAgentRuntimeId,
        AgentRuntimeVersion: runtime.attrAgentRuntimeVersion,
      },
    });
    this.agentName = plan.name;
    this.runtimeName = plan.runtimeName;
    this.runtimeArn = runtime.attrAgentRuntimeArn;
    this.runtimeId = runtime.attrAgentRuntimeId;
    this.runtimeVersion = mmds.getAttString(RUNTIME_VERSION_ATTRIBUTE);
    this.roleArn = runtimeRole.attrArn;
    this.repositoryUri = image.repositoryUri;
    this.imageUri = image.imageUri;
    this.runtime = AgentRuntime.fromAgentRuntimeAttributes(this, "RuntimeRef", {
      agentRuntimeArn: this.runtimeArn,
      agentRuntimeId: this.runtimeId,
      agentRuntimeName: this.runtimeName,
      roleArn: this.roleArn,
      agentRuntimeVersion: this.runtimeVersion,
    });
    if (props.exportOutputs === true) {
      new CfnOutput(this, "RuntimeArn", { value: this.runtimeArn });
      new CfnOutput(this, "RuntimeId", { value: this.runtimeId });
    }
    applyTags(this, "RayitoAgent", plan.tags);
  }

  /** Permite a `grantee` invocar el agente: `bedrock-agentcore:InvokeAgentRuntime` sobre el runtime y sus endpoints. */
  grantInvoke(grantee: IGrantable): Grant {
    return Grant.addToPrincipal({
      grantee,
      actions: INVOKE_ACTIONS,
      resourceArns: [this.runtimeArn, `${this.runtimeArn}${ENDPOINTS_SUFFIX}`],
    });
  }

  /** `infra/agentcore-runtime.yaml` con cada parámetro plegado igual que la CLI (las listas como listas). */
  #include(
    nested: NestedStack,
    bucket: string,
    arns: ArnLists,
    build: AgentImageBuild,
  ): CfnInclude {
    const plan = this.#plan;
    return new CfnInclude(nested, "Template", {
      templateFile: runtimeTemplatePath(),
      parameters: {
        [STACK_PARAMETERS.agentName]: canonicalAgentName(plan.name),
        [STACK_PARAMETERS.agentDisplayName]: plan.name,
        [STACK_PARAMETERS.runtimeName]: plan.runtimeName,
        [STACK_PARAMETERS.artifactBucket]: bucket,
        [STACK_PARAMETERS.bedrockModelArns]: arns.models,
        [STACK_PARAMETERS.readableSecretArns]: arns.secrets,
        [STACK_PARAMETERS.imageRetentionCount]: String(AGENTCORE_IMAGE_RETENTION_COUNT),
        [STACK_PARAMETERS.buildLogRetentionDays]: String(AGENTCORE_BUILD_LOG_RETENTION_DAYS),
        [STACK_PARAMETERS.buildTimeoutMinutes]: String(build.timeoutMinutes),
      },
    });
  }

  /**
   * La imagen en el repositorio de la pila anidada: el rol de build gana una
   * política que sólo lee el objeto del asset, y el manejador sólo puede
   * usar ese proyecto y ese repositorio.
   */
  #codeBuildImage(template: CfnInclude, source: Asset, handler: AgentCoreHandler): BuiltImage {
    const repository = template.getResource("Repository") as CfnRepository;
    const project = template.getResource("BuildProject") as CfnProject;
    template.getResource("BuildRole").addPropertyOverride(SOURCE_POLICY_OVERRIDE, {
      PolicyName: SOURCE_POLICY_NAME,
      PolicyDocument: {
        Version: POLICY_VERSION,
        Statement: [
          {
            Sid: SOURCE_STATEMENT_SID,
            Effect: "Allow",
            Action: SOURCE_ACTIONS,
            Resource: `arn:${Aws.PARTITION}:s3:::${source.s3BucketName}/${source.s3ObjectKey}`,
          },
        ],
      },
    });
    handler.addToPolicy(
      new PolicyStatement({ actions: BUILD_ACTIONS, resources: [project.attrArn] }),
    );
    handler.addToPolicy(
      new PolicyStatement({ actions: DESCRIBE_IMAGES_ACTIONS, resources: [repository.attrArn] }),
    );
    const repositoryUri = repository.attrRepositoryUri;
    return { repositoryUri, imageUri: `${repositoryUri}:${this.#plan.imageTag}` };
  }

  /** Las propiedades de `Custom::RayitoImageBuild` (`contract.constants.customResourceProperties`). */
  #imageBuildProperties(template: CfnInclude, source: Asset): Record<string, unknown> {
    return {
      ProjectName: template.getResource("BuildProject").ref,
      RepositoryName: template.getResource("Repository").ref,
      ImageTag: this.#plan.imageTag,
      SourceLocation: `${source.s3BucketName}/${source.s3ObjectKey}`,
      AgentBaseImage: baseImageReference(),
      MaxImageBytes: AGENTCORE_MAX_IMAGE_BYTES,
    };
  }

  /**
   * La imagen construida por `cdk deploy` (`Image`, `linux/arm64`) en el
   * repositorio del bootstrap: la pila anidada pierde el repositorio, el
   * build y sus salidas, y `PullImage` del rol del runtime apunta a ese
   * repositorio.
   */
  #localDockerImage(
    nested: NestedStack,
    template: CfnInclude,
    tree: ReadonlyMap<string, string | Uint8Array>,
  ): BuiltImage {
    const directory = writeTree(stagingPath(`agent-${this.#plan.imageTag}`), tree);
    const image = new DockerImageAsset(this, "Image", {
      directory,
      platform: Platform.LINUX_ARM64,
      buildArgs: { [AGENT_BASE_IMAGE_ARG]: baseImageReference() },
    });
    for (const resource of LOCAL_DOCKER_REMOVED_RESOURCES) {
      template.node.tryRemoveChild(resource);
    }
    const outputs = template.node.findChild(OUTPUTS_CHILD);
    for (const output of LOCAL_DOCKER_REMOVED_OUTPUTS) {
      outputs.node.tryRemoveChild(output);
    }
    template.getResource("RuntimeRole").addPropertyOverride(
      PULL_IMAGE_RESOURCE_OVERRIDE,
      nested.formatArn({
        service: ECR_SERVICE,
        resource: REPOSITORY_RESOURCE,
        resourceName: image.repository.repositoryName,
      }),
    );
    return { repositoryUri: image.repository.repositoryUri, imageUri: image.imageUri };
  }
}
