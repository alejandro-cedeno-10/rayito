"""`sbx.agent` con proveedores al estilo de OpenAI contra modelos reales
(`make local-e2e`), sin claves de terceros: OpenCode y deepagents en el
guest de `dev/local/agent/compose.yaml` (`make local-agent-up`) con el
egress cerrado y la pasarela de secretos como única salida.

Cada ruta es un camino de proveedor del SDK probado de verdad:

- `bedrock-chat`: `openai_compatible_gateway` hacia la Chat Completions
  compatible con OpenAI de `bedrock-runtime` (`/openai/v1`) con gpt-oss.
- `mantle-chat`: la misma pasarela hacia `bedrock-mantle` (`/v1`).
- `mantle-responses`: `AgentModel(provider="openai")` (la Responses API
  de `@ai-sdk/openai` y de `ChatOpenAI(use_responses_api=True)`) contra
  `bedrock-mantle`, con la allowlist de `openai_gateway` (`/v1/responses`
  y `/v1/chat/completions`) y otro upstream. Sólo una vuelta sin
  herramientas: `bedrock-mantle` se aparta de la API de OpenAI en dos
  puntos que rompen el resto (medido el 2026-10-07): el
  `response.output_item.done` de una `function_call` llega con `id: null`
  (OpenCode no casa los argumentos y aborta la herramienta) y rechaza un
  mensaje de asistente reenviado sin `id` ni `status` (la continuación de
  sesión de deepagents).
- `litellm`: `litellm_gateway` hacia un proxy de LiteLLM propio que
  alcanza el guest por HTTPS (con una CA de prueba en el almacén del
  sistema del guest) y que habla con Bedrock con sus propias credenciales.

Las tres primeras necesitan la clave de Bedrock de corta duración de
`test_local_agents.py` (`RAYITO_LOCAL_BEDROCK_KEY_FILE`, `make
local-bedrock-key`); `litellm` necesita `RAYITO_LOCAL_LITELLM_URL`
(`https://host:puerto` alcanzable desde el guest) y la clave del proxy en
el fichero que nombra `RAYITO_LOCAL_LITELLM_KEY_FILE`; el modelo de
LiteLLM se cambia con `RAYITO_LOCAL_LITELLM_MODEL`. Sin ellas, cada ruta se
salta. Las claves entran en el Secrets Manager de Floci (`rayito/...`, con
`Bearer ` delante) y sólo `rayd` las lee; los tests las buscan en el
sandbox para decir sí o no y nunca las imprimen. Coste: unas pocas
llamadas a gpt-oss-120b y Haiku (céntimos).
"""

from __future__ import annotations

import contextlib
import os
import secrets as stdlib_secrets
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import boto3
import pytest

from rayito import (
    AgentFailed,
    AgentModel,
    AgentSpec,
    Done,
    EgressEnforcement,
    Sandbox,
    SecretCache,
    SecretGateway,
    SecretStore,
    StepStarted,
    ToolCall,
    litellm_gateway,
    openai_compatible_gateway,
)
from rayito.exceptions import CommandExitException, SandboxNotFoundException

from .conftest import LocalSettings, create_local_sandbox
from .guest import LocalGuestControlPlane

pytestmark = pytest.mark.local

BEDROCK_KEY_FILE_VAR: Final = "RAYITO_LOCAL_BEDROCK_KEY_FILE"
LITELLM_URL_VAR: Final = "RAYITO_LOCAL_LITELLM_URL"
LITELLM_KEY_FILE_VAR: Final = "RAYITO_LOCAL_LITELLM_KEY_FILE"
LITELLM_MODEL_VAR: Final = "RAYITO_LOCAL_LITELLM_MODEL"
BEDROCK_REGION: Final = os.environ.get("RAYITO_E2E_BEDROCK_REGION") or "us-east-1"
#: gpt-oss-120b tiene Chat Completions en `bedrock-runtime` (con este id) y
#: Chat Completions y Responses en `bedrock-mantle` (sin el sufijo de
#: versión). docs.aws.amazon.com/bedrock/latest/userguide/
#: model-card-openai-gpt-oss-120b.html (consultado el 2026-10-07).
BEDROCK_RUNTIME_GPT_OSS: Final = "openai.gpt-oss-120b-1:0"
BEDROCK_MANTLE_GPT_OSS: Final = "openai.gpt-oss-120b"
BEDROCK_RUNTIME_UPSTREAM: Final = f"https://bedrock-runtime.{BEDROCK_REGION}.amazonaws.com"
BEDROCK_RUNTIME_OPENAI_BASE_PATH: Final = "/openai/v1"
BEDROCK_MANTLE_UPSTREAM: Final = f"https://bedrock-mantle.{BEDROCK_REGION}.api.aws"
BEDROCK_MANTLE_BASE_PATH: Final = "/v1"
#: El nombre de modelo que expone el proxy de LiteLLM de la prueba.
DEFAULT_LITELLM_MODEL: Final = "gpt-oss-120b"
LITELLM_BASE_PATH: Final = "/v1"
GATEWAY_NAME: Final = "model"
RUNTIMES: Final = ("opencode", "deepagents")
WORKDIR: Final = "/home/user"
AGENT_TIMEOUT_SECONDS: Final = 300
EGRESS_PROBE_TIMEOUT_SECONDS: Final = 5


