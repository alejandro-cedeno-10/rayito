"""`sbx.agent` contra un modelo real (`make local-e2e`): OpenCode y
deepagents en el guest de `dev/local/agent/compose.yaml`
(`make local-agent-up`), con Claude en Amazon Bedrock como modelo y la
pasarela de secretos (`bedrock_gateway`) como única salida.

Para cada runtime comprueba, por la API pública: uso de herramientas,
continuación de la sesión, `abort()`, los límites de `AgentLimits` (pasos,
tokens y timeout) sin dejar procesos vivos, tampoco los que su herramienta
de shell lanzó en una sesión propia, la forma del stream de eventos, que la
credencial no es legible desde el sandbox, que el egress directo está
cerrado mientras el modelo sí responde y que no hay telemetría encendida por
defecto. Sin modelo, con un runtime de doble cuyo script deja un demonio
(`setsid` y un padre que sale), comprueba además que el timeout y `abort()`
de `sbx.agent` lo paran: esos tests sólo usan Floci y el guest, y corren
también sin la clave.

Además de `RAYITO_LOCAL_GUEST`, necesita una clave de API de Bedrock de corta
duración en el fichero que nombra `RAYITO_LOCAL_BEDROCK_KEY_FILE` (el runner
la define; `make local-bedrock-key` la acuña en el host con tu sesión de AWS y
la deja en el tmpfs del runner). Sin ese fichero los tests se saltan: es lo
único que sale del entorno local, y cuesta céntimos de Bedrock. El modelo y
su región se cambian con `RAYITO_E2E_BEDROCK_MODEL` y
`RAYITO_E2E_BEDROCK_REGION`. La clave entra en el Secrets Manager de Floci y
sólo `rayd` la lee; los tests la buscan en el sandbox para decir sí o no y
nunca la imprimen.
"""

from __future__ import annotations

import contextlib
import os
import secrets as stdlib_secrets
import shlex
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Final

import boto3
import pytest

from rayito import (
    AgentFailed,
    AgentLimits,
    AgentModel,
    AgentSpec,
    Done,
    EgressEnforcement,
    Sandbox,
    SecretCache,
    SecretStore,
    StepFinished,
    StepStarted,
    Text,
    ToolCall,
    bedrock_gateway,
)
from rayito._agent._events import AgentEvent
from rayito._agent._runtime import (
    RunCommand,
    RunRequest,
    RuntimeFile,
    RuntimeFiles,
    RuntimeState,
    TemplateStep,
    WarmupStep,
)
from rayito.exceptions import CommandExitException, SandboxNotFoundException

from .conftest import LocalSettings, create_local_sandbox
from .guest import LocalGuestControlPlane

pytestmark = pytest.mark.local

BEDROCK_KEY_FILE_VAR: Final = "RAYITO_LOCAL_BEDROCK_KEY_FILE"
#: Perfil de inferencia de sistema de Claude Haiku 4.5 en us-east-1, el del
#: spike (docs/research/2026-10-agent-spike.md, "Cómo llega el modelo").
DEFAULT_BEDROCK_MODEL: Final = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
DEFAULT_BEDROCK_REGION: Final = "us-east-1"
BEDROCK_MODEL: Final = os.environ.get("RAYITO_E2E_BEDROCK_MODEL") or DEFAULT_BEDROCK_MODEL
BEDROCK_REGION: Final = os.environ.get("RAYITO_E2E_BEDROCK_REGION") or DEFAULT_BEDROCK_REGION
BEDROCK_UPSTREAM: Final = f"https://bedrock-runtime.{BEDROCK_REGION}.amazonaws.com"
GATEWAY_NAME: Final = "bedrock"
RUNTIMES: Final = ("opencode", "deepagents")
WORKDIR: Final = "/home/user"
#: Una llamada al modelo con herramientas tarda 5 s de mediana (spike); el
#: margen cubre los reintentos de Bedrock y el primer `exec` del runtime.
AGENT_TIMEOUT_SECONDS: Final = 240
#: El `timeout_seconds` de una ejecución que no puede terminar a tiempo.
SHORT_TIMEOUT_SECONDS: Final = 20
EGRESS_PROBE_TIMEOUT_SECONDS: Final = 5
#: Cuánto se espera a que aparezca (o desaparezca) un proceso del agente.
PROCESS_WAIT_SECONDS: Final = 60
POLL_SECONDS: Final = 0.5
#: Ficheros que se piden de uno en uno para obligar a varios pasos.
STEPS_FILES: Final = 6
#: Variables que encenderían trazas hacia fuera (OTLP, LangSmith/LangChain).
TELEMETRY_ENV_PREFIXES: Final = ("OTEL_", "LANGSMITH_", "LANGCHAIN_TRACING", "LANGCHAIN_API_KEY")
#: Segundos del `sleep` que se le pide al agente, distintos por test para
#: que un proceso de uno no confunda al siguiente.
ABORT_SLEEP_SECONDS: Final = 301
TIMEOUT_SLEEP_SECONDS: Final = {"opencode": 302, "deepagents": 303}


