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

## With nothing to write: `rayito agent deploy` { #sin-escribir-nada }

If you already have an agent (LangGraph, LangChain, deepagents, Strands, Hermes,
OpenCode, the Vercel AI SDK or an HTTP app of your own) you do not have to
describe it: `deploy_agent()` reads the project files, decides how to build and
serve it, and deploys it. It does not run your code to find out.

=== "CLI"

    ```bash
    rayito agent deploy ./mi-agente --dry-run   # what it detected and what it would cost; no AWS calls
    rayito agent deploy ./mi-agente             # deploy it
    ```

=== "Python"

    ```python
    from rayito import AgentDeployOptions, deploy_agent

    fleet = deploy_agent(
        "./mi-agente", options=AgentDeployOptions(bucket="amzn-s3-demo-bucket")
    )
    try:
        reply = fleet.invoke("Summarize the README.")
        print(reply.text, reply.session_id)
    finally:
        fleet.stop()
    ```

=== "Python (async)"

    ```python
    from rayito import AgentDeployOptions, deploy_agent_async


    async def main() -> None:
        fleet = await deploy_agent_async(
            "./mi-agente", options=AgentDeployOptions(bucket="amzn-s3-demo-bucket")
        )
        try:
            reply = await fleet.invoke("Summarize the README.")
            print(reply.text, reply.session_id)
        finally:
            await fleet.stop()
    ```

=== "TypeScript"

    ```ts
    import { deployAgent } from "rayito";

    const fleet = await deployAgent("./mi-agente", {
      options: { bucket: "amzn-s3-demo-bucket" },
    });
    try {
      const reply = await fleet.invoke("Summarize the README.");
      console.log(reply.text, reply.sessionId);
    } finally {
      await fleet.stop();
    }
    ```

What it returns is a [fleet](flota-de-agentes.md) (`AgentFleet`): one or more
MicroVMs serving the agent, with the sessions spread across them. With the
defaults it is a single VM, the same as `AgentProject.deploy()`.

### Prefer Amazon Bedrock AgentCore? { #desplegar-en-agentcore }

The same project deploys to
[Amazon Bedrock AgentCore Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agents-tools-runtime.html)
with `--target agentcore` (`target="agentcore"` in Python and
`target: "agentcore"` in TypeScript), with no local Docker: Rayito builds the
arm64 image with CodeBuild, creates the runtime with a least-privilege role and
returns an object with the same `invoke()` and the same sessions as a fleet. The
default target is still `microvm`. One important difference: AgentCore has no
Rayito secrets gateway, so the agent can read the runtime role's credentials.
Everything is in [Deploy to AgentCore](desplegar-en-agentcore.md).

```bash
rayito agent deploy ./my-agent --target agentcore --dry-run
```

### What it detects and from where { #que-detecta-y-de-donde }

| What | Where it comes from |
|---|---|
| Language | `pyproject.toml`, `requirements*.txt`, `uv.lock` (Python); `package.json` (TypeScript and JavaScript) |
| Framework | the dependencies (`langgraph`, `langchain`, `deepagents`, `strands-agents`, `strands-harness`, `ai`, `@langchain/langgraph`, `@strands-agents/sdk`…), `langgraph.json`, the Hermes or OpenCode configuration, and web apps (`fastapi`, `flask`, `express`, `hono`, `bedrock-agentcore`…) |
| Entrypoint | the agent object or a factory with no arguments: `agent = create_react_agent(...)`, `graph = workflow.compile()`, `export const agent = new Agent(...)`, `export async function createAgent()` |
| HTTP app (`custom`) | the start command (`uvicorn`, `flask`, the `start` script), the port, the health route and the invoke route, and the request and response body fields |
| Protocol | `agentcore` if there are `/ping` and `/invocations`; `json` for an app with its own POST route; `raw` if nothing is invocable |
| Install | the lockfile: `uv.lock`, `requirements.txt`, `package-lock.json`, `pnpm-lock.yaml` or `yarn.lock` |
| Versions | `requires-python`, `.python-version`, `engines.node`, `.nvmrc`; the highest pinned build that satisfies them is picked |
| Provider and model | the dependencies, the variable names (`AWS_BEARER_TOKEN_BEDROCK`, `ANTHROPIC_API_KEY`…) and the model id as a literal in the code or the configuration |

The values of a `.env` are never read, and `.git`, `.env*`, `.venv` and
`node_modules` do not go into the image: the dependencies are installed in it,
not copied from your laptop.

When a project has a framework object **and** an HTTP app (a graph and a
FastAPI `server.py`, for example), the framework wins, because it brings
`invoke()`, the events and the token usage. The app stays in the report as an
alternative; `--entry http` serves the app instead.

### Changing what was detected { #cambiar-lo-detectado }

The order is: the project file, then the flag or argument, then detection, then
the named default. A `rayito-agent.toml` only needs the keys you want to pin;
`rayito agent detect --write` saves what was detected so you can edit it.

