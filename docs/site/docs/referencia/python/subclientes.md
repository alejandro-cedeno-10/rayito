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

### Runtimes y despliegue de agentes

Los runtimes que `AgentTemplate`, `Agent` y `AgentProject` aceptan. Instalan sus dependencias con hashes fijados y sacan el modelo por la pasarela de secretos ([Despliega tu agente](../../guias/despliega-tu-agente.md)).

::: rayito.StrandsRuntime

::: rayito.HermesRuntime

::: rayito.CustomRuntime

::: rayito.HttpApp

#### Proyecto y despliegue

`AgentProject` construye, ejecuta, despliega o exporta un agente descrito en `rayito-agent.toml`; `AgentDeployment` lo invoca, consulta su `DeploymentStatus` y lo detiene. Cuestan (imágenes y MicroVMs) y no hacen nada hasta llamar al método.

::: rayito.AgentProject

::: rayito.AsyncAgentProject

::: rayito.ServeOptions

::: rayito.AgentExport

::: rayito.AgentDeployment

::: rayito.AgentDeploymentStream

::: rayito.AsyncAgentDeployment

::: rayito.AsyncAgentDeploymentStream

::: rayito.DeploymentStatus

#### Despliegue sin configuración y flotas

`deploy_agent()` detecta un proyecto de agente que ya tienes, lo despliega y devuelve una `AgentFleet`: varios MicroVMs de la misma imagen con las sesiones repartidas entre ellos ([Despliega tu agente](../../guias/despliega-tu-agente.md#sin-escribir-nada), [Varias sesiones y varias VMs](../../guias/flota-de-agentes.md)). Cuestan (imágenes y MicroVMs) y no hacen nada hasta llamarlos; `plan_deploy()` y `AgentProject.detect()` no llaman a AWS.

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

#### Puertos de runtime

Describen cómo se instala y cómo se sirve un runtime sin cambiar el puerto `AgentRuntime`.

::: rayito.InstallableRuntime

::: rayito.ServableRuntime

::: rayito.HostConfig

::: rayito.GatewayWiring

::: rayito.DirectWiring

::: rayito.ModelWiring

::: rayito.InstallTarget

::: rayito.is_installable

::: rayito.is_servable

## Listado

::: rayito.SandboxListPaginator

::: rayito.AsyncSandboxListPaginator
