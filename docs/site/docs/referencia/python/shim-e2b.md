# Shim E2B

`rayito.e2b` reexporta la API del SDK de E2B 2.x sobre Rayito. Cómo migrar:
[Migrar desde E2B](../../migrar-desde-e2b/index.md).

::: rayito.e2b
    options:
      members: false

Los nombres de `rayito.e2b` que son las mismas clases que en `rayito`
(`CommandHandle`, `AsyncCommandHandle`, `CommandResult`, `EntryInfo`,
`WriteEntry`, `FilesystemEvent`, `FilesystemEventType`, `FileType`,
`WatchHandle`, `AsyncWatchHandle`, `Execution`, `Result`, `Logs`,
`OutputMessage`, `ExecutionError`, `DownloadLink`, `UploadTicket`,
`ProcessInfo`, `Git` y sus modelos, los gráficos, `ALL_TRAFFIC`,
`SecretInfo` y las excepciones nativas) se documentan en
[Modelos](modelos.md), [Subclientes](subclientes.md) y
[Excepciones](excepciones.md). Dos son alias con el nombre de E2B:
`Context` es `CodeContext` y `WriteInfo` es `EntryInfo`. Los alias de tipos
`Username`, `Stdout`, `Stderr`, `MIMEType` (`str`), `PtyOutput` (`bytes`),
`RunCodeLanguage` y `OutputHandler` existen sólo para que las anotaciones
de un programa de E2B sigan resolviendo.

## Sandbox

::: rayito.e2b.Sandbox

::: rayito.e2b.AsyncSandbox

::: rayito.e2b.SandboxInfo

`ListedSandbox` es la misma clase que `SandboxInfo`, con el nombre que da
E2B a los elementos de `Sandbox.list()`.

::: rayito.e2b.SandboxState

::: rayito.e2b.SandboxQuery

::: rayito.e2b.SandboxMetrics

::: rayito.e2b.PtySize

::: rayito.e2b.SandboxPaginator

::: rayito.e2b.AsyncSandboxPaginator

::: rayito.e2b.Chart2D

## Cliente y conexión

::: rayito.e2b.E2B

::: rayito.e2b.ConnectionConfig

::: rayito.e2b.get_signature

## Secretos

::: rayito.e2b.Secret

::: rayito.e2b.AsyncSecret

::: rayito.e2b.SecretPaginator

::: rayito.e2b.AsyncSecretPaginator

## Templates

::: rayito.e2b.Template

::: rayito.e2b.AsyncTemplate

## Volúmenes (experimental)

::: rayito.e2b.Volume

::: rayito.e2b.AsyncVolume

## Excepciones

::: rayito.e2b.exceptions
