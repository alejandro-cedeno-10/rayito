/**
 * Registro `nombre -> AgentRuntime` (`ai-agent-core`, design.md §3):
 * `"opencode"` (`ai-agent-core`) y `"deepagents"` (`ai-agent-deepagents`). Un
 * nombre sin adaptador falla con `UnimplementedError` en vez de con un
 * error opaco. Un llamante siempre puede saltarse el registro
 * pasando su propio objeto `AgentRuntime` (lo que hacen los tests con un
 * doble).
 */

import { InvalidArgumentError, UnimplementedError } from "../errors.js";
import { DeepAgents } from "./deepagents.js";
import { OpenCodeRuntime } from "./opencode.js";
import type { AgentRuntime } from "./runtime.js";

/** Quita y pon: cada adaptador añade su entrada aquí cuando aterriza, sin
 * tocar el resto de este módulo. */
export const AGENT_RUNTIMES: Record<string, AgentRuntime> = {
  opencode: new OpenCodeRuntime(),
  deepagents: new DeepAgents(),
};

function looksLikeRuntime(candidate: unknown): candidate is AgentRuntime {
  if (typeof candidate !== "object" || candidate === null) {
    return false;
  }
  const required = ["buildConfig", "command", "newState", "parseLine", "finish", "warmupSteps"];
  return required.every((key) => typeof (candidate as Record<string, unknown>)[key] === "function");
}

/** `runtime` ya es un `AgentRuntime` (duck typing con los métodos del
 * puerto) o un nombre de `AGENT_RUNTIMES`. Un nombre desconocido es
 * `UnimplementedError`; cualquier otro valor es `InvalidArgumentError`. */
export function resolveRuntime(runtime: string | AgentRuntime): AgentRuntime {
  if (typeof runtime === "string") {
    const found = AGENT_RUNTIMES[runtime];
    if (found === undefined) {
      throw new UnimplementedError(
        `runtime: "${runtime}"`,
        "no hay ningún adaptador registrado con ese nombre",
        "https://rayito.dev/referencia/errores",
      );
    }
    return found;
  }
  if (!looksLikeRuntime(runtime)) {
    throw new InvalidArgumentError(
      "runtime debe ser un nombre registrado en AGENT_RUNTIMES o un objeto que implemente AgentRuntime",
    );
  }
  return runtime;
}
