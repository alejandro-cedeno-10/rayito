"""El host del agente con `RAYITO_SECRET_ENV` (ADR-033 D4,
`_runner/agent_host/secret_env.py`): exporta los secretos antes de crear el
driver, saca la especificación de su entorno, falla en cerrado con cada código
sin nombrar nunca un secreto ni un valor, sólo registra `count=N` y redacta los
valores cortos en el diagnóstico. La herramienta es un script de doble que
corre de verdad como subproceso; `aws_secret_env.py` se prueba con un botocore
de mentira."""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from rayito._agent._recipes import asset_bytes
from rayito._agent._runner.agent_host import __main__ as host_main
from rayito._agent._runner.agent_host import diagnostics, serve
from rayito._agent._runner.agent_host import secret_env as host
from rayito._agent._secret_env_domain import SCRIPT_NAME
from rayito._limits import AGENT_SECRET_ENV_REDACT_MIN_CHARS
from tests.unit.fake_agentcore import CONTRACT

SECRET_ENV_CONSTANTS: dict[str, Any] = CONTRACT["constants"]["secretEnv"]
PACKAGE = "rayito._agent._runner.agent_host"
DATABASE_URL = "postgres://db.internal.example/app"
SHORT_VALUE = "s3cr3"
SPEC = json.dumps(
    {
        "env": {
            "DATABASE_URL": {"secret": "app/db-url"},
            "TICKETS_TOKEN": {"secret": "app/tickets", "key": "token"},
        },
        "from": [{"secret": "app/config", "prefix": "APP_"}],
        "v": 1,
    }
)
TOKEN_JSON = json.dumps({"token": "x"})
VALUES = [DATABASE_URL, json.dumps({"token": SHORT_VALUE}), json.dumps({"MODE": "demo"})]
HELPER_TEMPLATE = """
import json, sys
request = json.load(sys.stdin)
with open({log!r}, "w") as log:
    json.dump(request, log)
code = {code}
if code:
    sys.exit(code)
sys.stdout.write(json.dumps({{"values": {values!r}}}))
"""


def _helper(tmp_path: Path, *, code: int = 0, values: list[str] | None = None) -> Path:
    script = tmp_path / "fake_secret_env.py"
    script.write_text(
        HELPER_TEMPLATE.format(
            log=str(tmp_path / "requests.json"), code=code, values=values or VALUES
        ),
        encoding="utf-8",
    )
    return script


def _host_config(tmp_path: Path, helper: Path | None, reserved: list[str] | None = None) -> Path:
    document: dict[str, Any] = {
        "driver": "callable",
        "options": {},
        "ready_marker": str(tmp_path / "ready"),
    }
    if helper is not None:
        document[host.HOST_KEY] = {
            "python": sys.executable,
            "script": str(helper),
            "reserved": reserved or [],
        }
    path = tmp_path / "host.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


class _Stop(Exception):
    pass