@dataclass(frozen=True)
class Route:
    """Un camino de proveedor: la pasarela, el modelo y la clave que usa."""

    gateway: Callable[[str], SecretGateway]
    model: Callable[[], AgentModel]
    key: Callable[[], str]
    upstream: Callable[[], str]


def read_key_file(var: str) -> str:
    path = os.environ.get(var)
    if not path or not Path(path).is_file():
        pytest.skip(f"sin clave: exporta {var}")
    key = Path(path).read_text(encoding="utf-8").strip()
    if not key:
        pytest.skip(f"el fichero de {var} está vacío")
    return key


def bedrock_key() -> str:
    return read_key_file(BEDROCK_KEY_FILE_VAR)


def litellm_key() -> str:
    if not os.environ.get(LITELLM_URL_VAR):
        pytest.skip(f"sin proxy de LiteLLM: exporta {LITELLM_URL_VAR}")
    return read_key_file(LITELLM_KEY_FILE_VAR)


def litellm_url() -> str:
    return os.environ[LITELLM_URL_VAR]


def litellm_model() -> str:
    return os.environ.get(LITELLM_MODEL_VAR) or DEFAULT_LITELLM_MODEL


def mantle_responses_gateway(secret: str) -> SecretGateway:
    """La allowlist de `openai_gateway` hacia otro upstream: el preset fija
    `api.openai.com`, y `bedrock-mantle` sirve la misma API bajo `/v1`."""
    return SecretGateway(
        upstream=BEDROCK_MANTLE_UPSTREAM,
        headers={"authorization": secret},
        allow=[
            ("POST", f"{BEDROCK_MANTLE_BASE_PATH}/responses"),
            ("POST", f"{BEDROCK_MANTLE_BASE_PATH}/chat/completions"),
        ],
    )


ROUTES: Final[dict[str, Route]] = {
    "bedrock-chat": Route(
        gateway=lambda secret: openai_compatible_gateway(
            secret, upstream=BEDROCK_RUNTIME_UPSTREAM, base_path=BEDROCK_RUNTIME_OPENAI_BASE_PATH
        ),
        model=lambda: AgentModel(
            provider="openai-compatible",
            id=BEDROCK_RUNTIME_GPT_OSS,
            gateway=GATEWAY_NAME,
            base_path=BEDROCK_RUNTIME_OPENAI_BASE_PATH,
        ),
        key=bedrock_key,
        upstream=lambda: BEDROCK_RUNTIME_UPSTREAM,
    ),
    "mantle-chat": Route(
        gateway=lambda secret: openai_compatible_gateway(
            secret, upstream=BEDROCK_MANTLE_UPSTREAM, base_path=BEDROCK_MANTLE_BASE_PATH
        ),
        model=lambda: AgentModel(
            provider="openai-compatible",
            id=BEDROCK_MANTLE_GPT_OSS,
            gateway=GATEWAY_NAME,
            base_path=BEDROCK_MANTLE_BASE_PATH,
        ),
        key=bedrock_key,
        upstream=lambda: BEDROCK_MANTLE_UPSTREAM,
    ),
    "mantle-responses": Route(
        gateway=mantle_responses_gateway,
        model=lambda: AgentModel(
            provider="openai", id=BEDROCK_MANTLE_GPT_OSS, gateway=GATEWAY_NAME
        ),
        key=bedrock_key,
        upstream=lambda: BEDROCK_MANTLE_UPSTREAM,
    ),
    "litellm": Route(
        gateway=lambda secret: litellm_gateway(
            secret, upstream=litellm_url(), base_path=LITELLM_BASE_PATH
        ),
        model=lambda: AgentModel(
            provider="openai-compatible",
            id=litellm_model(),
            gateway=GATEWAY_NAME,
            base_path=LITELLM_BASE_PATH,
        ),
        key=litellm_key,
        upstream=litellm_url,
    ),
}


@dataclass(frozen=True)
class ProviderBox:
    route: str
    sandbox: Sandbox
    spec: AgentSpec
    upstream: str
    key: str = field(repr=False)

    def sh(self, cmd: str) -> tuple[int, str]:
        """Una orden como `user`; devuelve (exit, stdout) sin lanzar por un
        exit distinto de cero."""
        try:
            result = self.sandbox.commands.run(cmd, timeout=AGENT_TIMEOUT_SECONDS)
        except CommandExitException as exc:
            return exc.exit_code, exc.stdout
        return result.exit_code, result.stdout

    def leaks(self, text: str) -> bool:
        """Si `text` contiene la clave. Los tests guardan el resultado en una
        variable antes del `assert` para que pytest no muestre el texto."""
        return self.key in text