| Flag | What it sets |
|---|---|
| `--framework`, `--language`, `--entry`, `--protocol` | what detection would decide (`--entry http` serves the HTTP app) |
| `--memory` | MiB of each VM (fixed in the image: changing it builds a new version) |
| `--max-sessions`, `--queue`, `--queue-timeout` | sessions per VM and the host queue ([Several sessions and several VMs](flota-de-agentes.md)) |
| `--min-vms`, `--max-vms`, `--warm`, `--sessions`, `--idle` | how many VMs and how sessions are spread |
| `--env KEY=VALUE`, `--allow-out destination` | non-secret variables and allowed outbound destinations |
| `--secret provider=name`, `--create-secret`, `--bucket`, `--base-artifact`, `--yes` | where the model credential and the bucket are, and what gets created |
| `--dry-run`, `--write` | show the plan without calling AWS, and save what was detected |

An unclear case fails with an error naming what resolves it and never guesses:
`ambiguous` (two frameworks or two entrypoints are possible: use `--framework`
or `--entry`), `not_detected` (the framework is not recognized, or the model id
is missing: write it as a literal or in `[model]`) and `unsupported` (a
combination that cannot reach the gateway without changing the code).

An `--entry` that does not exist (a module, a file or an attribute that is not
there) fails the deploy, not the first invocation: the agent host imports the
entrypoint at boot and `deploy` ends with «el host del agente no pudo arrancar
(entrypoint_not_found)».

### Model chosen by your code { #modelo-desde-tu-codigo }

If your code picks the model from its configuration, for example
`ChatOpenAI(model=settings.llm_model, base_url=settings.base_url)`, you do not
need to write the model id anywhere. Detection recognizes two things without
running your code or reading any value:

- A custom `base_url=` in the client constructor (in TypeScript, `baseURL`,
  also inside `configuration`). With it, an `openai` provider detected from a
  dependency becomes `openai-compatible`. The official OpenAI URL does not
  count, and if your code sets the model id as a literal (what was already
  deployed before), the provider stays `openai` unless you pass
  `--model-base-url`; detection says so with `custom_base_url`.
- The **names** of the model, base URL and key variables (`LLM_MODEL`,
  `LLM_BASE_URL`, `LLM_API_KEY`). They come from `.env.example`, from
  `os.getenv` / `process.env` reads, from pydantic-settings fields
  (`env_prefix` plus the field) and from the `env` keys of `langgraph.json`.
  This is a best-effort heuristic: if it misses, write the names in `[model]`.

The resolved project has `[model]` without `id` and with those names, and the
report warns with `model_id_from_code`. The URL of your LLM gateway cannot be
detected, so you pass it with a flag:

```bash
rayito agent deploy --model-provider openai-compatible \
  --model-base-url https://llm-gateway.example.com --secret openai-compatible=llm-key
```

In the project file it looks like this:

```toml
[model]
provider = "openai-compatible"
gateway = "llm"
base_path = "/v1"
id_env = "LLM_MODEL"
base_url_env = "LLM_BASE_URL"
api_key_env = "LLM_API_KEY"

[gateways.llm]
preset = "openai-compatible"
secret = "llm-key"
upstream = "https://llm-gateway.example.com"
base_path = "/v1"
```

On MicroVMs, Rayito points `LLM_BASE_URL` at the secret gateway and puts the
placeholder in `LLM_API_KEY`. Your code reads its own configuration and goes
through the gateway without ever seeing the real key. `LLM_MODEL` is only set
when the project knows the id (`--model-id` or `[model] id`).

The id is still required where Rayito uses it:

- With the `bedrock` and `gemini` gateways, which only let the models on their
  list through.
- With the runtimes that build the model: `opencode`, `hermes`, and
  `deepagents` or `strands` without an `entrypoint`.

