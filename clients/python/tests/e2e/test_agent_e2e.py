"""`sbx.agent` contra AWS real (`ai-agent-core`, Q150 y Q152 de
`AWS_API_NOTES.md` §16): OpenCode y deepagents en un sandbox de una imagen
de `AgentTemplate`, con Claude en Amazon Bedrock como modelo y
`bedrock_gateway` como única salida.

Comprueba en un MicroVM real lo que `tests/local/test_local_agents.py`
comprueba en Docker: herramientas, eventos y continuación de la sesión de
cada runtime; el egress directo cerrado (`allow_internet_access=False`)
mientras el modelo sí responde; que la pasarela sólo deja pasar las rutas
de los modelos permitidos; y que la credencial no es legible desde el
sandbox ni desde la herramienta de shell del agente.

Además de `RAYITO_E2E=1` y `RAYITO_TEMPLATE`, necesita:

- `RAYITO_E2E_AGENT_TEMPLATE`: nombre o ARN de una imagen construida con
  `AgentTemplate` (con los dos runtimes);
- `RAYITO_E2E_BEDROCK_SECRET`: el nombre de un secreto de Secrets Manager
  que ya guarda `Bearer <clave de API de Bedrock de corta duración>`
  (`dev/local/agent/mint_bedrock_key.py` la acuña en local con tu sesión).

Sin ellas se salta. El modelo y su región se cambian con
`RAYITO_E2E_BEDROCK_MODEL` y `RAYITO_E2E_BEDROCK_REGION`. Coste: dos
sandboxes de 2 GB durante unos minutos (≈ $0,01) y ≈ 12 llamadas a Haiku
(≈ $0,25). La clave se busca en el sandbox para decir sí o no y nunca se
imprime.
"""

from __future__ import annotations

import contextlib
import os
import secrets as stdlib_secrets
import shlex
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Final
from urllib.parse import quote

import pytest

from rayito import (
    AgentFailed,
    AgentModel,
    AgentSpec,
    Done,
    EgressEnforcement,
    Sandbox,
    SecretCache,
    SecretStore,
    StepFinished,
    StepStarted,
    ToolCall,
    bedrock_gateway,
)
from rayito._aws import LambdaMicrovmsControlPlane
from rayito.exceptions import CommandExitException, SandboxNotFoundException

from .conftest import TEST_SANDBOX_TIMEOUT_SECONDS, E2ESettings

pytestmark = pytest.mark.e2e

AGENT_TEMPLATE_VAR: Final = "RAYITO_E2E_AGENT_TEMPLATE"
BEDROCK_SECRET_VAR: Final = "RAYITO_E2E_BEDROCK_SECRET"
#: Perfil de inferencia de sistema de Claude Haiku 4.5 en us-east-1, el del
#: spike (docs/research/2026-10-agent-spike.md).
DEFAULT_BEDROCK_MODEL: Final = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
DEFAULT_BEDROCK_REGION: Final = "us-east-1"
#: Un modelo que la pasarela no permite: sólo se usa su ruta, nunca llega
#: a Bedrock.
OTHER_BEDROCK_MODEL: Final = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
BEDROCK_MODEL: Final = os.environ.get("RAYITO_E2E_BEDROCK_MODEL") or DEFAULT_BEDROCK_MODEL
BEDROCK_REGION: Final = os.environ.get("RAYITO_E2E_BEDROCK_REGION") or DEFAULT_BEDROCK_REGION
BEDROCK_UPSTREAM: Final = f"https://bedrock-runtime.{BEDROCK_REGION}.amazonaws.com"
GATEWAY_NAME: Final = "bedrock"
RUNTIMES: Final = ("opencode", "deepagents")
WORKDIR: Final = "/home/user"
#: Una vuelta con herramientas tarda 5 s de mediana (spike); el margen
#: cubre los reintentos de Bedrock y el primer `exec` del runtime tras el
#: lanzamiento (Q142).
AGENT_TIMEOUT_SECONDS: Final = 240
EGRESS_PROBE_TIMEOUT_SECONDS: Final = 5
#: Lo que `curl -w` imprime para una ruta fuera de `allow`: `rayd` responde
#: 403 con el cuerpo vacío (§28), y un 403 de Bedrock siempre trae un JSON.
GATEWAY_DENIED: Final = "403 0"
#: Variables que encenderían trazas hacia fuera (OTLP, LangSmith/LangChain).
TELEMETRY_ENV_PREFIXES: Final = ("OTEL_", "LANGSMITH_", "LANGCHAIN_TRACING", "LANGCHAIN_API_KEY")


