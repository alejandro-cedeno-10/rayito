"""Agente de IA dentro del sandbox (`ai-agent-core`, ADR-025). Ver
`_domain.py` (puro: `AgentSpec`, `AgentModel`, permisos, límites),
`_events.py` (eventos, `TokenUsage`, `AgentResult`, tabla de fallos) y
`_gateways.py` (pasarelas ya hechas para el modelo)."""

from __future__ import annotations

from rayito._agent._domain import (
    DEFAULT_AGENT_RUNTIME,
    DEFAULT_DENIED_TOOLS,
    RESERVED_CONFIG_KEYS,
    AgentLimits,
    AgentModel,
    AgentPermissions,
    AgentSpec,
    McpLocal,
    McpRemote,
    McpServer,
    SubAgent,
)
from rayito._agent._events import (
    AGENT_EVENT_TYPES,
    AGENT_FAILURE_MESSAGES,
    AGENT_FAILURE_REASONS,
    AgentEvent,
    AgentFailed,
    AgentResult,
    Done,
    Reasoning,
    StepFinished,
    StepStarted,
    Text,
    TextDelta,
    TokenUsage,
    ToolCall,
    failure_message,
    truncate_tool_output,
)
from rayito._agent._gateways import (
    anthropic_gateway,
    bedrock_gateway,
    openai_compatible_gateway,
)

__all__ = [
    "AGENT_EVENT_TYPES",
    "AGENT_FAILURE_MESSAGES",
    "AGENT_FAILURE_REASONS",
    "DEFAULT_AGENT_RUNTIME",
    "DEFAULT_DENIED_TOOLS",
    "RESERVED_CONFIG_KEYS",
    "AgentEvent",
    "AgentFailed",
    "AgentLimits",
    "AgentModel",
    "AgentPermissions",
    "AgentResult",
    "AgentSpec",
    "Done",
    "McpLocal",
    "McpRemote",
    "McpServer",
    "Reasoning",
    "StepFinished",
    "StepStarted",
    "SubAgent",
    "Text",
    "TextDelta",
    "TokenUsage",
    "ToolCall",
    "anthropic_gateway",
    "bedrock_gateway",
    "failure_message",
    "openai_compatible_gateway",
    "truncate_tool_output",
]
