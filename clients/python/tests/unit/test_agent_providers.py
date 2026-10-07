"""Catálogo de proveedores del agente (`ai-agent-providers`): los presets de
pasarela, `ModelProvider` y su traducción a OpenCode y deepagents, contra
`testdata/agent/provider-catalogue.json`, el mismo fichero que lee el test
de TypeScript. Nada aquí llama a AWS ni a la red."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import boto3
import pytest

import rayito
from rayito import AgentModel, AgentSpec, InvalidArgumentException, SecretGateway
from rayito._agent._deepagents import DeepAgents
from rayito._agent._domain import MODEL_PROVIDERS
from rayito._agent._opencode import OPENCODE_CONFIG_PATH, OpenCodeRuntime
from rayito._agent._telemetry import start_attributes
from rayito._secret_gateway._domain import is_safe_request_path
from rayito.exceptions import UnimplementedError

CATALOGUE: dict[str, Any] = json.loads(
    (Path(__file__).parents[4] / "testdata" / "agent" / "provider-catalogue.json").read_text(
        encoding="utf-8"
    )
)
SECRET: str = CATALOGUE["secret"]
GATEWAY_URL = "http://127.0.0.1:18005"
PLACEHOLDER = "placeholder-not-a-secret"


def _build(entry: dict[str, Any]) -> SecretGateway:
    preset = getattr(rayito, entry["python"])
    gateway: SecretGateway = preset(SECRET, **entry["args"])
    return gateway


def _spec(provider: str, base_path: str = "") -> AgentSpec:
    return AgentSpec(
        model=AgentModel(provider=provider, id="modelo-a", gateway="modelo", base_path=base_path)  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("entry", CATALOGUE["presets"], ids=lambda entry: str(entry["name"]))
def test_preset_matches_the_catalogue(entry: dict[str, Any]) -> None:
    gateway = _build(entry)
    assert gateway.upstream == entry["upstream"]
    assert list(gateway.headers) == entry["headers"]
    assert all(value == SECRET for value in gateway.headers.values())
    assert [list(rule) for rule in gateway.allow] == entry["allow"]
    assert all(is_safe_request_path(path) for _, path in gateway.allow)
    assert gateway.rate_per_minute == 0


@pytest.mark.parametrize("entry", CATALOGUE["presets"], ids=lambda entry: str(entry["name"]))
def test_preset_pairs_with_its_agent_model(entry: dict[str, Any]) -> None:
    """El `ModelProvider` y el `base_path` del catálogo forman un
    `AgentModel` válido, y su ruta casa con la allowlist del preset."""
    model = AgentModel(
        provider=entry["model_provider"],
        id="modelo-a",
        gateway="modelo",
        base_path=entry["base_path"],
    )
    if model.provider == "openai-compatible":
        assert any(path.startswith(f"{model.base_path}/") for _, path in entry["allow"])


@pytest.mark.parametrize("entry", CATALOGUE["presets"], ids=lambda entry: str(entry["name"]))
def test_only_path_models_are_allowlisted(entry: dict[str, Any]) -> None:
    """Gemini lleva el modelo en la ruta; las APIs al estilo de OpenAI, en
    el cuerpo (SECURITY.md T29)."""
    models = entry["args"].get("models", [])
    assert entry["model_allowlist"] is bool(models)
    for model_id in models:
        assert any(f"/{model_id}:" in path for _, path in entry["allow"])


def test_presets_carry_the_rate_limit() -> None:
    assert rayito.openai_gateway(SECRET, rate_per_minute=30).rate_per_minute == 30
    assert (
        rayito.litellm_gateway(
            SECRET, upstream="https://litellm.example.com", rate_per_minute=30
        ).rate_per_minute
        == 30
    )


@pytest.mark.parametrize("case", CATALOGUE["invalid"], ids=lambda case: str(case["why"]))
def test_preset_rejects(case: dict[str, Any]) -> None:
    with pytest.raises(InvalidArgumentException):
        getattr(rayito, case["python"])(SECRET, **case["args"])


def test_presets_are_public() -> None:
    for entry in CATALOGUE["presets"]:
        assert entry["python"] in rayito.__all__


def test_model_providers_match_the_catalogue() -> None:
    assert set(MODEL_PROVIDERS) == set(CATALOGUE["model_providers"])
    refused = {entry["id"] for entry in CATALOGUE["refused"]}
    assert refused.isdisjoint(MODEL_PROVIDERS)
    with pytest.raises(InvalidArgumentException):
        AgentModel(provider="chatgpt-plan", id="m", gateway="modelo")  # type: ignore[arg-type]


@pytest.mark.parametrize("provider", sorted(CATALOGUE["model_providers"]))
def test_otel_provider_name(provider: str) -> None:
    expected = CATALOGUE["model_providers"][provider]["otel_provider"]
    model = AgentModel(
        provider=provider,  # type: ignore[arg-type]
        id="modelo-a",
        gateway="modelo",
        region="us-east-1" if provider == "bedrock" else None,
    )
    attributes = start_attributes(AgentSpec(model=model), runtime_name="opencode")
    assert attributes["gen_ai.provider.name"] == expected


NATIVE = sorted(
    name for name, entry in CATALOGUE["model_providers"].items() if "opencode_base_path" in entry
)


@pytest.mark.parametrize("provider", NATIVE)
def test_opencode_native_provider_goes_through_the_gateway(provider: str) -> None:
    entry = CATALOGUE["model_providers"][provider]
    files = OpenCodeRuntime().build_config(
        _spec(provider), gateway_urls={"modelo": GATEWAY_URL}, workdir="/home/user"
    )
    config = json.loads(files.files[0].data)
    provider_id = entry["opencode_provider_id"]
    assert config["enabled_providers"] == [provider_id]
    assert config["model"] == f"{provider_id}/modelo-a"
    assert config["provider"] == {
        provider_id: {
            "options": {
                "baseURL": GATEWAY_URL + entry["opencode_base_path"],
                "apiKey": PLACEHOLDER,
            },
            "models": {"modelo-a": {}},
        }
    }


@pytest.mark.parametrize("provider", NATIVE)
def test_opencode_never_ships_an_auth_file(provider: str) -> None:
    """Las suscripciones rechazadas (plan de ChatGPT, Claude Pro/Max,
    Copilot, SuperGrok) entran en OpenCode por su `auth.json`: el adaptador
    nunca escribe uno ni una clave `auth`/`plugin` en la configuración."""
    files = OpenCodeRuntime().build_config(
        _spec(provider), gateway_urls={"modelo": GATEWAY_URL}, workdir="/home/user"
    )
    assert [item.path for item in files.files] == [OPENCODE_CONFIG_PATH]
    config = json.loads(files.files[0].data)
    assert "auth" not in config
    assert "plugin" not in config


@pytest.mark.parametrize("key", ["provider", "enabled_providers"])
def test_raw_config_cannot_bring_another_provider(key: str) -> None:
    with pytest.raises(InvalidArgumentException):
        AgentSpec(
            model=AgentModel(provider="openai", id="modelo-a", gateway="modelo"),
            raw_config={key: {"openai": {}}},
        )


@pytest.mark.parametrize("provider", NATIVE)
def test_deepagents_native_provider(provider: str) -> None:
    entry = CATALOGUE["model_providers"][provider]
    runtime = DeepAgents()
    if entry["deepagents"] == "unimplemented":
        with pytest.raises(UnimplementedError):
            runtime.build_config(
                _spec(provider), gateway_urls={"modelo": GATEWAY_URL}, workdir="/home/user"
            )
        return
    files = runtime.build_config(
        _spec(provider), gateway_urls={"modelo": GATEWAY_URL}, workdir="/home/user"
    )
    config = json.loads(files.files[0].data)
    assert config["provider"] == provider
    assert config["base_url"] == GATEWAY_URL + entry["deepagents_base_path"]
    assert config["credential_placeholder"] == PLACEHOLDER


def test_building_presets_creates_no_aws_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Coste cero: un preset sólo describe la pasarela; Secrets Manager se
    lee al aplicarla en `Sandbox.create(gateways=...)`."""
    calls: list[str] = []

    def spy(
        self: boto3.session.Session, service_name: str, *args: object, **kwargs: object
    ) -> None:
        calls.append(service_name)

    monkeypatch.setattr(boto3.session.Session, "client", spy)
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: calls.append("default"))
    for entry in CATALOGUE["presets"]:
        _build(entry)
    assert calls == []
