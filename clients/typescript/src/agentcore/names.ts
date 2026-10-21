/**
 * Los nombres de todo lo que `--target agentcore` crea para un agente
 * (ADR-031, D4) y la regla que dice cuáles son de Rayito. Puro. Todo se deriva
 * del nombre del agente, así que un redespliegue encuentra lo mismo y un
 * borrado sólo toca eso.
 */

import { AgentCoreError } from "../errors.js";
import {
  AGENTCORE_DESCRIPTION_MARKER,
  AGENTCORE_ENDPOINT_NAME,
  AGENTCORE_REPOSITORY_PREFIX,
  AGENTCORE_RUNTIME_LOG_GROUP_PREFIX,
  AGENTCORE_RUNTIME_NAME_MAX_LENGTH,
  AGENTCORE_RUNTIME_NAME_PREFIX,
  AGENTCORE_SOURCE_PREFIX,
  AGENTCORE_STACK_NAME_PREFIX,
  CDK_OWNERSHIP_SUFFIX,
} from "../limits.js";
import { formatTemplate } from "../template-text.js";
import { CDK_MANAGED_MESSAGE, CDK_STACK_HINT_TEMPLATE } from "./messages.js";
import type { ListedRuntime } from "./models.js";

/** `AgentName` de `infra/agentcore-runtime.yaml`: minúsculas y dígitos con `-` o `_` sueltos entre ellos. */
export const AGENT_NAME_PATTERN = /^[a-z0-9]+(?:[_-][a-z0-9]+)*$/;
/** Lo que cabe del nombre del agente en el del runtime (48) tras el prefijo `rayito_`. */
export const AGENT_NAME_MAX_LENGTH =
  AGENTCORE_RUNTIME_NAME_MAX_LENGTH - AGENTCORE_RUNTIME_NAME_PREFIX.length;
/** `Rayito agent <nombre> (<marca>)`: la descripción de un runtime de Rayito, en dos mitades para poder leer el nombre. */
export const RUNTIME_DESCRIPTION_PREFIX = "Rayito agent ";
export const RUNTIME_DESCRIPTION_SUFFIX = ` (${AGENTCORE_DESCRIPTION_MARKER})`;
/** La marca de un runtime que gestiona CloudFormation con `RayitoAgent` (ADR-032 D6). */
export const CDK_DESCRIPTION_MARKER = `${AGENTCORE_DESCRIPTION_MARKER}${CDK_OWNERSHIP_SUFFIX}`;
export const CDK_RUNTIME_DESCRIPTION_SUFFIX = ` (${CDK_DESCRIPTION_MARKER})`;
const NAME_SEPARATOR = "-";
const RUNTIME_NAME_SEPARATOR = "_";

/** El nombre del agente si es válido para todo lo que se deriva de él; si no, `name_invalid`. */
export function validateAgentName(name: string): string {
  if (name.length > AGENT_NAME_MAX_LENGTH || !AGENT_NAME_PATTERN.test(name)) {
    throw new AgentCoreError("name_invalid", { name, max: AGENT_NAME_MAX_LENGTH });
  }
  return name;
}

/**
 * La forma canónica del nombre: `_` como `-`. Todo nombre derivado (pila,
 * repositorio, proyecto de CodeBuild, prefijo del código y runtime) sale de
 * ella, así que `a-b` y `a_b` dan los mismos recursos y se detecta que son el
 * mismo (`name_conflict`) en vez de pisarse. Los nombres sólo con `-` quedan
 * igual que siempre.
 */
export function canonicalAgentName(name: string): string {
  return name.replaceAll(RUNTIME_NAME_SEPARATOR, NAME_SEPARATOR);
}

/** `rayito_<nombre canónico>` con `-` como `_` (el nombre de un runtime no admite `-`). */
export function runtimeNameFor(name: string): string {
  return `${AGENTCORE_RUNTIME_NAME_PREFIX}${canonicalAgentName(name).replaceAll(NAME_SEPARATOR, RUNTIME_NAME_SEPARATOR)}`;
}

/** `rayito-agentcore-<nombre canónico>` (CloudFormation no admite `_`). */
export function stackNameFor(name: string): string {
  return `${AGENTCORE_STACK_NAME_PREFIX}${canonicalAgentName(name)}`;
}

