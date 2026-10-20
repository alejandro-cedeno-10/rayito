/**
 * El componente `agentcore-runtime` (ADR-031, D3): la plantilla
 * `infra/agentcore-runtime.yaml` que se despliega como `rayito-agentcore-<agente>`
 * con el repositorio ECR, el proyecto de CodeBuild y los dos roles de un
 * agente. **No** está en el catálogo de `rayito stack` (como las pilas de IAM y
 * del bucket de `rayito setup`): sólo `--target agentcore` la despliega. Los
 * nombres de sus parámetros y salidas son los de `contract.json`
 * (`constants.stackParameters` y `constants.stackOutputs`).
 */

import {
  AGENTCORE_BUILD_LOG_RETENTION_DAYS,
  AGENTCORE_BUILD_TIMEOUT_MINUTES,
  AGENTCORE_IMAGE_RETENTION_COUNT,
} from "../limits.js";
import type { StackComponent } from "../stacks/model.js";

export const AGENTCORE_STACK_COMPONENT_NAME = "agentcore-runtime";
/** Los roles de la plantilla no llevan nombre fijo: hace falta `CAPABILITY_IAM`. */
export const AGENTCORE_STACK_CAPABILITIES: readonly string[] = ["CAPABILITY_IAM"];
/** Separador de `BedrockModelArns` y `ReadableSecretArns` (`CommaDelimitedList`). */
export const ARN_LIST_SEPARATOR = ",";

export const STACK_PARAMETERS = {
  agentName: "AgentName",
  agentDisplayName: "AgentDisplayName",
  runtimeName: "RuntimeName",
  artifactBucket: "ArtifactBucket",
  bedrockModelArns: "BedrockModelArns",
  readableSecretArns: "ReadableSecretArns",
  imageRetentionCount: "ImageRetentionCount",
  buildLogRetentionDays: "BuildLogRetentionDays",
  buildTimeoutMinutes: "BuildTimeoutMinutes",
} as const;

export const STACK_OUTPUTS = {
  repositoryName: "RepositoryName",
  repositoryUri: "RepositoryUri",
  repositoryArn: "RepositoryArn",
  buildProjectName: "BuildProjectName",
  buildLogGroupName: "BuildLogGroupName",
  runtimeRoleArn: "RuntimeRoleArn",
} as const;

export const AGENTCORE_STACK_COMPONENT: StackComponent = {
  name: AGENTCORE_STACK_COMPONENT_NAME,
  description:
    "Un agente en Amazon Bedrock AgentCore Runtime: repositorio ECR, proyecto de CodeBuild " +
    "con su grupo de logs y los roles de build y del runtime (rayito agent deploy --target agentcore).",
  parameters: [
    {
      name: STACK_PARAMETERS.agentName,
      description: "Nombre canónico del agente (con `-`): nombra los recursos de la pila.",
      required: true,
    },
    {
      name: STACK_PARAMETERS.agentDisplayName,
      description: "Nombre del agente tal como se escribió: distingue `a-b` de `a_b`.",
      required: true,
    },
    { name: STACK_PARAMETERS.runtimeName, description: "Nombre del runtime.", required: true },
    {
      name: STACK_PARAMETERS.artifactBucket,
      description: "Bucket de artefactos con el contexto de build.",
      required: true,
    },
    {
      name: STACK_PARAMETERS.bedrockModelArns,
      description: "ARN de los modelos de Bedrock que el rol del runtime puede invocar.",
      default: "",
    },
    {
      name: STACK_PARAMETERS.readableSecretArns,
      description: "ARN de los secretos que el rol del runtime puede leer.",
      default: "",
    },
    {
      name: STACK_PARAMETERS.imageRetentionCount,
      description: "Imágenes que conserva el repositorio.",
      default: String(AGENTCORE_IMAGE_RETENTION_COUNT),
    },
    {
      name: STACK_PARAMETERS.buildLogRetentionDays,
      description: "Días de retención de los logs del build.",
      default: String(AGENTCORE_BUILD_LOG_RETENTION_DAYS),
    },
    {
      name: STACK_PARAMETERS.buildTimeoutMinutes,
      description: "Minutos tras los que CodeBuild para un build.",
      default: String(AGENTCORE_BUILD_TIMEOUT_MINUTES),
    },
  ],
  capabilities: AGENTCORE_STACK_CAPABILITIES,
  cost: {
    creates: [
      "AWS::ECR::Repository",
      "AWS::CodeBuild::Project",
      "AWS::Logs::LogGroup",
      "AWS::IAM::Role (x2)",
    ],
    idleMonthly:
      "$0,10 por GB-mes de las imágenes que conserva el repositorio; el resto no cuesta sin builds",
    perUse: ["CodeBuild: $0,0034 por minuto de build arm1.small"],
    removal: "rayito agent delete <nombre> --target agentcore borra la pila con el repositorio",
    source: "AWS_API_NOTES.md, «Despliegue en AgentCore Runtime»",
  },
};
