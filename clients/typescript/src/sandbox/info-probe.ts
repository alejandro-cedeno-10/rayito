/**
 * La sonda interna de `Sandbox.getInfo(sandboxId)` del shim `rayito/e2b`
 * (el espejo de `Sandbox.get_info(sandbox_id)` de Python); no forma parte de
 * la API pública de `rayito`. `get-microvm` y, sólo sobre un sandbox
 * `RUNNING`, un JWE más un `Health` anónimo por un transporte dedicado que
 * rellena `lifecycle` (así `expiresAt` es el plazo lógico) y, con el agente
 * listo, los metadatos y la vista del guest. En cualquier otro estado no toca
 * el endpoint: una sonda despertaría un suspendido.
 *
 * Vivía como `Sandbox.probedInfo` en `sandbox.ts`, un `static` público que
 * aparecía en el `.d.ts` de `rayito` aunque su docblock ya decía que era
 * interno de `rayito/e2b`: la dependencia quedaba invertida (el núcleo nativo
 * cargando API específica del shim). Este módulo la mueve fuera de la
 * superficie nativa sin cambiar su cuerpo; `sandbox.ts` no lo importa, así
 * que no hay ciclo.
 */

import { abortReasonOr, raceAbort } from "../abort.js";
import type { HealthResponse } from "../gen/rayito/v1/health_pb.js";
import { type SandboxInfo, sandboxInfo, withLifecycle } from "../models.js";
import { resolveTransportSettings, type TransportSettings } from "../transport/transport.js";
import { validateSandboxId } from "./launch.js";
import { lifecycleFromProto } from "./lifecycle.js";
import { METADATA_PROBE_TIMEOUT_MS, metadataProbeFailure } from "./paginator.js";
import { probeHealth } from "./probe.js";
import { guestFactsFromHealth, metadataFromHealth } from "./readiness.js";
import { type ControlPlaneOptions, resolveControlPlane } from "./sandbox.js";

/** Opciones de `probeSandboxInfo` (interno de `rayito/e2b`). */
export interface ProbedInfoOptions extends ControlPlaneOptions {
  readonly requestTimeoutMs?: number | undefined;
  readonly transport?: Partial<TransportSettings> | undefined;
  readonly signal?: AbortSignal | undefined;
}

/**
 * Acceso interno para `rayito/e2b` (el espejo de `Sandbox.get_info(sandbox_id)`
 * de Python); no forma parte de la API pública. `get-microvm` y, sólo sobre
 * un sandbox `RUNNING`, un JWE más un `Health` anónimo por un transporte
 * dedicado que rellena `lifecycle` (así `expiresAt` es el plazo lógico) y,
 * con el agente listo, los metadatos y la vista del guest. En cualquier otro
 * estado no toca el endpoint: una sonda despertaría un suspendido.
 */
export async function probeSandboxInfo(
  sandboxId: string,
  options: ProbedInfoOptions = {},
): Promise<SandboxInfo> {
  options.signal?.throwIfAborted();
  const plane = resolveControlPlane(options);
  const info = await plane.getMicrovm(validateSandboxId(sandboxId), { signal: options.signal });
  if (info.state !== "RUNNING") {
    return info;
  }
  let response: HealthResponse;
  try {
    response = await raceAbort(
      probeHealth(
        plane,
        info,
        resolveTransportSettings(options.transport),
        options.requestTimeoutMs ?? METADATA_PROBE_TIMEOUT_MS,
      ),
      options.signal,
    );
  } catch (error) {
    throw abortReasonOr(options.signal, metadataProbeFailure(info.sandboxId, error));
  }
  const lifecycle = lifecycleFromProto(response.lifecycle);
  if (!response.agentReady) {
    return withLifecycle(info, lifecycle);
  }
  return sandboxInfo({
    ...info,
    lifecycle,
    ...guestFactsFromHealth(response),
    metadata: metadataFromHealth(response),
  });
}
