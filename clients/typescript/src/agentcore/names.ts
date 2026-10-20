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
} from "../limits.js";
import type { ListedRuntime } from "./models.js";

/** `AgentName` de `infra/agentcore-runtime.yaml`: minúsculas y dígitos con `-` o `_` sueltos entre ellos. */
export const AGENT_NAME_PATTERN = /^[a-z0-9]+(?:[_-][a-z0-9]+)*$/;
/** Lo que cabe del nombre del agente en el del runtime (48) tras el prefijo `rayito_`. */
export const AGENT_NAME_MAX_LENGTH =
  AGENTCORE_RUNTIME_NAME_MAX_LENGTH - AGENTCORE_RUNTIME_NAME_PREFIX.length;
/** `Rayito agent <nombre> (<marca>)`: la descripción de un runtime de Rayito, en dos mitades para poder leer el nombre. */
export const RUNTIME_DESCRIPTION_PREFIX = "Rayito agent ";
export const RUNTIME_DESCRIPTION_SUFFIX = ` (${AGENTCORE_DESCRIPTION_MARKER})`;
const NAME_SEPARATOR = "-";
const RUNTIME_NAME_SEPARATOR = "_";

/** El nombre del agente si es válido para todo lo que se deriva de él; si no, `name_invalid`. */
export function validateAgentName(name: string): string {
  if (name.length > AGENT_NAME_MAX_LENGTH || !AGENT_NAME_PATTERN.test(name)) {
    throw new AgentCoreError("name_invalid", { name, max: AGENT_NAME_MAX_LENGTH });
  }
  return name;
}

/** `rayito_<nombre>` con `-` como `_` (el nombre de un runtime no admite `-`). */
export function runtimeNameFor(name: string): string {
  return `${AGENTCORE_RUNTIME_NAME_PREFIX}${name.replaceAll(NAME_SEPARATOR, RUNTIME_NAME_SEPARATOR)}`;
}

/** `rayito-agentcore-<nombre>` con `_` como `-` (CloudFormation no admite `_`). */
export function stackNameFor(name: string): string {
  return `${AGENTCORE_STACK_NAME_PREFIX}${name.replaceAll(RUNTIME_NAME_SEPARATOR, NAME_SEPARATOR)}`;
}

export function repositoryNameFor(name: string): string {
  return `${AGENTCORE_REPOSITORY_PREFIX}${name}`;
}

/** El proyecto de CodeBuild de la pila lleva el mismo nombre que ella. */
export function buildProjectNameFor(name: string): string {
  return stackNameFor(name);
}

/** `rayito/images/agentcore/<nombre>/<etiqueta>.zip`, dentro del prefijo que el bucket de `rayito setup` caduca. */
export function sourceKeyFor(name: string, imageTag: string): string {
  return `${AGENTCORE_SOURCE_PREFIX}${name}/${imageTag}.zip`;
}

/** La descripción que marca un runtime como de Rayito. */
export function runtimeDescriptionFor(name: string): string {
  return `${RUNTIME_DESCRIPTION_PREFIX}${name}${RUNTIME_DESCRIPTION_SUFFIX}`;
}

/** El grupo de logs que AgentCore crea para el runtime `runtimeId`. */
export function runtimeLogGroupFor(runtimeId: string): string {
  return `${AGENTCORE_RUNTIME_LOG_GROUP_PREFIX}${runtimeId}-${AGENTCORE_ENDPOINT_NAME}`;
}

/** Un runtime es del agente `name` sólo con su nombre y su descripción marcada: `rayito_a_b` es el de `a-b` y el de `a_b`. */
export function isOwned(listed: ListedRuntime, name: string): boolean {
  return (
    listed.runtimeName === runtimeNameFor(name) &&
    listed.description === runtimeDescriptionFor(name)
  );
}

/** El nombre del agente de un runtime de Rayito (el de su descripción), o `undefined` si no lo es. */
export function ownedAgentName(listed: ListedRuntime): string | undefined {
  if (!listed.runtimeName.startsWith(AGENTCORE_RUNTIME_NAME_PREFIX)) {
    return undefined;
  }
  const { description } = listed;
  if (
    !description.startsWith(RUNTIME_DESCRIPTION_PREFIX) ||
    !description.endsWith(RUNTIME_DESCRIPTION_SUFFIX)
  ) {
    return undefined;
  }
  const name = description.slice(
    RUNTIME_DESCRIPTION_PREFIX.length,
    description.length - RUNTIME_DESCRIPTION_SUFFIX.length,
  );
  return AGENT_NAME_PATTERN.test(name) && listed.runtimeName === runtimeNameFor(name)
    ? name
    : undefined;
}
