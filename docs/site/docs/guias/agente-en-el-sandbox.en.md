---
title: Agent in the sandbox
description: "Run a coding agent (OpenCode or deepagents) inside the sandbox and call the model you prefer only through the secrets gateway."
---

# Agent in the sandbox

<small>Since 0.8.0 ([What's new in 0.8.0](../novedades/0.8.0.md)). More
model providers and the timeout that stops the whole process tree, since
0.10.0 ([What's new in 0.10.0](../novedades/0.10.0.md)).</small>

`sbx.agent` runs a coding agent ([OpenCode](https://github.com/anomalyco/opencode))
**inside** the sandbox itself. The agent sees the same filesystem, the same
commands and the same egress as any other code you run with
[`commands.run`](comandos.md), and calls its model only through the
[secrets gateway](../funciones-opcionales/pasarela-de-secretos.md): the
agent **uses** the model credential, but can never **read** it.

!!! tip "Prefer to describe the agent in a file?"
    Since 0.14.0, [Deploy your agent](despliega-tu-agente.md) builds the image,
    runs the agent and serves it as an HTTP service from a `rayito-agent.toml`,
    with Strands, Hermes, LangGraph, deepagents, OpenCode or your own
    framework. This page remains the reference for `sbx.agent`, the engine they
    use underneath.

The normal way to use it is two calls: `Sandbox.create(...)` with the
model's gateway and `sbx.agent.run(...)` ([section 2](#2-arranque-normal-crear-el-sandbox-y-ejecutar)).
No pool and no warm-up is needed: the
[fast startup](#arranque-rapido) (prefetch, pause between turns, pool with
`warmup`) is optional, and [Which one do I use?](#que-uso) says when each
option pays off. If the conversation can sit idle for more than 8 h, the
option is [`persist=`](#mas-de-8-h-parado-persist).

!!! note "Before you start (once per account and region, ≈ 20 min)"
    1. [Set up AWS](../primeros-pasos/configurar-aws.md) done (≈ 15 min)
       and `rayito-base-caps` published (caps variant).
    2. The `rayito-agent` image:
       `rayito agent template build --bucket <your-bucket>` (≈ 5 min,
       ≈ $0.056/week per version;
       [Agent templates](../funciones-opcionales/templates-de-agente.md)).
       Whoever builds it needs `RayitoTemplateBuilder`
       (`rayito stack deploy templates`) or to be an administrator.
    3. The model key in Secrets Manager (≈ $0.40/month) and
       `RayitoSecretsReader` for whoever calls the SDK
       (`rayito stack deploy secrets-access`).
    4. Access to the model at your provider
       ([Agent providers](agente-proveedores.md)).

The sandbox needs an image with the runtime installed (OpenCode in
`/opt/agents`). If `opencode` is not on the `PATH`, the run fails with
`reason="runtime_missing"`. `AgentTemplate` builds that image (OpenCode,
ripgrep and, if you want, deepagents) on top of `rayito-base-caps`, the
only base that enforces closed egress: see
[Agent templates](../funciones-opcionales/templates-de-agente.md). The
examples on this page use the `rayito-agent` image it builds.

## 1. The secret and the gateway { #1-el-secreto-y-la-pasarela }

The model credential lives in Secrets Manager. One preset per provider
builds the `SecretGateway` with `allow=` already restricted:

| Preset (Python / TypeScript) | Upstream | Header | Allowed paths |
|---|---|---|---|
| `bedrock_gateway(secret, *, region, models, rate_per_minute=0)` / `bedrockGateway(secret, { region, models, ratePerMinute })` | `https://bedrock-runtime.<region>.amazonaws.com` | `authorization` | `POST /model/<id>/converse-stream` and `/converse`, **only** for the given `models` |
| `anthropic_gateway(secret, *, rate_per_minute=0)` / `anthropicGateway(secret, { ratePerMinute })` | `https://api.anthropic.com` | `x-api-key` | `POST /v1/messages` |
| `openai_compatible_gateway(secret, *, upstream, base_path="", rate_per_minute=0)` / `openaiCompatibleGateway(secret, { upstream, basePath, ratePerMinute })` | the `upstream` you pass | `authorization` | `POST <base_path>/chat/completions` |

OpenAI, Gemini, Azure OpenAI, OpenRouter, Groq, Mistral, DeepSeek, xAI and a
LiteLLM proxy have their own preset since 0.10.0: see
[Agent providers](agente-proveedores.md), which also says which one to
choose, which have been tested with a real model and which subscriptions
are not supported.

`secret` is the name of a secret (or a `SecretRef`). `bedrock_gateway`
never opens `/model/*`: that would let code inside the sandbox call any
model in the account, outside the SDK's token budget. It does not accept
ARNs (the gateway rejects an encoded `/` in the path): use the model or
inference profile id.

For Bedrock, the secret holds `Bearer <Bedrock API key>`. Use a short-lived
key (≤ 12 h) and rotate it with `sbx.gateways.refresh()`. For an
OpenAI-compatible API, also `Bearer <key>`; for Anthropic, the key as is.

`rate_per_minute` / `ratePerMinute` (0 by default, no cap) limits the calls
per minute the gateway lets through, including those the sandbox code makes
on its own outside the SDK's token budget (see
[Security](../security.md#agente-de-codigo-dentro-del-sandbox)).

## 2. Normal startup: create the sandbox and run { #2-arranque-normal-crear-el-sandbox-y-ejecutar }

This is all you need for most cases: a sandbox with closed egress, the
model's gateway and `run()`.

=== "Python"

    ```python
    from rayito import AgentModel, AgentSpec, Sandbox, SecretStore, bedrock_gateway

    MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

    SecretStore().create("bedrock-key", "Bearer <short-lived Bedrock key>")

    spec = AgentSpec(
        model=AgentModel(
            provider="bedrock",
            id=MODEL_ID,
            gateway="bedrock",
            region="us-east-1",
        ),
        instructions="Answer in English and do not modify files outside /home/user.",
    )

    with Sandbox.create(
        template="rayito-agent",
        allow_internet_access=False,
        gateways={
            "bedrock": bedrock_gateway("bedrock-key", region="us-east-1", models=[MODEL_ID], rate_per_minute=60),
        },
    ) as sbx:
        result = sbx.agent.run("List the files in /home/user and summarize what is there.", spec=spec)
        print(result.text, result.steps, result.usage.total)
    ```

=== "Python (async)"

    ```python
    from rayito import AgentModel, AgentSpec, AsyncSandbox, bedrock_gateway

    MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


    async def main() -> None:
        spec = AgentSpec(
            model=AgentModel(provider="bedrock", id=MODEL_ID, gateway="bedrock", region="us-east-1"),
        )
        async with await AsyncSandbox.create(
            template="rayito-agent",
            allow_internet_access=False,
            gateways={
                "bedrock": bedrock_gateway("bedrock-key", region="us-east-1", models=[MODEL_ID], rate_per_minute=60),
            },
        ) as sbx:
            result = await sbx.agent.run("Summarize the README.", spec=spec)
            print(result.text)
    ```

=== "TypeScript"

    ```ts
    import { AgentModel, AgentSpec, Sandbox, bedrockGateway } from "rayito";

    const MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0";

    const spec = new AgentSpec({
      model: new AgentModel({
        provider: "bedrock",
        id: MODEL_ID,
        gateway: "bedrock",
        region: "us-east-1",
      }),
    });

    await using sbx = await Sandbox.create({
      template: "rayito-agent",
      allowInternetAccess: false,
      gateways: {
        bedrock: bedrockGateway("bedrock-key", { region: "us-east-1", models: [MODEL_ID], ratePerMinute: 60 }),
      },
    });
    const result = await sbx.agent.run("Summarize the README.", { spec });
    console.log(result.text, result.usage.total);
    ```

The first `run()` on a new VM takes a few seconds longer than the following
ones (it reads the runtime binary from the freshly restored disk); later
turns on the same VM no longer pay for it. Only if that matters to you, see
[Fast startup](#arranque-rapido).

`AgentModel.gateway` must name an entry of `gateways=`; if it does not
exist, `run()` raises `InvalidArgumentException`/`InvalidArgumentError`
before any call. The gateway lives on the handle that created it:
`Sandbox.connect(sandbox_id)` from another process does not recover it, so
`agent.run()` on that handle fails the same way. For several turns use the
same handle (`sbx.pause()` / `sbx.connect()`, [option B](#que-uso)) or,
beyond 8 h, [`persist=`](#mas-de-8-h-parado-persist).

### `run()` and `stream()` options { #opciones-de-run-y-stream }

| Python | TypeScript | Default | What it does |
|---|---|---|---|
| `spec` | `spec` | required | the `AgentSpec` (below) |
| `runtime` | `runtime` | `"opencode"` | `"opencode"`, `"deepagents"` or a runtime object such as [`DeepAgents(...)`](#deepagents) |
| `session_id` | `sessionId` | none (new session) | continues a session: the `session_id` of an earlier `AgentResult` |
| `model` | `model` | `spec.model.id` | another model id for this run, from the same provider and gateway (with `bedrock_gateway`, it has to be in its `models`) |
| `limits` | `limits` | `AgentLimits()` | step, token, time and output [limits](#3-stream-sesiones-abortar-y-limites) |
| `workdir` | `workdir` | `/home/user` | the agent's working directory |
| `reasoning` | `reasoning` | `False` | also emits `Reasoning` events |
| — | `signal` | none | an `AbortSignal` that aborts the run |

### `AgentSpec` { #agentspec }

| Field (Python / TypeScript) | Default | What it does |
|---|---|---|
| `model` | required | `AgentModel(provider, id, gateway, region=None, base_path="", prompt_caching=True)` (TS: `new AgentModel({ provider, id, gateway, region, basePath, promptCaching })`). `provider` is `bedrock`, `anthropic`, `openai` (also xAI), `google` (Gemini), `azure` (Azure OpenAI) or `openai-compatible`; `region` is required on Bedrock; `base_path` only with `openai-compatible` |
| `small_model` / `smallModel` | the same `model.id` | model for the runtime's auxiliary tasks (titles, summaries) |
| `instructions` | none | the agent's system instructions |
| `permissions` | `AgentPermissions(default="allow")` | see [Permissions](#permisos-no-son-una-frontera-de-seguridad) |
| `agents` | `{}` | subagents by name: `SubAgent(description, instructions, model=None, permissions=None)`. `build` is reserved |
| `mcp` | `{}` | MCP servers by name: `McpLocal(command, envs={}, timeout_seconds=None)` (TS: `timeoutMs`; a process inside the sandbox) or `McpRemote(gateway, path="/")` (always through a gateway). OpenCode only |
| `raw_config` / `rawConfig` | none | merged into OpenCode's configuration. The keys the SDK manages (`provider`, `autoupdate`, `share`, `enabled_providers`, `model`, `small_model`, `mcp`, `agent`, `permission`, `instructions`) raise `InvalidArgumentException`/`InvalidArgumentError`. OpenCode only |
| `runtime_version` / `runtimeVersion` | none | today it is only validated as text: it is not compared with the image version and never produces `runtime_version_mismatch` |

The names in `agents` and `mcp` are 1 to 64 characters `[a-z0-9_-]`.
`AgentSpec` has no field for keys: the credential only arrives through the
gateway.

Other providers: change the preset and `AgentModel.provider` (`bedrock`,
`anthropic`, `openai`, `google`, `azure` or `openai-compatible`). The full
table, with each one's secret and `AgentModel`, is in
[Agent providers](agente-proveedores.md#configurar-cada-proveedor).
If you do not know which one to use, [Which one do I choose?](agente-proveedores.md#cual-elijo).
With OpenAI-style APIs the gateway cannot limit the model: set the spending
cap at the provider
([Provider-side limits](agente-proveedores.md#limites-del-lado-del-proveedor)).

=== "Python"

    ```python
    from rayito import AgentModel, anthropic_gateway, openai_compatible_gateway

    anthropic = anthropic_gateway("anthropic-key")
    anthropic_model = AgentModel(provider="anthropic", id="claude-haiku-4-5", gateway="anthropic")

    openai = openai_compatible_gateway("openai-key", upstream="https://llm.example.com", base_path="/v1")
    openai_model = AgentModel(
        provider="openai-compatible", id="my-model", gateway="openai", base_path="/v1"
    )
    ```

=== "TypeScript"

    ```ts
    import { AgentModel, anthropicGateway, openaiCompatibleGateway } from "rayito";

    const anthropic = anthropicGateway("anthropic-key");
    const anthropicModel = new AgentModel({ provider: "anthropic", id: "claude-haiku-4-5", gateway: "anthropic" });

    const openai = openaiCompatibleGateway("openai-key", { upstream: "https://llm.example.com", basePath: "/v1" });
    const openaiModel = new AgentModel({
      provider: "openai-compatible",
      id: "my-model",
      gateway: "openai",
      basePath: "/v1",
    });
    console.log(anthropic, anthropicModel, openai, openaiModel);
    ```

### Subagents, MCP servers and permissions { #subagentes-servidores-mcp-y-permisos }

A `SubAgent` is an agent the main one can invoke; a local MCP server runs
as a child process inside the sandbox (argument list, no shell) and a
remote one is reached **only** through a gateway, which also has to be in
`gateways=`. `McpLocal.envs` is written into the runtime's configuration,
readable by the sandbox user: never put a credential there.

=== "Python"

    ```python
    from rayito import AgentModel, AgentPermissions, AgentSpec, McpLocal, McpRemote, SubAgent

    spec = AgentSpec(
        model=AgentModel(
            provider="bedrock",
            id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
            gateway="bedrock",
            region="us-east-1",
        ),
        agents={
            "reviewer": SubAgent(
                description="Reviews a diff and points out errors.",
                instructions="Read only: do not edit files.",
                permissions=AgentPermissions(tools={"edit": "deny"}),
            ),
        },
        mcp={
            "fs": McpLocal(command=["node", "/opt/mcp/fs/index.js"], timeout_seconds=30),
            "docs": McpRemote(gateway="docs-mcp", path="/mcp"),
        },
        permissions=AgentPermissions(tools={"bash": {"git *": "allow", "*": "deny"}}),
    )
    ```

=== "TypeScript"

    ```ts
    import { AgentModel, AgentPermissions, AgentSpec, McpLocal, McpRemote, SubAgent } from "rayito";

    const spec = new AgentSpec({
      model: new AgentModel({
        provider: "bedrock",
        id: "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        gateway: "bedrock",
        region: "us-east-1",
      }),
      agents: {
        reviewer: new SubAgent({
          description: "Reviews a diff and points out errors.",
          instructions: "Read only: do not edit files.",
          permissions: new AgentPermissions({ tools: { edit: "deny" } }),
        }),
      },
      mcp: {
        fs: new McpLocal({ command: ["node", "/opt/mcp/fs/index.js"], timeoutMs: 30_000 }),
        docs: new McpRemote({ gateway: "docs-mcp", path: "/mcp" }),
      },
      permissions: new AgentPermissions({ tools: { bash: { "git *": "allow", "*": "deny" } } }),
    });
    console.log(spec.gatewayNames());
    ```

With this `spec`, the sandbox needs two gateways: `bedrock` and
`docs-mcp` (`spec.gateway_names()` / `spec.gatewayNames()` lists them). A
generic gateway is created with `SecretGateway` (see
[Secrets gateway](../funciones-opcionales/pasarela-de-secretos.md)).

!!! info "Cost and activation"
    - **Activates:** calling `sbx.agent.run()`, `stream()` or `prepare()`.
      `sbx.agent` is lazy: touching it makes no call.
    - **AWS resources and calls:** nothing new from Rayito. The runtime
      calls the model (Bedrock `Converse`/`ConverseStream`, or the
      provider's API) through the gateway. Each run makes 2-3 RPCs to the
      sandbox: write the configuration (skipped if its sha did not
      change), `Start` and the *stdin* with the prompt.
    - **Approximate cost:** the model dominates it, not the VM: in the
      reference example (10 steps of Haiku 4.5), the model costs between
      ≈ 7× and ≈ 21× the VM. Figures and formula in
      [Pricing: cost of an agent](../cost.md#coste-de-un-agente-vm-frente-a-modelo).
    - **IAM:** that of the gateway (`secretsmanager:GetSecretValue` on the
      secret).
    - **How to turn it off:** do not call `sbx.agent`. The rest of the
      sandbox does not change.

## 3. `stream`, sessions, abort and limits { #3-stream-sesiones-abortar-y-limites }

`run()` returns an `AgentResult` (`text`, the assistant's last text;
`session_id`/`sessionId`, `steps`, `usage` (a `TokenUsage` with `input`,
`output`, `reasoning`, `cache_read`, `cache_write` and `total`),
`exit_code`/`exitCode`, `tool_calls`/`toolCalls` and
`dropped_lines`/`droppedLines`, the runtime lines that were discarded) or
raises `AgentException`/`AgentError`. `stream()` **never** raises because
of an agent failure: the last event is `Done` or `AgentFailed`. Sandbox or
transport errors do propagate.

Events (`event.type`): `step_started` (`StepStarted`), `text` (`Text`, the
full text of a part; OpenCode does not emit deltas), `text_delta`
(`TextDelta`, chunks of text in progress; deepagents only), `reasoning`
(`Reasoning`, only with `reasoning=True`), `tool_call` (`ToolCall`: `name`,
`status`, `input` and a trimmed `output`), `step_finished` (`StepFinished`,
with `usage`), `agent_failed` (`AgentFailed`, with `reason`) and `done`
(`Done`).

The `AgentStream` (`AsyncAgentStream` in async) has `result()`, which
consumes what is left and returns the `AgentResult` (or raises), `abort()`,
`session_id`/`sessionId` and `dropped_lines`/`droppedLines`; it is closed
with `with`/`async with`, `close()`/`aclose()` (Python) or `close()`
(TypeScript).

=== "Python"

    ```python
    from rayito import AgentLimits, AgentSpec, Sandbox


    def fix_tests(sbx: Sandbox, spec: AgentSpec) -> None:
        with sbx.agent.stream(
            "Fix the failing test.",
            spec=spec,
            limits=AgentLimits(max_steps=20, timeout_seconds=300),
        ) as s:
            for event in s:
                if event.type == "tool_call":
                    print(event.name, event.status)
            result = s.result()

        sbx.agent.run("Add a regression test.", spec=spec, session_id=result.session_id)
    ```

=== "Python (async)"

    ```python
    from rayito import AgentSpec, AsyncSandbox


    async def fix_tests(sbx: AsyncSandbox, spec: AgentSpec) -> None:
        async with await sbx.agent.stream("Fix the failing test.", spec=spec) as s:
            async for event in s:
                if event.type == "tool_call":
                    print(event.name, event.status)
            result = await s.result()
        print(result.session_id)
    ```

=== "TypeScript"

    ```ts
    import { AgentLimits, type AgentSpec, type Sandbox } from "rayito";

    export async function fixTests(sbx: Sandbox, spec: AgentSpec): Promise<void> {
      const stream = await sbx.agent.stream("Fix the failing test.", {
        spec,
        limits: new AgentLimits({ maxSteps: 20, timeoutMs: 300_000 }),
      });
      for await (const event of stream) {
        if (event.type === "tool_call") console.log(event.name, event.status);
      }
      const result = await stream.result();

      const controller = new AbortController();
      setTimeout(() => controller.abort(), 60_000);
      await sbx.agent.run("Add a regression test.", {
        spec,
        sessionId: result.sessionId,
        signal: controller.signal,
      });
    }
    ```

`abort()` (Python, also from another thread), cancelling the `asyncio`
task or an `AbortSignal` kill the runtime process and everything it
launched. The result is `reason="aborted"`.

A sandbox runs a single agent execution at a time: a second `run()` while
another is still in progress ends with `reason="busy"`.

| Limit (Python / TypeScript) | Default | What it does |
|---|---|---|
| `timeout_seconds` / `timeoutMs` | 600 s | deadline of the runtime process; required because, on a model 5xx, OpenCode retries forever. When it expires: `timeout` |
| `max_steps` / `maxSteps` | 50 | the stream ends with `max_steps` when the next step starts |
| `max_total_tokens` / `maxTotalTokens` | 1,000,000 (`None`/`null` turns it off) | checked after each `StepFinished` (it can overshoot by up to one step); the stream ends with `token_budget` |
| `max_output_bytes` / `maxOutputBytes` | 16 MiB | cap on the runtime output the SDK keeps; exceeding it does not cut the run or produce `output_limit` (that `reason` exists, but is not emitted today) |

When `max_steps` or `max_total_tokens` is exceeded the SDK stops the
runtime just like `abort()` and waits for it to finish: the stream ends
with that `reason` and the next `run()` does not find it busy (`busy`).

### What the timeout and `abort()` guarantee { #que-garantizan-el-timeout-y-abort }

<small>Since 0.10.0, with a `rayd` 0.10.0 image.</small>

OpenCode and deepagents run each command of their shell tool in a session
of its own (`setsid`), outside the runtime process's group, and a command
can also daemonize itself (a dev server, a `nohup ... &`). That is why each
run starts as a command with `kill_tree`
([Commands](comandos.md#primer-plano)): `rayd` makes the runtime a *child
subreaper* of everything it launches, so no descendant escapes it even if
its parent exits.

- **Timeout** (`timeout_seconds` / `timeoutMs`): `rayd` sends `SIGTERM` to
  the runtime and all its descendants; if any is left after 5 s, it freezes
  (`SIGSTOP`) the whole tree and kills it (`SIGKILL`). The stream ends with
  `reason="timeout"` only when none is left, so the next `run()` does not
  find the agent `busy`.
- **`abort()`** and the SDK limits (`max_steps`, `max_total_tokens`):
  `rayd` freezes the tree, kills the descendants and then the runtime, with
  no grace period.
- Only processes in that run's tree are signaled: nothing from the rest of
  the sandbox (other commands, the PTY, the `run_code` kernel).
- A run that ends by itself (`Done` or a runtime failure) does not kill
  what it left running: if the agent started a server on purpose, it stays
  alive.
- With an image whose `rayd` is older than 0.10.0, the timeout and
  `abort()` only reach the runtime's group: what the shell tool launched in
  its own session survives and the next `run()` can find the agent `busy`.
  Rebuild the image with `AgentTemplate` from your SDK's version;
  `rayito doctor` gives FAIL on `compatibility` if the image's `rayd` is
  older than the SDK's series.

The possible `reason` values are in
[Errors](../referencia/errores.md#agentexception-agenterror). No message
carries the provider's text, the prompt or sandbox content.

## Use the agent from another process { #usar-el-agente-desde-otro-proceso }

One process can create the sandbox with `gateways=` and another (a worker,
an API, a scheduled task) drive the agent with `Sandbox.connect()`, without
recreating it. That other process needs the sandbox's access token: the
`sbx.access_token` (`sbx.accessToken` in TypeScript) saved by whoever
created it, passed as `access_token=` (`accessToken`) or in
`RAYITO_ACCESS_TOKEN`; without it, `connect()` raises
`AuthenticationException` (`AuthenticationError`). `connect()` asks `rayd`
for a read-only `ConfigureStatus` and rebuilds `sbx.gateways`: the name,
port and last error of each route. No secret leaves `rayd`: not the
upstream, not the headers, not their values.

=== "Python"

    ```python
    import os

    from rayito import AgentModel, AgentSpec, Sandbox

    MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    spec = AgentSpec(
        model=AgentModel(provider="bedrock", id=MODEL_ID, gateway="bedrock", region="us-east-1"),
    )

    sbx = Sandbox.connect(
        "microvm-00000000-0000-0000-0000-000000000001",
        access_token=os.environ["RAYITO_ACCESS_TOKEN"],
    )
    print(sbx.gateways["bedrock"].url)
    result = sbx.agent.run("Summarize the README.", spec=spec)
    ```

=== "Python (async)"

    ```python
    import os

    from rayito import AgentModel, AgentSpec, AsyncSandbox

    MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


    async def main() -> None:
        spec = AgentSpec(
            model=AgentModel(provider="bedrock", id=MODEL_ID, gateway="bedrock", region="us-east-1"),
        )
        sbx = await AsyncSandbox.connect(
            "microvm-00000000-0000-0000-0000-000000000001",
            access_token=os.environ["RAYITO_ACCESS_TOKEN"],
        )
        result = await sbx.agent.run("Summarize the README.", spec=spec)
        print(result.text)
    ```

=== "TypeScript"

    ```ts
    import { AgentModel, AgentSpec, Sandbox } from "rayito";

    const MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0";
    const spec = new AgentSpec({
      model: new AgentModel({ provider: "bedrock", id: MODEL_ID, gateway: "bedrock", region: "us-east-1" }),
    });

    const accessToken = process.env.RAYITO_ACCESS_TOKEN;
    if (accessToken === undefined) {
      throw new Error("RAYITO_ACCESS_TOKEN is missing");
    }
    const sbx = await Sandbox.connect("microvm-00000000-0000-0000-0000-000000000001", {
      accessToken,
    });
    console.log(sbx.gateways.get("bedrock")?.url);
    const result = await sbx.agent.run("Summarize the README.", { spec });
    ```

Without the gateway definition there is nothing to rotate: on a recovered
handle, `sbx.gateways.refresh()` only rereads the state (ports and errors).
Rotating the key, with `refresh()` or with `reincarnate()`, is only
possible from the handle that called `create(gateways=)`: on a recovered
handle, `reincarnate()` raises because it does not know the original
definition. The handle that created the sandbox keeps its own even if it
calls `connect()` again; a recovered one rereads the state on every
`connect()`, with the same `request_timeout` (`requestTimeoutMs` in
TypeScript) as that call. On an agent without the `secret_gateway`
feature, `sbx.gateways` stays empty and `connect()` makes no extra call. If
`rayd` rejects the token on that read, `connect()` does not fail:
`sbx.gateways` stays empty and the error shows up on the first
authenticated call, as with any other `connect()`.

## Permissions: they are not a security boundary { #permisos-no-son-una-frontera-de-seguridad }

`AgentPermissions(default="allow"|"deny", tools={...})` decides which tools
the agent can use. A `tools` value is an action or a map of patterns (for
example, `{"bash": {"git *": "allow", "*": "deny"}}`). `"ask"` is not
valid: a run without a terminal cannot answer. By default `question`,
`webfetch` and `websearch` are denied; an entry of yours in `tools` wins.
With deepagents only the tools `read`, `edit`, `list`, `glob`, `grep`,
`bash`, `task` and `todowrite` exist, and only `bash` accepts patterns.

But the runtime runs whatever the model asks within what is allowed,
without asking. **The real boundary is the MicroVM plus closed egress**
(`allow_internet_access=False`), just as for any code in the sandbox.
`Sandbox.create` leaves egress **open** by default and the SDK does not
warn if the agent runs like that: pass `allow_internet_access=False`
yourself. Risks and mitigations in
[Security](../security.md#agente-de-codigo-dentro-del-sandbox).

## Fast startup { #arranque-rapido }

Everything in this section is **optional**. A freshly restored VM reads
each page of code slowly the first time, during a window that ranges from
about 10 to over 20 s depending on the batch (Q142, Q155): OpenCode's first
`exec` after `create()` pays for that read, and a new VM's first token
arrives in ≈ 12-24 s median depending on the batch (tables below). **For
most cases no pool is needed**: that cost is paid once per conversation,
and the following turns run on the same VM.

### Which one do I use? { #que-uso }

1. **Does the conversation last less than 8 h from `create()`?** (the
   platform cap counts launched time **plus** suspended time) → **one VM per
   conversation, paused between turns (B)**. Create once, pause at the end
   of each turn and resume with `connect()` (≈ 3 s to the first token,
   Q149). It only costs one suspend/resume cycle per pause and the
   snapshot storage while paused
   ([Pricing](../cost.md#coste-de-la-vm-con-fast-start)). With no code to
   write: the default [auto-suspend](pausar-reanudar.md#auto-suspension-e-idlepolicy)
   (`IdlePolicy(max_idle_seconds=300, auto_resume=True)`) already suspends
   it after 300 s without traffic, and the SDK's next call (`agent.run`
   included) wakes it on its own. You only have to create it with a
   `timeout` that covers the conversation (3600 s by default, cap 8 h).
2. **Can it sit idle for more than 8 h?** → save its `HOME` to S3 with
   [persistence](../persistence.md) (`persist=`) and, when the user comes
   back, create another VM with the same `persist=`: it restores the
   `HOME`, and with it the OpenCode sessions, so `session_id` is still
   valid. You pay for the cold first `exec` again
   ([example](#mas-de-8-h-parado-persist)).
3. **Do many *new* conversations arrive and does their first message have
   to be fast?** → a **pool with `warmup` (C)**: the first token arrives
   ≈ 5 s after the take, in exchange for a fixed cost per idle slot per
   month ([Pricing](../cost.md#coste-de-la-vm-con-fast-start);
   [example](#pool-de-agentes-c)).
4. **None of the above?** → **no pool**: `Sandbox.create(...)` and
   `sbx.agent.run(...)`, the [normal startup](#2-arranque-normal-crear-el-sandbox-y-ejecutar).

A multi-turn conversation on the same VM (B):

=== "Python"

    ```python
    from rayito import AgentModel, AgentSpec, Sandbox, bedrock_gateway

    MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    spec = AgentSpec(model=AgentModel(provider="bedrock", id=MODEL_ID, gateway="bedrock", region="us-east-1"))

    sbx = Sandbox.create(
        template="rayito-agent",
        timeout=8 * 3600,  # (1)!
        allow_internet_access=False,
        gateways={"bedrock": bedrock_gateway("bedrock-key", region="us-east-1", models=[MODEL_ID])},
    )
    try:
        first = sbx.agent.run("Read the README and summarize the project.", spec=spec)
        sbx.pause()  # (2)!
        sbx.connect()  # (3)!
        second = sbx.agent.run("Now suggest three improvements.", spec=spec, session_id=first.session_id)
        print(second.text)
    finally:
        sbx.kill()
    ```

    1. The cap: 8 h launched + suspended.
    2. Between turns; without this line, auto-suspend does it after 300 s without traffic.
    3. ≈ 3 s to the next turn's first token. It resumes **this handle**,
       which keeps the gateway; a `Sandbox.connect(sandbox_id)` from
       another process does not recover it and `agent.run()` would fail.

=== "TypeScript"

    ```ts
    import { AgentModel, AgentSpec, Sandbox, bedrockGateway } from "rayito";

    const MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0";
    const spec = new AgentSpec({
      model: new AgentModel({ provider: "bedrock", id: MODEL_ID, gateway: "bedrock", region: "us-east-1" }),
    });

    const sbx = await Sandbox.create({
      template: "rayito-agent",
      timeoutMs: 8 * 3600 * 1000, // the cap: 8 h launched + suspended
      allowInternetAccess: false,
      gateways: { bedrock: bedrockGateway("bedrock-key", { region: "us-east-1", models: [MODEL_ID] }) },
    });
    try {
      const first = await sbx.agent.run("Read the README and summarize the project.", { spec });
      await sbx.pause(); // without this line, auto-suspend does it after 300 s without traffic
      await sbx.connect(); // ≈ 3 s to the first token; resumes this handle, which keeps the gateway
      const second = await sbx.agent.run("Now suggest three improvements.", { spec, sessionId: first.sessionId });
      console.log(second.text);
    } finally {
      await sbx.kill();
    }
    ```

### More than 8 h idle: `persist=` { #mas-de-8-h-parado-persist }

The `HOME` (with the agent's sessions) goes to S3 and comes back in the
next VM. Each turn creates a new VM with the same `persist=` and the
gateway, and saves the `HOME` before releasing it:

=== "Python"

    ```python
    import os

    from rayito import AgentSpec, S3Prefix, Sandbox, bedrock_gateway

    MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    role = os.environ["RAYITO_EXECUTION_ROLE_ARN"]
    home = S3Prefix("amzn-s3-demo-bucket", prefix="rayito-home", name="conversation-42")


    def turn(spec: AgentSpec, prompt: str, session_id: str | None) -> str:
        with Sandbox.create(  # (1)!
            "rayito-agent",
            execution_role_arn=role,
            persist=home,
            allow_internet_access=False,
            gateways={"bedrock": bedrock_gateway("bedrock-key", region="us-east-1", models=[MODEL_ID])},
        ) as sbx:
            result = sbx.agent.run(prompt, spec=spec, session_id=session_id)
            sbx.checkpoint_files()  # (2)!
            return result.session_id
    ```

    1. Restores the saved `HOME`, if there is one. The image needs the
       persistence capabilities (`AgentTemplate` starts from
       `rayito-base-caps`) and an execution role with access to the prefix.
    2. `s3://amzn-s3-demo-bucket/rayito-home/conversation-42/home.tar.gz`.

=== "TypeScript"

    ```ts
    import { type AgentSpec, S3Prefix, Sandbox, bedrockGateway } from "rayito";

    const MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0";
    const executionRoleArn = process.env.RAYITO_EXECUTION_ROLE_ARN ?? "";
    const persist = new S3Prefix({ bucket: "amzn-s3-demo-bucket", prefix: "rayito-home", name: "conversation-42" });

    export async function turn(spec: AgentSpec, prompt: string, sessionId?: string): Promise<string> {
      await using sbx = await Sandbox.create({
        template: "rayito-agent",
        executionRoleArn,
        persist,
        allowInternetAccess: false,
        gateways: { bedrock: bedrockGateway("bedrock-key", { region: "us-east-1", models: [MODEL_ID] }) },
      });
      const result = await sbx.agent.run(prompt, { spec, sessionId });
      await sbx.checkpointFiles(); // the HOME, with the agent's sessions, to S3
      return result.sessionId;
    }
    ```

### Agent pool (C) { #pool-de-agentes-c }

Only if many **new** conversations arrive whose first message has to be
fast. The slot is parked without a gateway (warm slots never carry
secrets): the gateway is opened when it is taken, with
`pool.take(gateways=...)`. `Sandbox.create(pool=...)` does not accept
`gateways=`, so in an agent pool `take()` is used directly.

=== "Python"

    ```python
    from rayito import AgentModel, AgentSpec, PoolConfig, SandboxPool, agent_pool_warmup, bedrock_gateway

    MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    spec = AgentSpec(model=AgentModel(provider="bedrock", id=MODEL_ID, gateway="bedrock", region="us-east-1"))
    config = PoolConfig(
        size=2,
        template="rayito-agent",
        allow_internet_access=False,
        warmup=agent_pool_warmup("opencode"),
    )

    with SandboxPool(config) as pool:
        gateway = bedrock_gateway("bedrock-key", region="us-east-1", models=[MODEL_ID])
        with pool.take(gateways={"bedrock": gateway}) as sbx:
            print(sbx.agent.run("Summarize the README.", spec=spec).text)
    ```

=== "TypeScript"

    ```ts
    import { AgentModel, AgentSpec, SandboxPool, agentPoolWarmup, bedrockGateway } from "rayito";

    const MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0";
    const spec = new AgentSpec({
      model: new AgentModel({ provider: "bedrock", id: MODEL_ID, gateway: "bedrock", region: "us-east-1" }),
    });

    await using pool = await new SandboxPool({
      size: 2,
      template: "rayito-agent",
      allowInternetAccess: false,
      warmup: agentPoolWarmup("opencode"),
    }).start();
    await using sbx = await pool.take({
      gateways: { bedrock: bedrockGateway("bedrock-key", { region: "us-east-1", models: [MODEL_ID] }) },
    });
    console.log((await sbx.agent.run("Summarize the README.", { spec })).text);
    ```

Details of the warm-up and its per-slot cost in
[Pool: warm-up](../pool.md#calentamiento-warmup).

### The options { #las-opciones }

They can be combined (a pool with `warmup` on an image with prefetch, for
example). The amounts for each are in
[Pricing: VM cost, with fast-start](../cost.md#coste-de-la-vm-con-fast-start).

| Option | What it does | When to use it | Cost above the VM |
|---|---|---|---|
| **A. Prefetch** | `AgentTemplate(prefetch=True)` (the default) bakes OpenCode's binary into the memory snapshot and a daemon that, after each restore, brings it into the page cache; in addition, the template does not warm the `run_code` kernel (`kernel_warmup=False`). On any image, `sbx.agent.prepare()` does the warm-up by hand | whenever you build the image with `AgentTemplate`; it shortens the first turn of each new VM | ≈ $0: the memory snapshot ends up smaller than before |
| **B. `pause()` between turns** | the VM is suspended with everything in memory and `connect()` (or any call, with auto-resume) resumes it | **the general case**: a conversation of less than 8 h from `create()` | one suspend/resume cycle per pause + storage while paused |
| **C. Pool with `warmup`** | `PoolConfig(warmup=agent_pool_warmup("opencode"))` runs the binary in each slot before parking it | only if many **new** conversations arrive and the first message has to be fast | a bit more than a base pool slot, per idle slot per month |
| **No pool** | `Sandbox.create(...)` and `sbx.agent.run(...)`, no warm-up | new conversations whose first message can take as long as a new VM | nothing besides the VM |

Each run, in addition, costs its VM (launch plus the seconds of compute
until the answer) and its model; the breakdown per scenario is in
[Pricing](../cost.md#coste-de-la-vm-con-fast-start).

!!! success "Measured on real AWS (2026-10-07, us-east-1, Claude Haiku 4.5, n=5)"
    Time to first token of a short answer, with egress closed and the model
    through `bedrock_gateway` (`AWS_API_NOTES.md` Q146-Q150, and Q153 for
    the row after A's fix):

    | Scenario | `create()` / take p50 | First token p50 | First token p95 |
    |---|---|---|---|
    | `create()` without prefetch | 8.7 s | 28.3 s | 41.1 s |
    | A, **before the fix** (the daemon read during startup) | 16.6 s | 20.5 s | 37.0 s |
    | without prefetch, in the same batch as the next row | 8.8 s | 13.3 s | 19.5 s |
    | A, with the daemon that waits for the guest to settle | 9.2 s | 14.7 s | 15.5 s |
    | B: `connect()` after `pause()` | 0.55 s | 3.1 s | 3.2 s |
    | C: `pool.take()` with `warmup` | 0.95 s | 5.1 s | 6.7 s |

    - **A**: the daemon cuts the stretch after `create()` from 19.6 s to
      4.7 s (−76 %), but in its first version it read right after restore
      and competed with the startup that `create()` waits for, so `create()`
      took ≈ 8 s more and end to end only 28 % was gained. Now it waits for
      the guest to have 1 s without I/O in progress before reading, and
      `create()` no longer grows (9.2 s versus 8.8 s without prefetch in the
      same batch). The stretch after `create()` drops from 5.2 s to 3.8 s,
      but end to end the difference (14.7 s versus 13.3 s) falls within the
      noise of n=5: the no-prefetch control came out much faster than in the
      first measurement (13.3 s versus 28.3 s), so today A's gain is small.
      It stays on because it no longer costs anything.
    - **deepagents** is not cached with a short prompt: its one-word prompt
      is ≈ 3,503 input tokens, below the 4,096 that Haiku requires for a
      cache point, so each turn pays for all of them (≈ 4 times what
      OpenCode's turn costs, which reads 7,596 from cache;
      [Pricing](../cost.md#coste-de-la-vm-con-fast-start)). After
      `create()`, its first token arrives in 7.5 s median versus 5.0 s for
      OpenCode on the same image.

!!! success "Measured on real AWS (2026-10-09, us-east-1, Claude Haiku 4.5, n=6)"
    The same test, with each phase timed (`AWS_API_NOTES.md` Q155 and Q156;
    images interleaved, after two disposable launches per version). With
    n=6, the last column is the maximum:

    | Batch | `AgentTemplate` image | `create()` p50 | First token p50 | First token max. | Memory snapshot |
    |---|---|---|---|---|---|
    | 1 | the previous one (warmed kernel) | 12.0 s | 16.4 s | 23.1 s | 920 MB |
    | 1 | without warming the kernel | 7.65 s | 16.3 s | 28.8 s | 671 MB |
    | 2 | without warming the kernel | 8.8 s | 15.6 s | 21.2 s | 671 MB |
    | 2 | without warming the kernel, with the binary and deepagents baked in (trial) | 6.3 s | 12.4 s | 13.6 s | 1,030 MB |
    | 3 | the previous one (warmed kernel; n=4) | 19.1 s | 23.7 s | 27.0 s | 920 MB |
    | 3 | **the current one**: without warming the kernel, with the binary baked in | 6.4 s | 20.9 s | 34.9 s | 858 MB |

    - **Where the time goes**: on the client, resolving the ARN of an image
      name ≈ 1 s (only the process's first `create()`; pass the ARN to save
      it), `run-microvm` ≈ 0.5 s, the gateway secret ≈ 0.35 s, `Configure`
      ≈ 0.7 s and writing OpenCode's configuration ≈ 0.2 s. OpenCode
      starts in ≈ 1.3 s with a warm cache, and a second turn on the same VM
      gives the first token in ≈ 3 s. The rest is the slow window after
      restore.
    - **Removing the kernel warm-up** brings `create()` forward (4.4 s
      median in batch 1, 12.7 s in batch 3) but, by itself, not the first
      token: OpenCode's first `exec` pays the window the kernel used to
      pay.
    - **Baking the binary into the snapshot** brought the median first token
      forward against the control of its own batch (−3.2 s in 2, −2.8 s in
      3), but the dispersion is large and the slow window changes a lot from
      one batch to another (the previous image went from 16.4 s to 23.7 s):
      with n=6 it is not a guaranteed improvement. What is certain is that
      `create()` returns sooner and that the memory snapshot weighs 62 MB
      less than before.
    - **Reading right after restore**, without waiting for the guest to
      settle, still does not work: `create()` 15-26 s.

!!! warning "The first launches of a new version are slow"
    Right after publishing an image version, its first launches take much
    longer: in Q146 the first 3 of version 1 took 48-65 s in `create()`,
    and the first 5 of version 2, 26-63 s, versus 6.9-12.5 s for the
    following ones (without prefetch). Before measuring or sending it
    traffic, warm it with a few disposable launches or with a pool.

Without the daemon (for example, on an image of your own without
`AgentTemplate`), `prepare()` triggers the same warm-up by hand:

=== "Python"

    ```python
    from rayito import Sandbox


    def warm(sbx: Sandbox) -> None:
        sbx.agent.prepare()
    ```

=== "TypeScript"

    ```ts
    import type { Sandbox } from "rayito";

    export async function warm(sbx: Sandbox): Promise<void> {
      await sbx.agent.prepare();
    }
    ```

`prepare(runtime="opencode")` (TS: `prepare({ runtime })`) launches the
warm-up steps in the background and returns right away, without waiting
for any of them.

## deepagents { #deepagents }

The default runtime is OpenCode. With `runtime="deepagents"` (or
`runtime=DeepAgents(...)`) the same `run`/`stream` runs a
[deepagents](https://github.com/langchain-ai/deepagents) (LangChain) graph
in a runner isolated from the image's venv (`AgentTemplate` with
deepagents): the same events (plus `TextDelta`), the same SDK limits and
the model only through the gateway. Without `entrypoint`, the runner
assembles `create_deep_agent` with the `AgentSpec`'s model, `instructions`
and subagents; with `entrypoint="package.module:build"` it imports your
function from the working directory and passes it a `RunnerContext` (model,
instructions, subagents, backend, permissions middleware, working
directory) so it returns your compiled graph. `mcp` and `raw_config` do not
exist in this runtime and fail before any call. Its `session_id` values
have the form `rda_<32 hex>`.

=== "Python"

    ```python
    from rayito import AgentSpec, DeepAgents, Sandbox


    def run_deepagents(sbx: Sandbox, spec: AgentSpec) -> str:
        result = sbx.agent.run(
            "Summarize the README.",
            spec=spec,
            runtime=DeepAgents(entrypoint="my_agent.graph:build"),
        )
        return result.text
    ```

=== "TypeScript"

    ```ts
    import { type AgentSpec, DeepAgents, type Sandbox } from "rayito";

    export async function runDeepAgents(sbx: Sandbox, spec: AgentSpec): Promise<string> {
      const result = await sbx.agent.run("Summarize the README.", {
        spec,
        runtime: new DeepAgents({ entrypoint: "my_agent.graph:build" }),
      });
      return result.text;
    }
    ```

deepagents supports the same providers as OpenCode. Since 0.10.0 the agent
image ships `socksio` (without it, any model other than Bedrock failed at
startup with `AgentFailed` and `ImportError`) and, with
`provider="openai-compatible"`, the runner requests token usage in
streaming, so `result.usage` and `max_total_tokens` work there too. Both
things need an image built with `AgentTemplate` from 0.10.0.

If your `build` does not pass `ctx.middleware` to the graph, the
`AgentSpec` permissions are lost. With a short prompt, deepagents does not
reach Haiku's prompt-cache minimum (see [Fast startup](#arranque-rapido)).

## Build the image from the CLI { #construir-la-imagen-desde-la-cli }

The CLI does not run the agent or manage pools (that is the SDK's job); it
does build the image:

```bash
rayito agent template build --bucket amzn-s3-demo-bucket
```

Options (`--name`, `--base`, `--memory-mb`, `--no-deepagents`,
`--no-prefetch`, `--force`, `--timeout`) in
[Agent templates](../funciones-opcionales/templates-de-agente.md).

To describe the agent in a file and build, run or deploy it from the CLI,
see `rayito agent init`, `build`, `run`, `deploy`, `invoke`, `status`, `stop`
and `export` in [Deploy your agent](despliega-tu-agente.md).

## The E2B shim { #el-shim-de-e2b }

`rayito.e2b.Sandbox` has no `.agent`, because E2B does not define this
surface, and it does not accept `gateways=`. To use the agent, create the
sandbox with the native SDK (`rayito.Sandbox.create(..., gateways=...)`, as
in [section 2](#2-arranque-normal-crear-el-sandbox-y-ejecutar)). The native
handle of a shim sandbox (`sbx.native.agent`) does not work: it has no
gateway, so `agent.run()` fails with `InvalidArgumentException` before any
call.

## See also { #ver-tambien }

- [Agent providers](agente-proveedores.md)
- [Agent templates](../funciones-opcionales/templates-de-agente.md)
- [Pool: warm-up](../pool.md#calentamiento-warmup)
- [Pricing (MicroVMs, pool, agents)](../cost.md#coste-de-un-agente-vm-frente-a-modelo)
- [Secrets gateway](../funciones-opcionales/pasarela-de-secretos.md)
- [Errors: `AgentException`/`AgentError`](../referencia/errores.md#agentexception-agenterror)
- [Security: coding agent inside the sandbox](../security.md#agente-de-codigo-dentro-del-sandbox)
