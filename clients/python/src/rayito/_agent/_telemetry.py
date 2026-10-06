"""Atributos del span `rayito.agent.run` (`ai-agent-core`, design.md §7).
Sólo existen con `tracer_provider=` (`_otel.py`): sin la opción,
`_sandbox._instrumentation` es `NOOP` y ninguna de estas funciones se
llama con un `Span` real. Los nombres `gen_ai.*` siguen la convención
semántica de OpenTelemetry para agentes de IA (`gen_ai.operation.name`,
`gen_ai.provider.name`, `gen_ai.usage.*`); nunca llevan el prompt, el texto
de una respuesta ni argumentos de herramienta."""

from __future__ import annotations

from typing import Any

from rayito._agent._domain import AgentSpec
from rayito._agent._events import AgentFailed, Done, TokenUsage

#: `gen_ai.provider.name` según el `provider` de `AgentModel`.
_PROVIDER_NAMES: dict[str, str] = {
    "bedrock": "aws.bedrock",
    "anthropic": "anthropic",
    "openai-compatible": "openai",
}


def start_attributes(spec: AgentSpec, *, runtime_name: str) -> dict[str, Any]:
    """Atributos conocidos antes de ejecutar nada: el modelo pedido, su
    proveedor y qué runtime lo corre."""
    return {
        "gen_ai.operation.name": "invoke_agent",
        "gen_ai.provider.name": _PROVIDER_NAMES.get(spec.model.provider, spec.model.provider),
        "gen_ai.request.model": spec.model.id,
        "gen_ai.agent.name": runtime_name,
        "rayito.agent.runtime": runtime_name,
    }


def result_attributes(
    *,
    session_id: str,
    steps: int,
    exit_code: int,
    usage: TokenUsage,
    attached: bool,
) -> dict[str, Any]:
    """Atributos de una ejecución terminada (con éxito o no): tokens y
    estado, nunca el texto producido."""
    attrs = {
        "gen_ai.conversation.id": session_id,
        "gen_ai.usage.input_tokens": usage.input,
        "gen_ai.usage.output_tokens": usage.output,
        "rayito.agent.steps": steps,
        "rayito.agent.exit_code": exit_code,
        "rayito.agent.attached": attached,
        "rayito.agent.cache_read_tokens": usage.cache_read,
        "rayito.agent.cache_write_tokens": usage.cache_write,
    }
    return attrs


def failure_attributes(failed: AgentFailed) -> dict[str, Any]:
    return {"rayito.agent.failure_reason": failed.reason}


def done_attributes(done: Done, *, steps: int, attached: bool) -> dict[str, Any]:
    return result_attributes(
        session_id=done.session_id,
        steps=steps,
        exit_code=done.exit_code,
        usage=done.usage,
        attached=attached,
    )