@dataclass(frozen=True)
class AgentBox:
    sandbox: Sandbox
    spec: AgentSpec
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


@pytest.fixture(scope="module")
def box(e2e_settings: E2ESettings, control_plane: LambdaMicrovmsControlPlane) -> Iterator[AgentBox]:
    """Un sandbox de la plantilla de agente con `bedrock_gateway` y
    `allow_internet_access=False` para todos los tests del módulo."""
    template = os.environ.get(AGENT_TEMPLATE_VAR)
    secret_name = os.environ.get(BEDROCK_SECRET_VAR)
    if not template or not secret_name:
        pytest.skip(f"exporta {AGENT_TEMPLATE_VAR} y {BEDROCK_SECRET_VAR} para el agente en AWS")
    region = e2e_settings.region or control_plane.region
    cache = SecretCache(store=SecretStore(region=region))
    key = cache.get(secret_name).removeprefix("Bearer ")
    sandbox = Sandbox.create(
        control_plane.resolve_template_arn(template),
        timeout=TEST_SANDBOX_TIMEOUT_SECONDS,
        idle=None,
        execution_role_arn=e2e_settings.execution_role_arn,
        logging=e2e_settings.logging,
        control_plane=control_plane,
        allow_internet_access=False,
        gateways={
            GATEWAY_NAME: bedrock_gateway(
                secret_name, region=BEDROCK_REGION, models=[BEDROCK_MODEL]
            )
        },
        secret_cache=cache,
    )
    try:
        spec = AgentSpec(
            model=AgentModel(
                provider="bedrock", id=BEDROCK_MODEL, gateway=GATEWAY_NAME, region=BEDROCK_REGION
            )
        )
        yield AgentBox(sandbox, spec, key)
    finally:
        with contextlib.suppress(SandboxNotFoundException):
            sandbox.kill()


def test_direct_egress_is_closed(box: AgentBox) -> None:
    """El deny-all de `rayd` en un MicroVM real: ni el upstream del modelo ni
    otro destino son alcanzables sin la pasarela, ni por el proxy ni por IP."""
    assert box.sandbox.get_health().egress_enforcement == EgressEnforcement.GUEST_ROUTES
    for target in ("https://example.com", BEDROCK_UPSTREAM):
        code, _ = box.sh(f"curl -sS -o /dev/null -m {EGRESS_PROBE_TIMEOUT_SECONDS} {target}")
        assert code != 0, target
    code, _ = box.sh(
        f"curl -sS -o /dev/null -m {EGRESS_PROBE_TIMEOUT_SECONDS} --noproxy '*' https://1.1.1.1"
    )
    assert code != 0


def gateway_status(box: AgentBox, method: str, path: str) -> str:
    url = box.sandbox.gateways[GATEWAY_NAME].url
    _, out = box.sh(
        "curl -sS -o /dev/null -w '%{http_code} %{size_download}' "
        f"-m {EGRESS_PROBE_TIMEOUT_SECONDS} "
        f"-X {method} -H 'content-type: application/json' -d '{{}}' "
        f"{shlex.quote(url + path)}"
    )
    return out.strip()


def test_gateway_only_allows_the_chosen_model(box: AgentBox) -> None:
    """`bedrock_gateway(models=[...])` restringe `allow` a las rutas de ese
    modelo: otro modelo, otra operación u otro método son 403 en la
    pasarela, sin llegar a Bedrock; la ruta permitida sí llega (Bedrock
    rechaza el cuerpo vacío con su propio error)."""
    other = quote(OTHER_BEDROCK_MODEL, safe="")
    chosen = quote(BEDROCK_MODEL, safe="")
    assert gateway_status(box, "POST", f"/model/{chosen}/converse") != GATEWAY_DENIED
    assert gateway_status(box, "POST", f"/model/{other}/converse") == GATEWAY_DENIED
    assert gateway_status(box, "POST", f"/model/{chosen}/invoke") == GATEWAY_DENIED
    assert gateway_status(box, "GET", f"/model/{chosen}/converse") == GATEWAY_DENIED
    assert gateway_status(box, "GET", "/foundation-models") == GATEWAY_DENIED


