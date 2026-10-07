/**
 * Calentamiento de plazas de pool para el agente de IA
 * (`ai-agent-fast-start`, design.md §6, opción C). Espejo de
 * `rayito/_agent/_warmup.py`: los pasos del runtime (`warmupSteps`), que
 * cargan el runtime en la caché de páginas antes del `pause()` de la plaza.
 */

import { DEFAULT_AGENT_RUNTIME } from "./domain.js";
import type { AgentRuntime, WarmupStep } from "./runtime.js";
import { resolveRuntime } from "./runtimes.js";

/**
 * Los pasos de `PoolConfig.warmup` para un pool de agentes: cargan el
 * runtime en la caché de páginas antes de aparcar cada plaza.
 *
 * Un pool sólo compensa si llegan muchas conversaciones nuevas cuyo primer
 * mensaje tiene que ser rápido: los turnos de una misma conversación de menos
 * de 8 h van mejor en una sola VM pausada entre turnos (`pause()` y
 * `connect()`, o la auto-suspensión de `idle`), y más allá de 8 h, con
 * `persist`. Guía "Agente en el sandbox", "¿Qué uso?".
 *
 * Coste y activación
 * -------------------
 * Activa: `new SandboxPool({ ..., warmup: agentPoolWarmup(...) })`.
 * Recursos y llamadas AWS: ninguna llamada nueva; cada plaza corre los pasos
 *   antes de su `pause()` y su snapshot crece con la caché.
 * Coste aproximado: ≈ $0,0054 por ciclo de reciclado (≈ 103 al mes) ≈
 *   $0,64/plaza/mes, frente a ≈ $0,60 de una plaza base; cada `take()` lee
 *   ≈ $0,0014. Tiempos medidos en AWS (AWS_API_NOTES Q147) por precios de
 *   lista, us-east-1, consultados 2026-10-06
 *   (https://aws.amazon.com/lambda/pricing/).
 * IAM: ninguna además de la del pool.
 * Cómo apagarla: `warmup: []` (por defecto).
 * Ejemplo:
 *   new SandboxPool({ size: 2, template: "rayito-agent", allowInternetAccess: false,
 *     warmup: agentPoolWarmup("opencode") });
 */
export function agentPoolWarmup(
  runtime: string | AgentRuntime = DEFAULT_AGENT_RUNTIME,
): readonly WarmupStep[] {
  return [...resolveRuntime(runtime).warmupSteps()];
}
