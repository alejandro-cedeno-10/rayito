---
title: Rayito
description: "Hardware-isolated sandboxes for AI agents on AWS Lambda MicroVMs, in your own AWS account. E2B-compatible API, no server and no API key."
seo_title: "Rayito: AI agent sandboxes in your own AWS account"
template: home.html
hide:
  - navigation
  - toc
---

<div class="rx-hero" markdown>
<div class="rx-hero__copy" markdown>

[New in 0.13: the CLI, the MCP server and the local dashboard in TypeScript too](novedades/0.13.0.md){ .rx-pill }

# Sandboxes for AI agents, in your own AWS account { .rx-hero__title }

Every sandbox is a hardware-isolated AWS Lambda MicroVM. The SDK calls the
AWS API directly with your credentials: no Rayito server or API key, and
the same API as E2B.
{ .rx-hero__lead }

<div class="rx-hero__actions" markdown>
[Get started](primeros-pasos/index.md){ .md-button .md-button--primary }
[:fontawesome-brands-github: View on GitHub](https://github.com/alejandro-cedeno-10/rayito){ .md-button }
</div>

Coming from E2B? [Change one import](migrar-desde-e2b/index.md). Free and Apache-2.0: you only pay AWS.
{ .rx-hero__note }

</div>
<div class="rx-hero__code" markdown>

<div class="rx-window" aria-hidden="true"><span></span><span></span><span></span></div>

=== "Python"

    ```bash
    pip install rayito
    ```

    ```python
    from rayito import Sandbox

    with Sandbox.create() as sbx:
        print(sbx.commands.run("echo hello").stdout)  # "hello\n"
        print(sbx.run_code("x = 40; x + 2").text)  # "42"
    ```

=== "TypeScript"

    ```bash
    pnpm add rayito
    ```

    ```ts
    import { Sandbox } from "rayito";

    await using sbx = await Sandbox.create();
    console.log((await sbx.commands.run("echo hello")).stdout); // "hello\n"
    console.log((await sbx.runCode("x = 40; x + 2")).text); // "42"
    ```

<p class="rx-hero__foot" markdown>Needs the `rayito-base` image in your account: [Set up AWS](primeros-pasos/configurar-aws.md), once.</p>

</div>
</div>

<div class="rx-ruler">
<div class="rx-ruler__figure" role="img" aria-label="Measured times: resume 0.38 s; pool.take 0.77 s; ready for commands 2.3 s; kernel ready 5.2 s">
<div class="rx-ruler__track">
<span class="rx-ruler__tick" style="--s:0"><span>0 s</span></span>
<span class="rx-ruler__tick" style="--s:1"><span>1 s</span></span>
<span class="rx-ruler__tick" style="--s:2"><span>2 s</span></span>
<span class="rx-ruler__tick" style="--s:3"><span>3 s</span></span>
<span class="rx-ruler__tick" style="--s:4"><span>4 s</span></span>
<span class="rx-ruler__tick" style="--s:5"><span>5 s</span></span>
<span class="rx-ruler__tick" style="--s:6"><span>6 s</span></span>
<span class="rx-ruler__mark rx-ruler__mark--up" style="--s:0.38"><span class="rx-ruler__value">0.38 s</span><span class="rx-ruler__label"><code>resume()</code></span></span>
<span class="rx-ruler__mark rx-ruler__mark--down" style="--s:0.77"><span class="rx-ruler__value">0.77 s</span><span class="rx-ruler__label"><code>pool.take()</code></span></span>
<span class="rx-ruler__mark rx-ruler__mark--up" style="--s:2.3"><span class="rx-ruler__value">2.3 s</span><span class="rx-ruler__label">ready for commands</span></span>
<span class="rx-ruler__mark rx-ruler__mark--down rx-ruler__mark--end" style="--s:5.2"><span class="rx-ruler__value">5.2 s</span><span class="rx-ruler__label">kernel ready</span></span>
</div>
</div>
<p class="rx-ruler__note">Medians measured on real AWS (us-east-1, 2026-09). Details in <a href="cost/#tiempos">Timings</a>.</p>
</div>

## Why Rayito { .rx-sr-only #por-que-rayito }

<div class="rx-hooks" markdown>

<div class="rx-hook" markdown>

### A sandbox in seconds { #un-sandbox-en-segundos }

`Sandbox.create()` leaves the sandbox ready for commands in about 2.3 s and
the Python kernel in about 5.2 s. From a pool of suspended sandboxes, in
under a second.

[Pool](pool.md) · [Measured timings](cost.md#tiempos)

</div>
<div class="rx-hook" markdown>

### A coding agent inside { #un-agente-de-codigo-dentro }

`sbx.agent.run()` runs OpenCode or deepagents in the sandbox itself. The
model (Bedrock, Anthropic, OpenAI and [more providers](guias/agente-proveedores.md)) is
called through the secret gateway: the agent uses the credential but cannot
read it.

[Agent in the sandbox](guias/agente-en-el-sandbox.md)

</div>
<div class="rx-hook" markdown>

### From E2B in one import { #de-e2b-en-un-import }

```diff
- from e2b_code_interpreter import Sandbox
+ from rayito.e2b import Sandbox
```

What Lambda MicroVMs cannot do raises `UnimplementedError`: nothing is
approximated silently.

[Migrate from E2B](migrar-desde-e2b/index.md) · [Parity, row by row](e2b-parity.md)

</div>

</div>

<div class="rx-section" markdown>

## How it works { #como-funciona }

Your code asks for a sandbox; AWS Lambda starts it in your account and
hands back the result.
{ .rx-section__lead }

<figure class="rx-art" data-rx-art>
<div class="rx-art__stage" role="img" aria-label="Rayito illustration: an agent sends code to three isolated sandboxes on AWS Lambda MicroVMs; two cats run the machine and the results leave on a conveyor belt.">
<img class="rx-art__plate" src="../assets/rayito/art/plate-1672.webp" srcset="../assets/rayito/art/plate-960.webp 960w, ../assets/rayito/art/plate-1672.webp 1672w" sizes="(min-width: 76.25em) 1220px, 100vw" width="1672" height="941" loading="lazy" decoding="async" alt="Rayito illustration: an agent sends code to three isolated sandboxes on AWS Lambda MicroVMs; two cats run the machine and the results leave on a conveyor belt.">
<img class="rx-art__l rx-art__gear rx-art__gear--1" src="../assets/rayito/art/gear-1.webp" width="69" height="70" loading="lazy" decoding="async" alt="" aria-hidden="true">
<img class="rx-art__l rx-art__gear rx-art__gear--2" src="../assets/rayito/art/gear-2.webp" width="71" height="72" loading="lazy" decoding="async" alt="" aria-hidden="true">
<img class="rx-art__l rx-art__gear rx-art__gear--3" src="../assets/rayito/art/gear-3.webp" width="71" height="72" loading="lazy" decoding="async" alt="" aria-hidden="true">
<span class="rx-art__l rx-art__male" aria-hidden="true">
<img class="rx-art__tail" src="../assets/rayito/art/male-tail.webp" width="300" height="270" loading="lazy" decoding="async" alt="" aria-hidden="true">
<img src="../assets/rayito/art/male-body.webp" width="300" height="270" loading="lazy" decoding="async" alt="" aria-hidden="true">
</span>
<span class="rx-art__l rx-art__female" aria-hidden="true">
<img class="rx-art__tail" src="../assets/rayito/art/female-tail.webp" width="205" height="235" loading="lazy" decoding="async" alt="" aria-hidden="true">
<img src="../assets/rayito/art/female-body.webp" width="205" height="235" loading="lazy" decoding="async" alt="" aria-hidden="true">
</span>
<img class="rx-art__l rx-art__rail" src="../assets/rayito/art/rail-front.webp" width="165" height="96" loading="lazy" decoding="async" alt="" aria-hidden="true">
<span class="rx-art__l rx-art__cube" aria-hidden="true"><span class="rx-art__cube-bob">
<img class="rx-art__wing rx-art__wing--l" src="../assets/rayito/art/cube-wing-l.webp" width="203" height="122" loading="lazy" decoding="async" alt="" aria-hidden="true">
<img class="rx-art__wing rx-art__wing--r" src="../assets/rayito/art/cube-wing-r.webp" width="203" height="122" loading="lazy" decoding="async" alt="" aria-hidden="true">
<img src="../assets/rayito/art/cube-body.webp" width="203" height="122" loading="lazy" decoding="async" alt="" aria-hidden="true">
</span></span>
<img class="rx-art__l rx-art__belt" src="../assets/rayito/art/packet.webp" style="--i:0" width="90" height="86" loading="lazy" decoding="async" alt="" aria-hidden="true">
<img class="rx-art__l rx-art__belt" src="../assets/rayito/art/packet.webp" style="--i:1" width="90" height="86" loading="lazy" decoding="async" alt="" aria-hidden="true">
<img class="rx-art__l rx-art__belt" src="../assets/rayito/art/packet.webp" style="--i:2" width="90" height="86" loading="lazy" decoding="async" alt="" aria-hidden="true">
<img class="rx-art__l rx-art__arrow rx-art__arrow--0" src="../assets/rayito/art/packet.webp" style="--i:0" width="90" height="86" loading="lazy" decoding="async" alt="" aria-hidden="true">
<img class="rx-art__l rx-art__arrow rx-art__arrow--1" src="../assets/rayito/art/packet.webp" style="--i:1" width="90" height="86" loading="lazy" decoding="async" alt="" aria-hidden="true">
<span class="rx-art__l rx-art__glow rx-art__glow--0" style="--i:0" aria-hidden="true"></span>
<span class="rx-art__l rx-art__glow rx-art__glow--1" style="--i:1" aria-hidden="true"></span>
<span class="rx-art__l rx-art__glow rx-art__glow--2" style="--i:2" aria-hidden="true"></span>
<span class="rx-art__l rx-art__dot rx-art__dot--0" style="--n:0" aria-hidden="true"></span>
<span class="rx-art__l rx-art__dot rx-art__dot--1" style="--n:1" aria-hidden="true"></span>
<span class="rx-art__l rx-art__dot rx-art__dot--2" style="--n:2" aria-hidden="true"></span>
<span class="rx-art__l rx-art__dot rx-art__dot--3" style="--n:1" aria-hidden="true"></span>
<span class="rx-art__l rx-art__dot rx-art__dot--4" style="--n:2" aria-hidden="true"></span>
<span class="rx-art__l rx-art__dot rx-art__dot--5" style="--n:3" aria-hidden="true"></span>
<span class="rx-art__l rx-art__dot rx-art__dot--6" style="--n:2" aria-hidden="true"></span>
<span class="rx-art__l rx-art__dot rx-art__dot--7" style="--n:3" aria-hidden="true"></span>
<span class="rx-art__l rx-art__dot rx-art__dot--8" style="--n:4" aria-hidden="true"></span>
<span class="rx-art__l rx-art__signal" aria-hidden="true"></span>
</div>
<figcaption><ol><li>Your agent or your code calls the SDK with your AWS credentials.</li><li>Every sandbox starts in its own hardware-isolated Lambda MicroVM.</li><li>Outputs, files and artifacts come back to your code; nothing goes through a Rayito server.</li></ol></figcaption>
<button type="button" class="rx-art__toggle" data-rx-art-toggle aria-label="Pause animation" aria-pressed="false" hidden><span class="rx-art__icon" aria-hidden="true"></span></button>
</figure>

<div class="rx-actions" markdown>
[Try it locally, no AWS account](guias/probar-en-local.md){ .md-button }
</div>

</div>

<div class="rx-section" markdown>

## Start in three steps { #empieza-en-tres-pasos }

<div class="rx-steps" markdown>
<div class="rx-step" markdown>

### Install the SDK { #instala-el-sdk }

Python ≥ 3.11 or Node ≥ 20. The CLI (`rayito doctor`) is installed with pip.

=== "Python"

    ```bash
    pip install "rayito[cli]"
    ```

=== "TypeScript"

    ```bash
    pnpm add rayito
    npx rayito doctor          # the CLI also ships in the npm package
    ```

[Installation](primeros-pasos/instalacion.md)

</div>
<div class="rx-step" markdown>

### Prepare your account, once { #prepara-tu-cuenta-una-vez }

About 15 minutes, most of them waiting for AWS to build the image: an IAM
stack, a bucket and the `rayito-base` image. Then the diagnosis tells you
what is missing.

```bash
export AWS_PROFILE=<your-profile>
export AWS_REGION=us-east-1
rayito doctor --template rayito-base
```

[Set up AWS](primeros-pasos/configurar-aws.md)

</div>
<div class="rx-step" markdown>

### Launch your first sandbox { #lanza-tu-primer-sandbox }

Save the code from the top in `first.py` (or `first.ts`) and run it.

=== "Python"

    ```bash
    python first.py
    ```

=== "TypeScript"

    ```bash
    npx tsx first.ts
    ```

[First sandbox](quickstart.md)

</div>
</div>

<div class="rx-actions rx-actions--steps" markdown>
[Launch your first sandbox](quickstart.md){ .md-button .md-button--primary }
</div>

</div>

<div class="rx-section rx-split" markdown>
<div class="rx-split__copy" markdown>

<p class="rx-eyebrow">Agents</p>

## A coding agent inside the sandbox { #un-agente-de-codigo-dentro-del-sandbox }

A single `Sandbox.create()` on the `rayito-agent` image is enough. With
`allow_internet_access=False` the sandbox only reaches the model, through the
secret gateway. The agent is [OpenCode](https://github.com/anomalyco/opencode)
or deepagents, and the provider is whichever you prefer (see
[Agent providers](guias/agente-proveedores.md)).

[Agent templates](funciones-opcionales/templates-de-agente.md) ·
[Fast start? Which one to use](guias/agente-en-el-sandbox.md#que-uso) ·
[VM versus tokens](cost.md#coste-de-un-agente-vm-frente-a-modelo)

<div class="rx-actions" markdown>
[Read the agent guide](guias/agente-en-el-sandbox.md){ .md-button .md-button--primary }
[Choose a model provider](guias/agente-proveedores.md)
</div>

</div>
<div class="rx-split__code" markdown>

=== "Python"

    ```python
    from rayito import AgentModel, AgentSpec, Sandbox, SecretStore, bedrock_gateway

    MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    SecretStore().create("bedrock-key", "Bearer <short-lived Bedrock key>")
    spec = AgentSpec(model=AgentModel(provider="bedrock", id=MODEL_ID, gateway="bedrock", region="us-east-1"))

    with Sandbox.create(
        template="rayito-agent",
        allow_internet_access=False,
        gateways={"bedrock": bedrock_gateway("bedrock-key", region="us-east-1", models=[MODEL_ID])},
    ) as sbx:
        result = sbx.agent.run("List the files in /home/user and summarize what is there.", spec=spec)
        print(result.text, result.usage.total)
    ```

=== "TypeScript"

    ```ts
    import { AgentModel, AgentSpec, Sandbox, SecretStore, bedrockGateway } from "rayito";

    const MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0";
    await new SecretStore().create("bedrock-key", "Bearer <short-lived Bedrock key>");
    const spec = new AgentSpec({
      model: new AgentModel({ provider: "bedrock", id: MODEL_ID, gateway: "bedrock", region: "us-east-1" }),
    });

    await using sbx = await Sandbox.create({
      template: "rayito-agent",
      allowInternetAccess: false,
      gateways: { bedrock: bedrockGateway("bedrock-key", { region: "us-east-1", models: [MODEL_ID] }) },
    });
    const result = await sbx.agent.run("List the files in /home/user and summarize what is there.", { spec });
    console.log(result.text, result.usage.total);
    ```

</div>
</div>

<div class="rx-section rx-split rx-split--stack" markdown>
<div class="rx-split__copy" markdown>

## A dashboard to see and operate your sandboxes { #un-panel-para-ver-y-operar-tus-sandboxes }

`rayito dashboard` opens in your browser a local dashboard with your
account's sandboxes, quotas, cost and images. It is read-only unless you
pass `--allow-actions`, and there is no third-party server.
[Step-by-step tour](funciones-opcionales/panel-de-gestion.md#recorrido) ·
[Deploy it in your account](funciones-opcionales/panel-en-tu-cuenta.md#recorrido)

![Overview of the Rayito management dashboard](assets/panel/panel-overview.png){ loading=lazy width=1280 height=760 }

</div>
</div>

<div class="rx-section" markdown>

## What is included { #lo-que-viene-incluido }

<div class="rx-features" markdown>

<div class="rx-feature" markdown>
:material-pause-circle-outline:{ .lg .middle } [**Pause and resume**](guias/pausar-reanudar.md)

Freeze the sandbox with its state and come back in 0.38 s.
</div>
<div class="rx-feature" markdown>
:material-layers-outline:{ .lg .middle } [**Pool**](pool.md)

Suspended sandboxes ready in under a second.
</div>
<div class="rx-feature" markdown>
:material-database-outline:{ .lg .middle } [**Persistence**](persistence.md)

`HOME` on S3, beyond the 8 h.
</div>
<div class="rx-feature" markdown>
:material-shield-off-outline:{ .lg .middle } [**Outbound network**](network.md)

Cut or filter outbound internet access.
</div>
<div class="rx-feature" markdown>
:material-server-network:{ .lg .middle } [**MCP server**](mcp.md)

A sandbox for your editor or assistant over MCP.
</div>
<div class="rx-feature" markdown>
:material-link-variant:{ .lg .middle } [**LangChain y Vercel AI**](guias/langchain-y-vercel-ai.md)

Rayito as a tool for your agent.
</div>
<div class="rx-feature" markdown>
:material-cube-outline:{ .lg .middle } [**Templates**](funciones-opcionales/templates.md)

Your own images with E2B's `Template` DSL.
</div>
<div class="rx-feature" markdown>
:material-chart-line:{ .lg .middle } [**OTLP export**](funciones-opcionales/exportacion-otlp.md)

Sandbox metrics over OTLP to CloudWatch.
</div>

</div>

</div>

<div class="rx-section rx-split" markdown>
<div class="rx-split__copy" markdown>

## Migrate from E2B in minutes { #migra-desde-e2b-en-minutos }

`rayito.e2b` and `rayito/e2b` accept code written for E2B 2.x. Change the
import, prepare your account and run.

- Same classes and methods: `Sandbox`, `commands`, `files`, `run_code`.
- What does not exist in Lambda MicroVMs raises `UnimplementedError` with a
  link to the explanation.
- The [parity table](e2b-parity.md) says it row by row.

<div class="rx-actions" markdown>
[Migrate from E2B](migrar-desde-e2b/index.md){ .md-button }
[Parity, row by row](e2b-parity.md)
</div>

</div>
<div class="rx-split__code" markdown>

=== "Python"

    ```diff
    - from e2b_code_interpreter import Sandbox
    + from rayito.e2b import Sandbox
    ```

=== "TypeScript"

    ```diff
    - import { Sandbox } from "@e2b/code-interpreter";
    + import { Sandbox } from "rayito/e2b";
    ```

</div>
</div>

<div class="rx-band" markdown>

## Your account, your bill { #tu-cuenta-tu-factura }

Sandboxes run in your AWS account and are paid on your AWS bill. There is
no Rayito server, subscription or API key. Features with a cost of their own
are off until you pass their option, and `rayito stack deploy` shows you the
cost before asking for confirmation.
{ .rx-section__lead }

<div class="rx-band__figures" markdown>
<p class="rx-figure"><strong>≈ $0.126/h</strong><span>a 2 GB sandbox while it runs</span></p>
<p class="rx-figure"><strong>≈ $0.022</strong><span>ten minutes, including the launch</span></p>
<p class="rx-figure"><strong>≈ $0.60/month</strong><span>one suspended pool slot</span></p>
<p class="rx-figure"><strong>$0</strong><span>what Rayito charges</span></p>
</div>

us-east-1, list prices queried on 2026-10-06.
[Pricing](cost.md) · [Optional features and their cost](optional-features.md)

</div>

<div class="rx-section" markdown>

## Explore the manual { #explora-el-manual }

<div class="grid cards rx-cards" markdown>

-   :material-rocket-launch:{ .lg .middle } **[Getting started](primeros-pasos/index.md)**

    ---

    Install the SDK, set up your AWS account and launch your first sandbox.

-   :material-book-open-variant:{ .lg .middle } **[Guides](guias/index.md)**

    ---

    One page per feature: commands, code, files, network, pause, pool and
    persistence.

-   :material-robot-outline:{ .lg .middle } **[Agents](guias/agente-en-el-sandbox.md)**

    ---

    The agent inside the sandbox, the [MCP server](mcp.md) and the
    LangChain and Vercel AI integrations.

-   :material-toggle-switch-outline:{ .lg .middle } **[Optional features](optional-features.md)**

    ---

    What each one enables, what it costs and how to turn it off.

-   :material-swap-horizontal:{ .lg .middle } **[Migrate from E2B](migrar-desde-e2b/index.md)**

    ---

    What changes, what does not and what does not exist in Lambda MicroVMs.

-   :material-api:{ .lg .middle } **[Reference](referencia/index.md)**

    ---

    Python and TypeScript API, CLI, errors, environment variables and limits.

-   :material-server-security:{ .lg .middle } **[Operations](operacion/index.md)**

    ---

    Images, IAM, security, pricing and troubleshooting.

-   :material-new-box:{ .lg .middle } **[What's new](novedades/index.md)**

    ---

    What each version brings and how to upgrade.

</div>

</div>

<div class="rx-incluye" markdown>

??? info "What is included: every surface"

    | Surface | What it does | Guide |
    |---|---|---|
    | `Sandbox.create / connect / kill / list / get_info` | MicroVM lifecycle, metadata and deadlines | [Lifecycle](guias/ciclo-de-vida.md) |
    | `pause()` / `resume()`, `IdlePolicy` | suspend and resume with all the state alive | [Pause and resume](guias/pausar-reanudar.md) |
    | `timeout`, `max_lifetime`, `on_timeout`, `set_timeout()` | a deadline that `rayd` enforces even if your process dies | [Server-side deadline](lifecycle.md) |
    | `sbx.commands` | foreground and background processes, `stdin`, `kill`, reconnection | [Commands](guias/comandos.md) |
    | `sbx.run_code` | stateful Jupyter kernel, results, logs, errors and plots | [Run code](guias/ejecutar-codigo.md) |
    | `run_code(language="bash" / "javascript" / "typescript")` | bash and Deno kernels in `rayito-base-poly` | [Languages and kernels](kernels.md) |
    | `sbx.pty` | real terminals with `resize` and reconnection | [Terminal (PTY)](guias/terminal-pty.md) |
    | `sbx.files` | read, write, list and watch; large files and S3 presigned URLs | [Files and S3](files.md) |
    | `sbx.get_host(port)` | HTTP access to a sandbox port | [Ports and host](guias/puertos-y-host.md) |
    | `network=`, `allow_internet_access=False` | E2B's outbound internet policy on `rayito-base-caps` | [Outbound network](network.md) |
    | `SandboxPool` | sandboxes in under a second from a pool of suspended ones | [Pool](pool.md) |
    | `persist=`, `reincarnate()` | `HOME` on S3, beyond the 8 h | [Persistence](persistence.md) |
    | `get_metrics_history()`, `Sandbox.paginate()` | metrics history and resumable listing | [Metrics and listing](observability.md) |
    | `sbx.git` | E2B's git API | [Git](git.md) |
    | `sbx.agent` | a coding agent (OpenCode or deepagents) inside the sandbox, with the model behind the secret gateway | [Agent in the sandbox](guias/agente-en-el-sandbox.md) |
    | `rayito.e2b`, `rayito/e2b` | the E2B 2.x SDK by changing only the import | [Migrate from E2B](migrar-desde-e2b/index.md) |
    | `rayito-mcp` | a sandbox for Claude Code, Cursor or VS Code over MCP | [MCP server](mcp.md) |
    | LangChain, Vercel AI SDK | Rayito as a tool for an agent that runs outside the sandbox | [LangChain and Vercel AI](guias/langchain-y-vercel-ai.md) |
    | `rayito` (CLI) | publish the image, diagnose the account, operate sandboxes and stacks | [CLI](cli.md) |
    | `make local-up`, `make local-e2e` | test the SDKs against a real `rayd` and an emulated AWS, without an account | [Test locally](guias/probar-en-local.md) |

    Optional features, all off by default until you pass their option (or,
    for the CLI-only ones, run their command):

    | Surface | What it does | Guide |
    |---|---|---|
    | `secrets=`, `SecretStore` | AWS Secrets Manager secrets as environment variables | [Secrets](secrets.md) |
    | `gateways=`, `SecretGateway` | use a secret from the sandbox without being able to read it | [Secret gateway](funciones-opcionales/pasarela-de-secretos.md) |
    | `mounts=`, `S3Mount` | an S3 bucket as a sandbox folder | [S3 mounts](funciones-opcionales/montajes-s3.md) |
    | `size=` | sandboxes from 512 MiB to 8 GiB from per-size images | [Sizes](funciones-opcionales/tamanos.md) |
    | `events=`, `LifecycleEvents` | lifecycle events and webhooks with E2B's signature | [Events and webhooks](funciones-opcionales/eventos-y-webhooks.md) |
    | `telemetry=`, `TelemetryExport` | sandbox metrics over OTLP to CloudWatch | [OTLP export](funciones-opcionales/exportacion-otlp.md) |
    | `tracer_provider=` | SDK-side OpenTelemetry spans | [OpenTelemetry](funciones-opcionales/opentelemetry.md) |
    | `index=DynamoDbIndex(...)` | filter paused sandboxes by metadata too | [Metadata index](funciones-opcionales/indice-de-metadatos.md) |
    | `Template.build()` | images from E2B's `Template` DSL | [Templates](funciones-opcionales/templates.md) |
    | `AgentTemplate`, `rayito agent template build` | the `rayito-agent` image with OpenCode, ripgrep and deepagents | [Agent templates](funciones-opcionales/templates-de-agente.md) |
    | `PoolConfig(warmup=agent_pool_warmup(...))` | pool slots with the agent already warmed (fast start, optional) | [Pool](pool.md#calentamiento-warmup) |
    | `OptionalStacks`, `rayito stack` | deploy, query and delete each feature's infrastructure | [Optional stacks](funciones-opcionales/pilas-opcionales.md) |
    | `rayito sandbox proxy` | a sandbox port at `http://127.0.0.1` on your machine | [Local proxy](funciones-opcionales/proxy-local.md) |
    | `volumes=`, `VolumeStore`, `EfsVolumes` (experimental) | an EFS file system shared live between sandboxes, in your VPC | [EFS volumes](funciones-opcionales/volumenes-efs.md) |
    | `domain=`, `CustomDomain`, `rayito domain` (experimental) | an HTTPS URL under your domain for a sandbox port | [Custom domain](funciones-opcionales/dominio-propio.md) |

    Everything works the same in Python (sync and `asyncio`) and in
    TypeScript, with `snake_case` and seconds in Python and `camelCase` and
    milliseconds in TypeScript. The [cost](cost.md) and [limits](limits.md)
    numbers are measured against real AWS, never against a simulator.

</div>

<div class="rx-cta" markdown>

## Your first sandbox, in three steps { #tu-primer-sandbox-en-tres-pasos }

[Get started](primeros-pasos/index.md){ .md-button .md-button--primary }
[:fontawesome-brands-github: Star on GitHub](https://github.com/alejandro-cedeno-10/rayito){ .md-button }

<p class="rx-cta__note">Apache-2.0 · no Rayito server · your account, your bill</p>

</div>