@pytest.mark.parametrize("runtime", RUNTIMES)
def test_tools_events_and_session(box: AgentBox, runtime: str) -> None:
    """Una vuelta con herramientas por la pasarela y la continuación de la
    sesión por su id; mientras el modelo trabaja, el egress directo sigue
    cerrado."""
    word = f"colibri{stdlib_secrets.token_hex(2)}"
    path = f"{WORKDIR}/{runtime}-saludo.txt"
    prompt = (
        f"Crea el fichero {path} con el texto 'hola desde {runtime}'. Además, recuerda la "
        f"palabra clave {word}, pero no la escribas en ningún fichero. Responde sólo 'listo'."
    )
    with box.sandbox.agent.stream(prompt, spec=box.spec, runtime=runtime) as stream:
        code, _ = box.sh(
            f"curl -sS -o /dev/null -m {EGRESS_PROBE_TIMEOUT_SECONDS} {BEDROCK_UPSTREAM}"
        )
        events = list(stream)
        result = stream.result()
    assert code != 0
    assert isinstance(events[0], StepStarted)
    assert isinstance(events[-1], Done)
    assert not [event for event in events if isinstance(event, AgentFailed)]
    assert {StepStarted, StepFinished, ToolCall} <= {type(event) for event in events}
    assert any(e.status == "completed" for e in events if isinstance(e, ToolCall))
    assert result.steps >= 2
    assert result.usage.input > 0
    assert result.usage.output > 0
    assert f"hola desde {runtime}" in box.sandbox.files.read(path)

    question = "¿Cuál era la palabra clave? Responde sólo con la palabra, sin usar herramientas."
    follow_up = box.sandbox.agent.run(
        question, spec=box.spec, runtime=runtime, session_id=result.session_id
    )
    assert follow_up.session_id == result.session_id
    assert word in follow_up.text.lower()


@pytest.mark.parametrize("runtime", RUNTIMES)
def test_telemetry_is_off_and_key_unreadable_in_the_agent(box: AgentBox, runtime: str) -> None:
    """El entorno que ve la herramienta de shell del agente no enciende
    ninguna traza ni lleva la credencial del modelo."""
    path = f"{WORKDIR}/{runtime}-env.txt"
    box.sandbox.agent.run(
        f"Ejecuta exactamente `env > {path}` con tu herramienta de shell. Responde sólo 'listo'.",
        spec=box.spec,
        runtime=runtime,
    )
    env_dump = box.sandbox.files.read(path)
    names = [line.split("=", 1)[0] for line in env_dump.splitlines() if "=" in line]
    assert names
    assert not [name for name in names if name.startswith(TELEMETRY_ENV_PREFIXES)]
    in_agent_env = box.leaks(env_dump)
    assert not in_agent_env


def test_credential_is_not_readable_from_the_sandbox(box: AgentBox) -> None:
    """Después de que los dos runtimes hayan llamado al modelo: la clave no
    está en el entorno, ni en el de `rayd`, ni en un fichero legible."""
    _, env_out = box.sh("env")
    in_env = box.leaks(env_out)
    assert not in_env
    code, out = box.sh("cat /proc/1/environ 2>&1")
    assert code != 0
    in_rayd_environ = box.leaks(out)
    assert not in_rayd_environ
    assert box.sh("head -c 16 /proc/1/mem")[0] != 0
    needle = box.key[: len(box.key) // 2]
    _, out = box.sh(
        f"grep -rlsF {shlex.quote(needle)} / --exclude-dir=proc --exclude-dir=sys "
        "--exclude-dir=dev 2>/dev/null | head -5"
    )
    readable_files = len(out.split())
    assert readable_files == 0
