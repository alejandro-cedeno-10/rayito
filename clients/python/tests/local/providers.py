"""Lo que comparten los tests `local` de los proveedores de modelo (contra el
upstream falso y contra las APIs reales): los presets de
`testdata/agent/provider-catalogue.json`, el `AgentSpec` de cada uno y un
sandbox con su pasarela y el egress cerrado."""

from __future__ import annotations

import contextlib
import json
import secrets as stdlib_secrets
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

import boto3

import rayito
from rayito import AgentModel, AgentSpec, Sandbox, SecretCache, SecretStore
from rayito.exceptions import CommandExitException, SandboxNotFoundException

from .conftest import LocalSettings, create_local_sandbox
from .guest import LocalGuestControlPlane

CATALOGUE_PATH: Final = (
    Path(__file__).resolve().parents[4] / "testdata" / "agent" / "provider-catalogue.json"
)
#: Un preset por proveedor (`litellm_root` es la misma pasarela con otra
#: ruta base y ya la cubren los tests unitarios).
PRESETS: Final = (
    "openai",
    "gemini",
    "azure_openai",
    "openrouter",
    "groq",
    "mistral",
    "deepseek",
    "xai",
    "litellm",
)
RUNTIMES: Final = ("opencode", "deepagents")
#: El id de modelo cuando el preset no limita el modelo (va en el cuerpo).
GENERIC_MODEL_ID: Final = "rayito-local-model"
AUTHORIZATION_HEADER: Final = "authorization"
BEARER_PREFIX: Final = "Bearer "
COMMAND_TIMEOUT_SECONDS: Final = 60
#: Lo que se le pide al agente: una respuesta corta y sin herramientas.
PROMPT: Final = "Responde sólo con la palabra 'hola', sin usar herramientas."


def load_catalogue() -> dict[str, Any]:
    catalogue: dict[str, Any] = json.loads(CATALOGUE_PATH.read_text(encoding="utf-8"))
    return catalogue


def preset_entry(name: str) -> dict[str, Any]:
    return next(entry for entry in load_catalogue()["presets"] if entry["name"] == name)


def deepagents_supported(entry: Mapping[str, Any]) -> bool:
    provider = load_catalogue()["model_providers"][entry["model_provider"]]
    return bool(provider["deepagents"] == "supported")


def secret_value_for(header: str, key: str) -> str:
    """Lo que guarda el secreto: `Bearer <clave>` en `authorization`, la
    clave tal cual en las demás cabeceras (`x-goog-api-key`, `api-key`)."""
    return f"{BEARER_PREFIX}{key}" if header == AUTHORIZATION_HEADER else key


def random_key() -> str:
    return f"rayito-local-{stdlib_secrets.token_hex(12)}"


@dataclass(frozen=True)
class Provider:
    """Un preset del catálogo; `args` y `model_id` sustituyen a los del
    catálogo (el recurso de Azure, el upstream de LiteLLM, el modelo)."""

    name: str
    secret_value: str = field(repr=False)
    args: Mapping[str, Any] | None = None
    model_override: str | None = None

    @property
    def entry(self) -> dict[str, Any]:
        return preset_entry(self.name)

    @property
    def route(self) -> str:
        """El nombre de la ruta de la pasarela (`[a-z0-9-]`)."""
        return self.name.replace("_", "-")

    @property
    def host(self) -> str:
        host = urlsplit(self.entry["upstream"]).hostname
        assert host
        return str(host)

    @property
    def header(self) -> str:
        header: str = self.entry["headers"][0]
        return header

    @property
    def allowed_paths(self) -> set[str]:
        return {path for _, path in self.entry["allow"]}

    @property
    def model_id(self) -> str:
        if self.model_override:
            return self.model_override
        models = self.entry["args"].get("models")
        return str(models[0]) if models else GENERIC_MODEL_ID

    def gateway(self, secret_name: str) -> Any:
        build = getattr(rayito, self.entry["python"])
        args = self.entry["args"] if self.args is None else self.args
        return build(secret_name, **args)

    def spec(self) -> AgentSpec:
        provider = self.entry["model_provider"]
        base_path = self.entry["base_path"] if provider == "openai-compatible" else ""
        return AgentSpec(
            model=AgentModel(
                provider=provider, id=self.model_id, gateway=self.route, base_path=base_path
            )
        )


@dataclass(frozen=True)
class ProviderBox:
    sandbox: Sandbox
    provider: Provider

    def sh(self, cmd: str) -> tuple[int, str]:
        """Una orden como `user`; (exit, stdout) sin lanzar por un exit
        distinto de cero."""
        try:
            result = self.sandbox.commands.run(cmd, timeout=COMMAND_TIMEOUT_SECONDS)
        except CommandExitException as exc:
            return exc.exit_code, exc.stdout
        return result.exit_code, result.stdout


@contextlib.contextmanager
def provider_box(
    provider: Provider,
    local_settings: LocalSettings,
    control_plane: LocalGuestControlPlane,
    template_arn: str,
    aws_session: boto3.session.Session,
) -> Iterator[ProviderBox]:
    """Un sandbox con la pasarela del preset y `allow_internet_access=False`;
    el secreto vive en el Secrets Manager de Floci mientras dura."""
    store = SecretStore(session=aws_session)
    secret_name = f"local-providers-{provider.route}-{stdlib_secrets.token_hex(4)}"
    store.create(secret_name, provider.secret_value)
    try:
        sandbox = create_local_sandbox(
            local_settings,
            control_plane,
            template_arn,
            allow_internet_access=False,
            gateways={provider.route: provider.gateway(secret_name)},
            secret_cache=SecretCache(store=store),
        )
        try:
            box = ProviderBox(sandbox, provider)
            if box.sh("command -v opencode")[0] != 0:
                raise AssertionError("el guest no trae los agentes: make local-agent-up")
            yield box
        finally:
            with contextlib.suppress(SandboxNotFoundException):
                sandbox.kill()
    finally:
        store.destroy(secret_name)
