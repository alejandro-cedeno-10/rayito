/**
 * El entorno del stack de un constructo de `rayito/cdk` (ADR-032 D7 y D11):
 * la región debe ser concreta (`region_unresolved`), la cuenta puede ser un
 * token (los ARN usan entonces `AWS::AccountId` y `AWS::Partition`) y las
 * etiquetas fijas de Rayito más las del usuario van a cada recurso
 * etiquetable con `Tags.of`. Sin llamadas a AWS ni búsquedas de contexto.
 */

import { Aws, Stack, Tags, Token } from "aws-cdk-lib";
import type { IConstruct } from "constructs";
import { AWS_PARTITION } from "../agentcore/iam-resources.js";
import { CDK_MANAGED_BY_TAG_VALUE } from "../limits.js";
import { COMPONENT_TAG_KEY, MANAGED_BY_TAG_KEY, SDK_VERSION_TAG_KEY } from "../stacks/model.js";
import { VERSION } from "../version.js";
import { RayitoCdkError } from "./errors.js";

/** Una cuenta de AWS: doce dígitos. */
const ACCOUNT_PATTERN = /^[0-9]{12}$/;

/** `rayito:component` de cada constructo (`contract.constants.componentTagValues`). */
export const COMPONENT_TAG_VALUES = {
  RayitoAgent: "agentcore-runtime",
  RayitoMicrovmImage: "microvm-image",
} as const;

export type RayitoConstructName = keyof typeof COMPONENT_TAG_VALUES;

/** Dónde se despliega el constructo: región concreta y cuenta concreta o token. */
export interface StackEnvironment {
  readonly region: string;
  /** La cuenta, o el token `AWS::AccountId` si el stack no la fija. */
  readonly account: string;
  /** `aws`, o el token `AWS::Partition` si la cuenta es un token. */
  readonly partition: string;
  /** `true` si la cuenta es un token: no se puede comparar con la de un ARN. */
  readonly accountUnresolved: boolean;
}

/** La región y la cuenta del stack de `scope`; `region_unresolved` o `account_invalid` si no valen. */
export function stackEnvironment(
  scope: IConstruct,
  construct: RayitoConstructName,
): StackEnvironment {
  const stack = Stack.of(scope);
  if (Token.isUnresolved(stack.region)) {
    throw new RayitoCdkError("region_unresolved", { construct });
  }
  if (Token.isUnresolved(stack.account)) {
    return {
      region: stack.region,
      account: Aws.ACCOUNT_ID,
      partition: Aws.PARTITION,
      accountUnresolved: true,
    };
  }
  if (!ACCOUNT_PATTERN.test(stack.account)) {
    throw new RayitoCdkError("account_invalid", { account: stack.account });
  }
  return {
    region: stack.region,
    account: stack.account,
    partition: AWS_PARTITION,
    accountUnresolved: false,
  };
}

/** Las fijas ganan a las del usuario (`stackTags`): más prioridad que la de `Tags.of(...).add` por defecto (100). */
export const FIXED_TAG_PRIORITY = 200;

/** Las tres etiquetas fijas (`contract.constants.fixedTagKeys`). */
export function fixedTags(construct: RayitoConstructName): Record<string, string> {
  return {
    [COMPONENT_TAG_KEY]: COMPONENT_TAG_VALUES[construct],
    [MANAGED_BY_TAG_KEY]: CDK_MANAGED_BY_TAG_VALUE,
    [SDK_VERSION_TAG_KEY]: VERSION,
  };
}

/**
 * Las etiquetas del usuario y las tres fijas (que ganan) en cada recurso
 * etiquetable bajo `scope`, nested stack incluido, al crearlo: lo que piden
 * las políticas de etiquetado obligatorio.
 */
export function applyTags(
  scope: IConstruct,
  construct: RayitoConstructName,
  userTags: Readonly<Record<string, string>>,
): void {
  const tags = Tags.of(scope);
  for (const [key, value] of Object.entries(userTags)) {
    tags.add(key, value);
  }
  for (const [key, value] of Object.entries(fixedTags(construct))) {
    tags.add(key, value, { priority: FIXED_TAG_PRIORITY });
  }
}
