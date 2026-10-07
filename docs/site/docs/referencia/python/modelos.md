# Modelos

Los tipos de datos que devuelven y aceptan `Sandbox` y sus subclientes.
Todos se importan desde `rayito`.

## Ciclo de vida

::: rayito.IdlePolicy

::: rayito.SandboxLifecycle

::: rayito.SandboxInfo

::: rayito.SandboxListItem

::: rayito.MicrovmListPage

::: rayito.LaunchOptions

::: rayito.SandboxHealth

::: rayito.SandboxMetrics

::: rayito.HostAccess

`ListOrder` es el alias `Literal["asc", "desc"]` del `order=` de
`Sandbox.list()` y `Sandbox.paginate()`.

## Comandos y terminal

::: rayito.CommandResult

::: rayito.ProcessInfo

::: rayito.PtySize

## Ficheros

::: rayito.EntryInfo

::: rayito.FileType

::: rayito.FilesystemEvent

::: rayito.FilesystemEventType

::: rayito.WriteEntry

::: rayito.S3Staging

::: rayito.DownloadLink

::: rayito.TransferStatus

## Código

::: rayito.CodeContext

::: rayito.Execution

::: rayito.Result

::: rayito.Logs

::: rayito.OutputMessage

::: rayito.ExecutionError

## Red

::: rayito.NetworkPolicy

::: rayito.NetworkOptions

::: rayito.EgressProxy

::: rayito.NetworkState

::: rayito.EgressEnforcement

::: rayito.NetworkSelectorContext

`ALL_TRAFFIC` es la constante `"0.0.0.0/0"`, como en E2B:
`NetworkPolicy(deny_out=[ALL_TRAFFIC])` corta todo el egress salvo lo que
abra `allow_out`.

## Persistencia

::: rayito.S3Prefix

::: rayito.CheckpointResult

::: rayito.CheckpointProgress

::: rayito.RestoreResult

::: rayito.RestoreProgress

## Gráficos

Los gráficos que `run_code()` extrae de matplotlib en `Result.chart`, con la
forma de `e2b-code-interpreter`.

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

## Clientes y transporte

`ClientSettings` lo usa el shim de E2B (`retries=`, `proxy=`,
`ConnectionConfig.set_integration`); `TransportSettings` es el `transport=`
de `create()`/`connect()`. `rayito.__version__` es la versión del paquete.

::: rayito.ClientSettings

::: rayito.TransportSettings
