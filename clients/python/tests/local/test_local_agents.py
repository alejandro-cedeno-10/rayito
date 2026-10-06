"""Agentes dentro del sandbox contra un modelo real (`make local-e2e`):
OpenCode y deepagents en el guest de `dev/local/agent/compose.yaml`
(`make local-agent-up`), con Claude en Amazon Bedrock como modelo y la
pasarela de secretos como única salida.

Comprueba, para cada runtime, lo que el diseño de `sbx.agent` da por hecho
(docs/research/2026-10-agent-spike.md): uso de herramientas, continuación de
la sesión, abortar, límites (pasos y timeout), la forma de los eventos, que
la credencial no es legible desde el sandbox, que el egress directo está
cerrado mientras la llamada al modelo funciona y que no hay telemetría
encendida por defecto.

Además de `RAYITO_LOCAL_GUEST`, necesita una clave de API de Bedrock de corta
duración en el fichero que nombra `RAYITO_LOCAL_BEDROCK_KEY_FILE` (el runner
la define; `make local-bedrock-key` la acuña en el host con tu sesión de AWS y
la deja en el tmpfs del runner). Sin ese fichero los tests se saltan: es lo
único que sale del entorno local, y cuesta céntimos de Bedrock. La clave
entra en el Secrets Manager de Floci y sólo `rayd` la lee; los tests la
buscan en el sandbox para decir sí o no y nunca la imprimen.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets as stdlib_secrets
import shlex
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import boto3
import pytest

from rayito import EgressEnforcement, Sandbox, SecretCache, SecretGateway, SecretRef, SecretStore
from rayito.exceptions import CommandExitException, SandboxNotFoundException, TimeoutException

from .conftest import LocalSettings, create_local_sandbox
from .guest import LocalGuestControlPlane

pytestmark = pytest.mark.local

BEDROCK_KEY_FILE_VAR: Final = "RAYITO_LOCAL_BEDROCK_KEY_FILE"
#: Bedrock va siempre a us-east-1, con independencia de la región de Floci:
#: es donde la cuenta de pruebas tiene el perfil de inferencia (spike, "Cómo
#: llega el modelo").
BEDROCK_REGION: Final = "us-east-1"
#: Perfil de inferencia de sistema de Claude Haiku 4.5, el del spike
#: (`bedrock list-inference-profiles`, 2026-10-05).
BEDROCK_MODEL: Final = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
BEDROCK_UPSTREAM: Final = f"https://bedrock-runtime.{BEDROCK_REGION}.amazonaws.com"
#: Converse, ConverseStream, InvokeModel e InvokeModelWithResponseStream.
BEDROCK_ALLOW: Final = [("POST", "/model/*")]
GATEWAY_NAME: Final = "bedrock"
#: Lo que el proceso del sandbox pone como token para que botocore y el SDK
#: de AI de OpenCode elijan la autenticación bearer; la pasarela lo cambia.
PLACEHOLDER_TOKEN: Final = "placeholder-not-a-secret"
HOME: Final = "/home/user"
WORKDIR: Final = f"{HOME}/agents"
DEEPAGENTS_PYTHON: Final = "/opt/agents/deepagents/bin/python"
DEEPAGENTS_DRIVER: Final = (
    Path(__file__).resolve().parents[4] / "dev" / "local" / "agent" / "deepagents_check.py"
)
DEEPAGENTS_DRIVER_PATH: Final = f"{HOME}/.agents/deepagents_check.py"
OPENCODE_CONFIG_PATH: Final = f"{WORKDIR}/opencode.json"
OPENCODE_STEPS_CONFIG_PATH: Final = f"{WORKDIR}/opencode-steps.json"
#: Una llamada al modelo con herramientas tarda 5 s de mediana (spike); el
#: margen cubre los reintentos de Bedrock y el primer `exec` de OpenCode.
AGENT_TIMEOUT_SECONDS: Final = 240
#: El timeout que se le pone a una orden que no puede terminar a tiempo.
SHORT_TIMEOUT_SECONDS: Final = 5
EGRESS_PROBE_TIMEOUT_SECONDS: Final = 5
#: Cuánto se espera a que aparezca (o desaparezca) un proceso del agente.
PROCESS_WAIT_SECONDS: Final = 30
POLL_SECONDS: Final = 0.5
#: `steps` de OpenCode: pasos con herramientas antes de forzar una respuesta
#: sólo de texto (esquema de `agent.<nombre>.steps` en el binario 1.18.34).
OPENCODE_STEPS: Final = 1
#: `recursion_limit` de LangGraph: no cabe ni el bucle de ocho ficheros.
DEEPAGENTS_RECURSION_LIMIT: Final = 4
STEPS_FILES: Final = 8
#: Los tipos de evento de `opencode run --format json` que una vuelta con
#: herramientas emite siempre.
OPENCODE_EVENT_TYPES: Final = frozenset({"step_start", "tool_use", "text", "step_finish"})
#: Variables que encenderían trazas hacia fuera (OTLP, LangSmith/LangChain).
TELEMETRY_ENV_PREFIXES: Final = ("OTEL_", "LANGSMITH_", "LANGCHAIN_TRACING", "LANGCHAIN_API_KEY")
SLEEP_PROMPT: Final = (
    "Ejecuta la orden `sleep 300` con tu herramienta de shell y espera a que termine."
)
#: La huella de la orden de arriba en `ps`: si sobrevive al abort, el
#: agente dejó un hijo vivo.
SLEEP_PATTERN: Final = "sleep 300"
OPENCODE_ENV: Final = {
    "HOME": HOME,
    "OPENCODE_DISABLE_AUTOUPDATE": "1",
    "OPENCODE_DISABLE_MODELS_FETCH": "1",
    "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
    "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
    "OPENCODE_PURE": "1",
    "OPENCODE_CONFIG": OPENCODE_CONFIG_PATH,
    "AWS_BEARER_TOKEN_BEDROCK": PLACEHOLDER_TOKEN,
    "AWS_REGION": BEDROCK_REGION,
}


@dataclass(frozen=True)
class AgentSandbox:
    sandbox: Sandbox
    gateway_url: str
    key: str = field(repr=False)

    def run(self, cmd: str, **kwargs: Any) -> tuple[int, str, str]:
        """Una orden como `user`; devuelve (exit, stdout, stderr) sin lanzar
        por un exit distinto de cero."""
        kwargs.setdefault("timeout", AGENT_TIMEOUT_SECONDS)
        try:
            result = self.sandbox.commands.run(cmd, **kwargs)
        except CommandExitException as exc:
            return exc.exit_code, exc.stdout, exc.stderr
        return result.exit_code, result.stdout, result.stderr

    def leaks(self, text: str) -> bool:
        """Si `text` contiene la clave. Los tests guardan el resultado en una
        variable antes del `assert` para que pytest no muestre el texto."""
        return self.key in text

    def deepagents_env(self) -> dict[str, str]:
        return {
            "HOME": HOME,
            "AWS_BEARER_TOKEN_BEDROCK": PLACEHOLDER_TOKEN,
            "AGENT_MODEL": BEDROCK_MODEL,
            "AGENT_REGION": BEDROCK_REGION,
            "AGENT_GATEWAY_URL": self.gateway_url,
        }


def opencode_config(gateway_url: str, *, steps: int | None = None) -> str:
    """`opencode.json`: `amazon-bedrock` con `endpoint` en la pasarela y,
    con `steps`, el límite de pasos del agente por defecto (`build`)."""
    config: dict[str, Any] = {
        "model": f"amazon-bedrock/{BEDROCK_MODEL}",
        "autoupdate": False,
        "share": "disabled",
        "provider": {
            "amazon-bedrock": {"options": {"region": BEDROCK_REGION, "endpoint": gateway_url}}
        },
    }
    if steps is not None:
        config["agent"] = {"build": {"steps": steps}}
    return json.dumps(config)


def opencode_cmd(prompt: str, *, session_id: str | None = None) -> str:
    session = f" --session {shlex.quote(session_id)}" if session_id else ""
    return f"opencode run --format json --auto{session} {shlex.quote(prompt)}"


def json_lines(text: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in text.splitlines() if line.startswith("{")]


def opencode_text(events: list[dict[str, Any]]) -> str:
    return " ".join(event["part"].get("text", "") for event in events if event["type"] == "text")


def wait_until(predicate: Callable[[], bool]) -> bool:
    deadline = time.monotonic() + PROCESS_WAIT_SECONDS
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(POLL_SECONDS)
    return False


def user_process_running(agent: AgentSandbox, pattern: str) -> bool:
    code, _, _ = agent.run(f"pgrep -u user -f {shlex.quote(pattern)}")
    return code == 0


@pytest.fixture(scope="module")
def bedrock_key() -> str:
    path = os.environ.get(BEDROCK_KEY_FILE_VAR)
    if not path or not Path(path).is_file():
        pytest.skip(f"sin clave de Bedrock: make local-bedrock-key ({BEDROCK_KEY_FILE_VAR})")
    key = Path(path).read_text(encoding="utf-8").strip()
    if not key:
        pytest.skip(f"el fichero de {BEDROCK_KEY_FILE_VAR} está vacío")
    return key


@pytest.fixture(scope="module")
def agent(
    local_settings: LocalSettings,
    control_plane: LocalGuestControlPlane,
    template_arn: str,
    aws_session: boto3.session.Session,
    bedrock_key: str,
) -> Iterator[AgentSandbox]:
    """Un sandbox con la pasarela hacia Bedrock y `allow_internet_access=False`
    para todos los tests del módulo; la clave vive en el Secrets Manager de
    Floci mientras dura."""
    store = SecretStore(session=aws_session)
    secret_name = f"local-agents-{stdlib_secrets.token_hex(4)}"
    store.create(secret_name, f"Bearer {bedrock_key}")
    try:
        sandbox = create_local_sandbox(
            local_settings,
            control_plane,
            template_arn,
            allow_internet_access=False,
            gateways={
                GATEWAY_NAME: SecretGateway(
                    upstream=BEDROCK_UPSTREAM,
                    headers={"authorization": SecretRef(secret_name)},
                    allow=BEDROCK_ALLOW,
                )
            },
            secret_cache=SecretCache(store=store),
        )
        try:
            prepared = AgentSandbox(sandbox, sandbox.gateways[GATEWAY_NAME].url, bedrock_key)
            if prepared.run("command -v opencode")[0] != 0:
                pytest.fail("el guest no trae los agentes: make local-agent-up")
            sandbox.files.write(
                DEEPAGENTS_DRIVER_PATH, DEEPAGENTS_DRIVER.read_text(encoding="utf-8")
            )
            sandbox.files.write(OPENCODE_CONFIG_PATH, opencode_config(prepared.gateway_url))
            sandbox.files.write(
                OPENCODE_STEPS_CONFIG_PATH,
                opencode_config(prepared.gateway_url, steps=OPENCODE_STEPS),
            )
            yield prepared
        finally:
            with contextlib.suppress(SandboxNotFoundException):
                sandbox.kill()
    finally:
        store.destroy(secret_name)


def test_direct_egress_is_closed(agent: AgentSandbox) -> None:
    """Deny-all aplicado de verdad (CAP_NET_ADMIN en el compose de agentes):
    ni el upstream del modelo ni otro destino son alcanzables sin la
    pasarela, ni por el proxy local ni por IP."""
    assert agent.sandbox.get_health().egress_enforcement == EgressEnforcement.GUEST_ROUTES
    for target in ("https://example.com", BEDROCK_UPSTREAM):
        code, _, _ = agent.run(f"curl -sS -o /dev/null -m {EGRESS_PROBE_TIMEOUT_SECONDS} {target}")
        assert code != 0, target
    code, _, _ = agent.run(
        f"curl -sS -o /dev/null -m {EGRESS_PROBE_TIMEOUT_SECONDS} --noproxy '*' https://1.1.1.1"
    )
    assert code != 0
    assert agent.run("getent hosts example.com")[0] != 0


def test_opencode_tools_events_and_session(agent: AgentSandbox) -> None:
    """Una vuelta con herramientas por la pasarela, la forma de sus eventos
    JSON y la continuación de la sesión por su id."""
    word = f"colibri{stdlib_secrets.token_hex(2)}"
    prompt = (
        "Crea el fichero saludo.txt con el texto 'hola desde opencode'. Además, recuerda la "
        f"palabra clave {word}, pero no la escribas en ningún fichero. Responde sólo 'listo'."
    )
    code, out, err = agent.run(opencode_cmd(prompt), envs=OPENCODE_ENV, cwd=WORKDIR)
    assert code == 0, err[-400:]
    events = json_lines(out)
    assert events
    assert all({"type", "timestamp", "sessionID"} <= event.keys() for event in events)
    assert {event["type"] for event in events} >= OPENCODE_EVENT_TYPES
    assert len({event["sessionID"] for event in events}) == 1
    tools = {event["part"]["tool"] for event in events if event["type"] == "tool_use"}
    assert "write" in tools
    assert "hola desde opencode" in agent.sandbox.files.read(f"{WORKDIR}/saludo.txt")

    session_id = events[0]["sessionID"]
    question = "¿Cuál era la palabra clave? Responde sólo con la palabra, sin usar herramientas."
    code, out, err = agent.run(
        opencode_cmd(question, session_id=session_id), envs=OPENCODE_ENV, cwd=WORKDIR
    )
    assert code == 0, err[-400:]
    follow_up = json_lines(out)
    assert {event["sessionID"] for event in follow_up} == {session_id}
    assert word in opencode_text(follow_up).lower()


def test_opencode_steps_limit(agent: AgentSandbox) -> None:
    """`steps` corta el bucle de herramientas: tras ese número de pasos
    OpenCode sólo puede responder con texto."""
    prompt = (
        f"Crea {STEPS_FILES} ficheros, uno por llamada y de uno en uno: "
        + ", ".join(f"g{index}.txt" for index in range(STEPS_FILES))
        + ". Cada uno con su nombre como texto."
    )
    envs = {**OPENCODE_ENV, "OPENCODE_CONFIG": OPENCODE_STEPS_CONFIG_PATH}
    code, out, err = agent.run(opencode_cmd(prompt), envs=envs, cwd=WORKDIR)
    assert code == 0, err[-400:]
    steps = [event for event in json_lines(out) if event["type"] == "step_start"]
    assert len(steps) <= OPENCODE_STEPS + 1
    written = agent.run(f"ls {WORKDIR} | grep -c '^g[0-9].txt$'")[1].strip()
    assert int(written or "0") < STEPS_FILES


def test_deepagents_tools_events_and_session(agent: AgentSandbox) -> None:
    """Herramientas, `stream(stream_mode="updates")` y dos turnos sobre el
    mismo `thread_id` dentro del sandbox."""
    word = f"tucan{stdlib_secrets.token_hex(2)}"
    code, out, err = agent.run(
        f"{DEEPAGENTS_PYTHON} {DEEPAGENTS_DRIVER_PATH} session {WORKDIR}/deepagents {word}",
        envs=agent.deepagents_env(),
    )
    assert code == 0, err[-400:]
    report = json_lines(out)[-1]
    assert report["file_matches"]
    assert "write_file" in report["tool_calls"]
    assert report["events"]
    assert all(event["type"] == "dict" and event["nodes"] for event in report["events"])
    assert {"model", "tools"} <= {node for event in report["events"] for node in event["nodes"]}
    assert report["recalled"]


def test_deepagents_recursion_limit(agent: AgentSandbox) -> None:
    code, out, err = agent.run(
        f"{DEEPAGENTS_PYTHON} {DEEPAGENTS_DRIVER_PATH} steps {WORKDIR}/deepagents-steps "
        f"{DEEPAGENTS_RECURSION_LIMIT}",
        envs=agent.deepagents_env(),
    )
    assert code == 0, err[-400:]
    assert json_lines(out)[-1] == {"recursion_limit_hit": True}


def runtime_sleep_cmd(agent: AgentSandbox, runtime: str) -> tuple[str, dict[str, str]]:
    if runtime == "opencode":
        return opencode_cmd(SLEEP_PROMPT), OPENCODE_ENV
    return (
        f"{DEEPAGENTS_PYTHON} {DEEPAGENTS_DRIVER_PATH} sleep {WORKDIR}/deepagents-sleep",
        agent.deepagents_env(),
    )


@pytest.mark.parametrize("runtime", ["opencode", "deepagents"])
def test_abort_kills_the_agent_and_its_tools(agent: AgentSandbox, runtime: str) -> None:
    """Matar el comando en background se lleva al agente y a lo que su
    herramienta de shell lanzó."""
    cmd, envs = runtime_sleep_cmd(agent, runtime)
    handle = agent.sandbox.commands.run(
        cmd, background=True, envs=envs, cwd=WORKDIR, timeout=AGENT_TIMEOUT_SECONDS
    )
    assert wait_until(lambda: user_process_running(agent, SLEEP_PATTERN))
    assert handle.kill()
    assert wait_until(lambda: not user_process_running(agent, SLEEP_PATTERN))


@pytest.mark.parametrize("runtime", ["opencode", "deepagents"])
def test_timeout_stops_the_agent(agent: AgentSandbox, runtime: str) -> None:
    cmd, envs = runtime_sleep_cmd(agent, runtime)
    with pytest.raises(TimeoutException):
        agent.sandbox.commands.run(cmd, envs=envs, cwd=WORKDIR, timeout=SHORT_TIMEOUT_SECONDS)
    assert wait_until(lambda: not user_process_running(agent, SLEEP_PATTERN))


def test_telemetry_is_off_by_default(agent: AgentSandbox) -> None:
    """Rayito no enciende trazas en el sandbox: ninguna variable OTLP ni de
    LangSmith en el entorno, y LangSmith no trazaría a deepagents."""
    _, env_out, _ = agent.run("env")
    names = [line.split("=", 1)[0] for line in env_out.splitlines() if "=" in line]
    assert not [name for name in names if name.startswith(TELEMETRY_ENV_PREFIXES)]
    code, out, err = agent.run(
        f"{DEEPAGENTS_PYTHON} {DEEPAGENTS_DRIVER_PATH} telemetry", envs=agent.deepagents_env()
    )
    assert code == 0, err[-400:]
    assert json_lines(out)[-1] == {"langsmith_tracing": False}


def test_credential_is_not_readable_from_the_sandbox(agent: AgentSandbox) -> None:
    """Después de que los dos agentes hayan llamado al modelo: la clave no
    está en el entorno del sandbox, ni en el de `rayd`, ni en un fichero
    legible. Sólo se mira si aparece; nunca se imprime."""
    _, env_out, _ = agent.run("env")
    in_env = agent.leaks(env_out)
    assert not in_env
    code, out, err = agent.run("cat /proc/1/environ")
    assert code != 0
    in_rayd_environ = agent.leaks(out + err)
    assert not in_rayd_environ
    assert agent.run("head -c 16 /proc/1/mem")[0] != 0
    needle = agent.key[: len(agent.key) // 2]
    _, out, _ = agent.run(
        f"grep -rlsF {shlex.quote(needle)} / --exclude-dir=proc --exclude-dir=sys "
        "--exclude-dir=dev 2>/dev/null | head -5"
    )
    readable_files = len(out.split())
    assert readable_files == 0
