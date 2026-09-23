"""Los nombres de E2B 2.51 que exporta `rayito.e2b` (design D4 y D13 de
`m9-e2b-v2-surface`): cada import funciona, los alias son las clases
nativas y ningún nombre del pool se cuela."""

from __future__ import annotations

import importlib

import pytest

import rayito
import rayito.e2b
import rayito.e2b.exceptions
from rayito.exceptions import (
    AuthenticationException,
    CapacityException,
    DiskFullException,
    SandboxException,
    TransferException,
)

E2B_NAMES = [
    "Sandbox",
    "AsyncSandbox",
    "E2B",
    "ConnectionConfig",
    "get_signature",
    "ALL_TRAFFIC",
    "Execution",
    "Result",
    "Logs",
    "OutputMessage",
    "ExecutionError",
    "Context",
    "CommandResult",
    "CommandHandle",
    "AsyncCommandHandle",
    "CommandExitException",
    "ProcessInfo",
    "SandboxException",
    "TimeoutException",
    "NotFoundException",
    "FileNotFoundException",
    "SandboxNotFoundException",
    "AuthenticationException",
    "InvalidArgumentException",
    "RateLimitException",
    "NotEnoughSpaceException",
    "ServiceBusyException",
    "FileUploadException",
    "GitAuthException",
    "GitUpstreamException",
    "TemplateException",
    "BuildException",
    "UnimplementedError",
    "RayitoCompatWarning",
    "FilesystemEvent",
    "FilesystemEventType",
    "EntryInfo",
    "WriteInfo",
    "WriteEntry",
    "FileType",
    "WatchHandle",
    "AsyncWatchHandle",
    "PtySize",
    "SandboxInfo",
    "ListedSandbox",
    "SandboxState",
    "SandboxQuery",
    "SandboxMetrics",
    "SandboxPaginator",
    "AsyncSandboxPaginator",
    "Git",
    "GitStatus",
    "GitBranches",
    "GitFileStatus",
    "GitResetMode",
    "Stdout",
    "Stderr",
    "PtyOutput",
    "OutputHandler",
    "Username",
    "MIMEType",
    "RunCodeLanguage",
    "Template",
    "AsyncTemplate",
    "Volume",
    "AsyncVolume",
    "Secret",
    "AsyncSecret",
    "Chart",
    "Chart2D",
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

EXCEPTION_NAMES = [
    "SandboxException",
    "TimeoutException",
    "NotFoundException",
    "FileNotFoundException",
    "SandboxNotFoundException",
    "AuthenticationException",
    "InvalidArgumentException",
    "RateLimitException",
    "CommandExitException",
    "FileUploadException",
    "GitAuthException",
    "GitUpstreamException",
    "UnimplementedError",
    "NotEnoughSpaceException",
    "ServiceBusyException",
    "TemplateException",
    "BuildException",
    "RayitoCompatWarning",
]


@pytest.mark.parametrize("name", E2B_NAMES)
def test_every_e2b_name_imports(name: str) -> None:
    assert name in rayito.e2b.__all__
    assert getattr(rayito.e2b, name) is not None


@pytest.mark.parametrize("name", EXCEPTION_NAMES)
def test_every_exception_imports_from_e2b_exceptions(name: str) -> None:
    assert name in rayito.e2b.exceptions.__all__
    assert getattr(rayito.e2b.exceptions, name) is getattr(rayito.e2b, name)


def test_every_all_entry_imports() -> None:
    module = importlib.import_module("rayito.e2b")
    assert set(rayito.e2b.__all__) >= set(E2B_NAMES)
    for name in rayito.e2b.__all__:
        assert hasattr(module, name), name
    for name in rayito.e2b.exceptions.__all__:
        assert hasattr(rayito.e2b.exceptions, name), name


def test_aliases_are_the_native_classes() -> None:
    e2b = rayito.e2b
    assert e2b.NotEnoughSpaceException is DiskFullException
    assert e2b.ServiceBusyException is CapacityException
    assert e2b.UnimplementedError is rayito.UnimplementedError
    assert e2b.Git is rayito.Git
    assert e2b.Context is rayito.CodeContext
    assert e2b.WriteInfo is rayito.EntryInfo
    assert e2b.ALL_TRAFFIC == rayito.ALL_TRAFFIC == "0.0.0.0/0"
    assert e2b.FileUploadException is rayito.FileUploadException
    assert e2b.AsyncCommandHandle is rayito.AsyncCommandHandle


def test_exception_hierarchy_follows_d13() -> None:
    e2b = rayito.e2b
    assert issubclass(e2b.GitAuthException, AuthenticationException)
    assert issubclass(e2b.GitUpstreamException, SandboxException)
    assert issubclass(e2b.BuildException, Exception)
    assert not issubclass(e2b.BuildException, SandboxException)
    assert issubclass(e2b.TemplateException, SandboxException)
    assert issubclass(e2b.FileUploadException, TransferException)
    assert issubclass(e2b.UnimplementedError, NotImplementedError)
    assert not issubclass(e2b.UnimplementedError, SandboxException)
    assert issubclass(e2b.RayitoCompatWarning, UserWarning)
    assert "nunca" in (e2b.BuildException.__doc__ or "")
    assert "nunca" in (e2b.TemplateException.__doc__ or "")


def test_the_shim_unimplemented_error_is_the_native_class() -> None:
    """Un `except rayito.e2b.UnimplementedError` atrapa lo que lanza el SDK
    nativo y viceversa: una sola clase, no una subclase del shim."""
    native = rayito.UnimplementedError(
        "upload_url", "configura transfer=S3Staging(...) o RAYITO_TRANSFER_BUCKET"
    )
    with pytest.raises(rayito.e2b.UnimplementedError):
        raise native
    assert issubclass(rayito.e2b.exceptions.UnimplementedError, rayito.UnimplementedError)


def test_type_aliases_follow_e2b() -> None:
    e2b = rayito.e2b
    assert e2b.Username is str and e2b.Stdout is str and e2b.Stderr is str
    assert e2b.MIMEType is str
    assert e2b.PtyOutput is bytes
    handler: e2b.OutputHandler[str] = print
    assert callable(handler)
    assert vars(e2b)["OutputHandler"][str] is not None
    assert "typescript" in str(e2b.RunCodeLanguage)


def test_no_name_mentions_the_pool() -> None:
    assert not any("Pool" in name for name in rayito.e2b.__all__)
    assert not any("Pool" in name for name in rayito.e2b.exceptions.__all__)


def test_the_module_docstring_names_the_2x_contract() -> None:
    doc = rayito.e2b.__doc__ or ""
    assert "E2B 2.x" in doc
    assert "2.51.0" in doc and "2.10.0" in doc
