"""Dominio del agente de IA (`ai-agent-core`): validación de `AgentModel`,
`AgentPermissions`, `AgentSpec` y `AgentLimits` contra los vectores
compartidos con TypeScript (`testdata/agent/domain-vectors.json`), y
pasarelas ya hechas."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

import rayito
from rayito import (
    AgentLimits,
    AgentModel,
    AgentPermissions,
    AgentSpec,
    InvalidArgumentException,
    McpLocal,
    McpRemote,
    McpServer,
    SecretGateway,
    SubAgent,
    anthropic_gateway,
    bedrock_gateway,
    openai_compatible_gateway,
)
from rayito._agent import DEFAULT_DENIED_TOOLS, RESERVED_CONFIG_KEYS
from rayito._limits import (
    DEFAULT_AGENT_MAX_OUTPUT_BYTES,
    DEFAULT_AGENT_MAX_STEPS,
    DEFAULT_AGENT_MAX_TOTAL_TOKENS,
    DEFAULT_AGENT_TIMEOUT_SECONDS,
)
from rayito._secret_gateway._domain import is_safe_request_path

VECTORS: dict[str, Any] = json.loads(
    (Path(__file__).parents[4] / "testdata" / "agent" / "domain-vectors.json").read_text(
        encoding="utf-8"
    )
)
BEDROCK = AgentModel(
    provider="bedrock",
    id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
    gateway="bedrock",
    region="us-east-1",
)


def _mcp(raw: Mapping[str, Any]) -> dict[str, McpServer]:
    servers: dict[str, McpServer] = {}
    for name, entry in raw.items():
        if "remote" in entry:
            servers[name] = McpRemote(**entry["remote"])
        else:
            servers[name] = McpLocal(**entry["local"])
    return servers


@pytest.mark.parametrize("args", VECTORS["models"]["valid"])
def test_valid_models(args: dict[str, Any]) -> None:
    model = AgentModel(**args)
    assert model.id == args["id"]


@pytest.mark.parametrize("case", VECTORS["models"]["invalid"], ids=lambda case: str(case["why"]))
def test_invalid_models(case: dict[str, Any]) -> None:
    with pytest.raises(InvalidArgumentException):
        AgentModel(**case["args"])


@pytest.mark.parametrize("case", VECTORS["permissions"]["valid"])
def test_effective_permissions(case: dict[str, Any]) -> None:
    effective = AgentPermissions(**case["args"]).effective_tools()
    normalised = {
        tool: dict(rule) if isinstance(rule, Mapping) else rule for tool, rule in effective.items()
    }
    assert normalised == case["effective"]


@pytest.mark.parametrize(
    "case", VECTORS["permissions"]["invalid"], ids=lambda case: str(case["why"])
)
def test_invalid_permissions(case: dict[str, Any]) -> None:
    with pytest.raises(InvalidArgumentException):
        AgentPermissions(**case["args"])


def test_ask_is_rejected_with_a_reason() -> None:
    with pytest.raises(InvalidArgumentException, match="'ask' no se admite"):
        AgentPermissions(tools={"edit": "ask"})  # type: ignore[dict-item]


def test_default_denied_tools_match_the_vectors() -> None:
    assert set(DEFAULT_DENIED_TOOLS) == set(VECTORS["permissions"]["valid"][0]["effective"])


def test_permissions_are_frozen_copies() -> None:
    tools: dict[str, Any] = {"edit": "deny"}
    permissions = AgentPermissions(tools=tools)
    tools["edit"] = "allow"
    assert permissions.tools["edit"] == "deny"
    with pytest.raises(TypeError):
        permissions.tools["edit"] = "allow"  # type: ignore[index]


def test_reserved_config_keys_match_the_vectors() -> None:
    assert list(RESERVED_CONFIG_KEYS) == VECTORS["raw_config"]["reserved"]


@pytest.mark.parametrize("key", VECTORS["raw_config"]["reserved"])
def test_raw_config_rejects_reserved_keys_by_name(key: str) -> None:
    with pytest.raises(InvalidArgumentException, match=repr(key)):
        AgentSpec(model=BEDROCK, raw_config={key: {}})


@pytest.mark.parametrize("raw_config", VECTORS["raw_config"]["allowed"])
def test_raw_config_accepts_other_keys(raw_config: dict[str, object]) -> None:
    spec = AgentSpec(model=BEDROCK, raw_config=raw_config)
    assert dict(spec.raw_config or {}) == raw_config


@pytest.mark.parametrize("case", VECTORS["spec"]["invalid"], ids=lambda case: str(case["why"]))
def test_invalid_specs(case: dict[str, Any]) -> None:
    with pytest.raises(InvalidArgumentException):
        agents = {name: SubAgent(**args) for name, args in case.get("agents", {}).items()}
        AgentSpec(
            model=BEDROCK,
            agents=agents,
            mcp=_mcp(case.get("mcp", {})),
            instructions=case.get("instructions"),
        )


def test_small_model_defaults_to_the_model() -> None:
    assert AgentSpec(model=BEDROCK).effective_small_model == BEDROCK.id
    assert AgentSpec(model=BEDROCK, small_model="other").effective_small_model == "other"


def test_gateway_names_and_missing_gateway() -> None:
    vector = VECTORS["spec"]["gateways"]
    spec = AgentSpec(model=BEDROCK, mcp=_mcp(vector["mcp"]))
    assert spec.gateway_names() == frozenset(vector["needed"])
    spec.require_gateways(["bedrock", "docs-mcp", "unused"])
    with pytest.raises(InvalidArgumentException, match="'docs-mcp'"):
        spec.require_gateways(["bedrock"])


def test_mcp_local_hides_env_values_in_repr() -> None:
    server = McpLocal(command=["mcp-fs"], envs={"TOKEN_FILE": "value-that-stays-out"})
    assert "value-that-stays-out" not in repr(server)
    assert server.command == ("mcp-fs",)


def test_limits_defaults_come_from_limits_json() -> None:
    limits = AgentLimits()
    defaults = VECTORS["limits"]["defaults"]
    assert limits.max_steps == DEFAULT_AGENT_MAX_STEPS == defaults["max_steps"]
    assert limits.timeout_seconds == DEFAULT_AGENT_TIMEOUT_SECONDS == defaults["timeout_seconds"]
    assert limits.max_output_bytes == DEFAULT_AGENT_MAX_OUTPUT_BYTES == defaults["max_output_bytes"]
    assert limits.max_total_tokens == DEFAULT_AGENT_MAX_TOTAL_TOKENS == defaults["max_total_tokens"]
    assert AgentLimits(max_total_tokens=None).max_total_tokens is None


@pytest.mark.parametrize("args", VECTORS["limits"]["invalid"])
def test_invalid_limits(args: dict[str, Any]) -> None:
    with pytest.raises(InvalidArgumentException):
        AgentLimits(**args)


def test_bedrock_preset_matches_the_vectors() -> None:
    vector = VECTORS["gateway_presets"]["bedrock"]
    gateway = bedrock_gateway("bedrock-key", region=vector["region"], models=vector["models"])
    _assert_gateway(gateway, vector)
    assert all(is_safe_request_path(path) for _, path in gateway.allow)


def test_bedrock_preset_dedupes_models() -> None:
    gateway = bedrock_gateway("k", region="us-east-1", models=["m", "m"])
    assert len(gateway.allow) == 2


@pytest.mark.parametrize(
    "case",
    VECTORS["gateway_presets"]["bedrock_invalid"],
    ids=lambda case: str(case["why"]),
)
def test_bedrock_preset_rejects(case: dict[str, Any]) -> None:
    with pytest.raises(InvalidArgumentException):
        bedrock_gateway("k", region=case["region"], models=case["models"])


def test_anthropic_preset_matches_the_vectors() -> None:
    _assert_gateway(anthropic_gateway("anthropic"), VECTORS["gateway_presets"]["anthropic"])


def test_openai_compatible_preset_matches_the_vectors() -> None:
    vector = VECTORS["gateway_presets"]["openai_compatible"]
    gateway = openai_compatible_gateway(
        "openai", upstream=vector["upstream"], base_path=vector["base_path"]
    )
    _assert_gateway(gateway, vector)
    with pytest.raises(InvalidArgumentException):
        openai_compatible_gateway("openai", upstream=vector["upstream"], base_path="v1")


def test_presets_carry_the_rate_limit() -> None:
    assert anthropic_gateway("a", rate_per_minute=60).rate_per_minute == 60


def _assert_gateway(gateway: SecretGateway, vector: dict[str, Any]) -> None:
    assert gateway.upstream == vector["upstream"]
    assert list(gateway.headers) == vector["headers"]
    assert [list(rule) for rule in gateway.allow] == vector["allow"]


def test_agent_names_are_public() -> None:
    for name in (
        "AgentSpec",
        "AgentModel",
        "AgentException",
        "bedrock_gateway",
        "TokenUsage",
        "AgentResult",
    ):
        assert name in rayito.__all__
