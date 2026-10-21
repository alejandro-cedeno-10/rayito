# Guides

One page per feature. Each guide opens with a short example in Python,
async Python and TypeScript (the tabs are synced: pick your language once),
explains the options and ends with the usual errors and the differences from
E2B.

<div class="grid cards rx-cards" markdown>

-   :material-cube-outline:{ .lg .middle } **[Sandbox](#sandbox)**

    ---

    Lifecycle, pause, pool and persistence.

-   :material-play-circle-outline:{ .lg .middle } **[Run](#ejecutar)**

    ---

    Commands, code, kernels and terminal.

-   :material-folder-network-outline:{ .lg .middle } **[Files and network](#ficheros-y-red)**

    ---

    Files, S3, internet egress and ports.

-   :material-chart-line:{ .lg .middle } **[Observe and tools](#observar-y-herramientas)**

    ---

    Metrics, listing and git.

-   :material-robot-outline:{ .lg .middle } **[Agents](#agentes)**

    ---

    MCP, LangChain, the agent in the sandbox and deploying your agent to
    MicroVMs or AgentCore, also with CDK.

-   :material-flask-outline:{ .lg .middle } **[Develop and test](#desarrollar-y-probar)**

    ---

    Test locally, without an AWS account.

</div>

## Sandbox { #sandbox }

| Guide | What it is for |
|---|---|
| [Lifecycle](ciclo-de-vida.md) | create, connect, list, inspect and destroy sandboxes; templates and metadata |
| [Pause and resume](pausar-reanudar.md) | freeze a sandbox with all its state and wake it up; auto-suspend on inactivity |
| [Server-side deadline](../lifecycle.md) | a deadline the sandbox honors even if your process dies; `set_timeout()` |
| [Pool](../pool.md) | sandboxes ready in under a second |
| [Persistence](../persistence.md) | `HOME` on S3, beyond the 8 h mark |
| [Async](async.md) | `AsyncSandbox` in Python and the TypeScript equivalents |

## Run { #ejecutar }

| Guide | What it is for |
|---|---|
| [Commands](comandos.md) | foreground and background processes, `stdin`, signals and reconnection |
| [Run code](ejecutar-codigo.md) | stateful Python cells, results, charts, errors and contexts |
| [Languages and kernels](../kernels.md) | bash, JavaScript and TypeScript with Deno |
| [Terminal (PTY)](terminal-pty.md) | a real interactive terminal |

## Files and network { #ficheros-y-red }

| Guide | What it is for |
|---|---|
| [Files and S3](../files.md) | read, write, list, watch; large files and signed URLs |
| [Outbound network](../network.md) | cut or filter internet egress |
| [Ports and host](puertos-y-host.md) | reach a server running in the sandbox over HTTP |

## Observe and tools { #observar-y-herramientas }

| Guide | What it is for |
|---|---|
| [Metrics and listing](../observability.md) | CPU, memory and disk; history; paginated listing |
| [Git](../git.md) | clone, commit and push from the sandbox |

## Agents { #agentes }

Where does the agent run? **Outside** (your MCP host or your app with
LangChain/Vercel AI): the sandbox is its tool and `rayito-base` is enough.
**Inside** (`sbx.agent`): the whole loop runs in the sandbox; it needs the
`rayito-agent` image and the secrets gateway. Already have the agent?
**Deploy it** as a service on a MicroVM fleet or on Amazon Bedrock AgentCore
Runtime, from the SDK, the CLI or AWS CDK.

| Guide | What it is for |
|---|---|
| [MCP server](../mcp.md) | a sandbox for Claude Code, Claude Desktop, Cursor or VS Code |
| [LangChain and Vercel AI](langchain-y-vercel-ai.md) | Rayito as an agent tool |
| [Agent in the sandbox](agente-en-el-sandbox.md) | a coding agent (OpenCode or deepagents) **inside** the sandbox with `sbx.agent.run()`, the model behind the secrets gateway; fast startup is optional |
| [Agent providers](agente-proveedores.md) | the model providers `sbx.agent` supports behind the secrets gateway, how to configure each one and which are not supported |
| [Agent templates](../funciones-opcionales/templates-de-agente.md) | `AgentTemplate`: the `rayito-agent` image with OpenCode, ripgrep and deepagents, their pins and the cost of each version |
| [Deploy your agent](despliega-tu-agente.md) | `deploy_agent` / `deployAgent` and `rayito agent deploy`: the agent you already have, detected without running its code, as an HTTP service on MicroVMs; also build, run or export it to a container |
| [Several sessions and several VMs](flota-de-agentes.md) | `AgentFleet`: several MicroVMs, shared or dedicated sessions, a queue in front of the limit and warm slots |
| [Deploy to AgentCore](desplegar-en-agentcore.md) | the agent you already have on Amazon Bedrock AgentCore Runtime with `--target agentcore`: a CodeBuild build with no local Docker, a least-privilege role and the same `invoke` API as a MicroVM fleet |
| [Deploy with CDK](desplegar-con-cdk.md) | `RayitoAgent` and `RayitoMicrovmImage` (`rayito[cdk]`, `rayito/cdk`): the AgentCore agent and the MicroVMs image in your AWS CDK app, with the same resources and IAM as the CLI |

## Develop and test { #desarrollar-y-probar }

| Guide | What it is for |
|---|---|
| [Test locally (Docker + Floci)](probar-en-local.md) | run the SDKs against a real `rayd` and an emulated AWS, with no account and no cost |

The features that carry their own AWS cost and are off by default (secrets,
secrets gateway, S3 mounts, sizes, events and webhooks, OTLP export,
OpenTelemetry, metadata index, templates and agent templates, optional
stacks, EFS volumes and custom domain, the last two experimental) or that
are CLI-only (local proxy) are in
[Optional features](../optional-features.md). What is new in each release:
[What's new](../novedades/index.md).