#: Las rutas con herramientas y continuación de sesión completas.
TOOL_ROUTES: Final = ("bedrock-chat", "litellm", "mantle-chat")
#: Las rutas de la Responses API, con una vuelta sin herramientas.
RESPONSES_ROUTES: Final = ("mantle-responses",)


@pytest.fixture(scope="module", params=sorted(ROUTES))
def box(
    request: pytest.FixtureRequest,
    local_settings: LocalSettings,
    control_plane: LocalGuestControlPlane,
    template_arn: str,
    aws_session: boto3.session.Session,
) -> Iterator[ProviderBox]:
    """Un sandbox por ruta con su pasarela y `allow_internet_access=False`;
    la clave vive en el Secrets Manager de Floci mientras dura."""
    route = ROUTES[request.param]
    key = route.key()
    store = SecretStore(session=aws_session)
    secret_name = f"local-providers-{stdlib_secrets.token_hex(4)}"
    store.create(secret_name, f"Bearer {key}")
    try:
        sandbox = create_local_sandbox(
            local_settings,
            control_plane,
            template_arn,
            allow_internet_access=False,
            gateways={GATEWAY_NAME: route.gateway(secret_name)},
            secret_cache=SecretCache(store=store),
        )
        try:
            prepared = ProviderBox(
                request.param, sandbox, AgentSpec(model=route.model()), route.upstream(), key
            )
            if prepared.sh("command -v opencode")[0] != 0:
                pytest.fail("el guest no trae los agentes: make local-agent-up")
            yield prepared
        finally:
            with contextlib.suppress(SandboxNotFoundException):
                sandbox.kill()
    finally:
        store.destroy(secret_name)


def test_direct_egress_is_closed(box: ProviderBox) -> None:
    """Sin la pasarela, el upstream del proveedor no es alcanzable."""
    assert box.sandbox.get_health().egress_enforcement == EgressEnforcement.GUEST_ROUTES
    code, _ = box.sh(f"curl -sS -o /dev/null -m {EGRESS_PROBE_TIMEOUT_SECONDS} {box.upstream}")
    assert code != 0


def assert_key_not_readable(box: ProviderBox) -> None:
    """Ni el entorno, ni la configuración de OpenCode, ni la línea de
    órdenes de un proceso tienen la clave."""
    _, seen = box.sh(f"env; cat -- {WORKDIR}/.config/opencode/* 2>/dev/null; ps -eo args")
    leaked = box.leaks(seen)
    assert not leaked


@pytest.mark.parametrize("runtime", RUNTIMES)
def test_responses_turn(box: ProviderBox, runtime: str) -> None:
    """Una vuelta por la Responses API a través de la pasarela: texto, uso
    de tokens y fin limpio."""
    if box.route not in RESPONSES_ROUTES:
        pytest.skip("la ruta usa Chat Completions: la cubre test_tools_and_session")
    word = f"colibri{stdlib_secrets.token_hex(2)}"
    prompt = f"Responde sólo con la palabra {word}, sin usar herramientas."
    with box.sandbox.agent.stream(prompt, spec=box.spec, runtime=runtime) as stream:
        events = list(stream)
        result = stream.result()
    failures = [event for event in events if isinstance(event, AgentFailed)]
    assert not failures, failures
    assert isinstance(events[-1], Done)
    assert word in result.text.lower()
    assert result.usage.input > 0
    assert result.usage.output > 0
    assert_key_not_readable(box)


@pytest.mark.parametrize("runtime", RUNTIMES)
def test_tools_and_session(box: ProviderBox, runtime: str) -> None:
    """Una vuelta con herramientas por la pasarela y la continuación de la
    sesión; ni el entorno ni la configuración del runtime tienen la clave."""
    if box.route not in TOOL_ROUTES:
        pytest.skip("bedrock-mantle no sigue la Responses API en herramientas ni sesiones")
    word = f"colibri{stdlib_secrets.token_hex(2)}"
    path = f"{WORKDIR}/{runtime}-proveedor.txt"
    prompt = (
        f"Crea el fichero {path} con el texto 'hola desde {runtime}' usando tu herramienta "
        f"para escribir ficheros. Recuerda la palabra clave {word}, pero no la escribas en "
        "ningún fichero. Responde sólo 'listo'."
    )
    with box.sandbox.agent.stream(prompt, spec=box.spec, runtime=runtime) as stream:
        events = list(stream)
        result = stream.result()
    failures = [event for event in events if isinstance(event, AgentFailed)]
    assert not failures, failures
    assert isinstance(events[0], StepStarted)
    assert isinstance(events[-1], Done)
    assert any(isinstance(e, ToolCall) and e.status == "completed" for e in events)
    assert result.usage.input > 0
    assert result.usage.output > 0
    assert f"hola desde {runtime}" in box.sandbox.files.read(path)

    question = "¿Cuál era la palabra clave? Responde sólo con la palabra, sin usar herramientas."
    follow_up = box.sandbox.agent.run(
        question, spec=box.spec, runtime=runtime, session_id=result.session_id
    )
    assert follow_up.session_id == result.session_id
    assert word in follow_up.text.lower()

    assert_key_not_readable(box)
