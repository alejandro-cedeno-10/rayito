/**
 * Calentamiento de plazas de pool para el agente de IA
 * (`ai-agent-fast-start`, design.md §6, opciones C y D). Mismos pasos que
 * `rayito/_agent/_warmup.py`:
 *
 * 1. con `serve`, antes de arrancar el servidor, una configuración mínima en
 *    `OPENCODE_CONFIG` (la real, con el puerto de la pasarela, sólo existe
 *    tras el `take()`);
 * 2. los pasos del runtime (`warmupSteps`); con `serve`, el servidor en
 *    segundo plano con una contraseña aleatoria generada dentro de cada VM,
 *    así que cada plaza tiene la suya y un reciclado la regenera;
 * 3. un sondeo de `/global/health` con esa contraseña;
 * 4. una instancia caliente con `GET /config?directory=<estado>/warm`.
 *
 * OpenCode carga la configuración por directorio y de forma perezosa (F1):
 * tras el `take()`, `agent.run` escribe la configuración con el puerto real y
 * se engancha con `--dir <workdir>`, que crea una instancia nueva que la lee.
 */

import { InvalidArgumentError } from "../errors.js";
import { AGENT_STATE_DIR, DEFAULT_WARMUP_STEP_TIMEOUT_SECONDS } from "../limits.js";
import {
  OPENCODE_CONFIG_PATH,
  OPENCODE_SERVE_SECRET_PATH,
  OPENCODE_SERVE_URL,
  OPENCODE_SERVE_USER,
  shellQuote,
} from "./opencode.js";
import type { AgentRuntime, WarmupStep } from "./runtime.js";
import { resolveRuntime } from "./runtimes.js";

/** Directorio de la instancia que se calienta antes de aparcar la plaza. */
export const SERVE_WARM_DIR = `${AGENT_STATE_DIR}/warm`;
/** Configuración mínima con la que arranca el servidor residente en una plaza. */
export const SERVE_PLACEHOLDER_CONFIG = '{"$schema":"https://opencode.ai/config.json"}';
/** Segundos entre sondeos de `/global/health` mientras arranca el servidor. */
export const SERVE_HEALTH_POLL_SECONDS = 0.5;
const SERVE_CURL_TIMEOUT_SECONDS = 5;
const SERVE_CONFIG_TAG = "rayito-agent-serve-config";
const SERVE_READY_TAG = "rayito-agent-serve-ready";
const OPENCODE_RUNTIME_NAME = "opencode";
const MS_PER_SECOND = 1000;

function placeholderConfigStep(): WarmupStep {
  const config = shellQuote(OPENCODE_CONFIG_PATH);
  return {
    cmd:
      `mkdir -p "$(dirname ${config})" && ` +
      `{ [ -e ${config} ] || printf '%s\\n' ${shellQuote(SERVE_PLACEHOLDER_CONFIG)}` +
      ` > ${config}; }`,
    tag: SERVE_CONFIG_TAG,
  };
}

function serveReadyStep(): WarmupStep {
  const secret = shellQuote(OPENCODE_SERVE_SECRET_PATH);
  const health = shellQuote(`${OPENCODE_SERVE_URL}/global/health`);
  const warm = shellQuote(
    `${OPENCODE_SERVE_URL}/config?directory=${encodeURIComponent(SERVE_WARM_DIR)}`,
  );
  const curl = `curl -fsS -m ${SERVE_CURL_TIMEOUT_SECONDS} -u "${OPENCODE_SERVE_USER}:$(cat ${secret})"`;
  return {
    cmd:
      "set -u\n" +
      `mkdir -p ${shellQuote(SERVE_WARM_DIR)}\n` +
      `until [ -s ${secret} ] && ${curl} ${health} >/dev/null 2>&1; do\n` +
      `  sleep ${SERVE_HEALTH_POLL_SECONDS}\n` +
      "done\n" +
      `${curl} ${warm} >/dev/null\n`,
    timeoutMs: DEFAULT_WARMUP_STEP_TIMEOUT_SECONDS * MS_PER_SECOND,
    tag: SERVE_READY_TAG,
  };
}

/**
 * Los pasos de `PoolConfig.warmup` para un pool de agentes.
 *
 * Sin `serve` (opción C) sólo carga el runtime en la caché de páginas antes
 * de aparcar. Con `serve: true` (opción D, sólo OpenCode) deja además el
 * servidor residente arrancado y caliente; `agent.run` se engancha a él tras
 * el `take()`. D no se recomienda: sólo gana unas décimas a C (Q154) y
 * cuesta más memoria y más por plaza.
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
 *   antes de su `pause()` y su snapshot crece con la caché y, con `serve`, con
 *   el proceso del servidor.
 * Coste aproximado: ≈ $0,0054 por ciclo de reciclado (≈ 103 al mes) ≈
 *   $0,64/plaza/mes sin `serve` y ≈ $0,0069 ≈ $0,82 con `serve`, frente a
 *   ≈ $0,60 de una plaza base; cada `take()` lee ≈ $0,0014 (≈ $0,0020 con
 *   `serve`). Tiempos medidos en AWS (AWS_API_NOTES Q147 y Q148) por precios
 *   de lista, us-east-1, consultados 2026-10-06
 *   (https://aws.amazon.com/lambda/pricing/).
 * IAM: ninguna además de la del pool.
 * Cómo apagarla: `warmup: []` (por defecto).
 * Ejemplo:
 *   new SandboxPool({ size: 2, template: "rayito-agent", allowInternetAccess: false,
 *     warmup: agentPoolWarmup("opencode") });
 */
export function agentPoolWarmup(
  runtime: string | AgentRuntime = OPENCODE_RUNTIME_NAME,
  options: { readonly serve?: boolean | undefined } = {},
): readonly WarmupStep[] {
  const rt = resolveRuntime(runtime);
  const serve = options.serve ?? false;
  if (serve && rt.name !== OPENCODE_RUNTIME_NAME) {
    throw new InvalidArgumentError("serve: true sólo existe para el runtime opencode");
  }
  const steps = [...rt.warmupSteps({ serve })];
  return serve ? [placeholderConfigStep(), ...steps, serveReadyStep()] : steps;
}