def sleep_prompt(seconds: int) -> str:
    return f"Ejecuta la orden `sleep {seconds}` con tu herramienta de shell y espera a que termine."


def sleep_pattern(seconds: int) -> str:
    """La huella de esa orden en `ps`. El corchete evita que `pgrep -f` se
    encuentre a sí mismo en la línea de órdenes del shell que lo lanza."""
    return f"[s]leep {seconds}"


#: La huella de los procesos de cada runtime en `ps`.
RUNTIME_PATTERNS: Final = {
    "opencode": "[o]pencode run",
    "deepagents": "[d]eepagents_runner.py",
}


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

    def running(self, pattern: str) -> bool:
        return self.sh(f"pgrep -u user -f {shlex.quote(pattern)}")[0] == 0


def wait_until(predicate: Callable[[], bool]) -> bool:
    deadline = time.monotonic() + PROCESS_WAIT_SECONDS
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(POLL_SECONDS)
    return False


def runtime_gone(box: AgentBox, runtime: str) -> bool:
    return wait_until(lambda: not box.running(RUNTIME_PATTERNS[runtime]))


def tool_gone(box: AgentBox, seconds: int) -> bool:
    """Lo que lanzó la herramienta de shell del agente ya no está vivo."""
    return wait_until(lambda: not box.running(sleep_pattern(seconds)))


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
def box(
    local_settings: LocalSettings,
    control_plane: LocalGuestControlPlane,
    template_arn: str,
    aws_session: boto3.session.Session,
    bedrock_key: str,
) -> Iterator[AgentBox]:
    """Un sandbox con `bedrock_gateway` y `allow_internet_access=False` para
    todos los tests del módulo; la clave vive en el Secrets Manager de Floci
    mientras dura."""
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
                GATEWAY_NAME: bedrock_gateway(
                    secret_name, region=BEDROCK_REGION, models=[BEDROCK_MODEL]
                )
            },
            secret_cache=SecretCache(store=store),
        )
        try:
            spec = AgentSpec(
                model=AgentModel(
                    provider="bedrock",
                    id=BEDROCK_MODEL,
                    gateway=GATEWAY_NAME,
                    region=BEDROCK_REGION,
                )
            )
            prepared = AgentBox(sandbox, spec, bedrock_key)
            if prepared.sh("command -v opencode")[0] != 0:
                pytest.fail("el guest no trae los agentes: make local-agent-up")
            yield prepared
        finally:
            with contextlib.suppress(SandboxNotFoundException):
                sandbox.kill()
    finally:
        store.destroy(secret_name)


def test_direct_egress_is_closed(box: AgentBox) -> None:
    """Deny-all aplicado de verdad (CAP_NET_ADMIN en el compose de agentes):
    ni el upstream del modelo ni otro destino son alcanzables sin la
    pasarela, ni por el proxy local ni por IP."""
    assert box.sandbox.get_health().egress_enforcement == EgressEnforcement.GUEST_ROUTES
    for target in ("https://example.com", BEDROCK_UPSTREAM):
        code, _ = box.sh(f"curl -sS -o /dev/null -m {EGRESS_PROBE_TIMEOUT_SECONDS} {target}")
        assert code != 0, target
    code, _ = box.sh(
        f"curl -sS -o /dev/null -m {EGRESS_PROBE_TIMEOUT_SECONDS} --noproxy '*' https://1.1.1.1"
    )
    assert code != 0


