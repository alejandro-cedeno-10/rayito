"""Prueba de humo opcional contra las APIs reales de cada proveedor de modelo
(`make local-providers-smoke`): una vuelta corta de OpenCode y de deepagents
por el preset de pasarela, con el egress del sandbox cerrado.

Cada preset se salta si no defines su clave. Variables (en el host; el
Makefile las pasa al runner por nombre, nunca por valor):

- `RAYITO_SMOKE_<PRESET>_SECRET`: la clave de API, sin `Bearer` (el test
  lo añade cuando la cabecera es `authorization`).
- `RAYITO_SMOKE_<PRESET>_MODEL`: el id del modelo (o del despliegue en
  Azure). Obligatorio si hay clave: elige el más barato de tu cuenta.
- `RAYITO_SMOKE_AZURE_OPENAI_RESOURCE`: el subdominio del recurso de Azure.
- `RAYITO_SMOKE_LITELLM_UPSTREAM`: `https://host[:puerto]` de tu proxy.

`<PRESET>` es el nombre del catálogo en mayúsculas: `OPENAI`, `GEMINI`,
`AZURE_OPENAI`, `OPENROUTER`, `GROQ`, `MISTRAL`, `DEEPSEEK`, `XAI` y
`LITELLM`.

Coste: cada ejecución lleva `AgentLimits(max_total_tokens=SMOKE_TOKEN_BUDGET)`
y el prompt no usa herramientas; con un modelo de hasta
`SMOKE_MAX_PRICE_PER_MILLION_INPUT` USD por millón de tokens de entrada, los
dos runtimes juntos cuestan menos de 0,01 USD por proveedor. Corre sobre
`make local-agent-up` (sin el upstream falso, que secuestra los nombres de
los proveedores). La clave entra en el Secrets Manager de Floci y sólo
`rayd` la lee; nunca se imprime.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Final

import boto3
import pytest

from rayito import AgentLimits, AgentResult

from .conftest import LocalSettings
from .guest import LocalGuestControlPlane
from .providers import (
    PRESETS,
    PROMPT,
    RUNTIMES,
    Provider,
    ProviderBox,
    deepagents_supported,
    preset_entry,
    provider_box,
    secret_value_for,
)
from .test_local_agent_providers_fake import FAKE_UPSTREAM_ADMIN_VAR

pytestmark = pytest.mark.local

SMOKE_VAR_PREFIX: Final = "RAYITO_SMOKE_"
#: Tope de tokens por ejecución: el prompt de sistema de OpenCode con sus
#: herramientas ronda los 8 000 tokens (medido con el upstream falso, cuerpo
#: de ~29 KB) y el de deepagents los 3 000.
SMOKE_TOKEN_BUDGET: Final = 20_000
#: Con este precio de entrada (USD por millón de tokens), dos ejecuciones al
#: tope cuestan 2 x 20 000 x 0,20 / 1e6 = 0,008 USD.
SMOKE_MAX_PRICE_PER_MILLION_INPUT: Final = 0.20
SMOKE_TIMEOUT_SECONDS: Final = 180
#: Los argumentos del preset que salen del entorno en vez del catálogo.
ENV_ARGS: Final = {
    "azure_openai": {"resource": "RESOURCE"},
    "litellm": {"upstream": "UPSTREAM"},
}


def smoke_var(name: str, suffix: str) -> str:
    return f"{SMOKE_VAR_PREFIX}{name.upper()}_{suffix}"


def smoke_provider(name: str) -> Provider | None:
    """El preset con la clave y el modelo del entorno, o None si no hay
    clave."""
    key = os.environ.get(smoke_var(name, "SECRET"))
    if not key:
        return None
    model = os.environ.get(smoke_var(name, "MODEL"))
    if not model:
        pytest.fail(f"{smoke_var(name, 'MODEL')} es obligatoria con {smoke_var(name, 'SECRET')}")
    entry = preset_entry(name)
    args = dict(entry["args"])
    for arg, suffix in ENV_ARGS.get(name, {}).items():
        value = os.environ.get(smoke_var(name, suffix))
        if not value:
            pytest.fail(f"{smoke_var(name, suffix)} es obligatoria con {name}")
        args[arg] = value
    if "models" in args:
        args["models"] = [model]
    return Provider(name, secret_value_for(entry["headers"][0], key), args, model)


@pytest.fixture(scope="module", params=PRESETS)
def box(
    request: pytest.FixtureRequest,
    local_settings: LocalSettings,
    control_plane: LocalGuestControlPlane,
    template_arn: str,
    aws_session: boto3.session.Session,
) -> Iterator[ProviderBox]:
    name: str = request.param
    if os.environ.get(FAKE_UPSTREAM_ADMIN_VAR):
        pytest.skip("el upstream falso está levantado: usa make local-agent-up")
    provider = smoke_provider(name)
    if provider is None:
        pytest.skip(f"sin {smoke_var(name, 'SECRET')}")
    with provider_box(provider, local_settings, control_plane, template_arn, aws_session) as built:
        yield built


@pytest.mark.parametrize("runtime", RUNTIMES)
def test_real_api_turn(box: ProviderBox, runtime: str) -> None:
    provider = box.provider
    if runtime == "deepagents" and not deepagents_supported(provider.entry):
        pytest.skip(f"deepagents no admite {provider.entry['model_provider']}")
    result: AgentResult = box.sandbox.agent.run(
        PROMPT,
        spec=provider.spec(),
        runtime=runtime,
        limits=AgentLimits(
            max_total_tokens=SMOKE_TOKEN_BUDGET, timeout_seconds=SMOKE_TIMEOUT_SECONDS
        ),
    )
    assert result.text.strip()
    assert result.usage.input > 0
    assert result.usage.output > 0
