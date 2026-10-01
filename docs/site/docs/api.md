# Referencia de API

Generada con `mkdocstrings` desde los docstrings del paquete `rayito`.

## Sandbox

::: rayito.Sandbox

## AsyncSandbox

::: rayito.AsyncSandbox

## Sub-clientes

::: rayito.sandbox_sync.commands.Commands

::: rayito.sandbox_sync.commands.CommandHandle

::: rayito.sandbox_sync.filesystem.Filesystem

::: rayito.sandbox_sync.filesystem.WatchHandle

::: rayito.sandbox_sync.pty.Pty

::: rayito.sandbox_sync.pty.PtyHandle

::: rayito.sandbox_sync.listing.SandboxListPaginator

## Git

::: rayito.Git

::: rayito.AsyncGit

## Pool de sandboxes

::: rayito.SandboxPool

::: rayito.AsyncSandboxPool

::: rayito.PoolConfig

::: rayito._pool_base
    options:
      members:
        - PoolStats
        - PoolSlotInfo

::: rayito._pool_backends
    options:
      members:
        - PoolBackend
        - InMemoryPoolBackend
        - JsonFilePoolBackend

## Secretos

Opcional y con coste (ADR-014): ver [Secretos](secrets.md) y
[Funciones opcionales y su coste](optional-features.md).

::: rayito.SecretStore

::: rayito.SecretCache

::: rayito.SecretRef

::: rayito.SecretInfo

::: rayito.SecretPage

## Índice de metadatos

Opcional y con coste (ADR-014, M14): ver
[Observabilidad](observability.md#listado-por-metadatos-con-indice-opcional) y
[Funciones opcionales y su coste](optional-features.md#metadata-index).

::: rayito.DynamoDbIndex

## Modelos

::: rayito._models
    options:
      members:
        - IdlePolicy
        - SandboxLifecycle
        - SandboxInfo
        - SandboxListItem
        - SandboxHealth
        - SandboxMetrics
        - HostAccess
        - PtySize
        - CommandResult
        - ProcessInfo
        - EntryInfo
        - FileType
        - FilesystemEvent
        - FilesystemEventType
        - WriteEntry
        - CodeContext
        - Execution
        - Result
        - Logs
        - OutputMessage
        - ExecutionError
        - S3Staging
        - UploadTicket
        - AsyncUploadTicket
        - DownloadLink
        - TransferStatus
        - NetworkPolicy
        - NetworkOptions
        - EgressProxy
        - NetworkState
        - EgressEnforcement

## Excepciones

::: rayito.exceptions

## Shim de E2B

::: rayito.e2b

::: rayito.e2b.Sandbox

::: rayito.e2b.AsyncSandbox

::: rayito.e2b.Secret

::: rayito.e2b.AsyncSecret

::: rayito.e2b.exceptions
