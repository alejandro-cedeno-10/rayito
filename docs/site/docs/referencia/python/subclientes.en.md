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

### Agent runtimes and deployment { #runtimes-y-despliegue-de-agentes }

The runtimes that `AgentTemplate`, `Agent` and `AgentProject` accept. They install their dependencies with pinned hashes and send the model through the secrets gateway ([Deploy your agent](../../guias/despliega-tu-agente.md)).

::: rayito.StrandsRuntime

::: rayito.HermesRuntime

::: rayito.CustomRuntime

::: rayito.HttpApp

#### Project and deployment { #proyecto-y-despliegue }

`AgentProject` builds, runs, deploys or exports an agent described in `rayito-agent.toml`; `AgentDeployment` invokes it, reads its `DeploymentStatus` and stops it. They cost (images and MicroVMs) and do nothing until you call the method.

::: rayito.AgentProject

::: rayito.AsyncAgentProject

::: rayito.ServeOptions

::: rayito.AgentExport

::: rayito.AgentDeployment

::: rayito.AgentDeploymentStream

::: rayito.AsyncAgentDeployment

::: rayito.AsyncAgentDeploymentStream

::: rayito.DeploymentStatus

#### Zero-config deploy and fleets { #despliegue-sin-configuracion-y-flotas }

`deploy_agent()` detects an agent project you already have, deploys it and returns an `AgentFleet`: several MicroVMs of the same image with the sessions spread across them ([Deploy your agent](../../guias/despliega-tu-agente.md#sin-escribir-nada), [Several sessions and several VMs](../../guias/flota-de-agentes.md)). They cost (images and MicroVMs) and do nothing until you call them; `plan_deploy()` and `AgentProject.detect()` make no AWS call.

::: rayito.deploy_agent

::: rayito.deploy_agent_async

::: rayito.plan_deploy

::: rayito.AgentDeployOptions

::: rayito.AgentDeployPlan

::: rayito.AgentDeployStep

::: rayito.AgentDeployCostEstimate

::: rayito.ScalingOptions

::: rayito.DetectOverrides

::: rayito.AgentFleet

::: rayito.AsyncAgentFleet

::: rayito.AgentFleetStream

::: rayito.AsyncAgentFleetStream

::: rayito.FleetStatus

::: rayito.FleetMemberStatus

#### AgentCore deploy { #despliegue-en-agentcore }

`deploy_agent(target="agentcore")` deploys the same project to Amazon Bedrock AgentCore Runtime and returns an `AgentCoreFleet` with the same `invoke()` and `stream()` as `AgentFleet` ([Deploy to AgentCore](../../guias/desplegar-en-agentcore.md)). Without `target`, `deploy_agent()`, `deploy_agent_async()` and `plan_deploy()` do what they always did.

::: rayito.AgentCoreOptions

::: rayito.AgentCoreDeployPlan

::: rayito.AgentCoreFleet

::: rayito.AsyncAgentCoreFleet

::: rayito.AgentCoreFleetStream

::: rayito.AsyncAgentCoreFleetStream

::: rayito.AgentCoreStatus

::: rayito.AgentCoreDeleteResult

#### Runtime ports { #puertos-de-runtime }

They describe how a runtime is installed and served without changing the `AgentRuntime` port.

::: rayito.InstallableRuntime

::: rayito.ServableRuntime

::: rayito.HostConfig

::: rayito.GatewayWiring

::: rayito.DirectWiring

::: rayito.ModelWiring

::: rayito.InstallTarget

::: rayito.is_installable

::: rayito.is_servable

## Listing { #listado }

::: rayito.SandboxListPaginator

::: rayito.AsyncSandboxListPaginator
