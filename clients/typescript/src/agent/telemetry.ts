/**
 * Atributos del span `rayito.agent.run` (`ai-agent-core`, design.md §7).
 * Espejo de `rayito._agent._telemetry`: sólo existen con `tracerProvider`
 * (`otel.ts`); sin la opción, `NOOP.span()` ejecuta el callback
 * directamente y ninguna de estas funciones se llama con un `Span` real.
 * Los nombres `gen_ai.*` siguen la convención semántica de OpenTelemetry
 * para agentes; nunca llevan el prompt, el texto de una respuesta ni
 * argumentos de herramienta.
 */

import type { RayitoSpanAttributes } from "../otel.js";
import type { AgentSpec, ModelProvider } from "./domain.js";
import type { AgentFailed, Done, TokenUsage } from "./events.js";

const PROVIDER_NAMES: Readonly<Record<ModelProvider, string>> = {
  bedrock: "aws.bedrock",
  anthropic: "anthropic",
  "openai-compatible": "openai",
};

/** Atributos conocidos antes de ejecutar nada: el modelo pedido, su
 * proveedor y qué runtime lo corre. */
export function startAttributes(spec: AgentSpec, runtimeName: string): RayitoSpanAttributes {
  return {
    "gen_ai.operation.name": "invoke_agent",
    "gen_ai.provider.name": PROVIDER_NAMES[spec.model.provider],
    "gen_ai.request.model": spec.model.id,
    "gen_ai.agent.name": runtimeName,
    "rayito.agent.runtime": runtimeName,
  };
}

/** Atributos de una ejecución terminada (con éxito o no): tokens y estado,
 * nunca el texto producido. */
export function resultAttributes(options: {
  readonly sessionId: string;
  readonly steps: number;
  readonly exitCode: number;
  readonly usage: TokenUsage;
  readonly attached: boolean;
}): RayitoSpanAttributes {
  return {
    "gen_ai.conversation.id": options.sessionId,
    "gen_ai.usage.input_tokens": options.usage.input,
    "gen_ai.usage.output_tokens": options.usage.output,
    "rayito.agent.steps": options.steps,
    "rayito.agent.exit_code": options.exitCode,
    "rayito.agent.attached": options.attached,
    "rayito.agent.cache_read_tokens": options.usage.cacheRead,
    "rayito.agent.cache_write_tokens": options.usage.cacheWrite,
  };
}

export function doneAttributes(
  done: Done,
  options: { readonly steps: number; readonly attached: boolean },
): RayitoSpanAttributes {
  return resultAttributes({
    sessionId: done.sessionId,
    steps: options.steps,
    exitCode: done.exitCode,
    usage: done.usage,
    attached: options.attached,
  });
}

export function failureAttributes(failed: AgentFailed): RayitoSpanAttributes {
  return { "rayito.agent.failure_reason": failed.reason };
}
