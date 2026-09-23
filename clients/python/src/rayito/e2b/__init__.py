"""Shim de compatibilidad con el SDK Python de E2B 2.x (contrato: `e2b`
2.51.0, `e2b-code-interpreter` 2.10.0).

Cambia la línea de import y el resto del programa sigue igual:

    from e2b_code_interpreter import Sandbox  ->  from rayito.e2b import Sandbox
    from e2b import AsyncSandbox, E2B         ->  from rayito.e2b import AsyncSandbox, E2B
    from e2b.exceptions import ...            ->  from rayito.e2b.exceptions import ...

El plazo del sandbox (`timeout`, `set_timeout`, `connect(timeout=)`,
`lifecycle`) lo impone `rayd` en una imagen M9; `upload_url`/`download_url`
firman en S3; `allow_internet_access=False` y `network` son una política de
egress en el guest (`rayito-base-caps`); `get_metrics(start, end)` es el
historial del agente. Lo que Lambda MicroVMs no puede hacer (`fork`,
snapshots, MCP, `iam`, volúmenes, secretos, templates, kernels que no sean
python/bash/javascript/typescript...) lanza `UnimplementedError` (un
`NotImplementedError`, nunca `SandboxException`) nombrando la feature y el
motivo. Los `ApiParams` sin sentido en AWS (`api_key`, `domain`, `debug`,
`api_url`, `sandbox_url`, `validate_api_key`, `api_headers`, `secure=False`)
avisan con `RayitoCompatWarning` y se ignoran. El `rayito.Sandbox` nativo
queda en `sbx.native`. Detalles: docs/site/docs/e2b-compat.md.
"""

from __future__ import annotations

from rayito import (
    ALL_TRAFFIC,
    AsyncCommandHandle,
    AsyncWatchHandle,
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
    DownloadLink,
    EntryInfo,
    Execution,
    ExecutionError,
    FilesystemEvent,
    FilesystemEventType,
    FileType,
    Git,
    GitBranches,
    GitFileStatus,
    GitResetMode,
    GitStatus,
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
    UploadTicket,
    WatchHandle,
    WriteEntry,
)
from rayito._charts import Chart2D
from rayito.e2b._async import AsyncSandbox
from rayito.e2b._client import E2B
from rayito.e2b._connection import ConnectionConfig
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
from rayito.e2b._types import (
    MIMEType,
    OutputHandler,
    PtyOutput,
    RunCodeLanguage,
    Stderr,
    Stdout,
    Username,
)
from rayito.e2b._unimplemented import (
    AsyncSecret,
    AsyncTemplate,
    AsyncVolume,
    Secret,
    Template,
    Volume,
    get_signature,
)
from rayito.e2b.exceptions import (
    AuthenticationException,
    BuildException,
    FileNotFoundException,
    FileUploadException,
    GitAuthException,
    GitUpstreamException,
    InvalidArgumentException,
    NotEnoughSpaceException,
    NotFoundException,
    RateLimitException,
    RayitoCompatWarning,
    SandboxException,
    SandboxNotFoundException,
    ServiceBusyException,
    TemplateException,
    TimeoutException,
    UnimplementedError,
)

Context = CodeContext
WriteInfo = EntryInfo

__all__ = [
    "ALL_TRAFFIC",
    "E2B",
    "AsyncCommandHandle",
    "AsyncSandbox",
    "AsyncSandboxPaginator",
    "AsyncSecret",
    "AsyncTemplate",
    "AsyncVolume",
    "AsyncWatchHandle",
    "AuthenticationException",
    "BarChart",
    "BarData",
    "BoxAndWhiskerChart",
    "BoxAndWhiskerData",
    "BuildException",
    "Chart",
    "Chart2D",
    "ChartType",
    "CommandExitException",
    "CommandHandle",
    "CommandResult",
    "ConnectionConfig",
    "Context",
    "DownloadLink",
    "EntryInfo",
    "Execution",
    "ExecutionError",
    "FileNotFoundException",
    "FileType",
    "FileUploadException",
    "FilesystemEvent",
    "FilesystemEventType",
    "Git",
    "GitAuthException",
    "GitBranches",
    "GitFileStatus",
    "GitResetMode",
    "GitStatus",
    "GitUpstreamException",
    "InvalidArgumentException",
    "LineChart",
    "ListedSandbox",
    "Logs",
    "MIMEType",
    "NotEnoughSpaceException",
    "NotFoundException",
    "OutputHandler",
    "OutputMessage",
    "PieChart",
    "PieData",
    "PointData",
    "ProcessInfo",
    "PtyOutput",
    "PtySize",
    "RateLimitException",
    "RayitoCompatWarning",
    "Result",
    "RunCodeLanguage",
    "Sandbox",
    "SandboxException",
    "SandboxInfo",
    "SandboxMetrics",
    "SandboxNotFoundException",
    "SandboxPaginator",
    "SandboxQuery",
    "SandboxState",
    "ScaleType",
    "ScatterChart",
    "Secret",
    "ServiceBusyException",
    "Stderr",
    "Stdout",
    "SuperChart",
    "Template",
    "TemplateException",
    "TimeoutException",
    "UnimplementedError",
    "UploadTicket",
    "Username",
    "Volume",
    "WatchHandle",
    "WriteEntry",
    "WriteInfo",
    "get_signature",
]
