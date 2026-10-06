# Subclientes

Los objetos que cuelgan de un sandbox (`sbx.commands`, `sbx.files`,
`sbx.pty`, `sbx.git`), sus handles y el paginador de `Sandbox.paginate()`.
Las variantes asíncronas tienen la misma superficie.

## Comandos

::: rayito.sandbox_sync.commands.Commands

::: rayito.CommandHandle

## Ficheros

::: rayito.sandbox_sync.filesystem.Filesystem

::: rayito.WatchHandle

::: rayito.UploadTicket

## Terminal (PTY)

::: rayito.sandbox_sync.pty.Pty

::: rayito.PtyHandle

## Git

::: rayito.Git

::: rayito.AsyncGit

## Agente de IA (`sbx.agent`)

`run()` corre el agente hasta el final (lanza `AgentException` si falla);
`stream()` devuelve un `AgentStream` iterable que nunca lanza por un fallo
del agente; `prepare()` dispara el calentamiento del runtime en segundo
plano.

::: rayito.sandbox_sync.agent.Agent

::: rayito.AgentStream

::: rayito.AsyncAgentStream

## Listado

::: rayito.SandboxListPaginator