@pytest.mark.parametrize("runtime", RUNTIMES)
def test_tools_events_and_session(box: AgentBox, runtime: str) -> None:
    """Una vuelta con herramientas por la pasarela, la forma del stream de
    eventos y la continuación de la sesión por su id; mientras el modelo
    trabaja, el egress directo sigue cerrado."""
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
    kinds = {type(event) for event in events}
    assert {StepStarted, StepFinished, ToolCall, Text} <= kinds
    tool_calls = [event for event in events if isinstance(event, ToolCall)]
    assert any(call.status == "completed" for call in tool_calls)
    assert result.session_id == events[-1].session_id
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
def test_abort_stops_the_agent_and_its_tools(box: AgentBox, runtime: str) -> None:
    """`abort()` para el runtime y lo que su herramienta de shell lanzó en
    una sesión propia (`setsid`), que un `SIGKILL` al grupo no alcanzaría:
    `rayd` congela y mata todo el árbol de la ejecución (`kill_tree`)."""
    seconds = ABORT_SLEEP_SECONDS
    with box.sandbox.agent.stream(sleep_prompt(seconds), spec=box.spec, runtime=runtime) as stream:
        assert wait_until(lambda: box.running(sleep_pattern(seconds)))
        stream.abort()
        events = list(stream)
    final = events[-1]
    assert isinstance(final, AgentFailed)
    assert final.reason == "aborted"
    assert runtime_gone(box, runtime)
    assert tool_gone(box, seconds)


@pytest.mark.parametrize("runtime", RUNTIMES)
def test_timeout_stops_the_agent_and_its_tools(box: AgentBox, runtime: str) -> None:
    """El timeout lo impone `rayd` sobre todo el árbol de la ejecución
    (`kill_tree`): también el `sleep` que la herramienta de shell lanzó en
    una sesión propia."""
    seconds = TIMEOUT_SLEEP_SECONDS[runtime]
    limits = AgentLimits(timeout_seconds=SHORT_TIMEOUT_SECONDS)
    with box.sandbox.agent.stream(
        sleep_prompt(seconds), spec=box.spec, runtime=runtime, limits=limits
    ) as stream:
        events = list(stream)
    final = events[-1]
    assert isinstance(final, AgentFailed)
    assert final.reason == "timeout"
    assert runtime_gone(box, runtime)
    assert tool_gone(box, seconds)


def steps_prompt(runtime: str) -> str:
    names = ", ".join(f"{WORKDIR}/{runtime}-paso{index}.txt" for index in range(STEPS_FILES))
    return (
        f"Crea estos {STEPS_FILES} ficheros, uno por llamada a herramienta y de uno en uno, "
        f"esperando el resultado de cada llamada antes de la siguiente: {names}. "
        "Cada uno con su nombre como texto."
    )


@pytest.mark.parametrize("runtime", RUNTIMES)
@pytest.mark.parametrize(
    ("limits", "reason"),
    [
        (AgentLimits(max_steps=1), "max_steps"),
        (AgentLimits(max_total_tokens=1), "token_budget"),
    ],
    ids=["max_steps", "token_budget"],
)
def test_limits_stop_the_agent(
    box: AgentBox, runtime: str, limits: AgentLimits, reason: str
) -> None:
    """Un límite del SDK corta la ejecución con su `reason` y no deja el
    runtime trabajando (y gastando tokens) en segundo plano."""
    with box.sandbox.agent.stream(
        steps_prompt(runtime), spec=box.spec, runtime=runtime, limits=limits
    ) as stream:
        events = list(stream)
    final = events[-1]
    assert isinstance(final, AgentFailed)
    assert final.reason == reason
    assert runtime_gone(box, runtime)


