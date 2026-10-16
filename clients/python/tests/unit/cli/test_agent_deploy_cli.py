"""Las ayudas de `rayito agent deploy` (`cli/_agent_deploy.py`) y los comandos
`invoke`, `status` y `stop` sobre una flota (`cli/agent.py`), con dobles. Nada
toca AWS."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from rayito import AgentDeployOptions, ScalingOptions, plan_deploy
from rayito._payload import generate_access_token
from rayito.cli import agent as agent_cli
from rayito.cli._agent_deploy import (
    AGENT_TOKEN_FILE_DEFAULT,
    COMPACT_LOG_PROGRESS_LINES,
    CompactBuildLog,
    apply_project_flags,
    apply_serve_flags,
    default_token,
    deploy_token,
    describe_plan,
    detection_lines,
    format_usd,
    parse_pairs,
    replace_field,
)
from rayito.cli._session import Clients
from rayito.cli._tokens import TOKEN_ENV_VAR, TokenFileError
from rayito.cli.app import app
from rayito.exceptions import AgentException, InvalidArgumentException
from tests.unit.fake_agent_project import SANDBOX_ID, project

FLEET_NAME = "support-bot"


def test_the_compact_log_prints_alerts_progress_and_a_summary() -> None:
    printed: list[str] = []
    log = CompactBuildLog(printed.append)

    for position in range(1, COMPACT_LOG_PROGRESS_LINES * 2 + 1):
        log("ERROR: paquete roto" if position == 3 else f"línea {position}")
    log.finish()

    assert printed[0] == "ERROR: paquete roto"
    assert printed[1] == f"construyendo la imagen: {COMPACT_LOG_PROGRESS_LINES} líneas de log"
    assert printed[-1].startswith(f"build: {COMPACT_LOG_PROGRESS_LINES * 2} líneas de log")
    assert len(printed) == 4


def test_the_compact_log_is_silent_when_there_was_no_build() -> None:
    printed: list[str] = []

    CompactBuildLog(printed.append).finish()

    assert printed == []


def test_pairs_become_a_dict_and_a_bad_one_never_echoes_its_value() -> None:
    assert parse_pairs(["A=1", "B=dos=tres"], "--env") == {"A": "1", "B": "dos=tres"}
    assert parse_pairs(None, "--env") == {}
    for bad in ("sin-igual", "=valor", "clave="):
        with pytest.raises(InvalidArgumentException) as raised:
            parse_pairs([bad], "--secret")
        assert bad not in str(raised.value)
        assert "--secret" in str(raised.value)


def test_replace_field_names_the_flag_when_the_field_is_missing() -> None:
    @dataclass(frozen=True)
    class Old:
        port: int = 1

    assert replace_field(Old(), "port", 2, "--port").port == 2
    with pytest.raises(InvalidArgumentException, match="--queue") as raised:
        replace_field(Old(), "queue_max", 4, "--queue")
    assert "queue_max" in str(raised.value)


def test_serve_flags_change_only_what_was_passed() -> None:
    the_project = project()

    unchanged = apply_serve_flags(
        the_project,
        max_sessions=None,
        idle_seconds=None,
        queue=None,
        queue_timeout=None,
        timeout=None,
        port=None,
    )
    changed = apply_serve_flags(
        the_project,
        max_sessions=3,
        idle_seconds=120,
        queue=None,
        queue_timeout=None,
        timeout=600,
        port=9100,
    )

    assert unchanged is the_project
    assert (changed.serve.max_concurrency, changed.serve.timeout_seconds, changed.serve.port) == (
        3,
        600,
        9100,
    )
    assert changed.serve.idle is not None
    assert changed.serve.idle.max_idle_seconds == 120


def test_project_flags_validate_the_memory_through_the_project() -> None:
    the_project = project()

    assert apply_project_flags(the_project, memory=4096, env={}, allow_out=[]).memory_mib == 4096
    with pytest.raises(InvalidArgumentException):
        apply_project_flags(the_project, memory=128, env={}, allow_out=[])


def test_env_and_allow_out_need_the_project_fields() -> None:
    with pytest.raises(InvalidArgumentException, match="--env"):
        apply_project_flags(project(), memory=None, env={"A": "b"}, allow_out=[])
    with pytest.raises(InvalidArgumentException, match="--allow-out"):
        apply_project_flags(project(), memory=None, env={}, allow_out=["api.example.com"])


def test_the_token_from_the_environment_is_not_written_anywhere(tmp_path: Path) -> None:
    token = generate_access_token()

    result = deploy_token(None, {TOKEN_ENV_VAR: token})

    assert result == (token, None)


def test_a_token_file_is_created_with_a_new_token_or_the_environment_one(
    tmp_path: Path,
) -> None:
    from_env = generate_access_token()
    created = tmp_path / "uno"
    reused = tmp_path / "dos"

    new_token, written = deploy_token(created, {})
    env_token, _ = deploy_token(reused, {TOKEN_ENV_VAR: from_env})

    assert written == created
    assert created.read_text() == new_token
    assert reused.read_text() == from_env == env_token


def test_an_existing_explicit_token_file_is_an_error(tmp_path: Path) -> None:
    existing = tmp_path / "token"
    existing.write_text("viejo")

    with pytest.raises(TokenFileError):
        deploy_token(existing, {})

    assert existing.read_text() == "viejo"


def test_the_default_token_file_is_created_private_and_then_reused(tmp_path: Path) -> None:
    target = tmp_path / ".rayito" / "agent-token"

    first = default_token(target)
    again = default_token(target)

    assert first == again
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.parent.stat().st_mode & 0o777 == 0o700


def test_an_empty_default_token_file_is_an_error(tmp_path: Path) -> None:
    target = tmp_path / "agent-token"
    target.write_text("\n")

    with pytest.raises(TokenFileError, match="vacío"):
        default_token(target)


def test_the_default_token_file_lives_inside_the_project() -> None:
    assert Path(".rayito/agent-token") == AGENT_TOKEN_FILE_DEFAULT


def test_the_plan_text_names_sources_never_secret_names() -> None:
    plan = plan_deploy(
        project=project(),
        options=AgentDeployOptions(scaling=ScalingOptions(max_vms=2, warm_vms=1)),
        region="us-east-1",
    )

    text = "\n".join(describe_plan(plan))

    assert "región: us-east-1" in text
    assert "VMs: mínimo 1, máximo 2, calientes 1, sesiones shared" in text
    assert "secreto de la pasarela bedrock: el del proyecto" in text
    assert "bedrock-key" not in text
    assert "coste de lista" in text
    assert "la memoria se fija en la imagen" in text


def test_usd_uses_a_decimal_comma() -> None:
    assert format_usd(0.1261) == "$0,1261"


def test_detection_lines_skip_evidence_lists_and_empty_values() -> None:
    @dataclass(frozen=True)
    class Report:
        language: str = "python"
        framework: str = ""
        port: int = 8000
        evidence: tuple[str, ...] = ("pyproject.toml",)
        warnings: list[str] = field(default_factory=lambda: ["aviso"])

    assert detection_lines(Report()) == ["detectado language: python", "detectado port: 8000"]
    assert detection_lines(None) == []
    assert detection_lines(Report) == []


# ------------------------------------------------------------ fleet commands


@dataclass
class FakeFleetStatus:
    name: str = FLEET_NAME
    members: tuple[Any, ...] = ()
    min_vms: int = 1
    max_vms: int = 2


@dataclass
class FakeMember:
    sandbox_id: str = SANDBOX_ID
    state: str = "unknown"


@dataclass
class FakeResult:
    session_id: str = f"rs1.{SANDBOX_ID}.ses_1"
    text: str = "hola"
    steps: int = 1
    usage: Any = None
    usage_reported: bool = False
    exit_code: int = 0


@dataclass
class FakeFleetClient:
    calls: list[tuple[str, Any]] = field(default_factory=list)
    sandbox_ids: tuple[str, ...] = (SANDBOX_ID, "microvm-00000000-0000-0000-0000-000000000002")
    busy: Exception | None = None

    def invoke(self, prompt: str, *, session_id: str | None = None) -> FakeResult:
        self.calls.append(("invoke", (prompt, session_id)))
        if self.busy is not None:
            raise self.busy
        return FakeResult()

    def status(self) -> FakeFleetStatus:
        return FakeFleetStatus(members=(FakeMember(), FakeMember("microvm-b", "healthy")))

    def stop(self) -> None:
        self.calls.append(("stop", None))


def install_fleet(monkeypatch: pytest.MonkeyPatch, fleet: FakeFleetClient) -> list[dict[str, Any]]:
    connected: list[dict[str, Any]] = []

    def connect(name: str, **kwargs: Any) -> FakeFleetClient:
        connected.append({"name": name, **kwargs})
        return fleet

    monkeypatch.setattr(
        agent_cli, "AgentFleet", type("FakeAgentFleet", (), {"connect": staticmethod(connect)})
    )
    return connected


def test_invoke_with_a_fleet_name_connects_the_fleet_with_the_token(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner, clients: Clients
) -> None:
    token = generate_access_token()
    monkeypatch.setenv(TOKEN_ENV_VAR, token)
    fleet = FakeFleetClient()
    connected = install_fleet(monkeypatch, fleet)

    result = runner.invoke(
        app, ["agent", "invoke", FLEET_NAME, "hola", "--session-id", "s"], obj=clients
    )

    assert result.exit_code == 0, result.output
    assert connected[0]["name"] == FLEET_NAME
    assert connected[0]["access_token"] == token
    assert fleet.calls == [("invoke", ("hola", "s"))]
    assert f"session_id=rs1.{SANDBOX_ID}.ses_1" in result.output


def test_invoke_with_a_sandbox_id_still_connects_one_vm(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner, clients: Clients
) -> None:
    monkeypatch.setenv(TOKEN_ENV_VAR, generate_access_token())
    connected = install_fleet(monkeypatch, FakeFleetClient())
    seen: list[str] = []

    def connect(sandbox_id: str, **kwargs: Any) -> Any:
        seen.append(sandbox_id)
        return type("D", (), {"invoke": lambda self, *a, **k: FakeResult()})()

    monkeypatch.setattr(
        agent_cli, "AgentDeployment", type("D", (), {"connect": staticmethod(connect)})
    )

    runner.invoke(app, ["agent", "invoke", SANDBOX_ID, "hola"], obj=clients)

    assert seen == [SANDBOX_ID]
    assert connected == []


def test_a_fleet_without_capacity_exits_one(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner, clients: Clients
) -> None:
    from rayito.exceptions import CapacityException

    monkeypatch.setenv(TOKEN_ENV_VAR, generate_access_token())
    install_fleet(
        monkeypatch, FakeFleetClient(busy=CapacityException("sin cupo", code="agent_capacity"))
    )

    result = runner.invoke(app, ["agent", "invoke", FLEET_NAME, "hola"], obj=clients)

    assert result.exit_code == 1
    assert "sin cupo" in result.output


def test_a_busy_agent_is_reported_like_before(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner, clients: Clients
) -> None:
    monkeypatch.setenv(TOKEN_ENV_VAR, generate_access_token())
    install_fleet(monkeypatch, FakeFleetClient(busy=AgentException("ocupado", reason="busy")))

    result = runner.invoke(app, ["agent", "invoke", FLEET_NAME, "hola"], obj=clients)

    assert result.exit_code == 1


def test_invoke_uses_the_default_token_file_when_there_is_no_other_token(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner, clients: Clients, tmp_path: Path
) -> None:
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    monkeypatch.chdir(tmp_path)
    token = default_token(AGENT_TOKEN_FILE_DEFAULT)
    connected = install_fleet(monkeypatch, FakeFleetClient())

    runner.invoke(app, ["agent", "invoke", FLEET_NAME, "hola"], obj=clients)

    assert connected[0]["access_token"] == token


def test_status_of_a_fleet_lists_its_members(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner, clients: Clients
) -> None:
    monkeypatch.setenv(TOKEN_ENV_VAR, generate_access_token())
    install_fleet(monkeypatch, FakeFleetClient())

    text = runner.invoke(app, ["agent", "status", FLEET_NAME], obj=clients)
    as_json = runner.invoke(app, ["--json", "agent", "status", FLEET_NAME], obj=clients)

    assert "members: 2 (min 1, max 2)" in text.output
    assert f"sandbox_id={SANDBOX_ID} state=unknown" in text.output
    assert json.loads(as_json.output)["members"][1]["state"] == "healthy"


def test_stop_of_a_fleet_needs_no_token_and_terminates_every_vm(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner, clients: Clients
) -> None:
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    fleet = FakeFleetClient()
    connected = install_fleet(monkeypatch, fleet)

    result = runner.invoke(app, ["agent", "stop", FLEET_NAME], obj=clients)

    assert result.exit_code == 0, result.output
    assert "access_token" not in connected[0]
    assert fleet.calls == [("stop", None)]
    assert "parados 2 VM(s)" in result.output


def test_stop_of_a_sandbox_id_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner, clients: Clients
) -> None:
    monkeypatch.setattr(
        agent_cli, "kill_one", lambda _clients, sandbox_id: sandbox_id == SANDBOX_ID
    )

    result = runner.invoke(app, ["agent", "stop", SANDBOX_ID], obj=clients)

    assert f"parado sandbox_id={SANDBOX_ID}" in result.output