export function repositoryNameFor(name: string): string {
  return `${AGENTCORE_REPOSITORY_PREFIX}${canonicalAgentName(name)}`;
}

/** El proyecto de CodeBuild de la pila lleva el mismo nombre que ella. */
export function buildProjectNameFor(name: string): string {
  return stackNameFor(name);
}

/** `rayito/images/agentcore/<nombre>/<etiqueta>.zip`, dentro del prefijo que el bucket de `rayito setup` caduca. */
export function sourceKeyFor(name: string, imageTag: string): string {
  return `${AGENTCORE_SOURCE_PREFIX}${canonicalAgentName(name)}/${imageTag}.zip`;
}

/** La descripción que marca un runtime como de Rayito; con `managedByCdk`, la de `RayitoAgent`. */
export function runtimeDescriptionFor(
  name: string,
  options: { readonly managedByCdk?: boolean | undefined } = {},
): string {
  const suffix =
    options.managedByCdk === true ? CDK_RUNTIME_DESCRIPTION_SUFFIX : RUNTIME_DESCRIPTION_SUFFIX;
  return `${RUNTIME_DESCRIPTION_PREFIX}${name}${suffix}`;
}

/** `true` si el runtime lleva la marca `+cdk`: es de Rayito, pero lo gestiona CloudFormation. */
export function isCdkManaged(listed: Pick<ListedRuntime, "description">): boolean {
  return listed.description.endsWith(CDK_RUNTIME_DESCRIPTION_SUFFIX);
}

/** El `name_conflict` de un agente de CDK: la CLI y `deployAgent` lo leen pero no lo cambian. */
export function cdkManagedError(name: string, runtimeName: string): AgentCoreError {
  return new AgentCoreError(
    "name_conflict",
    { runtimeName, name, stackHint: formatTemplate(CDK_STACK_HINT_TEMPLATE, { runtimeName }) },
    { template: CDK_MANAGED_MESSAGE },
  );
}

/** Lanza `cdkManagedError` si el runtime lo gestiona CloudFormation (antes de cualquier escritura). */
export function refuseCdkManaged(
  listed: Pick<ListedRuntime, "description" | "runtimeName">,
  name: string,
): void {
  if (isCdkManaged(listed)) {
    throw cdkManagedError(name, listed.runtimeName);
  }
}

/** El grupo de logs que AgentCore crea para el runtime `runtimeId`. */
export function runtimeLogGroupFor(runtimeId: string): string {
  return `${AGENTCORE_RUNTIME_LOG_GROUP_PREFIX}${runtimeId}-${AGENTCORE_ENDPOINT_NAME}`;
}

/** Un runtime es del agente `name` sólo con su nombre y su descripción marcada (la de la CLI o la `+cdk`): `rayito_a_b` es el de `a-b` y el de `a_b`. */
export function isOwned(listed: ListedRuntime, name: string): boolean {
  return (
    listed.runtimeName === runtimeNameFor(name) &&
    (listed.description === runtimeDescriptionFor(name) ||
      listed.description === runtimeDescriptionFor(name, { managedByCdk: true }))
  );
}

/** El nombre del agente de un runtime de Rayito (el de su descripción), o `undefined` si no lo es. */
export function ownedAgentName(listed: ListedRuntime): string | undefined {
  if (!listed.runtimeName.startsWith(AGENTCORE_RUNTIME_NAME_PREFIX)) {
    return undefined;
  }
  const { description } = listed;
  const suffix = [CDK_RUNTIME_DESCRIPTION_SUFFIX, RUNTIME_DESCRIPTION_SUFFIX].find((candidate) =>
    description.endsWith(candidate),
  );
  if (!description.startsWith(RUNTIME_DESCRIPTION_PREFIX) || suffix === undefined) {
    return undefined;
  }
  const name = description.slice(
    RUNTIME_DESCRIPTION_PREFIX.length,
    description.length - suffix.length,
  );
  return AGENT_NAME_PATTERN.test(name) && listed.runtimeName === runtimeNameFor(name)
    ? name
    : undefined;
}
