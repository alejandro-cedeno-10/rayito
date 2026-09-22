"""Shim de compatibilidad con el SDK Python de E2B (1.x).

Cambia la línea de import y el resto del programa sigue igual:

    from e2b_code_interpreter import Sandbox      ->  from rayito.e2b import Sandbox
    from e2b import Sandbox, SandboxQuery         ->  from rayito.e2b import Sandbox, SandboxQuery
    from e2b.exceptions import CommandExitException -> from rayito.e2b.exceptions import ...

Todo lo que Lambda MicroVMs no puede hacer (`set_timeout`, URLs firmadas,
historial de métricas, kernels que no sean Python, templates, `fork`...)
lanza `UnimplementedError` (un `NotImplementedError`, nunca
`SandboxException`) nombrando la feature y el motivo. Los kwargs de E2B sin
sentido en AWS (`api_key`, `domain`, `debug`, `proxy`, `secure=False`)
avisan con `RayitoCompatWarning` y se ignoran. El `rayito.Sandbox` nativo
queda en `sbx.native`. Detalles: docs/site/docs/e2b-compat.md.
"""

from __future__ import annotations

from rayito import (
    BarChart,
    BarData,
    BoxAndWhiskerChart,
    BoxAndWhiskerData,
    Chart,
    ChartType,
    CodeContext,
    CommandExitException,
    CommandHandle,
    CommandResult,
    EntryInfo,
    Execution,
    ExecutionError,
    FilesystemEvent,
    FilesystemEventType,
    FileType,
    LineChart,
    Logs,
    OutputMessage,
    PieChart,
    PieData,
    PointData,
    ProcessInfo,
    Result,
    ScaleType,
    ScatterChart,
    SuperChart,
    WatchHandle,
    WriteEntry,
)
from rayito.e2b._async import AsyncSandbox
from rayito.e2b._models import (
    AsyncSandboxPaginator,
    ListedSandbox,
    PtySize,
    SandboxInfo,
    SandboxMetrics,
    SandboxPaginator,
    SandboxQuery,
    SandboxState,
)
from rayito.e2b._sync import Sandbox
from rayito.e2b.exceptions import (
    AuthenticationException,
    InvalidArgumentException,
    NotEnoughSpaceException,
    NotFoundException,
    RateLimitException,
    RayitoCompatWarning,
    SandboxException,
    TemplateException,
    TimeoutException,
    UnimplementedError,
)

Context = CodeContext
WriteInfo = EntryInfo

__all__ = [
    "AsyncSandbox",
    "AsyncSandboxPaginator",
    "AuthenticationException",
    "BarChart",
    "BarData",
    "BoxAndWhiskerChart",
    "BoxAndWhiskerData",
    "Chart",
    "ChartType",
    "CommandExitException",
    "CommandHandle",
    "CommandResult",
    "Context",
    "EntryInfo",
    "Execution",
    "ExecutionError",
    "FileType",
    "FilesystemEvent",
    "FilesystemEventType",
    "InvalidArgumentException",
    "LineChart",
    "ListedSandbox",
    "Logs",
    "NotEnoughSpaceException",
    "NotFoundException",
    "OutputMessage",
    "PieChart",
    "PieData",
    "PointData",
    "ProcessInfo",
    "PtySize",
    "RateLimitException",
    "RayitoCompatWarning",
    "Result",
    "Sandbox",
    "SandboxException",
    "SandboxInfo",
    "SandboxMetrics",
    "SandboxPaginator",
    "SandboxQuery",
    "SandboxState",
    "ScaleType",
    "ScatterChart",
    "SuperChart",
    "TemplateException",
    "TimeoutException",
    "UnimplementedError",
    "WatchHandle",
    "WriteEntry",
    "WriteInfo",
]
