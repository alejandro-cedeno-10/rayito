# Subclientes

Los objetos que cuelgan de un sandbox (`sbx.commands`, `sbx.files`,
`sbx.pty`, `sbx.git`, `sbx.agent`), sus handles y el paginador de
`Sandbox.paginate()`. Las variantes asíncronas (`AsyncCommandHandle`,
`AsyncWatchHandle`, `AsyncUploadTicket`, `AsyncPtyHandle`, `AsyncGit`,
`AsyncAgentStream`, `AsyncSandboxListPaginator`) tienen la misma superficie
con corrutinas.

## Comandos

::: rayito.sandbox_sync.commands.Commands

::: rayito.CommandHandle

::: rayito.AsyncCommandHandle

## Ficheros

::: rayito.sandbox_sync.filesystem.Filesystem

::: rayito.WatchHandle

::: rayito.AsyncWatchHandle

::: rayito.UploadTicket

::: rayito.AsyncUploadTicket

## Terminal (PTY)

::: rayito.sandbox_sync.pty.Pty

::: rayito.PtyHandle

::: rayito.AsyncPtyHandle

## Git

::: rayito.Git

::: rayito.AsyncGit

::: rayito.GitStatus

::: rayito.GitFileStatus

::: rayito.GitBranches

`GitResetMode` es el alias `Literal["soft", "mixed", "hard", "merge", "keep"]`
del `mode=` de `git.reset()`.

## Agente de IA (`sbx.agent`)

`run()` corre el agente hasta el final (lanza `AgentException` si falla);
`stream()` devuelve un `AgentStream` iterable que nunca lanza por un fallo
del agente; `prepare()` dispara el calentamiento del runtime en segundo
plano.

::: rayito.sandbox_sync.agent.Agent

::: rayito.AgentStream

::: rayito.AsyncAgentStream

### Especificación

::: rayito.AgentSpec

::: rayito.AgentModel

::: rayito.AgentLimits

::: rayito.AgentPermissions

::: rayito.SubAgent

::: rayito.McpLocal

::: rayito.McpRemote

`McpServer` es el alias `McpLocal | McpRemote`.

::: rayito.DeepAgents

### Pasarelas del modelo

Construyen el `SecretGateway` que nombra `AgentModel.gateway`.

::: rayito.bedrock_gateway

::: rayito.anthropic_gateway

::: rayito.openai_compatible_gateway

### Resultado y eventos

`AgentEvent` es el alias de la unión cerrada de eventos que da
`agent.stream()`: `TextDelta | Text | Reasoning | ToolCall | StepStarted |
StepFinished | AgentFailed | Done`.

::: rayito.AgentResult

::: rayito.TokenUsage

::: rayito.TextDelta

::: rayito.Text

::: rayito.Reasoning

::: rayito.ToolCall

::: rayito.StepStarted

::: rayito.StepFinished

::: rayito.AgentFailed

::: rayito.Done

### Imagen y arranque rápido

`AgentTemplate` construye una vez la imagen con el runtime
([Templates de agente](../../funciones-opcionales/templates-de-agente.md));
con ella, el arranque normal es `Sandbox.create(...)` + `sbx.agent.run(...)`.
`agent_pool_warmup` y los pools calientes son opcionales:
[¿Qué uso?](../../guias/agente-en-el-sandbox.md#que-uso).

::: rayito.AgentTemplate

::: rayito.AsyncAgentTemplate

::: rayito.agent_pool_warmup

::: rayito.WarmupStep

## Listado

::: rayito.SandboxListPaginator

::: rayito.AsyncSandboxListPaginator
