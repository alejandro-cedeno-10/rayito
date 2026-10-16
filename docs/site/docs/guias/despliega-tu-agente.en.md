---
title: Deploy your agent
description: Describe an agent (Strands, Strands harness, Hermes, LangGraph, deepagents, OpenCode or your own) in one file, then build it, run it, deploy it as an HTTP service or export it to a container, from Python, TypeScript or the CLI.
---

# Deploy your agent

<small>Since 0.14.0.</small>

`AgentProject` is the description of an agent in a single object or a single
file (`rayito-agent.toml`): which framework it uses, its code, its model
(always through the [secret gateway](../funciones-opcionales/pasarela-de-secretos.md))
and how it is served. You do four things with it, with the same object in
Python, in TypeScript and in the CLI:

| Operation | What it does | Calls AWS |
|---|---|---|
| `build()` | builds the image with the framework and your code, everything pinned by hash | yes |
| `run()` | creates a sandbox, runs the agent once and destroys it | yes |
| `deploy()` | leaves the agent as an HTTP service in a live sandbox | yes |
| `export()` | writes a container build context (Docker or AgentCore) | no |

The frameworks with their own support are Strands (the Python SDK, the harness
and the TypeScript SDK), Hermes, LangGraph, deepagents and OpenCode. Any other
(CrewAI, OpenAI Agents SDK, Google ADK, Claude Agent SDK, Vercel AI SDK…) goes
through [`CustomRuntime`](#otros-frameworks-customruntime).

## When to use it { #cuando-usarlo }

- You want the whole agent (the model loop and its tools) to run **inside** the
  sandbox, with the model behind the gateway, and to ask it for work as a
  service: use this guide.
- You want to control the sandbox yourself and call the agent turn by turn from
  your code: use [`sbx.agent`](agente-en-el-sandbox.md). It is the same engine;
  this guide adds the image, the service and the export.
- You want to give a code interpreter to an agent that already runs somewhere
  else: use [LangChain and Vercel AI](langchain-y-vercel-ai.md) or the
  [MCP server](../mcp.md).

!!! info "Cost and activation"
    - **Activates:** calling `build()`, `run()` or `deploy()` on `AgentProject`
      (in the CLI, `rayito agent build`, `run` or `deploy`). Building or
      loading a project, `export()`, `rayito agent init` and `rayito agent
      export` make no AWS call and create no client.
    - **AWS resources and calls:** `build()` creates an image version
      (`AgentTemplate`). `run()` and `deploy()` launch a MicroVM and read the
      gateway secret from Secrets Manager; `run()` destroys it when it ends and
      `deploy()` leaves it alive until its `timeout` (8 h at most).
    - **Approximate cost:** an agent image version costs ≈ $0.056 per week
      (one-week minimum); Hermes and Node make it larger, so measure yours with
      `rayito image sizes`. An active deployment costs $0.126 per hour of 2 GB
      VM (≈ $1.01 over the 8 h maximum) plus the launch ($0.0014); with `idle`
      it suspends and only pays for snapshot storage. The model is billed
      separately, per token. List prices, us-east-1, checked on 2026-10-06:
      [Pricing](../cost.md#coste-de-un-agente-desplegado).
    - **IAM:** `RayitoTemplateBuilder` for `build()` (the `templates` stack) and
      `RayitoSecretsReader` for `run()` and `deploy()` (the `secrets-access`
      stack), the same ones [Agent in the sandbox](agente-en-el-sandbox.md)
      uses.
    - **How to turn it off:** do not call those operations. A deployment is
      stopped with `deployment.stop()` or `rayito agent stop`; image versions
      with `rayito image prune`.
    - **Example:** `AgentProject.load(".").deploy()` (all four languages are
      [below](#3-despliegalo-como-servicio)).

## Before you start { #antes-de-empezar }

1. [Configure AWS](../primeros-pasos/configurar-aws.md) done and
   `rayito-base-caps` published: it is the base of the agent image, the only
   one that enforces closed egress.
2. The model key in Secrets Manager, and `RayitoSecretsReader` for whoever
   calls the SDK ([Agent in the sandbox](agente-en-el-sandbox.md#1-el-secreto-y-la-pasarela)).
3. Access to the model at your provider
   ([Agent providers](agente-proveedores.md)).
4. An S3 bucket for the build artifact and `RayitoTemplateBuilder` for whoever
   builds ([Agent templates](../funciones-opcionales/templates-de-agente.md)).

`rayito agent init` and `rayito agent export` work without any of this: they
only write files.

## Which runtime to choose { #que-runtime-elegir }

| `runtime` | What runs | Model providers | Image |
|---|---|---|---|
| `opencode` | the OpenCode coding agent | all | the `rayito-agent` one |
| `deepagents` | a deepagents (LangChain) graph | all | the `rayito-agent` one |
| `langgraph` | your LangGraph graph (`entrypoint` required) | all | the `rayito-agent` one + your requirements |
| `strands` | a Strands agent in Python (or in TypeScript, with `language = "typescript"`) | Bedrock, Anthropic, OpenAI and compatible, Azure | its own venv with Strands |
| `strands-harness` | the Strands harness (shell, files, subagents) | the same | its own venv with the harness |
| `hermes` | the Hermes agent (API on loopback only) | Anthropic, OpenAI and compatible, Azure; not Bedrock | Hermes pinned by commit |
| `custom` | a command, a Python function or an HTTP app | whatever your code wires through the gateway | your install steps |

An unsupported runtime and provider combination fails with
`InvalidArgumentException`/`InvalidArgumentError` before any call. Everything
that is downloaded (binaries, Python requirements, the Hermes commit, Node) is
pinned by sha256 in `limits.json`, and Python dependencies are installed with
`--require-hashes`.

## 1. Write the agent and its project file { #1-escribe-el-agente-y-su-fichero-de-proyecto }

`rayito agent init --runtime <runtime>` writes `rayito-agent.toml` and a
minimal scaffold for the runtime (the example agent in each tab). It never
overwrites anything without `--force`.

=== "Strands"

    `agent.py`: a function that receives `ctx.model`, the model already wired to
    the gateway, and returns the `Agent`.

    ```python
    from strands import Agent, tool


    @tool
    def word_count(text: str) -> int:
        """Count the words in a text."""
        return len(text.split())


    def agent(ctx):
        return Agent(model=ctx.model, tools=[word_count])
    ```

    `rayito-agent.toml`:

    ```toml
    schema = "rayito.agent-project/1"
    name = "support-bot"

    [runtime]
    kind = "strands"
    entrypoint = "agent:agent"

    [source]
    path = "."
    requirements = "requirements.txt"
    exclude = [".venv", "__pycache__"]

    [model]
    provider = "bedrock"
    id = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    region = "us-east-1"
    gateway = "model"

    [gateways.model]
    preset = "bedrock"
    secret = "provider-key"
    region = "us-east-1"
    models = ["us.anthropic.claude-haiku-4-5-20251001-v1:0"]

    [agent]
    instructions_file = "AGENTS.md"
    ```

=== "Strands harness"

    Without an `entrypoint`, the server builds the Strands harness with safe
    defaults: the tool that runs model-written code and web search are off,
    `web_fetch` follows the `AgentSpec` permissions (denied by default) and
    interventions never ask for confirmation. No code file is needed.

    ```toml
    schema = "rayito.agent-project/1"
    name = "reviewer"

    [runtime]
    kind = "strands-harness"

    [source]
    path = "."

    [model]
    provider = "anthropic"
    id = "claude-haiku-4-5"
    gateway = "model"

    [gateways.model]
    preset = "anthropic"
    secret = "provider-key"

    [agent]
    instructions = "Review the diff and point out mistakes. Do not edit files."

    [agent.permissions]
    default = "deny"
    tools = { read = "allow" }
    ```

=== "Strands on Node"

    The TypeScript agent uses the `@strands-agents/sdk` SDK; the image carries
    Node and runs `npm ci --ignore-scripts` on your `package-lock.json`.

    <!-- noqa: example: imports `@strands-agents/sdk` and `zod`, which the repository does not install -->
    ```ts
    import { Agent, type Model, tool } from "@strands-agents/sdk";
    import { z } from "zod";

    const wordCount = tool({
      name: "word_count",
      description: "Count the words in a text.",
      inputSchema: z.object({ text: z.string() }),
      callback: ({ text }) => text.split(/\s+/).filter(Boolean).length,
    });

    export function agent(ctx: { model: Model }): Agent {
      return new Agent({ model: ctx.model, tools: [wordCount] });
    }
    ```

    In `rayito-agent.toml`, change the runtime:

    ```toml
    [runtime]
    kind = "strands"
    entrypoint = "agent.ts"
    language = "typescript"
    ```

=== "Hermes"

    Hermes is installed from its repository at a fixed commit, with its own
    state directory, and its API only listens on `127.0.0.1`. No code is
    needed: it is configured through the `AgentSpec`.

    ```toml
    schema = "rayito.agent-project/1"
    name = "assistant"

    [runtime]
    kind = "hermes"

    [source]
    path = "."

    [model]
    provider = "openai"
    id = "gpt-4.1-mini"
    gateway = "model"

    [gateways.model]
    preset = "openai"
    secret = "provider-key"

    [agent]
    instructions = "Answer in short sentences."
    ```

=== "LangGraph"

    `graph.py`: the function receives the runner context (model, instructions,
    subagents, backend and the permissions middleware) and returns your
    compiled graph. Pass `ctx.middleware`: without it the permissions are lost.

    ```python
    from deepagents import create_deep_agent


    def build(ctx):
        return create_deep_agent(
            model=ctx.model,
            system_prompt=ctx.instructions,
            subagents=ctx.subagents,
            backend=ctx.backend,
            middleware=ctx.middleware,
        )
    ```

    ```toml
    [runtime]
    kind = "langgraph"
    entrypoint = "graph:build"
    ```

=== "deepagents"

    Without an `entrypoint`, the server builds `create_deep_agent` with the
    model, instructions and subagents of the `AgentSpec`.

    ```toml
    [runtime]
    kind = "deepagents"
    ```

=== "OpenCode"

    OpenCode is the default runtime of `sbx.agent`. Here it is served the same
    way: the server launches `opencode run` and translates its events.

    ```toml
    [runtime]
    kind = "opencode"

    [agent.permissions]
    default = "allow"
    tools = { webfetch = "deny" }
    ```

=== "Custom"

    A command that reads the request (one JSON line) on standard input and
    writes the answer. With `protocol = "text"`, what it prints is the answer.

    ```bash
    #!/usr/bin/env bash
    set -euo pipefail
    python3 -c 'import json, sys; print(json.loads(sys.stdin.readline())["prompt"])'
    ```

    ```toml
    [runtime]
    kind = "custom"
    command = ["bash", "run.sh"]
    protocol = "text"
    ```

Every project's model always carries the **name** of the secret
(`secret = "provider-key"`), never its value: no field in the file accepts a
credential. Unknown keys are rejected with their path (`runtime.entrpoint`),
and relative paths are resolved against the file's directory and cannot leave
it. The full reference is in [The project file](#el-fichero-de-proyecto).

## 2. Build the image and try it { #2-construye-la-imagen-y-pruebalo }

`build()` installs the runtime and copies your code into the image; `run()`
creates a sandbox from that image, runs the agent once and **always** destroys
it, also on failure.

=== "Python"

    ```python
    from rayito import AgentProject

    project = AgentProject.load("./support-bot")
    info = project.build(bucket="amzn-s3-demo-bucket", on_build_logs=print)
    result = project.run("Summarize the README.")
    print(result.text, result.steps, result.usage.total)
    ```

=== "Python (async)"

    ```python
    from rayito import AsyncAgentProject


    async def main() -> None:
        project = AsyncAgentProject.load("./support-bot")
        info = await project.build(bucket="amzn-s3-demo-bucket")
        result = await project.run("Summarize the README.")
        print(info.template_id, result.text)
    ```

=== "TypeScript"

    ```ts
    import { AgentProject } from "rayito";

    const project = await AgentProject.load("./support-bot");
    const info = await project.build({ bucket: "amzn-s3-demo-bucket" });
    const result = await project.run("Summarize the README.");
    console.log(info.templateId, result.text, result.usage.total);
    ```

=== "CLI"

    ```bash
    rayito agent build --file ./support-bot --bucket amzn-s3-demo-bucket
    rayito agent run "Summarize the README." --file ./support-bot --stream
    ```

You can also define the project in code, with no file. The constructor
validates everything before calling anything: the name, that every gateway the
`AgentSpec` asks for is in `gateways`, the port and the runtime and provider
combination.

=== "Python"

    ```python
    from rayito import AgentModel, AgentProject, AgentSpec, StrandsRuntime, bedrock_gateway

    MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

    project = AgentProject(
        name="support-bot",
        runtime=StrandsRuntime(entrypoint="agent:agent"),
        spec=AgentSpec(
            model=AgentModel(provider="bedrock", id=MODEL_ID, gateway="bedrock", region="us-east-1"),
            instructions="Answer in short sentences.",
        ),
        gateways={"bedrock": bedrock_gateway("provider-key", region="us-east-1", models=[MODEL_ID])},
        source="./support-bot",
    )
    ```

=== "TypeScript"

    ```ts
    import { AgentModel, AgentProject, AgentSpec, StrandsRuntime, bedrockGateway } from "rayito";

    const MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0";

    const project = new AgentProject({
      name: "support-bot",
      runtime: new StrandsRuntime({ entrypoint: "agent:agent" }),
      spec: new AgentSpec({
        model: new AgentModel({
          provider: "bedrock",
          id: MODEL_ID,
          gateway: "bedrock",
          region: "us-east-1",
        }),
        instructions: "Answer in short sentences.",
      }),
      gateways: {
        bedrock: bedrockGateway("provider-key", { region: "us-east-1", models: [MODEL_ID] }),
      },
      source: "./support-bot",
    });
    console.log(project.name);
    ```

## 3. Deploy it as a service { #3-despliegalo-como-servicio }

`deploy()` creates the sandbox, starts the agent server, waits until it is
ready and returns an `AgentDeployment`: the URL and the headers to call it, and
the methods to invoke it, check its status and stop it. If anything fails along
the way, it destroys the sandbox and raises the exception.

=== "Python"

    ```python
    from rayito import AgentProject

    project = AgentProject.load("./support-bot")
    deployment = project.deploy()
    print(deployment.sandbox_id, deployment.url)
    try:
        result = deployment.invoke("Hello")
        print(result.text)
    finally:
        deployment.stop()
    ```

=== "Python (async)"

    ```python
    from rayito import AsyncAgentProject


    async def main() -> None:
        project = AsyncAgentProject.load("./support-bot")
        deployment = await project.deploy()
        try:
            result = await deployment.invoke("Hello")
            print(result.text)
        finally:
            await deployment.stop()
    ```

=== "TypeScript"

    ```ts
    import { AgentProject } from "rayito";

    const project = await AgentProject.load("./support-bot");
    const deployment = await project.deploy();
    try {
      const reply = await deployment.invoke("Hello");
      console.log(deployment.sandboxId, deployment.url, reply.text);
    } finally {
      await deployment.stop();
    }
    ```

=== "CLI"

    ```bash
    rayito --json agent deploy --file ./support-bot --token-file ./agent.token
    rayito agent invoke microvm-00000000-0000-0000-0000-000000000001 "Hello" --token-file ./agent.token
    rayito agent status microvm-00000000-0000-0000-0000-000000000001 --token-file ./agent.token
    rayito agent stop microvm-00000000-0000-0000-0000-000000000001
    ```

`rayito agent deploy` prints the sandbox id, the URL and the port, and **never**
the access headers: they come from `deployment.headers`, which mints fresh
headers on every read. The service listens on port 8088 of the MicroVM (8080
and 9000 belong to `rayd`).

Talking to the deployment again from another process takes the access token of
its sandbox. `deploy` saves it to `--token-file` (a new file, mode `0600`; it
never goes through argv and is never printed) or uses the one in
`RAYITO_ACCESS_TOKEN` if you have it set, and it does not start with neither,
because there would be no way to reconnect. `invoke` and `status` read it from
`--token-file` or `RAYITO_ACCESS_TOKEN`. `stop` does not need it: it terminates
the sandbox by its id. From the SDK, `deploy(access_token=...)` (`deploy({
accessToken })` in TypeScript) does the same as the variable.

### Calling the service { #llamar-al-servicio }

`deployment.invoke()` returns an `AgentResult` (the same one
`sbx.agent.run()` returns), and `deployment.stream()` the events as they
arrive. From another process, reattach with the sandbox id:

=== "Python"

    ```python
    from rayito import AgentDeployment

    deployment = AgentDeployment.connect("microvm-00000000-0000-0000-0000-000000000001")
    with deployment.stream("Hello") as events:
        for event in events:
            print(event.type)
    print(deployment.status())
    ```

=== "Python (async)"

    ```python
    from rayito import AsyncAgentDeployment


    async def main() -> None:
        deployment = await AsyncAgentDeployment.connect("microvm-00000000-0000-0000-0000-000000000001")
        async with deployment.stream("Hello") as events:
            async for event in events:
                print(event.type)
    ```

=== "TypeScript"

    ```ts
    import { AgentDeployment } from "rayito";

    const deployment = await AgentDeployment.connect("microvm-00000000-0000-0000-0000-000000000001");
    for await (const event of deployment.stream("Hello")) {
      console.log(event.type);
    }
    console.log(await deployment.status());
    ```

=== "CLI"

    ```bash
    rayito --json agent invoke microvm-00000000-0000-0000-0000-000000000001 "Hello" --stream
    ```

### The service contract { #el-contrato-del-servicio }

The service speaks the AgentCore runtime contract, so the same agent is served
the same way in a MicroVM and in an exported container:

- `GET /ping` returns `200 {"status": "Healthy"}`, or `"HealthyBusy"` while it
  handles a request.
- `POST /invocations` receives `{"prompt", "session_id"?, "model"?, "reasoning"?}`
  (1 MiB at most). Without `Accept: text/event-stream` it answers a JSON with
  the `text`, the `session_id` and the usage; with it, an SSE stream with one
  event per agent protocol event, ending in `done` or `agent_failed`. An agent
  failure is a `200` with `failed`; HTTP codes are only for the contract
  (`400`, `413`, `429` when busy, `503` when not ready yet).
- It handles one request at a time. The
  `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` header acts as the `session_id`
  if the body does not carry one.

## 4. Export to a container { #4-exporta-a-un-contenedor }

`export()` writes, with no network call, a directory with the `Dockerfile`
(`linux/arm64`, base pinned by digest), the server, your code and a `README.md`
with the exact commands. With `agentcore` it also checks the AgentCore contract
constraints (arm64 and port 8080). It does not overwrite a non-empty directory
unless `overwrite` is set.

=== "Python"

    ```python
    from rayito import AgentProject

    exported = AgentProject.load("./support-bot").export(target="agentcore", out_dir="./build")
    print(exported.path, exported.port, exported.platform)
    ```

=== "Python (async)"

    ```python
    from rayito import AsyncAgentProject


    async def main() -> None:
        exported = await AsyncAgentProject.load("./support-bot").export(
            target="docker", out_dir="./build"
        )
        print(exported.files)
    ```

=== "TypeScript"

    ```ts
    import { AgentProject } from "rayito";

    const project = await AgentProject.load("./support-bot");
    const exported = await project.export({ target: "docker", outDir: "./build" });
    console.log(exported.path, exported.port);
    ```

=== "CLI"

    ```bash
    rayito agent export --file ./support-bot --target agentcore --out ./build
    ```

!!! warning "Outside Rayito, the platform supplies the credential"
    Inside a MicroVM the agent can never read the model credential: the gateway
    adds it when forwarding the request. In an exported container that
    guarantee **does not exist**: there is no gateway, so the agent reads the
    credential from the provider's standard source (environment variables, the
    task or runtime role). Give it only the permissions it needs and, if the
    container is reachable from outside, protect the service with
    `RAYITO_AGENT_SERVICE_TOKEN`: the server then requires
    `Authorization: Bearer <token>`.

## How the credential reaches the model { #como-llega-la-credencial-al-modelo }

Inside Rayito, every framework calls the model only through the gateway and
with a placeholder key:

- The real key lives in Secrets Manager and the gateway adds it. The agent code
  (yours and the framework's) only sees a local URL and a marker.
- Bedrock uses a Bedrock API key through the gateway, never AWS credentials the
  agent could read. IMDS and the rest of the Internet stay closed while
  `allow_internet_access` is `False` (the default).
- Framework telemetry is off, Hermes listens on loopback only and the Strands
  harness lacks the tool that runs model-written code.
- The gateways of OpenAI-style providers cannot limit the model: set the spend
  cap at the provider
  ([Agent providers](agente-proveedores.md#limites-del-lado-del-proveedor)).

## A deployment lasts 8 hours at most { #un-despliegue-dura-8-horas-como-mucho }

A MicroVM cannot live more than 8 h between launched and suspended, and the AWS
proxy headers expire after 60 minutes. That is why a deployment is not a
permanent service:

- `deployment.headers` mints fresh headers on every read; use them on every
  request instead of storing them.
- With `idle` (on by default) the sandbox suspends when it goes idle and
  resumes with the next request, paying no compute while it sleeps. Suspended
  time counts towards the 8 h.
- To keep going, deploy a new one before the previous one expires and switch the
  traffic; the old one is stopped with `stop()`.

=== "Python"

    ```python
    from rayito import AgentDeployment, AgentProject

    project = AgentProject.load("./support-bot")
    old = AgentDeployment.connect("microvm-00000000-0000-0000-0000-000000000001")
    fresh = project.deploy()
    print(fresh.sandbox_id)
    old.stop()
    ```

=== "TypeScript"

    ```ts
    import { AgentDeployment, AgentProject } from "rayito";

    const project = await AgentProject.load("./support-bot");
    const old = await AgentDeployment.connect("microvm-00000000-0000-0000-0000-000000000001");
    const fresh = await project.deploy();
    console.log(fresh.sandboxId);
    await old.stop();
    ```

## The project file { #el-fichero-de-proyecto }

`rayito-agent.toml` (preferred) or `rayito-agent.json`, with the same content,
looked up in that order in a directory. The schema is `rayito.agent-project/1`
and is published as a JSON Schema at `testdata/agent-project/schema.json` in
the repository.

| Table | Keys | What it is |
|---|---|---|
| (root) | `schema`, `name` | schema version and name (image and deployment label) |
| `[runtime]` | `kind`, `entrypoint`, `language`, `command`, `protocol` | the framework and how it starts |
| `[source]` | `path`, `requirements`, `exclude` | the directory copied into the image and its requirements |
| `[model]` | `provider`, `id`, `region`, `gateway` | the model, and which gateway it uses |
| `[gateways.<name>]` | `preset`, `secret`, `region`, `models`… | a gateway: one of the presets in [Agent providers](agente-proveedores.md) and the **name** of the secret |
| `[agent]` | `instructions` or `instructions_file`, `permissions` | instructions (mutually exclusive) and permissions |
| `[limits]` | `max_steps`, `timeout_seconds` | the limits of one run |
| `[serve]` | `timeout_seconds`, `idle_seconds` | deployment lifetime (≤ 8 h) and idle suspension |
| `[network]` | `allow_internet_access`, `egress` | sandbox network: closed unless you open it |

Times are in seconds in the file, in both SDKs. The CLI never changes what the
file says unless you pass the option (`--port`, `--timeout`).

## Other frameworks: `CustomRuntime` { #otros-frameworks-customruntime }

`CustomRuntime` serves any agent in one of three shapes, and exactly one:

- `command`: a command that reads the request on standard input. With
  `protocol="rayito-v1"` it writes agent protocol lines; with `protocol="text"`,
  what it prints is the answer.
- `entrypoint`: a Python function `package.module:function`.
- `app`: an HTTP application that already speaks `GET /ping` and
  `POST /invocations` (`HttpApp(command=..., port=...)`). If it speaks another
  protocol (for example, an MCP server), `protocol="raw"` only exposes it:
  `deploy()` gives you the URL and the headers, and `invoke()` raises
  `UnimplementedError`.

=== "Python"

    ```python
    from rayito import CustomRuntime, HttpApp

    command = CustomRuntime(command=["bash", "run.sh"], protocol="text")
    function = CustomRuntime(entrypoint="my_agent.main:answer")
    server = CustomRuntime(
        install=["pip install crewai"],
        app=HttpApp(command=["python", "serve.py"], port=9100),
    )
    ```

=== "TypeScript"

    ```ts
    import { CustomRuntime, HttpApp } from "rayito";

    const command = new CustomRuntime({ command: ["bash", "run.sh"], protocol: "text" });
    const server = new CustomRuntime({
      install: ["npm ci --ignore-scripts"],
      app: new HttpApp({ command: ["node", "serve.js"], port: 9100 }),
    });
    console.log(command, server);
    ```

| Framework | Recommended shape |
|---|---|
| CrewAI | `entrypoint` with a function that launches the `Crew`, or `app` if you already serve it |
| OpenAI Agents SDK | `entrypoint`, with the gateway `base_url` in `OpenAIChatCompletionsModel` |
| Google ADK | `app`: the ADK HTTP server behind `HttpApp` |
| Claude Agent SDK | `command`, with `protocol="text"` |
| Vercel AI SDK | `command` with `node`, or `app` with your server |

In all of them, the model is called with the gateway URL and a placeholder key:
there is no path for the agent to read the real credential.

## Strands with Rayito as the sandbox { #strands-con-rayito-como-sandbox }

If your Strands agent runs outside Rayito but you want its shell, file and code
tools to run in a MicroVM, the adapter fits in fewer than fifty lines:

```python
--8<-- "docs/examples/strands_rayito_sandbox.py"
```

## Errors { #errores }

An invalid project raises `InvalidArgumentException`/`InvalidArgumentError`
before any call (in the CLI, exit 2). An agent failure is
`AgentException`/`AgentError`, with the same closed `reason` list as
[Agent in the sandbox](agente-en-el-sandbox.md). Against a deployed service,
`429` is `AgentException(reason="busy")`, `503` after the startup deadline is
`TimeoutException`, and a `403` twice in a row is `AuthenticationException`.
Messages never carry the prompt, the headers or the name of a secret.

## See also { #ver-tambien }

- [Agent in the sandbox](agente-en-el-sandbox.md)
- [Agent providers](agente-proveedores.md)
- [Agent templates](../funciones-opcionales/templates-de-agente.md)
- [Secret gateway](../funciones-opcionales/pasarela-de-secretos.md)
- [Pricing (MicroVMs, pool, agents)](../cost.md#coste-de-un-agente-desplegado)
- [Security: code agent inside the sandbox](../security.md#agente-de-codigo-dentro-del-sandbox)
