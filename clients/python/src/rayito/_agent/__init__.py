"""Agente de IA dentro del sandbox (`ai-agent-core`, ADR-025, ADR-028). Ver
`_domain.py` (puro: `AgentSpec`, `AgentModel`, permisos, límites),
`_events.py` (eventos, `TokenUsage`, `AgentResult`, tabla de fallos) y
`_gateways.py` (pasarelas ya hechas para el modelo), `_runtime.py` (los puertos
`AgentRuntime`, `InstallableRuntime` y `ServableRuntime`) y
`_project_domain.py` (`AgentProject`)."""

from __future__ import annotations

from rayito._agent._custom import CustomRuntime, HttpApp
from rayito._agent._deepagents import DeepAgents
from rayito._agent._deploy_facade import (
    deploy_agent,
    deploy_agent_async,
    plan_deploy,
)
from rayito._agent._deploy_plan import (
    AgentDeployCostEstimate,
    AgentDeployOptions,
    AgentDeployPlan,
    AgentDeployStep,
    AgentPlannedSecret,
)
from rayito._agent._deployment import AgentDeployment, AgentDeploymentStream
from rayito._agent._deployment_async import AsyncAgentDeployment, AsyncAgentDeploymentStream
from rayito._agent._deployment_base import DeploymentStatus
from rayito._agent._detect_domain import DetectOverrides
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
from rayito._agent._export import AgentExport
from rayito._agent._fleet import AgentFleet, AgentFleetStream
from rayito._agent._fleet_async import AsyncAgentFleet, AsyncAgentFleetStream
from rayito._agent._fleet_base import FleetMemberStatus, FleetStatus
from rayito._agent._gateways import (
    anthropic_gateway,
    azure_openai_gateway,
    bedrock_gateway,
    deepseek_gateway,
    gemini_gateway,
    groq_gateway,
    litellm_gateway,
    mistral_gateway,
    openai_compatible_gateway,
    openai_gateway,
    openrouter_gateway,
    xai_gateway,
)
from rayito._agent._hermes import HermesRuntime
from rayito._agent._project_domain import AgentProject, AsyncAgentProject, ServeOptions
from rayito._agent._runtime import (
    DirectWiring,
    GatewayWiring,
    HostConfig,
    InstallableRuntime,
    InstallTarget,
    ModelWiring,
    ServableRuntime,
    WarmupStep,
    is_installable,
    is_servable,
)
from rayito._agent._scaling_domain import ScalingOptions
from rayito._agent._strands import StrandsRuntime
from rayito._agent._template import AgentTemplate, AsyncAgentTemplate
from rayito._agent._warmup import agent_pool_warmup

__all__ = [
    "AGENT_EVENT_TYPES",
    "AGENT_FAILURE_MESSAGES",
    "AGENT_FAILURE_REASONS",
    "DEFAULT_AGENT_RUNTIME",
    "DEFAULT_DENIED_TOOLS",
    "RESERVED_CONFIG_KEYS",
    "AgentDeployCostEstimate",
    "AgentDeployOptions",
    "AgentDeployPlan",
    "AgentDeployStep",
    "AgentDeployment",
    "AgentDeploymentStream",
    "AgentEvent",
    "AgentExport",
    "AgentFailed",
    "AgentFleet",
    "AgentFleetStream",
    "AgentLimits",
    "AgentModel",
    "AgentPermissions",
    "AgentPlannedSecret",
    "AgentProject",
    "AgentResult",
    "AgentSpec",
    "AgentTemplate",
    "AsyncAgentDeployment",
    "AsyncAgentDeploymentStream",
    "AsyncAgentFleet",
    "AsyncAgentFleetStream",
    "AsyncAgentProject",
    "AsyncAgentTemplate",
    "CustomRuntime",
    "DeepAgents",
    "DeploymentStatus",
    "DetectOverrides",
    "DirectWiring",
    "Done",
    "FleetMemberStatus",
    "FleetStatus",
    "GatewayWiring",
    "HermesRuntime",
    "HostConfig",
    "HttpApp",
    "InstallTarget",
    "InstallableRuntime",
    "McpLocal",
    "McpRemote",
    "McpServer",
    "ModelWiring",
    "Reasoning",
    "ScalingOptions",
    "ServableRuntime",
    "ServeOptions",
    "StepFinished",
    "StepStarted",
    "StrandsRuntime",
    "SubAgent",
    "Text",
    "TextDelta",
    "TokenUsage",
    "ToolCall",
    "WarmupStep",
    "agent_pool_warmup",
    "anthropic_gateway",
    "azure_openai_gateway",
    "bedrock_gateway",
    "deepseek_gateway",
    "deploy_agent",
    "deploy_agent_async",
    "failure_message",
    "gemini_gateway",
    "groq_gateway",
    "is_installable",
    "is_servable",
    "litellm_gateway",
    "mistral_gateway",
    "openai_compatible_gateway",
    "openai_gateway",
    "openrouter_gateway",
    "plan_deploy",
    "truncate_tool_output",
    "xai_gateway",
]
