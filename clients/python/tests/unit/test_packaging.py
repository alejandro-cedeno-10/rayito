"""Metadatos del paquete: paridad de versión con `pyproject.toml`, el `__all__`
de `rayito.e2b` y que cada nombre de E2B se importe (design D8 item 10)."""

from __future__ import annotations

import dataclasses
import importlib
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import rayito
import rayito.e2b
import rayito.e2b.exceptions

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"

E2B_NAMES = [
    "Sandbox",
    "AsyncSandbox",
    "Execution",
    "Result",
    "Logs",
    "OutputMessage",
    "ExecutionError",
    "Context",
    "CommandResult",
    "CommandHandle",
    "CommandExitException",
    "ProcessInfo",
    "SandboxException",
    "TimeoutException",
    "NotFoundException",
    "AuthenticationException",
    "InvalidArgumentException",
    "RateLimitException",
    "NotEnoughSpaceException",
    "TemplateException",
    "UnimplementedError",
    "RayitoCompatWarning",
    "FilesystemEvent",
    "FilesystemEventType",
    "EntryInfo",
    "WriteInfo",
    "WriteEntry",
    "FileType",
    "WatchHandle",
    "PtySize",
    "SandboxInfo",
    "ListedSandbox",
    "SandboxState",
    "SandboxQuery",
    "SandboxMetrics",
    "SandboxPaginator",
    "AsyncSandboxPaginator",
    "Chart",
    "ChartType",
    "BarChart",
    "LineChart",
    "PieChart",
    "ScatterChart",
    "BoxAndWhiskerChart",
    "SuperChart",
    "ScaleType",
    "BarData",
    "PieData",
    "PointData",
    "BoxAndWhiskerData",
]

E2B_V2_NAMES = [
    "ALL_TRAFFIC",
    "AsyncCommandHandle",
    "AsyncWatchHandle",
    "AsyncSecret",
    "AsyncTemplate",
    "AsyncVolume",
    "BuildException",
    "Chart2D",
    "ConnectionConfig",
    "E2B",
    "FileNotFoundException",
    "FileUploadException",
    "Git",
    "GitAuthException",
    "GitBranches",
    "GitFileStatus",
    "GitResetMode",
    "GitStatus",
    "GitUpstreamException",
    "MIMEType",
    "OutputHandler",
    "PtyOutput",
    "RunCodeLanguage",
    "SandboxNotFoundException",
    "Secret",
    "ServiceBusyException",
    "Stderr",
    "Stdout",
    "Template",
    "Username",
    "Volume",
    "get_signature",
]

E2B_SIBLING_NAMES = ["UploadTicket", "DownloadLink"]

E2B_EXCEPTION_NAMES = [
    "SandboxException",
    "TimeoutException",
    "NotFoundException",
    "AuthenticationException",
    "InvalidArgumentException",
    "RateLimitException",
    "CommandExitException",
    "NotEnoughSpaceException",
    "TemplateException",
    "UnimplementedError",
    "RayitoCompatWarning",
    "BuildException",
    "FileNotFoundException",
    "FileUploadException",
    "GitAuthException",
    "GitUpstreamException",
    "SandboxNotFoundException",
    "ServiceBusyException",
]


def test_version_matches_pyproject() -> None:
    with PYPROJECT.open("rb") as handle:
        project = tomllib.load(handle)["project"]
    assert project["version"] == rayito.__version__
    assert project["requires-python"] == ">=3.11"
    assert "Typing :: Typed" in project["classifiers"]
    assert {"Homepage", "Repository", "Documentation", "Changelog"} <= set(project["urls"])


def test_cli_extra_and_dev_group_pin_typer_identically() -> None:
    with PYPROJECT.open("rb") as handle:
        document = tomllib.load(handle)
    project = document["project"]
    assert project["optional-dependencies"]["cli"] == ["typer>=0.15,<1"]
    assert "typer>=0.15,<1" in document["dependency-groups"]["dev"]
    assert project["scripts"]["rayito"] == "rayito.cli.__main__:main"


def test_license_is_apache_2_expression() -> None:
    with PYPROJECT.open("rb") as handle:
        project = tomllib.load(handle)["project"]
    assert project["license"] == "Apache-2.0"
    assert project["license-files"] == ["LICENSE", "NOTICE"]
    assert not [c for c in project["classifiers"] if c.startswith("License ::")]


@pytest.mark.parametrize("name", [*E2B_NAMES, *E2B_V2_NAMES, *E2B_SIBLING_NAMES])
def test_e2b_names_importable(name: str) -> None:
    module = importlib.import_module("rayito.e2b")
    assert getattr(module, name) is not None


@pytest.mark.parametrize("name", E2B_EXCEPTION_NAMES)
def test_e2b_exception_names_importable(name: str) -> None:
    module = importlib.import_module("rayito.e2b.exceptions")
    assert issubclass(getattr(module, name), BaseException | Warning)


def test_e2b_all_matches_the_proposal_list() -> None:
    assert set(rayito.e2b.__all__) == {*E2B_NAMES, *E2B_V2_NAMES, *E2B_SIBLING_NAMES}
    assert set(rayito.e2b.exceptions.__all__) == set(E2B_EXCEPTION_NAMES)


def test_native_names_are_reexported_not_copied() -> None:
    assert rayito.e2b.Execution is rayito.Execution
    assert rayito.e2b.CommandHandle is rayito.CommandHandle
    assert rayito.e2b.WatchHandle is rayito.WatchHandle
    assert rayito.e2b.Context is rayito.CodeContext
    assert rayito.e2b.WriteInfo is rayito.EntryInfo
    assert rayito.e2b.SandboxException is rayito.SandboxException
    assert rayito.e2b.ListedSandbox is rayito.e2b.SandboxInfo
    assert [field.name for field in dataclasses.fields(rayito.e2b.PtySize)] == ["rows", "cols"]


def test_mcp_extra_and_script() -> None:
    with PYPROJECT.open("rb") as handle:
        pyproject = tomllib.load(handle)
    project = pyproject["project"]
    assert project["optional-dependencies"]["mcp"] == ["mcp>=2.2,<3"]
    assert project["scripts"]["rayito-mcp"] == "rayito.mcp.__main__:main"
    assert "rayito[mcp]" in pyproject["dependency-groups"]["dev"]
    runtime = [dependency.split(">=")[0] for dependency in project["dependencies"]]
    assert runtime == ["grpcio", "protobuf", "boto3"]


def test_import_rayito_does_not_import_mcp() -> None:
    completed = subprocess.run(
        [sys.executable, "-c", "import rayito, sys; raise SystemExit('mcp' in sys.modules)"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
