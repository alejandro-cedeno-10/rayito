"""La herramienta que lee las credenciales del rol (agentcore-deploy): sus dos
salidas, las variables de entorno del puente de Node y el documento de
`credential_process` de OpenCode, con una sesión de botocore de mentira."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from rayito._agent._recipes import asset_bytes
from tests.unit.fake_agentcore import CONTRACT

CREDENTIALS: dict[str, Any] = CONTRACT["constants"]["export"]["awsCredentials"]
PROCESS: dict[str, Any] = CREDENTIALS["credentialProcess"]
EXPIRY = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


class _Frozen:
    access_key = "access"
    secret_key = "secret"
    token = "token"


class _Credentials:
    _expiry_time = EXPIRY

    def get_frozen_credentials(self) -> _Frozen:
        return _Frozen()


class _Session:
    def __init__(self, credentials: object) -> None:
        self._credentials = credentials

    def get_credentials(self) -> object:
        return self._credentials


def _load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, credentials: object) -> ModuleType:
    botocore = ModuleType("botocore")
    session = ModuleType("botocore.session")
    session.get_session = lambda: _Session(credentials)  # type: ignore[attr-defined]
    botocore.session = session  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "botocore", botocore)
    monkeypatch.setitem(sys.modules, "botocore.session", session)
    path = tmp_path / CREDENTIALS["scriptAsset"]
    path.write_bytes(asset_bytes(CREDENTIALS["scriptAsset"]))
    spec = importlib.util.spec_from_file_location("aws_env_credentials_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_default_output_is_the_environment_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load(tmp_path, monkeypatch, _Credentials())
    assert module.main([]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "AWS_ACCESS_KEY_ID": "access",
        "AWS_SECRET_ACCESS_KEY": "secret",
        "AWS_SESSION_TOKEN": "token",
    }


def test_the_credential_process_output_has_the_fields_of_the_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load(tmp_path, monkeypatch, _Credentials())
    assert module.main([PROCESS["helperArgument"]]) == 0
    document = json.loads(capsys.readouterr().out)
    assert list(document) == PROCESS["outputFields"]
    assert document["Version"] == PROCESS["outputVersion"]
    assert document["Expiration"] == EXPIRY.isoformat()


def test_it_never_reads_the_profile_that_calls_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in PROCESS["envs"].items():
        monkeypatch.setenv(name, value)
    module = _load(tmp_path, monkeypatch, _Credentials())
    module.main([PROCESS["helperArgument"]])
    assert not any(name in os.environ for name in PROCESS["envs"])


def test_without_credentials_it_exits_one_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load(tmp_path, monkeypatch, None)
    assert module.main([PROCESS["helperArgument"]]) == 1
    assert module.main([]) == 1
    assert capsys.readouterr().out == ""
