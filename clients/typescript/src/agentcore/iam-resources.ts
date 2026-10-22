/**
 * Validación pura de lo que acaba como `Resource` de IAM en el rol del runtime
 * de `--target agentcore`: los secretos legibles, los modelos de Bedrock y el
 * modelo del proyecto (`contract.json`, `iamResourceRules`). Un nombre o id sólo
 * lleva los caracteres de su servicio, sin `*`, `?` ni `,`; un ARN debe nombrar
 * la partición, la región y la cuenta del despliegue, sin comodines.
 */

import { AgentCoreError } from "../errors.js";

export const ARN_PREFIX = "arn:";
/** La partición de las regiones comerciales de AgentCore (`constants.awsPartition`). */
export const AWS_PARTITION = "aws";

/** Un nombre de secreto de Secrets Manager: hasta 512 caracteres de `[A-Za-z0-9/_+=.@-]` (`iamResourceRules`). */
export const SECRET_NAME_PATTERN = /^[A-Za-z0-9/_+=.@-]+$/;
export const SECRET_NAME_MAX_LENGTH = 512;
/** Un id de modelo o de perfil de inferencia de Bedrock, sin comodines. */
export const MODEL_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]*$/;
export const ACCOUNT_PATTERN = /^[0-9]{12}$/;

const ARN_FIELD_SEPARATOR = ":";
const ARN_FIELD_COUNT = 6;
const SECRETS_MANAGER_SERVICE = "secretsmanager";
const SECRET_RESOURCE_PREFIX = "secret:";
const BEDROCK_SERVICE = "bedrock";
const FOUNDATION_MODEL_PREFIX = "foundation-model/";
const ANY_REGION = "*";
const GLOBAL_REGION = "";
const PROFILE_RESOURCE_PREFIXES = ["inference-profile/", "application-inference-profile/"];

export const FIELD_READABLE_SECRETS = "readable_secrets";
export const FIELD_BEDROCK_MODELS = "bedrock_models";
export const FIELD_MODEL_ID = "model.id";

interface ArnFields {
  readonly service: string;
  readonly region: string;
  readonly account: string;
  readonly resource: string;
}

/** Los campos de un ARN de la partición dada; `resource` conserva los `:` del id. */
function arnFields(value: string, partition: string): ArnFields | undefined {
  const parts = value.split(ARN_FIELD_SEPARATOR);
  if (parts.length < ARN_FIELD_COUNT || parts[1] !== partition) {
    return undefined;
  }
  return {
    service: parts[2] ?? "",
    region: parts[3] ?? "",
    account: parts[4] ?? "",
    resource: parts.slice(ARN_FIELD_COUNT - 1).join(ARN_FIELD_SEPARATOR),
  };
}

function accountMatches(found: string, account: string | undefined): boolean {
  return account === undefined ? ACCOUNT_PATTERN.test(found) : found === account;
}

function secretArnIsValid(fields: ArnFields, region: string, account: string | undefined): boolean {
  return (
    fields.service === SECRETS_MANAGER_SERVICE &&
    fields.region === region &&
    accountMatches(fields.account, account) &&
    fields.resource.startsWith(SECRET_RESOURCE_PREFIX) &&
    SECRET_NAME_PATTERN.test(fields.resource.slice(SECRET_RESOURCE_PREFIX.length))
  );
}

function modelArnIsValid(fields: ArnFields, region: string, account: string | undefined): boolean {
  if (fields.service !== BEDROCK_SERVICE) {
    return false;
  }
  if (fields.resource.startsWith(FOUNDATION_MODEL_PREFIX)) {
    return (
      [region, ANY_REGION, GLOBAL_REGION].includes(fields.region) &&
      fields.account === "" &&
      MODEL_ID_PATTERN.test(fields.resource.slice(FOUNDATION_MODEL_PREFIX.length))
    );
  }
  const prefix = PROFILE_RESOURCE_PREFIXES.find((candidate) =>
    fields.resource.startsWith(candidate),
  );
  return (
    prefix !== undefined &&
    fields.region === region &&
    accountMatches(fields.account, account) &&
    MODEL_ID_PATTERN.test(fields.resource.slice(prefix.length))
  );
}

export interface ReferenceContext {
  readonly region: string;
  /** Sin cuenta sólo se exige que un ARN lleve una de doce dígitos. */
  readonly account?: string | undefined;
  readonly partition?: string | undefined;
  readonly field?: string | undefined;
}

/** Un nombre de secreto o un ARN de Secrets Manager de la región y cuenta de `context`, o `iam_resource_invalid`. */
export function checkSecretReference(secret: string, context: ReferenceContext): void {
  let valid: boolean;
  if (secret.startsWith(ARN_PREFIX)) {
    const fields = arnFields(secret, context.partition ?? AWS_PARTITION);
    valid = fields !== undefined && secretArnIsValid(fields, context.region, context.account);
  } else {
    valid = secret.length <= SECRET_NAME_MAX_LENGTH && SECRET_NAME_PATTERN.test(secret);
  }
  if (!valid) {
    throw new AgentCoreError("iam_resource_invalid", {
      field: context.field ?? FIELD_READABLE_SECRETS,
    });
  }
}

/** Un id de modelo o de perfil de Bedrock, o un ARN de Bedrock de la región y cuenta de `context` (el modelo base también sin región o en cualquiera), o `iam_resource_invalid`. */
export function checkModelReference(model: string, context: ReferenceContext): void {
  let valid: boolean;
  if (model.startsWith(ARN_PREFIX)) {
    const fields = arnFields(model, context.partition ?? AWS_PARTITION);
    valid = fields !== undefined && modelArnIsValid(fields, context.region, context.account);
  } else {
    valid = MODEL_ID_PATTERN.test(model);
  }
  if (!valid) {
    throw new AgentCoreError("iam_resource_invalid", {
      field: context.field ?? FIELD_BEDROCK_MODELS,
    });
  }
}

export interface IamInputs {
  /** `undefined`: un modelo que no es un recurso de IAM (fuera de Bedrock, o sin id). */
  readonly projectModel: string | undefined;
  readonly bedrockModels: readonly string[];
  readonly readableSecrets: readonly string[];
  readonly modelRegion: string;
  readonly region: string;
}

/** Valida, sin AWS, el modelo del proyecto, `bedrockModels` y `readableSecrets`, en ese orden. */
export function checkIamInputs(inputs: IamInputs): void {
  if (inputs.projectModel !== undefined) {
    checkModelReference(inputs.projectModel, {
      region: inputs.modelRegion,
      field: FIELD_MODEL_ID,
    });
  }
  for (const model of inputs.bedrockModels) {
    checkModelReference(model, { region: inputs.modelRegion });
  }
  for (const secret of inputs.readableSecrets) {
    checkSecretReference(secret, { region: inputs.region });
  }
}
