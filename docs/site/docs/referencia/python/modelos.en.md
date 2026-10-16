# Models

The data types that `Sandbox` and its subclients return and accept.
All of them are imported from `rayito`.

## Lifecycle { #ciclo-de-vida }

::: rayito.IdlePolicy

::: rayito.SandboxLifecycle

::: rayito.SandboxInfo

::: rayito.SandboxListItem

::: rayito.MicrovmListPage

::: rayito.LaunchOptions

::: rayito.SandboxHealth

::: rayito.SandboxMetrics

::: rayito.HostAccess

`ListOrder` is the alias `Literal["asc", "desc"]` for the `order=` of
`Sandbox.list()` and `Sandbox.paginate()`.

## Commands and terminal { #comandos-y-terminal }

::: rayito.CommandResult

::: rayito.ProcessInfo

::: rayito.PtySize

## Files { #ficheros }

::: rayito.EntryInfo

::: rayito.FileType

::: rayito.FilesystemEvent

::: rayito.FilesystemEventType

::: rayito.WriteEntry

::: rayito.S3Staging

::: rayito.DownloadLink

::: rayito.TransferStatus

## Code { #codigo }

::: rayito.CodeContext

::: rayito.Execution

::: rayito.Result

::: rayito.Logs

::: rayito.OutputMessage

::: rayito.ExecutionError

## Network { #red }

::: rayito.NetworkPolicy

::: rayito.NetworkOptions

::: rayito.EgressProxy

::: rayito.NetworkState

::: rayito.EgressEnforcement

::: rayito.NetworkSelectorContext

`ALL_TRAFFIC` is the constant `"0.0.0.0/0"`, as in E2B:
`NetworkPolicy(deny_out=[ALL_TRAFFIC])` cuts all egress except what
`allow_out` opens.

## Persistence { #persistencia }

::: rayito.S3Prefix

::: rayito.CheckpointResult

::: rayito.CheckpointProgress

::: rayito.RestoreResult

::: rayito.RestoreProgress

## Charts { #graficos }

The charts that `run_code()` extracts from matplotlib into `Result.chart`, with
the shape of `e2b-code-interpreter`.

::: rayito.Chart

::: rayito.ChartType

::: rayito.ScaleType

::: rayito.LineChart

::: rayito.ScatterChart

::: rayito.PointData

::: rayito.BarChart

::: rayito.BarData

::: rayito.PieChart

::: rayito.PieData

::: rayito.BoxAndWhiskerChart

::: rayito.BoxAndWhiskerData

::: rayito.SuperChart

## Clients and transport { #clientes-y-transporte }

`ClientSettings` is used by the E2B shim (`retries=`, `proxy=`,
`ConnectionConfig.set_integration`); `TransportSettings` is the `transport=`
of `create()`/`connect()`. `rayito.__version__` is the package version.

::: rayito.ClientSettings

::: rayito.TransportSettings