@pytest.fixture
def environ(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Un entorno propio del host para la prueba: lo que exporta no se queda
    en el proceso de pytest."""
    fresh = {"PATH": os.environ.get("PATH", ""), host.SPEC_VARIABLE: SPEC}
    monkeypatch.setattr(os, "environ", fresh)
    return fresh


def _serve(
    tmp_path: Path, config: Path, monkeypatch: pytest.MonkeyPatch, failure: str = ""
) -> tuple[int, dict[str, str]]:
    seen: dict[str, str] = {}

    def create(package: str, name: str, options: dict[str, Any]) -> Any:
        seen.update(os.environ)
        raise _Stop(failure)

    monkeypatch.setattr(host_main, "create_driver", create)
    args = argparse.Namespace(host_config=str(config), port=0)
    return host_main.serve(PACKAGE, args), seen


def _marker(tmp_path: Path) -> str:
    return (tmp_path / f"ready{serve.FAILED_SUFFIX}").read_text(encoding="utf-8")


def test_the_secrets_are_in_the_environment_before_the_driver_is_created(
    tmp_path: Path,
    environ: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = _host_config(tmp_path, _helper(tmp_path))
    with caplog.at_level(logging.INFO):
        code, seen = _serve(tmp_path, config, monkeypatch)
    assert code == 1
    assert seen["DATABASE_URL"] == DATABASE_URL
    assert seen["TICKETS_TOKEN"] == SHORT_VALUE
    assert seen["APP_MODE"] == "demo"
    assert host.SPEC_VARIABLE not in seen
    assert host.SPEC_VARIABLE not in environ
    requests = json.loads((tmp_path / "requests.json").read_text(encoding="utf-8"))
    assert requests == {
        "secrets": [{"secret": "app/db-url"}, {"secret": "app/tickets"}, {"secret": "app/config"}]
    }
    assert "secret_env_loaded count=3" in caplog.text
    for leaked in (DATABASE_URL, SHORT_VALUE, "app/db-url", "DATABASE_URL"):
        assert leaked not in caplog.text


def test_a_short_injected_value_is_redacted_from_the_startup_diagnostics(
    tmp_path: Path, environ: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _host_config(tmp_path, _helper(tmp_path))
    _serve(tmp_path, config, monkeypatch, failure=f"no pude conectar con {SHORT_VALUE}")
    marker = _marker(tmp_path)
    assert marker.startswith("_Stop")
    assert SHORT_VALUE not in marker
    assert diagnostics.REDACTED in marker


@pytest.mark.parametrize(
    ("exit_code", "detail_code"),
    [
        (host.HELPER_EXIT_DENIED, host.DENIED),
        (host.HELPER_EXIT_INVALID, host.INVALID),
        (1, host.UNAVAILABLE),
    ],
)
def test_a_failing_helper_fails_closed_with_its_code(
    tmp_path: Path,
    environ: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    exit_code: int,
    detail_code: str,
) -> None:
    config = _host_config(tmp_path, _helper(tmp_path, code=exit_code))
    code, seen = _serve(tmp_path, config, monkeypatch)
    assert code == 1
    assert seen == {}
    assert _marker(tmp_path) == f"{detail_code}\n"
    assert host.SPEC_VARIABLE not in environ
    assert "app/" not in caplog.text


@pytest.mark.parametrize(
    ("spec", "values", "reserved", "detail_code"),
    [
        ("{not json", VALUES, [], host.INVALID),
        (json.dumps({"env": {}, "from": [], "v": 2}), [], [], host.INVALID),
        (SPEC, [DATABASE_URL, "plain", json.dumps({"MODE": "x"})], [], host.INVALID),
        (SPEC, [DATABASE_URL, json.dumps({}), json.dumps({"MODE": "x"})], [], host.INVALID_KEY),
        (SPEC, [DATABASE_URL, TOKEN_JSON, json.dumps({"x-y": "z"})], [], host.INVALID_KEY),
        (SPEC, VALUES, ["APP_MODE"], host.RESERVED),
        (SPEC, VALUES[:2], [], host.UNAVAILABLE),
    ],
)
def test_every_failure_is_closed_and_names_nothing(
    tmp_path: Path,
    environ: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    spec: str,
    values: list[str],
    reserved: list[str],
    detail_code: str,
) -> None:
    environ[host.SPEC_VARIABLE] = spec
    config = _host_config(tmp_path, _helper(tmp_path, values=values), reserved)
    code, seen = _serve(tmp_path, config, monkeypatch)
    assert (code, seen) == (1, {})
    marker = _marker(tmp_path)
    assert marker == f"{detail_code}\n"
    assert "DATABASE_URL" not in environ


def test_a_spec_without_a_helper_is_unavailable(
    tmp_path: Path, environ: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    code, _ = _serve(tmp_path, _host_config(tmp_path, None), monkeypatch)
    assert code == 1
    assert _marker(tmp_path) == f"{host.UNAVAILABLE}\n"


def test_without_the_spec_the_host_starts_as_before(
    tmp_path: Path, environ: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    del environ[host.SPEC_VARIABLE]
    code, seen = _serve(tmp_path, _host_config(tmp_path, None), monkeypatch, failure="boom")
    assert code == 1
    assert _marker(tmp_path).startswith("_Stop")
    assert "DATABASE_URL" not in seen


def test_the_helper_timeout_is_unavailable() -> None:
    def run(*args: Any, **kwargs: Any) -> Any:
        assert kwargs["timeout"] == host.HELPER_TIMEOUT_SECONDS
        raise subprocess.TimeoutExpired("helper", host.HELPER_TIMEOUT_SECONDS)

    helper = host.SecretEnvHelper("python", "script", run=run)
    with pytest.raises(host.SecretEnvError) as raised:
        helper([("app/db-url", None)])
    assert raised.value.detail_code == host.UNAVAILABLE


def test_the_helper_sends_the_version_stage_only_when_set() -> None:
    sent: list[bytes] = []

    def run(argv: list[str], **kwargs: Any) -> Any:
        sent.append(kwargs["input"])
        return subprocess.CompletedProcess(argv, 0, stdout=b'{"values": ["a", "b"]}')

    helper = host.SecretEnvHelper("python", "script", run=run)
    assert helper([("one", None), ("two", "AWSPREVIOUS")]) == ["a", "b"]
    assert json.loads(sent[0]) == {
        "secrets": [{"secret": "one"}, {"secret": "two", "version_stage": "AWSPREVIOUS"}]
    }


def test_the_host_reads_its_config_key_defensively() -> None:
    assert host.helper_of(None) is None
    assert host.helper_of({"python": 1, "script": "x"}) is None
    assert host.reserved_of({"reserved": ["A", 2]}) == frozenset({"A"})
    assert host.reserved_of("nope") == frozenset()


def test_extra_values_are_redacted_down_to_the_limit() -> None:
    assert diagnostics.MIN_EXTRA_REDACTED_VALUE_LENGTH == AGENT_SECRET_ENV_REDACT_MIN_CHARS
    text = "conecté con abcd y con xyz"
    cleaned = diagnostics.sanitize_diagnostics(text, {}, ("abcd", "xyz"))
    assert "abcd" not in cleaned
    assert "xyz" in cleaned
    assert diagnostics.sanitize_diagnostics(text, {}) == text


def test_the_contract_names_the_host_key_and_the_helper() -> None:
    assert SECRET_ENV_CONSTANTS["hostConfigKey"] == host.HOST_KEY
    assert SECRET_ENV_CONSTANTS["scriptAsset"] == SCRIPT_NAME
    assert SECRET_ENV_CONSTANTS["variable"] == host.SPEC_VARIABLE
    assert SECRET_ENV_CONSTANTS["helperExitCodes"] == {
        host.DENIED: host.HELPER_EXIT_DENIED,
        host.INVALID: host.HELPER_EXIT_INVALID,
    }


# ------------------------------------------------------ aws_secret_env.py


class _ClientError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code, "Message": "secret app/db-url not found"}}


class _BotoCoreError(Exception):
    pass


class _Client:
    def __init__(self, values: dict[str, dict[str, Any]], error: Exception | None) -> None:
        self.values = values
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def get_secret_value(self, **parameters: Any) -> dict[str, Any]:
        self.calls.append(parameters)
        if self.error is not None:
            raise self.error
        return self.values[parameters["SecretId"]]


def _script(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    client: _Client,
) -> ModuleType:
    botocore = ModuleType("botocore")
    session = ModuleType("botocore.session")
    exceptions = ModuleType("botocore.exceptions")
    created: list[str] = []

    class _Session:
        def create_client(self, service: str) -> _Client:
            created.append(service)
            return client

    session.get_session = lambda: _Session()  # type: ignore[attr-defined]
    exceptions.ClientError = _ClientError  # type: ignore[attr-defined]
    exceptions.BotoCoreError = _BotoCoreError  # type: ignore[attr-defined]
    botocore.session = session  # type: ignore[attr-defined]
    botocore.exceptions = exceptions  # type: ignore[attr-defined]
    for name, module in (
        ("botocore", botocore),
        ("botocore.session", session),
        ("botocore.exceptions", exceptions),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    path = tmp_path / SCRIPT_NAME
    path.write_bytes(asset_bytes(SCRIPT_NAME))
    spec = importlib.util.spec_from_file_location("aws_secret_env_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.created = created  # type: ignore[attr-defined]
    return module


def _request(*items: dict[str, str]) -> io.StringIO:
    return io.StringIO(json.dumps({"secrets": list(items)}))


def test_the_script_reads_each_secret_with_only_the_documented_parameters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _Client({"one": {"SecretString": "a"}, "two": {"SecretString": "b"}}, None)
    module = _script(tmp_path, monkeypatch, client)
    out = io.StringIO()
    code = module.main(
        _request({"secret": "one"}, {"secret": "two", "version_stage": "AWSPREVIOUS"}), out
    )
    assert code == module.EXIT_OK
    assert json.loads(out.getvalue()) == {"values": ["a", "b"]}
    assert client.calls == [
        {"SecretId": "one"},
        {"SecretId": "two", "VersionStage": "AWSPREVIOUS"},
    ]
    assert module.created == ["secretsmanager"]


@pytest.mark.parametrize(
    ("error", "values", "expected"),
    [
        (_ClientError("AccessDeniedException"), {}, host.HELPER_EXIT_DENIED),
        (_ClientError("ResourceNotFoundException"), {}, host.HELPER_EXIT_DENIED),
        (_ClientError("ThrottlingException"), {}, 1),
        (_BotoCoreError(), {}, 1),
        (None, {"one": {"SecretBinary": b"x"}}, host.HELPER_EXIT_INVALID),
    ],
)
def test_the_script_exit_codes_are_the_hosts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception | None,
    values: dict[str, Any],
    expected: int,
) -> None:
    module = _script(tmp_path, monkeypatch, _Client(values, error))
    out = io.StringIO()
    assert module.main(_request({"secret": "one"}), out) == expected
    assert out.getvalue() == ""
    captured = capsys.readouterr()
    assert "one" not in captured.err and "app/db-url" not in captured.err


def test_the_script_refuses_a_malformed_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _script(tmp_path, monkeypatch, _Client({}, None))
    assert module.main(io.StringIO("{"), io.StringIO()) == host.HELPER_EXIT_INVALID
    assert module.main(io.StringIO('{"secrets": [1]}'), io.StringIO()) == host.HELPER_EXIT_INVALID
    assert module.EXIT_DENIED == host.HELPER_EXIT_DENIED
    assert module.EXIT_INVALID == host.HELPER_EXIT_INVALID
