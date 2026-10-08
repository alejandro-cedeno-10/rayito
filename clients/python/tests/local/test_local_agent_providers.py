"""Los presets de pasarela de cada proveedor de modelo con OpenCode y
deepagents (`make local-providers-up` y `make local-providers-e2e`), sin
claves reales ni Internet: el upstream es el servidor falso de
`dev/local/providers/fake_upstream.py`, que contesta con las formas de Chat
Completions, Responses y Gemini y apunta lo que recibe.

Para cada preset de `testdata/agent/provider-catalogue.json` y cada runtime
que lo admite, con el egress del sandbox cerrado:

- el agente completa una vuelta por la pasarela y devuelve el texto del
  upstream falso;
- al upstream llega el valor del secreto en la cabecera del preset, y el
  marcador que ven los runtimes (`MODEL_CREDENTIAL_PLACEHOLDER`) no sale
  nunca, ni en cabeceras ni en el cuerpo;
- toda ruta que llega está en la allowlist del preset, y una que no lo está
  recibe 403 de `rayd` sin llegar al upstream.

Necesita, además de `RAYITO_LOCAL_GUEST`, `RAYITO_LOCAL_FAKE_UPSTREAM_ADMIN`
(la define `dev/local/providers/compose.yaml`); sin ella se saltan. Los
secretos son valores aleatorios en el Secrets Manager de Floci.
"""

from __future__ import annotations

import json
import os
import urllib.request
from collections.abc import Iterator
from typing import Any, Final
from urllib.parse import urlsplit

import boto3
import pytest

from rayito import AgentLimits, EgressEnforcement
from rayito._limits import MODEL_CREDENTIAL_PLACEHOLDER

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
    random_key,
    secret_value_for,
)

pytestmark = pytest.mark.local

FAKE_UPSTREAM_ADMIN_VAR: Final = "RAYITO_LOCAL_FAKE_UPSTREAM_ADMIN"
#: El texto con el que contesta siempre el upstream falso (`REPLY_TEXT`).
FAKE_REPLY: Final = "rayito-fake-ok"
AGENT_TIMEOUT_SECONDS: Final = 240
EGRESS_PROBE_TIMEOUT_SECONDS: Final = 5
ADMIN_TIMEOUT_SECONDS: Final = 10
#: Una ruta que ningún preset permite.
FORBIDDEN_PATH: Final = "/v1/models"
FORBIDDEN_STATUS: Final = 403


class FakeUpstream:
    """La API de administración del upstream falso (`ADMIN_PORT`)."""

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")

    def clear(self) -> None:
        request = urllib.request.Request(f"{self._base_url}/requests", method="DELETE")
        with urllib.request.urlopen(request, timeout=ADMIN_TIMEOUT_SECONDS):
            pass

    def received(self, host: str) -> list[dict[str, Any]]:
        with urllib.request.urlopen(
            f"{self._base_url}/requests", timeout=ADMIN_TIMEOUT_SECONDS
        ) as response:
            entries: list[dict[str, Any]] = json.loads(response.read())
        return [entry for entry in entries if entry["host"].split(":", 1)[0] == host]


@pytest.fixture(scope="module")
def fake_upstream() -> FakeUpstream:
    base_url = os.environ.get(FAKE_UPSTREAM_ADMIN_VAR)
    if not base_url:
        pytest.skip(f"sin upstream falso: make local-providers-up ({FAKE_UPSTREAM_ADMIN_VAR})")
    return FakeUpstream(base_url)


@pytest.fixture(scope="module", params=PRESETS)
def box(
    request: pytest.FixtureRequest,
    local_settings: LocalSettings,
    control_plane: LocalGuestControlPlane,
    template_arn: str,
    aws_session: boto3.session.Session,
    fake_upstream: FakeUpstream,
) -> Iterator[ProviderBox]:
    """Un sandbox por preset: el guest local aloja uno a la vez."""
    name: str = request.param
    provider = Provider(name, secret_value_for(preset_entry(name)["headers"][0], random_key()))
    with provider_box(provider, local_settings, control_plane, template_arn, aws_session) as built:
        yield built


def test_egress_is_closed_and_other_paths_are_refused(
    box: ProviderBox, fake_upstream: FakeUpstream
) -> None:
    """Sin la pasarela el sandbox no llega al upstream (aunque su nombre
    resuelva al falso), y por la pasarela una ruta fuera de la allowlist
    recibe 403 de `rayd` sin llegar al upstream."""
    provider = box.provider
    assert box.sandbox.get_health().egress_enforcement == EgressEnforcement.GUEST_ROUTES
    code, _ = box.sh(
        f"curl -sS -o /dev/null -m {EGRESS_PROBE_TIMEOUT_SECONDS} https://{provider.host}/"
    )
    assert code != 0
    url = box.sandbox.gateways[provider.route].url
    fake_upstream.clear()
    code, status = box.sh(
        f"curl -sS -o /dev/null -w '%{{http_code}}' -X POST -d '{{}}' {url}{FORBIDDEN_PATH}"
    )
    assert code == 0
    assert int(status) == FORBIDDEN_STATUS
    assert fake_upstream.received(provider.host) == []


@pytest.mark.parametrize("runtime", RUNTIMES)
def test_agent_turn_through_the_preset(
    box: ProviderBox, fake_upstream: FakeUpstream, runtime: str
) -> None:
    """Una vuelta del agente por el preset: llega la credencial real, nunca
    el marcador, y sólo a rutas permitidas."""
    provider = box.provider
    if runtime == "deepagents" and not deepagents_supported(provider.entry):
        pytest.skip(f"deepagents no admite {provider.entry['model_provider']}")
    fake_upstream.clear()
    result = box.sandbox.agent.run(
        PROMPT,
        spec=provider.spec(),
        runtime=runtime,
        limits=AgentLimits(timeout_seconds=AGENT_TIMEOUT_SECONDS),
    )
    received = fake_upstream.received(provider.host)
    assert received, "el upstream no recibió ninguna petición"
    assert FAKE_REPLY in result.text
    paths = {urlsplit(entry["path"]).path for entry in received}
    assert paths <= provider.allowed_paths
    injected = [entry["headers"].get(provider.header) for entry in received]
    real_key_upstream = all(value == provider.secret_value for value in injected)
    assert real_key_upstream, "la cabecera del preset no llevó el valor del secreto"
    placeholder_left = [
        entry["path"]
        for entry in received
        if MODEL_CREDENTIAL_PLACEHOLDER in json.dumps(entry["headers"])
        or MODEL_CREDENTIAL_PLACEHOLDER in entry["body"]
    ]
    assert placeholder_left == []
