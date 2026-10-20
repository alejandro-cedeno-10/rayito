/**
 * Los adaptadores que sólo leen las credenciales de variables de entorno
 * (ADR-031, D9): en AgentCore no hay pasarela de secretos y el cliente de
 * Bedrock de esos frameworks no usa la cadena de credenciales de AWS, así que
 * el host las lee del rol del runtime y se las pone en cada invocación. Puro:
 * lo importan el export del contexto de build y el plan de despliegue.
 */

import { CustomRuntime } from "../agent/custom.js";
import type { AgentRuntime } from "../agent/runtime.js";
import { resolveRuntime } from "../agent/runtimes.js";
import {
  AGENTCORE_CREDENTIAL_PROCESS_RUNTIMES,
  AGENTCORE_ENV_CREDENTIAL_ADAPTERS,
} from "../limits.js";

/** Si el runtime del proyecto es un `CustomRuntime` cuyo adaptador está en `AGENTCORE_ENV_CREDENTIAL_ADAPTERS`. */
export function usesEnvCredentials(runtime: string | AgentRuntime): boolean {
  const resolved = resolveRuntime(runtime);
  const adapter = resolved instanceof CustomRuntime ? resolved.adapter : undefined;
  return (
    adapter !== undefined &&
    (AGENTCORE_ENV_CREDENTIAL_ADAPTERS as readonly string[]).includes(adapter)
  );
}

/** El perfil de `credential_process` (`contract.json`, `constants.export.awsCredentials.credentialProcess`): OpenCode sólo arranca su proveedor de Bedrock con un perfil o una variable de AWS, y en AgentCore el rol sólo lo sirve el servicio de metadatos de la microVM. */
export const AWS_CONFIG_NAME = "aws-config";
export const AWS_PROFILE_NAME = "rayito-agentcore";
export const CREDENTIAL_PROCESS_ARGUMENT = "--credential-process";
export const AWS_CONFIG_FILE_ENV = "AWS_CONFIG_FILE";
export const AWS_PROFILE_ENV = "AWS_PROFILE";

/** Si el runtime del proyecto (un nombre del registro o un `AgentRuntime`) es uno de `AGENTCORE_CREDENTIAL_PROCESS_RUNTIMES`, por su `name`. */
export function usesCredentialProcess(runtime: string | AgentRuntime): boolean {
  const name = typeof runtime === "string" ? runtime : runtime.name;
  return (AGENTCORE_CREDENTIAL_PROCESS_RUNTIMES as readonly string[]).includes(name);
}

/** El `AWS_CONFIG_FILE`: un perfil cuyas credenciales salen de la herramienta (`credentialProcess.configTemplate`). */
export function awsConfigText(python: string, script: string): string {
  return `[profile ${AWS_PROFILE_NAME}]\ncredential_process = ${python} ${script} ${CREDENTIAL_PROCESS_ARGUMENT}\n`;
}

/** Las variables que apuntan al proceso de OpenCode a ese perfil. */
export function awsProfileEnvs(configPath: string): Record<string, string> {
  return {
    [AWS_CONFIG_FILE_ENV]: configPath,
    [AWS_PROFILE_ENV]: AWS_PROFILE_NAME,
  };
}
