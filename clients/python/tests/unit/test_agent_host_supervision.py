"""La supervisión de las apps de larga vida del host (`process.py`, 0.14 D6):
el presupuesto de relanzamientos (`RestartBudget`, puro y con reloj de
mentira), el `Supervisor` con procesos reales, y el driver `http-app` de punta a
punta: una app que muere se relanza con espera creciente, mientras tanto
`/ping` es `HealthyBusy` y las invocaciones reciben `503` con `Retry-After`, y
pasado el presupuesto `/ping` es `503` y el marcador de fallo lleva
`app_crashloop` con la cola saneada de su salida de error. El host se prueba una
vez, aquí (`parity-checklist.md`)."""

from __future__ import annotations

import http.client
import json
import signal
import socket
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from rayito._agent import _runner
from rayito._agent._runner.agent_host import process, serve
from rayito._agent._runner.agent_host.diagnostics import REDACTED
from rayito._agent._runner.agent_host.drivers import http_app
from rayito._agent._runner.agent_host.process import (
    ChildProcess,
    RestartBudget,
    RestartPolicy,
    Supervisor,
)
from rayito._agent._runner.agent_host.protocol import AgentFailure
from rayito._limits import (
    AGENT_APP_RESTART_BACKOFF_MAX_SECONDS,
    AGENT_APP_RESTART_BACKOFF_SECONDS,
    AGENT_APP_RESTART_MAX,
    AGENT_APP_RESTART_WINDOW_SECONDS,
)
from tests.unit.agent_host_support import (
    free_port,
    invoke,
    json_app_driver,
    request,
    running_service,
    wait_for,
)

POLICY = RestartPolicy(
    max_restarts=3, window_seconds=100.0, backoff_seconds=2.0, backoff_max_seconds=30.0
)
HOST_DIR = Path(_runner.__file__).parent / "agent_host"
START_TIMEOUT_SECONDS = 20
LIFETIME_SECONDS = 0.4
CRASH_AFTER_SECONDS = 0.6
SECRET_LINE = "token=" + "s3cr3t-value-" + "0123456789"


def test_restart_constants_match_limits_json() -> None:
    assert process.DEFAULT_RESTART_MAX == AGENT_APP_RESTART_MAX
    assert process.DEFAULT_RESTART_WINDOW_SECONDS == AGENT_APP_RESTART_WINDOW_SECONDS
    assert process.DEFAULT_RESTART_BACKOFF_SECONDS == AGENT_APP_RESTART_BACKOFF_SECONDS
    assert process.DEFAULT_RESTART_BACKOFF_MAX_SECONDS == AGENT_APP_RESTART_BACKOFF_MAX_SECONDS


def test_the_budget_grows_the_wait_and_gives_up_past_the_limit() -> None:
    budget = RestartBudget(POLICY)
    assert [budget.record_death(now) for now in (0.0, 1.0, 2.0)] == [2.0, 4.0, 8.0]
    assert budget.record_death(3.0) is None


def test_deaths_outside_the_window_do_not_count() -> None:
    budget = RestartBudget(POLICY)
    for now in (0.0, 1.0, 2.0):
        budget.record_death(now)
    assert budget.record_death(2.0 + POLICY.window_seconds + 1) == POLICY.backoff_seconds


def test_the_wait_is_capped() -> None:
    policy = RestartPolicy(
        max_restarts=10, window_seconds=1000.0, backoff_seconds=2.0, backoff_max_seconds=5.0
    )
    budget = RestartBudget(policy)
    assert [budget.record_death(float(now)) for now in range(5)] == [2.0, 4.0, 5.0, 5.0, 5.0]


def test_no_restarts_gives_up_at_the_first_death() -> None:
    budget = RestartBudget(RestartPolicy(max_restarts=0))
    assert budget.record_death(0.0) is None


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({}, RestartPolicy()),
        ({"restart_max": 1}, RestartPolicy(max_restarts=1)),
        ({"restart_max": -1, "restart_backoff_seconds": "x"}, RestartPolicy()),
        ({"restart_max": True}, RestartPolicy()),
        (
            {
                "restart_max": 5,
                "restart_window_seconds": 10,
                "restart_backoff_seconds": 0.5,
                "restart_backoff_max_seconds": 3,
            },
            RestartPolicy(
                max_restarts=5, window_seconds=10.0, backoff_seconds=0.5, backoff_max_seconds=3.0
            ),
        ),
    ],
)
def test_the_policy_reads_the_driver_config(
    config: dict[str, Any], expected: RestartPolicy
) -> None:
    assert RestartPolicy.from_config(config) == expected


