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
import { AGENTCORE_ENV_CREDENTIAL_ADAPTERS } from "../limits.js";

/** Si el runtime del proyecto es un `CustomRuntime` cuyo adaptador está en `AGENTCORE_ENV_CREDENTIAL_ADAPTERS`. */
export function usesEnvCredentials(runtime: string | AgentRuntime): boolean {
  const resolved = resolveRuntime(runtime);
  const adapter = resolved instanceof CustomRuntime ? resolved.adapter : undefined;
  return (
    adapter !== undefined &&
    (AGENTCORE_ENV_CREDENTIAL_ADAPTERS as readonly string[]).includes(adapter)
  );
}
