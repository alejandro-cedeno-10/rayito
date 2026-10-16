# E2B shim

`rayito.e2b` re-exports the E2B 2.x SDK API on top of Rayito. How to
migrate: [Migrate from E2B](../../migrar-desde-e2b/index.md).

::: rayito.e2b
    options:
      members: false

The `rayito.e2b` names that are the same classes as in `rayito`
(`CommandHandle`, `AsyncCommandHandle`, `CommandResult`, `EntryInfo`,
`WriteEntry`, `FilesystemEvent`, `FilesystemEventType`, `FileType`,
`WatchHandle`, `AsyncWatchHandle`, `Execution`, `Result`, `Logs`,
`OutputMessage`, `ExecutionError`, `DownloadLink`, `UploadTicket`,
`ProcessInfo`, `Git` and its models, the charts, `ALL_TRAFFIC`,
`SecretInfo` and the native exceptions) are documented in
[Models](modelos.md), [Subclients](subclientes.md) and
[Exceptions](excepciones.md). Two are aliases under E2B's name:
`Context` is `CodeContext` and `WriteInfo` is `EntryInfo`. The type aliases
`Username`, `Stdout`, `Stderr`, `MIMEType` (`str`), `PtyOutput` (`bytes`),
`RunCodeLanguage` and `OutputHandler` exist only so that the annotations of
an E2B program keep resolving.

## Sandbox { #sandbox }

::: rayito.e2b.Sandbox

::: rayito.e2b.AsyncSandbox

::: rayito.e2b.SandboxInfo

`ListedSandbox` is the same class as `SandboxInfo`, under the name E2B gives
to the elements of `Sandbox.list()`.

::: rayito.e2b.SandboxState

::: rayito.e2b.SandboxQuery

::: rayito.e2b.SandboxMetrics

::: rayito.e2b.PtySize

::: rayito.e2b.SandboxPaginator

::: rayito.e2b.AsyncSandboxPaginator

::: rayito.e2b.Chart2D

## Client and connection { #cliente-y-conexion }

::: rayito.e2b.E2B

::: rayito.e2b.ConnectionConfig

::: rayito.e2b.get_signature

## Secrets { #secretos }

::: rayito.e2b.Secret

::: rayito.e2b.AsyncSecret

::: rayito.e2b.SecretPaginator

::: rayito.e2b.AsyncSecretPaginator

## Templates { #templates }

::: rayito.e2b.Template

::: rayito.e2b.AsyncTemplate

## Volumes (experimental) { #volumenes-experimental }

::: rayito.e2b.Volume

::: rayito.e2b.AsyncVolume

## Exceptions { #excepciones }

::: rayito.e2b.exceptions