def _script(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    return path


def _spawn(script: Path, *args: str) -> Any:
    return lambda: ChildProcess(
        [sys.executable, str(script), *args],
        env={},
        capture_stdout=False,
        capture_stderr_tail=True,
    )


def _supervisor(
    tmp_path: Path, source: str, *, policy: RestartPolicy, ready: Path | None = None
) -> Supervisor:
    script = _script(tmp_path, "child.py", source)
    marker = ready or tmp_path / "ready"
    return Supervisor(
        _spawn(script, str(marker)),
        marker.exists,
        failure_codes=("child_exited", "child_not_ready"),
        policy=policy,
        start_timeout_seconds=START_TIMEOUT_SECONDS,
        environ={},
    )


DIES_AFTER_READY = f"""
import pathlib, sys, time
pathlib.Path(sys.argv[1]).write_text("ok")
time.sleep({LIFETIME_SECONDS})
print({SECRET_LINE!r}, file=sys.stderr)
print("boom", file=sys.stderr)
"""
STAYS_UP = """
import pathlib, sys, time
pathlib.Path(sys.argv[1]).write_text("ok")
time.sleep(60)
"""
FAST = RestartPolicy(
    max_restarts=2, window_seconds=60.0, backoff_seconds=0.05, backoff_max_seconds=0.1
)


def test_a_child_that_stays_up_is_never_restarted(tmp_path: Path) -> None:
    supervisor = _supervisor(tmp_path, STAYS_UP, policy=FAST)
    try:
        supervisor.start()
        assert supervisor.state == process.STATE_RUNNING
        assert supervisor.restarts == 0
    finally:
        supervisor.stop()
    assert supervisor.state == process.STATE_STOPPED


def test_a_child_that_dies_is_restarted_until_the_budget_runs_out(tmp_path: Path) -> None:
    supervisor = _supervisor(tmp_path, DIES_AFTER_READY, policy=FAST)
    failures: list[tuple[str, str | None]] = []
    supervisor.add_failure_listener(lambda code, text: failures.append((code, text)))
    try:
        supervisor.start()
        assert wait_for(lambda: supervisor.state == process.STATE_FAILED)
    finally:
        supervisor.stop()
    assert supervisor.restarts == FAST.max_restarts
    ((code, diagnostics),) = failures
    assert code == process.CRASHLOOP_CODE
    assert diagnostics is not None
    assert "boom" in diagnostics


def test_the_crashloop_diagnostics_are_redacted(tmp_path: Path) -> None:
    script = _script(tmp_path, "child.py", DIES_AFTER_READY)
    marker = tmp_path / "ready"
    supervisor = Supervisor(
        _spawn(script, str(marker)),
        marker.exists,
        failure_codes=("child_exited", "child_not_ready"),
        policy=FAST,
        start_timeout_seconds=START_TIMEOUT_SECONDS,
        environ={"APP_SECRET": SECRET_LINE.split("=", 1)[1]},
    )
    failures: list[tuple[str, str | None]] = []
    supervisor.add_failure_listener(lambda code, text: failures.append((code, text)))
    try:
        supervisor.start()
        assert wait_for(lambda: bool(failures))
    finally:
        supervisor.stop()
    assert SECRET_LINE.split("=", 1)[1] not in str(failures)
    assert REDACTED in str(failures)


def test_a_child_that_exits_during_startup_is_not_restarted(tmp_path: Path) -> None:
    supervisor = _supervisor(
        tmp_path,
        """
        import sys
        print("no puedo arrancar", file=sys.stderr)
        sys.exit(3)
        """,
        policy=FAST,
    )
    with pytest.raises(AgentFailure) as error:
        supervisor.start()
    assert error.value.detail_code == "child_exited"
    assert error.value.diagnostics is not None
    assert "no puedo arrancar" in error.value.diagnostics
    assert supervisor.restarts == 0
    assert supervisor.state == process.STATE_STOPPED


def test_a_child_that_never_answers_is_not_ready(tmp_path: Path) -> None:
    script = _script(tmp_path, "child.py", "import time\ntime.sleep(60)\n")
    supervisor = Supervisor(
        _spawn(script),
        lambda: False,
        failure_codes=("child_exited", "child_not_ready"),
        start_timeout_seconds=0.5,
        environ={},
    )
    with pytest.raises(AgentFailure) as error:
        supervisor.start()
    assert error.value.detail_code == "child_not_ready"


def _crash(driver: http_app.Driver) -> None:
    connection = http.client.HTTPConnection("127.0.0.1", int(driver._config["port"]), timeout=5)
    try:
        connection.request("GET", "/crash")
        connection.getresponse()
    except OSError:
        pass
    finally:
        connection.close()


def _ping(port: int) -> tuple[int, str]:
    status, _, body = request(port, "GET", "/ping")
    return status, json.loads(body)["status"]


def _answer(port: int, prompt: str) -> object:
    _, _, payload = invoke(port, prompt)
    return payload.get("text")


def test_a_crashed_app_is_restarted_and_served_again(tmp_path: Path) -> None:
    driver = json_app_driver(
        tmp_path,
    )
    driver.warm()
    with running_service(driver) as (_, port):
        try:
            assert _answer(port, "hola") == "eco: hola"
            _crash(driver)
            assert wait_for(lambda: _answer(port, "otra") == "eco: otra")
            assert _ping(port) == (200, serve.STATUS_HEALTHY)
        finally:
            driver.cancel()


def test_while_the_app_restarts_the_host_is_busy_and_rejects_with_503(tmp_path: Path) -> None:
    driver = json_app_driver(
        tmp_path, restart_backoff_seconds=1.5, restart_backoff_max_seconds=1.5, restart_max=3
    )
    driver.warm()
    with running_service(driver) as (_, port):
        try:
            _crash(driver)
            assert wait_for(lambda: driver.availability() == http_app.RESTARTING)
            assert _ping(port) == (200, serve.STATUS_BUSY)
            status, headers, payload = invoke(port, "x")
            assert (status, payload) == (503, {"error": "app_restarting"})
            assert headers["retry-after"] == str(serve.RETRY_AFTER_SECONDS)
            assert wait_for(lambda: driver.availability() == http_app.AVAILABLE)
        finally:
            driver.cancel()


def test_a_crash_loop_makes_the_host_unhealthy_and_leaves_a_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fatal = "s3cr3t-" + "9876543210"
    monkeypatch.setenv("APP_FATAL", fatal)
    monkeypatch.setenv("CRASH_AFTER", str(CRASH_AFTER_SECONDS))
    driver = json_app_driver(tmp_path, restart_max=1)
    marker = tmp_path / "host" / "ready"
    config = serve.ServiceConfig(ready_marker=str(marker))
    driver.warm()
    with running_service(driver, config=config) as (service, port):
        try:
            assert wait_for(lambda: driver.availability() == http_app.FAILED)
            assert _ping(port) == (503, serve.STATUS_UNHEALTHY)
            status, _, payload = invoke(port, "x")
            assert (status, payload) == (503, {"error": "app_crashloop"})
            failed = Path(str(marker) + serve.FAILED_SUFFIX).read_text("utf-8")
            assert failed.splitlines()[0] == process.CRASHLOOP_CODE
            assert "fatal" in failed
            assert fatal not in failed
            assert service.availability() == serve.AVAILABILITY_FAILED
        finally:
            driver.cancel()


def _accepts_connections(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
    except OSError:
        return False
    return True


def test_the_host_process_takes_its_app_down_with_it(tmp_path: Path) -> None:
    app = json_app_driver(tmp_path)
    app_port = int(app._config["port"])
    host_json = tmp_path / "host.json"
    marker = tmp_path / "state" / "ready"
    host_json.write_text(
        json.dumps(
            {
                "v": 1,
                "driver": "http-app",
                "options": {**app._config, "command": app._config["command"]},
                "ready_marker": str(marker),
                "queue_max": 4,
                "queue_timeout_seconds": 5,
            }
        )
    )
    host_port = free_port()
    process = subprocess.Popen(
        [sys.executable, str(HOST_DIR), "serve", str(host_json), "--port", str(host_port)],
        stderr=subprocess.PIPE,
    )
    try:
        assert wait_for(marker.exists, START_TIMEOUT_SECONDS)
        assert _accepts_connections(app_port)
        assert invoke(host_port, "hola")[2]["text"] == "eco: hola"
    finally:
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=START_TIMEOUT_SECONDS)
    assert process.returncode == 0
    assert wait_for(lambda: not _accepts_connections(app_port))
