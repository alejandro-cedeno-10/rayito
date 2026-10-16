# Subclients

The objects that hang off a sandbox (`sbx.commands`, `sbx.files`,
`sbx.pty`, `sbx.git`, `sbx.agent`), their handles and the paginator of
`Sandbox.paginate()`. The async variants (`AsyncCommandHandle`,
`AsyncWatchHandle`, `AsyncUploadTicket`, `AsyncPtyHandle`, `AsyncGit`,
`AsyncAgentStream`, `AsyncSandboxListPaginator`) have the same surface
with coroutines.

## Commands { #comandos }

::: rayito.sandbox_sync.commands.Commands

::: rayito.CommandHandle

::: rayito.AsyncCommandHandle

## Files { #ficheros }

::: rayito.sandbox_sync.filesystem.Filesystem

::: rayito.WatchHandle

::: rayito.AsyncWatchHandle

::: rayito.UploadTicket

::: rayito.AsyncUploadTicket

## Terminal (PTY) { #terminal-pty }

::: rayito.sandbox_sync.pty.Pty

::: rayito.PtyHandle

::: rayito.AsyncPtyHandle

## Git { #git }

::: rayito.Git

::: rayito.AsyncGit

::: rayito.GitStatus

::: rayito.GitFileStatus

::: rayito.GitBranches

`GitResetMode` is the alias `Literal["soft", "mixed", "hard", "merge", "keep"]`
for the `mode=` of `git.reset()`.

## AI agent (`sbx.agent`) { #agente-de-ia-sbxagent }

`run()` runs the agent to completion (raises `AgentException` if it fails);
`stream()` returns an iterable `AgentStream` that never raises on an agent
failure; `prepare()` triggers the runtime warm-up in the background.

::: rayito.sandbox_sync.agent.Agent

::: rayito.AgentStream

::: rayito.AsyncAgentStream

### Specification { #especificacion }

::: rayito.AgentSpec

::: rayito.AgentModel

::: rayito.AgentLimits

::: rayito.AgentPermissions

::: rayito.SubAgent

::: rayito.McpLocal

::: rayito.McpRemote

`McpServer` is the alias `McpLocal | McpRemote`.

::: rayito.DeepAgents

### Model gateways { #pasarelas-del-modelo }

They build the `SecretGateway` that `AgentModel.gateway` names.

::: rayito.bedrock_gateway

::: rayito.anthropic_gateway

::: rayito.openai_compatible_gateway

### Result and events { #resultado-y-eventos }

`AgentEvent` is the alias of the closed union of events that
`agent.stream()` yields: `TextDelta | Text | Reasoning | ToolCall | StepStarted |
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

### Image and fast start { #imagen-y-arranque-rapido }

`AgentTemplate` builds the image with the runtime once
([Agent templates](../../funciones-opcionales/templates-de-agente.md));
with it, the normal start is `Sandbox.create(...)` + `sbx.agent.run(...)`.
`agent_pool_warmup` and warm pools are optional:
[What do I use?](../../guias/agente-en-el-sandbox.md#que-uso).

::: rayito.AgentTemplate

::: rayito.AsyncAgentTemplate

::: rayito.agent_pool_warmup

::: rayito.WarmupStep

## Listing { #listado }

::: rayito.SandboxListPaginator

::: rayito.AsyncSandboxListPaginator
