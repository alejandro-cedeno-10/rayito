/**
 * Los ARN que recibe el rol del runtime (ADR-031, D11): los modelos de Bedrock
 * que puede invocar (`BedrockModelArns`) y los secretos que puede leer
 * (`ReadableSecretArns`). Puro; los vectores compartidos con Python están en
 * `testdata/agentcore-deploy/model-arns.json`. Un id con un prefijo de
 * `BEDROCK_INFERENCE_PROFILE_PREFIXES` es un perfil de inferencia del sistema:
 * su ARN en la región del modelo y el modelo base en cualquier región (un
 * perfil global añade además el ARN del modelo base sin región). Otro id es un
 * modelo base en la región del modelo. Un ARN se usa tal cual.
 */

import { BEDROCK_GLOBAL_PROFILE_PREFIX, BEDROCK_INFERENCE_PROFILE_PREFIXES } from "../limits.js";
import { formatTemplate } from "../template-text.js";
import {
  ARN_PREFIX,
  AWS_PARTITION,
  checkModelReference,
  checkSecretReference,
} from "./iam-resources.js";

export const SECRET_ARN_TEMPLATE =
  "arn:{partition}:secretsmanager:{region}:{account}:secret:{name}-??????";
export const REGIONAL_FOUNDATION_MODEL_ARN_TEMPLATE =
  "arn:{partition}:bedrock:{region}::foundation-model/{modelId}";
export const ANY_REGION_FOUNDATION_MODEL_ARN_TEMPLATE =
  "arn:{partition}:bedrock:*::foundation-model/{modelId}";
export const GLOBAL_FOUNDATION_MODEL_ARN_TEMPLATE =
  "arn:{partition}:bedrock:::foundation-model/{modelId}";
export const INFERENCE_PROFILE_ARN_TEMPLATE =
  "arn:{partition}:bedrock:{region}:{account}:inference-profile/{profileId}";
const PROFILE_PREFIX_SEPARATOR = ".";

export interface ModelArnContext {
  readonly modelRegion: string;
  readonly account: string;
  readonly partition?: string | undefined;
}

function profilePrefixOf(modelId: string): string | undefined {
  const [prefix, ...rest] = modelId.split(PROFILE_PREFIX_SEPARATOR);
  return rest.length > 0 &&
    (BEDROCK_INFERENCE_PROFILE_PREFIXES as readonly string[]).includes(prefix ?? "")
    ? prefix
    : undefined;
}

function arnsOfModel(modelId: string, context: ModelArnContext): string[] {
  const partition = context.partition ?? AWS_PARTITION;
  const fields = { partition, region: context.modelRegion, account: context.account };
  checkModelReference(modelId, {
    region: context.modelRegion,
    account: context.account,
    partition,
  });
  if (modelId.startsWith(ARN_PREFIX)) {
    return [modelId];
  }
  const prefix = profilePrefixOf(modelId);
  if (prefix === undefined) {
    return [formatTemplate(REGIONAL_FOUNDATION_MODEL_ARN_TEMPLATE, { ...fields, modelId })];
  }
  const baseModel = modelId.slice(prefix.length + PROFILE_PREFIX_SEPARATOR.length);
  const arns = [
    formatTemplate(INFERENCE_PROFILE_ARN_TEMPLATE, { ...fields, profileId: modelId }),
    formatTemplate(ANY_REGION_FOUNDATION_MODEL_ARN_TEMPLATE, { ...fields, modelId: baseModel }),
  ];
  if (prefix === BEDROCK_GLOBAL_PROFILE_PREFIX) {
    arns.push(
      formatTemplate(GLOBAL_FOUNDATION_MODEL_ARN_TEMPLATE, { ...fields, modelId: baseModel }),
    );
  }
  return arns;
}

function unique(values: readonly string[]): string[] {
  return [...new Set(values)];
}

/** Los ARN de Bedrock del rol para `models`, sin duplicados y en orden. */
export function bedrockResourceArns(models: readonly string[], context: ModelArnContext): string[] {
  return unique(models.flatMap((modelId) => arnsOfModel(modelId, context)));
}

/** Los ARN de Secrets Manager del rol para `names` (nombres o ARN), sin duplicados y en orden. */
export function secretResourceArns(
  names: readonly string[],
  context: {
    readonly region: string;
    readonly account: string;
    readonly partition?: string | undefined;
  },
): string[] {
  const partition = context.partition ?? AWS_PARTITION;
  for (const name of names) {
    checkSecretReference(name, { ...context, partition });
  }
  return unique(
    names.map((name) =>
      name.startsWith(ARN_PREFIX)
        ? name
        : formatTemplate(SECRET_ARN_TEMPLATE, {
            partition,
            region: context.region,
            account: context.account,
            name,
          }),
    ),
  );
}