@pytest.mark.parametrize("runtime", RUNTIMES)
def test_telemetry_is_off_and_key_unreadable_in_the_agent(box: AgentBox, runtime: str) -> None:
    """El entorno que ve la herramienta de shell del agente no enciende
    ninguna traza (OTLP, LangSmith) ni lleva la credencial del modelo."""
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
    está en el entorno del sandbox, ni en el de `rayd`, ni en un fichero
    legible. Sólo se mira si aparece; nunca se imprime."""
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


#: El `sleep` que deja el runtime de doble, distinto por test.
DAEMON_TIMEOUT_SLEEP_SECONDS: Final = 3041
DAEMON_ABORT_SLEEP_SECONDS: Final = 3042
#: Timeout de una ejecución del runtime de doble: el script nunca acaba solo.
DAEMON_RUN_TIMEOUT_SECONDS: Final = 3
#: El runtime de doble no llama al modelo: la pasarela sólo tiene que existir.
DAEMON_GATEWAY_SECRET_VALUE: Final = "Bearer local-sin-modelo"


@dataclass
class DaemonRuntime:
    """Un `AgentRuntime` de doble sin modelo: su script deja un `sleep` en
    una sesión propia cuyo padre sale enseguida (un demonio, como el que
    deja una herramienta de shell) y luego espera para siempre. Su
    configuración es un JSON vacío: `sbx.agent` escribe al menos un fichero."""

    seconds: int
    name: str = "daemon"

    def build_config(
        self, spec: AgentSpec, *, gateway_urls: Mapping[str, str], workdir: str
    ) -> RuntimeFiles:
        config = RuntimeFile(f"{workdir}/.rayito/agent/daemon.json", b"{}")
        return RuntimeFiles(files=(config,), config_sha256=f"daemon-{self.seconds}")

    def command(self, request: RunRequest) -> RunCommand:
        daemon = f"sleep {self.seconds} >/dev/null 2>&1 </dev/null &"
        return RunCommand(script=f"setsid sh -c {shlex.quote(daemon)}; exec sleep infinity")

    def new_state(self) -> RuntimeState:
        return SimpleNamespace()

    def parse_line(self, line: bytes, state: RuntimeState) -> Sequence[AgentEvent]:
        return ()

    def finish(self, state: RuntimeState, exit_code: int) -> Done | AgentFailed:
        return AgentFailed(reason="runtime_error", exit_code=exit_code)

    def template_steps(self) -> Sequence[TemplateStep]:
        return ()

    def warmup_steps(self) -> Sequence[WarmupStep]:
        return ()


@pytest.fixture(scope="module")
def daemon_box(
    local_settings: LocalSettings,
    control_plane: LocalGuestControlPlane,
    template_arn: str,
    aws_session: boto3.session.Session,
) -> Iterator[AgentBox]:
    """Un sandbox con una pasarela de Bedrock que nadie usa: `sbx.agent`
    exige la del modelo antes de lanzar nada."""
    store = SecretStore(session=aws_session)
    secret_name = f"local-agents-daemon-{stdlib_secrets.token_hex(4)}"
    store.create(secret_name, DAEMON_GATEWAY_SECRET_VALUE)
    try:
        sandbox = create_local_sandbox(
            local_settings,
            control_plane,
            template_arn,
            gateways={
                GATEWAY_NAME: bedrock_gateway(
                    secret_name, region=BEDROCK_REGION, models=[BEDROCK_MODEL]
                )
            },
            secret_cache=SecretCache(store=store),
        )
        try:
            spec = AgentSpec(
                model=AgentModel(
                    provider="bedrock",
                    id=BEDROCK_MODEL,
                    gateway=GATEWAY_NAME,
                    region=BEDROCK_REGION,
                )
            )
            yield AgentBox(sandbox, spec, DAEMON_GATEWAY_SECRET_VALUE)
        finally:
            with contextlib.suppress(SandboxNotFoundException):
                sandbox.kill()
    finally:
        store.destroy(secret_name)


def test_timeout_stops_what_the_agent_daemonised(daemon_box: AgentBox) -> None:
    seconds = DAEMON_TIMEOUT_SLEEP_SECONDS
    limits = AgentLimits(timeout_seconds=DAEMON_RUN_TIMEOUT_SECONDS)
    with daemon_box.sandbox.agent.stream(
        "sin modelo", spec=daemon_box.spec, runtime=DaemonRuntime(seconds), limits=limits
    ) as stream:
        assert wait_until(lambda: daemon_box.running(sleep_pattern(seconds)))
        events = list(stream)
    final = events[-1]
    assert isinstance(final, AgentFailed)
    assert final.reason == "timeout"
    assert not daemon_box.running(sleep_pattern(seconds)), "el fin llegó con el demonio vivo"


def test_abort_stops_what_the_agent_daemonised(daemon_box: AgentBox) -> None:
    seconds = DAEMON_ABORT_SLEEP_SECONDS
    with daemon_box.sandbox.agent.stream(
        "sin modelo", spec=daemon_box.spec, runtime=DaemonRuntime(seconds)
    ) as stream:
        assert wait_until(lambda: daemon_box.running(sleep_pattern(seconds)))
        stream.abort()
        events = list(stream)
    final = events[-1]
    assert isinstance(final, AgentFailed)
    assert final.reason == "aborted"
    assert not daemon_box.running(sleep_pattern(seconds)), "abort() dejó el demonio vivo"