In every other case (a graph with an `entrypoint`, an HTTP app,
`CustomRuntime`), your code gets `ctx.model = None` (`undefined` in Node) and
picks its own. On AgentCore, a model outside Bedrock needs
[`--external-model`](desplegar-en-agentcore.md#modelos-fuera-de-bedrock).

### What resolves by itself and what does not { #que-se-resuelve-solo }

Each value comes, in this order, from the argument or flag, the file, an
environment variable and discovery; none of them creates anything silently:

- The **region** is `--region`, `[deploy] region`, `AWS_REGION` or your profile's.
- The **base image** is the newest version of `rayito-base-caps`, which
  `rayito setup --caps-image` publishes since 0.17. If it does not exist it is
  published only with `--base-artifact` and `--yes`. It is needed only when the
  deploy builds: with `--no-build`, or without a bucket and with the project
  image already built (`[image] template`), it is not read, and if it is
  missing while the project image exists, the error says how to reuse it.
- The artifact **bucket** is, in this order, `--bucket`, `[deploy] bucket`,
  `RAYITO_BUCKET`, the account's CDK bootstrap bucket if it exists (`HeadBucket`)
  and, otherwise, the one `rayito setup` created: the `BucketName` output of the
  `rayito-m0-artifacts` stack when it is ready (`DescribeStacks`, read-only;
  skipped without permission to read it).
  `rayito agent deploy` does not create buckets: if there is none, run
  [`rayito setup`](../primeros-pasos/configurar-aws.md#2-prepara-la-cuenta-con-rayito-setup)
  first, which creates one if needed.
- The **model secret** is `--secret`, the file's, or the one with the
  conventional name, which detection writes as `agents/<project>/<provider>`. A
  gateway's secret name is relative to the `rayito/` prefix the gateway reads it
  with, so in Secrets Manager it is `rayito/agents/<project>/<provider>`. Only
  with `--create-secret` is a missing one created, under that same name, from
  the standard variable of the gateway's **preset**: `AWS_BEARER_TOKEN_BEDROCK`
  for `[gateways.model] preset = "bedrock"`, even though the gateway is called
  `model` (with `preset = "custom"`, the one of the gateway name). The variable
  is read in your process and written to neither disk nor logs.
- The **access token** is `RAYITO_ACCESS_TOKEN`'s, `--token-file`'s or a new one
  saved to `.rayito/agent-token` (mode `0600`). The file never goes into the
  image: `.rayito/` is excluded at any depth, and so is a `--token-file` inside
  the project.

The agent image is only built if its content changed (code, dependencies,
runtime or memory); otherwise it is reused.

!!! warning "Coming from 0.13"
    `rayito agent deploy --file rayito-agent.toml` still works and keeps its
    output keys, but it now deploys a fleet and makes more AWS calls than in
    0.13: it reads the base image (`GetMicrovmImage`), the model secret
    (`DescribeSecret`) and the bucket (`HeadBucket`, `GetCallerIdentity`), and
    builds the image if its content changed. Your credentials need
    `lambda-microvms:GetMicrovmImage`, `secretsmanager:DescribeSecret`,
    `sts:GetCallerIdentity` and `s3:ListBucket` on the bucket, on top of the
    0.13 ones. The image no longer uploads `.git`, `.env*`, `.venv`, `venv`,
    `node_modules`, `__pycache__`, `*.pyc` or `.rayito`, so a project that
    uploaded them rebuilds its image. With `--no-build` you reuse the existing
    one. And `max_concurrency` in `[serve]` is capped by memory (7 with 2048
    MiB, 15 with 4096, 31 with 8192): a higher value fails when the project
    loads.

!!! warning "Coming from 0.16"
    0.16 detection wrote the secret as `rayito/agents/<project>/<provider>`,
    and since the gateway reads it under `rayito/`, `--create-secret` created it
    as `rayito/rayito/agents/...`. Since 0.17 detection writes
    `agents/<project>/<provider>` and the secret lives at
    `rayito/agents/<project>/<provider>`. If that name does not exist but the
    0.16 one does, `rayito agent deploy` reuses the 0.16 one and creates no
    other. To connect from another process with `AgentFleet.connect(project=...)`
    without a project file, rename the secret or pin its name in
    `rayito-agent.toml` (`secret = "rayito/agents/<project>/<provider>"`). A
    `rayito-agent.toml` that already wrote that name stays as it was.

### A framework that is not on the list { #un-framework-que-no-esta-en-la-lista }

If the framework has no support of its own, the minimum contract is one of
these three shapes, those of [`CustomRuntime`](#otros-frameworks-customruntime):

| Shape | What we expect | Protocol |
|---|---|---|
| HTTP app | a health `GET` and an invoke JSON `POST` | `json` (or `agentcore` if it already speaks `/ping` and `/invocations`) |
| Function | `module:attribute` (Python) or `path#export` (Node) that takes `(prompt, session_id)` and returns text or `{text, usage}` | — |
| Command | reads the prompt from standard input and writes text, or v1 protocol lines, to standard output | `text` or `rayito-v1` |

!!! info "Cost and activation"
    - **Activated by:** calling `deploy_agent()`, `deploy_agent_async()`,
      `deployAgent()` or `rayito agent deploy`. `plan_deploy()`/`planDeploy()`
      and `--dry-run` compute the plan and the cost without calling AWS or
      loading any AWS package; `rayito agent detect` and `AgentProject.detect()`
      only read files.
    - **AWS resources and calls:** `min_vms` MicroVMs running and `warm_vms`
      parked; a new image version if the content changed (and an object in the
      artifact bucket); reads of `GetCallerIdentity`, `HeadBucket`,
      `GetMicrovmImage` and `DescribeSecret`; with `--create-secret`,
      `CreateSecret`; with `--base-artifact`, a base image.
    - **Approximate cost:** for each running VM, $0.126 per hour with 2 GB
      (suspended it only pays for the snapshot storage); an image version ≈
      $0.056 per week; a secret $0.40 per month. List prices, us-east-1,
      checked on 2026-10-06: [Pricing](../cost.md#coste-de-un-agente-desplegado).
    - **IAM:** those of `Sandbox.create()`, `Sandbox.list()` and
      `AgentTemplate.build()` (`RayitoSandboxRunner`, `RayitoTemplateBuilder`),
      `sts:GetCallerIdentity`, `s3:ListBucket` on the bucket,
      `secretsmanager:DescribeSecret` and, with `--create-secret`,
      `secretsmanager:CreateSecret`.
    - **How to turn it off:** `fleet.stop()` or `rayito agent stop <name>`
      terminate every VM of the fleet; the defaults create no secret, base image
      or index.
    - **Example:** `deploy_agent("./mi-agente").invoke("Hello").text`.

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
      stopped with `deployment.stop()` or `rayito agent stop` (or suspended
      with `deployment.pause()`, which bills no compute); image versions with
      `rayito image prune`. `deploy(keep_on_failure=True)` leaves the sandbox
      of a failed startup alive so you can debug it and keeps billing until
      you terminate it; it is off by default.
    - **Token usage:** the runtimes with their own support always report the
      tokens of each step. An app, a command or a function of your own only
      reports them if its response carries `usage`; otherwise
      `result.usage_reported` is `False` and `result.usage` is zero because it
      is unknown, not because nothing was spent. Check consumption at your
      provider.
    - **Example:** `AgentProject.load(".").deploy()` (all four languages are
      [below](#3-despliegalo-como-servicio)).

## Before you start { #antes-de-empezar }

1. [Configure AWS](../primeros-pasos/configurar-aws.md) done (`rayito setup
   --caps-image`, which since 0.17 also publishes `rayito-base-caps`): it is the base of the
   agent image, the only one that enforces closed egress.
2. The model key in Secrets Manager, and `RayitoSecretsReader` for whoever
   calls the SDK ([Agent in the sandbox](agente-en-el-sandbox.md#1-el-secreto-y-la-pasarela)).
3. Access to the model at your provider
   ([Agent providers](agente-proveedores.md)).
4. An S3 bucket for the build artifact (the one `rayito setup` leaves) and `RayitoTemplateBuilder` for whoever
   builds ([Agent templates](../funciones-opcionales/templates-de-agente.md)).

`rayito agent init` and `rayito agent export` work without any of this: they
only write files.

### The one-time prerequisites { #los-preparativos-de-una-sola-vez }

There are three, done once per account and region, and `rayito doctor` checks
that they are in place. With them done, the first deployment is building the
agent image (≈ 5 minutes the first time, because it installs the runtime and
your requirements) and deploying it (≈ 20 seconds until the service answers).

| What | How | How long |
|---|---|---|
| An S3 bucket for the build artifact | `rayito setup` reuses or creates one and prints `RAYITO_BUCKET=<bucket>`; if you prefer your own, `export RAYITO_BUCKET=amzn-s3-demo-bucket` ([Configure AWS](../primeros-pasos/configurar-aws.md#2-prepara-la-cuenta-con-rayito-setup)) | included in `rayito setup` |
| The `rayito-base-caps` base image | `rayito setup --caps-image` publishes it since 0.17 (not without the option, because it costs money); by hand, from the release `rayito-image.zip` ([Configure AWS](../primeros-pasos/configurar-aws.md#a-mano-sin-rayito-setup)) | included in `rayito setup --caps-image`; by hand, ≈ 4 min |
| The version of the AWS managed base | no need to look it up by hand: with `--base-image-version latest`, `rayito image publish` uses the newest one AWS lists and prints it; an explicit number pins a reproducible build | — |

```bash
rayito image publish --artifact rayito-image.zip --base-image-version latest \
  --image-name rayito-base-caps --os-capabilities ALL
rayito agent build --file ./support-bot --bucket "$RAYITO_BUCKET"
```

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

    `graph.py`: the function receives the runner context (`ctx.model`, the model
    already wired to the gateway, and `ctx.instructions`) and returns your
    compiled graph. A plain LangGraph graph works, the one you already have: a
    `create_react_agent` or a `StateGraph` whose model node is called `agent`
    (or `model`) and whose tools node is called `tools`. Rayito translates the
    text, the steps, the tool calls and the token usage from those nodes.

    ```python
    from langchain_core.tools import tool
    from langgraph.prebuilt import create_react_agent


    @tool
    def word_count(text: str) -> int:
        """Count the words of a text."""
        return len(text.split())


    def build(ctx):
        return create_react_agent(ctx.model, [word_count], prompt=ctx.instructions)
    ```

    ```toml
    [runtime]
    kind = "langgraph"
    entrypoint = "graph:build"
    ```

    The `AgentSpec` permissions are applied by an `AgentMiddleware`, which
    `create_react_agent` and `StateGraph` do not have: their tools are yours and
    are not filtered. To get them applied, build with
    `langchain.agents.create_agent(..., middleware=ctx.middleware)`. If you
    want a deepagents deep agent, pass `ctx.subagents`, `ctx.backend` and
    `ctx.middleware` (without the middleware the permissions are lost), or use
    `kind = "deepagents"` directly:

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

    `rayito agent init --runtime custom` writes an HTTP app: the `[runtime.app]`
    table with your `command`, the `port` it listens on and its routes, plus
    `requirements` and `install`. Rayito starts the `command` from the
    directory of your copied code (also on its `PYTHONPATH`, so `python -m
    agent.server` works) and, with `requirements`, with the venv of your
    requirements first on the `PATH`: `python` is the venv one.

    ```toml
    [runtime]
    kind = "custom"
    requirements = "requirements.txt"
    install = []

    [runtime.app]
    command = ["python", "-m", "agent.server"]
    port = 9100
    health_path = "/health"
    invoke_path = "/invoke"
    protocol = "raw"
    ```

    This app declares its own routes (`/health` and `/invoke`), so it uses
    `protocol = "raw"`: the deployment forwards them as they are and you call
    them with `deployment.url` and `deployment.headers`. With the default
    protocol (`agentcore`) only `/ping` and `/invocations` are exposed, and
    `/health` and `/invoke` answer 404; [which to choose](#que-protocolo-elegir).
    If you want a command that reads the request (one JSON line) on standard
    input and writes the answer instead, with `protocol = "text"` what it
    prints is the answer:

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
the methods to invoke it, check its status, pause it and stop it. If anything
fails along the way, it destroys the sandbox (unless `keep_on_failure` is set,
see [If the startup fails](#si-el-arranque-falla)) and raises the exception.

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
because there would be no way to reconnect. If the deploy fails before the
fleet exists, it deletes the `--token-file` it has just created, so retrying
the same command works (since 0.17.1); with `--keep-on-failure` it keeps it,
because it is the only way into the sandbox that stays. `invoke` and `status` read it from
`--token-file` or `RAYITO_ACCESS_TOKEN`. `stop` does not need it: it terminates
the sandbox by its id. From the SDK, `deploy(access_token=...)` (`deploy({
accessToken })` in TypeScript) does the same as the variable; if you pass none,
`deploy()` generates one and `deployment.access_token` (`deployment.accessToken`
in TypeScript) hands it back so you can keep it (never print it or commit it).

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

### Pause, resume and reconnect { #pausar-reanudar-y-reconectar }

`deployment.pause()` suspends the sandbox: it bills no compute while it sleeps
and the service keeps its state. The next request resumes it (so does
`deployment.resume()`), and with `idle`, on by default, it suspends itself
once idle. To get back to the deployment from another process, pass the token
you saved:

=== "Python"

    ```python
    from rayito import AgentDeployment, AgentProject

    deployment = AgentProject.load("./support-bot").deploy()
    token = deployment.access_token
    deployment.pause()
    deployment.resume()

    same = AgentDeployment.connect(deployment.sandbox_id, access_token=token)
    print(same.status())
    deployment.stop()
    ```

=== "Python (async)"

    ```python
    from rayito import AsyncAgentDeployment, AsyncAgentProject


    async def main() -> None:
        deployment = await AsyncAgentProject.load("./support-bot").deploy()
        token = deployment.access_token
        await deployment.pause()
        await deployment.resume()

        same = await AsyncAgentDeployment.connect(deployment.sandbox_id, access_token=token)
        print(await same.status())
        await deployment.stop()
    ```

=== "TypeScript"

    ```ts
    import { AgentDeployment, AgentProject } from "rayito";

    const project = await AgentProject.load("./support-bot");
    const deployment = await project.deploy();
    const token = deployment.accessToken;
    await deployment.pause();
    await deployment.resume();

    const same = await AgentDeployment.connect(deployment.sandboxId, { accessToken: token });
    console.log(await same.status());
    await deployment.stop();
    ```

### If the startup fails { #si-el-arranque-falla }

If the app does not start (a module is missing, the command does not exist, it
does not answer its health route), `deploy()` raises `SandboxException` with the
failure code (`app_exited`, `app_not_ready`, the exception class) and the end of
the app's standard error, bounded to 2 KiB and with no environment values or
credentials. By default it destroys the sandbox. To get in and debug it, ask
for it to be kept (`keep_on_failure=True`; an `access_token` is required,
because without one nobody could connect); the error carries the id in a note.
It costs money until you terminate it with `Sandbox.kill(sandbox_id)` or
`rayito agent stop`.

=== "Python"

    ```python
    from rayito import AgentProject
    from rayito.exceptions import SandboxException

    project = AgentProject.load("./support-bot")
    try:
        project.deploy(access_token="<token>", keep_on_failure=True)
    except SandboxException as exc:
        print(exc)
        print(*getattr(exc, "__notes__", []), sep="\n")
    ```

=== "TypeScript"

    ```ts
    import { AgentProject, SandboxError } from "rayito";

    const project = await AgentProject.load("./support-bot");
    try {
      await project.deploy({ accessToken: "<token>", keepOnFailure: true });
    } catch (error) {
      if (error instanceof SandboxError) {
        console.error(error.message);
      }
    }
    ```

=== "CLI"

    ```bash
    rayito agent deploy --file ./support-bot --token-file ./agent.token --keep-on-failure
    ```

### Which protocol to choose { #que-protocolo-elegir }

The `protocol` of an `HttpApp` decides what the deployment exposes. The default
is `agentcore`, and with it your own routes cannot be reached from outside:

| `protocol` | What the deployment exposes | `invoke()` | When |
|---|---|---|---|
| `agentcore` (default) | `GET /ping` and `POST /invocations`; the host translates to your `invoke_path` | yes | your app receives `{"prompt"}` on a route and you want `deployment.invoke()` and `stream()` |
| `openai-chat` | the same, translating Chat Completions with streaming | yes | your app is an OpenAI-compatible server |
| `json` | `GET /ping` and `POST /invocations`, translated to your route (the prompt goes in `request_field`), and your other routes as they are | yes | your app has its own JSON contract and you want `invoke()` **and** `/health` or `/chat` to stay reachable; it is what [detection](#sin-escribir-nada) picks |
| `raw` | your routes as they are (`/health`, `/invoke`, an MCP server...) | no: `UnimplementedError` | your app already has its own routes and you call them with your HTTP client |

With `agentcore` or `openai-chat`, a `health_path` or `invoke_path` other than
`/ping` and `/invocations` is only used by the host inside; asking for
`/health` or `/invoke` on the deployment URL gives 404. `rayito agent deploy`
warns about it in that case, and `rayito agent init --runtime custom` writes
`protocol = "raw"` because its app declares its own routes.

### LangGraph Platform API (experimental) { #langgraph-platform-api }

If your project is already served with `langgraph dev` (a `langgraph.json` and
the LangGraph Platform API: `/ok`, `/threads`, `/runs/wait`), you can deploy it
as it is with `--protocol langgraph-api`. Without the option nothing changes:
Rayito keeps serving a `langgraph.json` as always, importing the graph in the
host process (`framework` protocol). The new protocol only gives you something
if you rely on the Platform API semantics (assistants, the thread state or an
input that is not messages).

```bash
rayito agent detect ./ticket-agent --protocol langgraph-api
rayito agent deploy ./ticket-agent --protocol langgraph-api
```

With the option, detection serves the project as a `custom` app that starts
`langgraph dev --no-browser --no-reload` on internal port 8000, with
`protocol = "langgraph-api"` and `assistant_id` equal to the graph chosen from
`graphs` (the one of `--graph`, or the one called `agent` if there are several). Without
`langgraph.json` it fails with `invalid_value` on `runtime.app.protocol`. It
always warns that `langgraph dev` is a development server and, if
`langgraph-cli` is not among your dependencies, that you should add it. The
equivalent project file is:

```toml
[runtime]
kind = "custom"
requirements = "requirements.txt"

[runtime.app]
command = ["langgraph", "dev", "--no-browser", "--no-reload", "--host", "127.0.0.1", "--port", "8000", "--config", "langgraph.json"]
port = 8000
health_path = "/ok"
protocol = "langgraph-api"
assistant_id = "agent"
```

=== "Python"

    ```python
    from rayito import CustomRuntime, HttpApp

    runtime = CustomRuntime(
        requirements="requirements.txt",
        app=HttpApp(
            command=["langgraph", "dev", "--no-browser", "--no-reload", "--port", "8000"],
            port=8000,
            protocol="langgraph-api",
            assistant_id="agent",
        ),
    )
    ```

=== "TypeScript"

    ```ts
    import { CustomRuntime, HttpApp } from "rayito";

    const runtime = new CustomRuntime({
      requirements: "requirements.txt",
      app: new HttpApp({
        command: ["langgraph", "dev", "--no-browser", "--no-reload", "--port", "8000"],
        port: 8000,
        protocol: "langgraph-api",
        assistantId: "agent",
      }),
    });
    console.log(runtime);
    ```

What the host does with each invocation:

- It translates it to `POST /threads/{thread_id}/runs/wait` with
  `{"assistant_id", "input", "if_not_exists": "create", "multitask_strategy": "reject"}`;
  the prompt reaches the graph as a single user message
  (`{"messages": [{"role": "user", "content": ...}]}`), and a structured
  input (`invoke(input={...})`, see
  [Several graphs and structured input](#varios-grafos-y-entrada-estructurada))
  goes as is as `input`.
- The thread comes from the session (`uuid5` of its id), so two invocations
  with the same session follow the same thread. A second run on a busy thread
  is not queued: it fails with `app_http_409`. If the client stops waiting
  (its timeout expires or it cancels the invocation), the run keeps going on
  the server until it finishes, and meanwhile another invocation with the
  same session may get `app_http_409`: wait for it to finish or use another
  session.
- The text comes from `response_field` (if you set it) or from the `content`
  of the last message of the final state; without messages, it is the whole
  state as strict JSON (a `NaN` or an infinity is `invalid_response`). If you
  called with `input` and the final state has no messages, that state is
  `output`, as with a graph with its own schema, and the text is empty so it
  is not repeated; if the final state has messages, there is no `output` and
  the text is that of its last message, as with a messages graph in
  deepagents. Token usage comes from its `usage_metadata`, and without it
  usage is unknown, not zero.
- A graph that stops at an `interrupt` (human-in-the-loop) returns
  `AgentException` with `reason="protocol_error"` and `detail_code`
  `graph_interrupted`; any other HTTP status is `app_http_<n>`.
- Health is `GET /ok` (if you leave `health_path` at its default) and the host
  waits up to 60 seconds for `langgraph dev` to start. `invoke_path` is not
  changed: the API sets the route.
- `langgraph dev` starts with `LANGGRAPH_CLI_NO_ANALYTICS=1` and
  `LANGSMITH_TRACING=false`, unless your project sets those variables.

!!! warning "Experimental: a development server"
    `langgraph dev` keeps the state in memory: threads are lost when the VM or
    the AgentCore session ends. Rayito does not install
    `langgraph-cli[inmem]` (add it to your requirements) and the
    `langgraph-api` package it brings is distributed under the Elastic-2.0
    license: review it before using it. The production server
    (`langgraph up` or the Agent Server, which need Postgres, Redis and a
    license), streaming (`stream()` delivers the whole text in one event),
    resuming an `interrupt` and `langgraphjs dev` are not supported. Only
    `/ping` and `/invocations` are exposed: the rest of the Platform API cannot
    be reached from outside. A `rayito-agent.toml` with
    `protocol = "langgraph-api"` or `assistant_id` fails with `unknown key` on
    an SDK older than this feature.

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
  (`400`, `413`, `429` when busy, `503` when not ready yet). The JSON carries
  `usage_reported`: `false` if the agent did not report tokens (`usage` is then
  zero because it is unknown); an older service without the field counts as
  `true`.
- It handles one request at a time. The
  `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` header acts as the `session_id`
  if the body does not carry one.

### Several graphs and structured input { #varios-grafos-y-entrada-estructurada }

**Several graphs.** If `langgraph.json` declares more than one graph, choose the
one to deploy with `--graph NAME` or with `[runtime] graph` in
`rayito-agent.toml` (the name is the key in `graphs`). Without a choice, the only
graph or the one named `agent` is used; with several and none named `agent`, the
deploy fails with `ambiguous` and the message lists the names. A name that is
not there is `invalid_value` on `runtime.graph`, and `graph` together with
`entrypoint` (or `--graph` together with `--entry`) is `conflict`. The choice is
made by reading `langgraph.json`, without running your code, and becomes the
graph's `entrypoint` before the runtime is built.

```toml
[runtime]
kind = "langgraph"
graph = "triage"
```

**Structured input.** A graph whose input schema is not a messages schema (for
example, `StateGraph(State, input_schema=InputState,
output_schema=OutputState)` with `InputState` = `{"ticket_id": str}`) is not
called with a prompt but with `input`, a JSON object, and returns `output`, its
last state filtered to the output schema:

=== "Python"

    ```python
    from rayito import AgentDeployment

    deployment = AgentDeployment.connect("microvm-00000000-0000-0000-0000-000000000001")
    result = deployment.invoke(input={"ticket_id": "T-1"})
    print(result.output)
    again = deployment.invoke(input={"ticket_id": "T-2"}, session_id=result.session_id)
    ```

=== "Python (async)"

    ```python
    from rayito import AsyncAgentDeployment


    async def main() -> None:
        deployment = await AsyncAgentDeployment.connect("microvm-00000000-0000-0000-0000-000000000001")
        result = await deployment.invoke(input={"ticket_id": "T-1"})
        print(result.output)
    ```

=== "TypeScript"

    ```ts
    import { AgentDeployment } from "rayito";

    const deployment = await AgentDeployment.connect("microvm-00000000-0000-0000-0000-000000000001");
    const result = await deployment.invoke({ input: { ticket_id: "T-1" } });
    console.log(result.output);
    ```

=== "CLI"

    ```bash
    rayito agent deploy --graph triage
    rayito agent invoke microvm-00000000-0000-0000-0000-000000000001 --input '{"ticket_id": "T-1"}'
    rayito agent invoke microvm-00000000-0000-0000-0000-000000000001 --input-file ticket.json --stream
    ```

The same works for `stream()`, for a fleet and for an agent on
[AgentCore](desplegar-en-agentcore.md#sesiones): the `done` event carries
`output`. Exactly one of the two, `prompt` or `input`: otherwise the SDK raises
`InvalidArgumentException` / `InvalidArgumentError` before calling anything, and
the CLI exits with 2. Inside the MicroVM, the host reads the schema with
`get_input_jsonschema()` (in LangGraph.js, `getInputJsonSchema`, since 1.1): a
graph with `messages` in its schema, or whose schema cannot be read, is called
as always. Failures arrive as `AgentException` with `reason="protocol_error"`
and a `detail_code`:

| `detail_code` | When |
|---|---|
| `input_required` | you called a graph whose input schema has required keys (`required`) with a prompt; without them, the prompt goes as a message, as always |
| `invalid_input` | `input` does not match the schema (required keys, extra keys with `additionalProperties: false`, top-level types), or the graph raised a pydantic `ValidationError` |
| `output_not_serializable` | the output cannot be converted to JSON or exceeds {{agentServiceMaxOutputBytes}} bytes |

The `session_id` is the graph's `thread_id`: if the graph has its own
`checkpointer`, passing the same `session_id` continues the thread. An agent
deployed with an earlier version answers `400` to `input`: the SDK turns it into
"redeploy it with this version of Rayito". Neither `input` nor `output` ever
reaches the logs, only sizes and codes.

## 4. Export to a container { #4-exporta-a-un-contenedor }

`export()` writes, with no network call, a directory with the `Dockerfile`
(`linux/arm64`, base pinned by digest), the server, your code and a `README.md`
with the exact commands. With `agentcore` it also checks the AgentCore contract
constraints (arm64 and port 8080). It does not overwrite a non-empty directory
unless `overwrite` is set.

With `agentcore`, the `Dockerfile` takes its base image from a build argument,
`AGENT_BASE_IMAGE`, whose default is the reference pinned by digest: use it to
pull the same image, with the same digest, from another registry (for example,
`public.ecr.aws/docker/library/python`). The exported `README.md` explains how
to deploy the context to AgentCore and the security difference. If you do not
want to build the image or create the runtime by hand,
[`rayito agent deploy --target agentcore`](desplegar-en-agentcore.md) does both.
The `docker` target is unchanged.

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

If your code reads from environment variables other values that live in
Secrets Manager (a database URL, the key of a gateway that does not go through
Rayito), `[secret_env]` and `--secret-env` set them at startup: the SDK reads
them with your credentials and the host receives them only in its process
environment. Unlike the gateway, the agent can read them. See
[Secrets as environment variables](secretos-como-entorno.md).

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
| `[runtime]` | `kind`, `entrypoint`, `language`, `framework`, `command`, `protocol`, `requirements`, `package_manager`, `python_version`, `node_version`, `adapter`, `install`, `python`, `toolsets`, `skills_dir` | the framework and how it is installed and started; each `kind` only accepts some (below). With [detection](#sin-escribir-nada), `kind` is optional and the rest is filled from what was detected |
| `[runtime.app]` | `command`, `port`, `health_path`, `invoke_path`, `protocol`, `request_field`, `response_field`, `port_env` | only with `kind = "custom"`: your HTTP app (`HttpApp`); `command` and `port` are required; `request_field` and `response_field` only with `protocol = "json"` |
| `[source]` | `path`, `requirements`, `exclude` | the directory copied into the image and its requirements |
| `[model]` | `provider`, `id`, `region`, `gateway`, `base_path`, `prompt_caching` | the model, and which gateway it uses |
| `[gateways.<name>]` | `preset`, `secret`, `region`, `models`… | a gateway: one of the presets in [Agent providers](agente-proveedores.md) and the **name** of the secret |
| `[agent]` | `instructions` or `instructions_file`, `small_model`, `runtime_version`, `raw_config`, `permissions`, `mcp.<name>`, `agents.<name>` | instructions (mutually exclusive), permissions, MCP servers and subagents |
| `[limits]` | `max_steps`, `timeout_seconds`, `max_output_bytes`, `max_total_tokens` | the limits of one run |
| `[serve]` | `port`, `timeout_seconds`, `idle_seconds`, `max_concurrency`, `queue_max`, `queue_timeout_seconds`, `metadata` | service port, deployment lifetime (≤ 8 h), idle suspension, concurrency per VM, its queue and labels |
| `[scaling]` | `min_vms`, `max_vms`, `warm_vms`, `sessions`, `scale_in_after_seconds` | how many VMs and how sessions are spread ([Several sessions and several VMs](flota-de-agentes.md)) |
| `[env]` | `NAME = "value"` | non-secret environment variables of the agent; a name or a value shaped like a secret is rejected (`secret_value`): credentials go through a gateway |
| `[network]` | `allow_internet_access`, `egress`, `allow_out`, `deny_out` | sandbox network: closed unless you open it; `allow_out` and `deny_out` are the outbound policy (needs `rayito-base-caps`) |
| `[deploy]` | `region`, `bucket`, `base_image_version` | where it is deployed: the region, the artifact bucket and the base image version (`latest` by default) |
| `[image]` | `base`, `template`, `memory_mib` | the base image, the name of an already built image and its memory |

`[runtime]` keys by `kind`: `langgraph` accepts `entrypoint`; `strands` and
`strands-harness`, `entrypoint`, `language` and `requirements`; `hermes`,
`toolsets` and `skills_dir`; `custom`, `install`, `requirements`, `python` and
exactly one of `command` (with `protocol`: `rayito-v1` or `text`), `entrypoint`
or the `[runtime.app]` table; `opencode` and `deepagents`, none. `install` are
shell commands run when the image is built, before your code is copied, and
`requirements` is a requirements file in your `source` that is installed into
its own venv.

Times are in seconds in the file, in both SDKs. The CLI never changes what the
file says unless you pass the option (`--port`, `--timeout`).

## Other frameworks: `CustomRuntime` { #otros-frameworks-customruntime }

`CustomRuntime` serves any agent in one of three shapes, and exactly one:

- `command`: a command that reads the request on standard input. With
  `protocol="rayito-v1"` it writes agent protocol lines; with `protocol="text"`,
  what it prints is the answer.
- `entrypoint`: a Python function `package.module:function`.
- `app`: an HTTP application (`HttpApp(command=..., port=...)`). With
  `protocol="agentcore"` (the default) the host serves `/ping` and
  `/invocations` and translates to your app's route (`invoke_path`). If your
  app has its own routes or speaks another protocol (for example, an MCP
  server), `protocol="raw"` only exposes it: `deploy()` gives you the URL and
  the headers, and `invoke()` raises `UnimplementedError`; [which to
  choose](#que-protocolo-elegir). The `command` runs from the directory of the
  copied code, with that directory on its `PYTHONPATH`, and `python` is the one
  of the `requirements` venv (without it, the image one).

=== "Python"

    ```python
    from rayito import CustomRuntime, HttpApp

    command = CustomRuntime(command=["bash", "run.sh"], protocol="text")
    function = CustomRuntime(entrypoint="my_agent.main:answer")
    server = CustomRuntime(
        requirements="requirements.txt",
        app=HttpApp(
            command=["python", "-m", "agent.server"],
            port=9100,
            health_path="/health",
            invoke_path="/invoke",
            protocol="raw",
        ),
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

- [Deploy to AgentCore](desplegar-en-agentcore.md)
- [Agent in the sandbox](agente-en-el-sandbox.md)
- [Agent providers](agente-proveedores.md)
- [Agent templates](../funciones-opcionales/templates-de-agente.md)
- [Secret gateway](../funciones-opcionales/pasarela-de-secretos.md)
- [Pricing (MicroVMs, pool, agents)](../cost.md#coste-de-un-agente-desplegado)
- [Security: code agent inside the sandbox](../security.md#agente-de-codigo-dentro-del-sandbox)
