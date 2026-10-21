---
title: Deploy to AgentCore
description: Deploy the agent you already have to Amazon Bedrock AgentCore Runtime with one command or one call, with no local Docker: Rayito builds the arm64 image with CodeBuild, creates the runtime with a least-privilege role, and you invoke it with the same API as a MicroVM fleet.
---

# Deploy to AgentCore

<small>Since 0.15.0.</small>

`rayito agent deploy ./my-agent --target agentcore` deploys the same project
that [`rayito agent deploy`](despliega-tu-agente.md#sin-escribir-nada) takes to
a MicroVM fleet, but to
[Amazon Bedrock AgentCore Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agents-tools-runtime.html).
Rayito builds the `linux/arm64` image in AWS (no Docker on your machine),
pushes it to Amazon ECR, creates a least-privilege execution role, creates or
updates the runtime, waits until it is ready and gives you an object to invoke
it with the same API as [`AgentFleet`](flota-de-agentes.md).

It works with every framework the 0.14 zero-config detection already deploys:
LangGraph and LangGraph.js, LangChain and deepagents (Python and TypeScript),
Strands (Python and TypeScript) and its harness, Hermes, the Vercel AI SDK and
your own Flask or Express app, and OpenCode.

!!! info "Cost and activation"
    - **Turns it on:** `--target agentcore` in `rayito agent deploy`, or
      `target="agentcore"` in `deploy_agent()` / `deploy_agent_async()` (Python)
      and `target: "agentcore"` in `deployAgent()` (TypeScript). Without it
      nothing is created: the default target is still `microvm`, and `--dry-run`
      makes no AWS call.
    - **AWS resources and calls:** one CloudFormation stack per agent
      (`rayito-agentcore-<name>`: ECR repository, CodeBuild project with its log
      group, build role and runtime role), one CodeBuild build for each new
      version of the content, and one AgentCore runtime. The calls are listed in
      [What it does underneath](#que-hace-por-dentro).
    - **Approximate cost** (us-east-1, list prices, checked on 2026-10-19): the
      runtime charges $0.0895 per vCPU-hour and $0.00945 per GB-hour of actual
      use, per second, and only while there are sessions; the build, $0.0034 per
      minute of CodeBuild `arm1.small` (100 minutes a month free); the image in
      ECR, $0.10 per GB-month (the last 5 are kept); logs, $0.50 per GB
      ingested. The model is billed by its provider. Details in
      [Pricing](../cost.md#coste-de-un-agente-en-agentcore).
    - **IAM:** the permissions in [Before you start](#antes-de-empezar).
    - **How to turn it off:** `rayito agent delete <name> --target agentcore` (or
      `fleet.delete()`) deletes the runtime, the stack with its repository and the
      log group; with no sessions, an idle runtime charges no compute.
    - **Example:** [Deploy](#desplegar).
    - **Measured** (real-AWS acceptance, 2026-10-19, us-east-1): the build took
      38 to 121 s depending on the framework, the runtime reached `READY` about
      13 s after creation, the first invocation of a new session took 10 to 34 s
      (the agent starts in a fresh microVM) and later ones 1 to 3 s.

!!! warning "Security: no secrets gateway"
    AgentCore has no Rayito secrets gateway: the agent gets credentials from the
    runtime role, and any code running in the session can read them. Inside a
    Rayito MicroVM the agent never sees the model credential; here it can read
    whatever the role allows. That is why the role Rayito creates is least
    privilege: it can only invoke in Bedrock the project's model and the ones you
    add with `--bedrock-model`, and only read the secrets you declare with
    `--readable-secret`. Details in
    [Security on AgentCore](#seguridad-en-agentcore).

!!! tip "Managing infrastructure with AWS CDK?"
    The `RayitoAgent` construct deploys the same agent, with the same
    resources and IAM, from your CDK app with `cdk deploy`:
    [Deploy with CDK](desplegar-con-cdk.md).

## Before you start { #antes-de-empezar }

1. **A region with AgentCore Runtime and CodeBuild arm64 builds.** There are 21:
   `us-east-1`, `us-east-2`, `us-west-1`, `us-west-2`, `ca-central-1`,
   `sa-east-1`, `eu-central-1`, `eu-west-1`, `eu-west-2`, `eu-west-3`,
   `eu-south-1`, `eu-south-2`, `eu-north-1`, `ap-south-1`, `ap-south-2`,
   `ap-southeast-1`, `ap-southeast-2`, `ap-southeast-5`, `ap-southeast-7`,
   `ap-northeast-1` and `ap-northeast-2`. In any other, the deploy fails before
   calling AWS with `unsupported_region`.
2. **The artifact bucket of that region**, the one
   [`rayito setup`](../primeros-pasos/configurar-aws.md#2-prepara-la-cuenta-con-rayito-setup)
   leaves (or the `cdk bootstrap` one): `rayito agent deploy` finds it the same
   way as for MicroVMs (`--bucket`, `[deploy].bucket`, `RAYITO_BUCKET`, and
   otherwise discovery) and never creates it. CodeBuild only reads code from a
   bucket in its own region: one in another region is `bucket_region`.
3. **Model access in Bedrock** in the model's region
   ([Agent providers](agente-proveedores.md)). AgentCore only wires the
   `bedrock` provider: any other is `provider_unsupported`, because without a
   gateway its key would sit in clear text in the runtime configuration.
4. **Permissions of whoever deploys.** The credentials of whoever runs the
   command (not the agent's) need:

    | For | Permissions |
    |---|---|
    | Each agent's stack | `cloudformation:CreateStack`, `UpdateStack`, `DescribeStacks` and `DeleteStack` on `rayito-agentcore-*`, and the permissions to create, update and delete what the template contains, because CloudFormation does it with your credentials unless you use a service role: an ECR repository, a CodeBuild project, a log group and two IAM roles (the stack is deployed with `CAPABILITY_IAM`, so also `iam:PassRole` on those roles) |
    | The build context | `s3:ListBucket` on the bucket and `s3:PutObject` and `s3:DeleteObject` on `rayito/images/agentcore/*` |
    | The build | `codebuild:StartBuild` and `BatchGetBuilds` on the project, and `ecr:DescribeImages` on the repository |
    | The runtime | `bedrock-agentcore:CreateAgentRuntime`, `UpdateAgentRuntime`, `GetAgentRuntime`, `ListAgentRuntimes` and `DeleteAgentRuntime`, and `iam:PassRole` on the runtime role |
    | Invoking and stopping sessions | `bedrock-agentcore:InvokeAgentRuntime` and `StopRuntimeSession` on the runtime |
    | Deleting the runtime's logs | `logs:DeleteLogGroup` on `/aws/bedrock-agentcore/runtimes/*` |
    | Identity | `sts:GetCallerIdentity` |

    `--dry-run` and `rayito agent detect` need none of them: they make no AWS
    call. The full IAM tables are in [IAM](../operacion/iam.md).

5. **In TypeScript, the AWS clients the deploy loads.** They are optional
   peers: they are only imported when you use `--target agentcore`, and without
   them the call fails naming the missing one.

    ```bash
    pnpm add @aws-sdk/client-bedrock-agentcore @aws-sdk/client-bedrock-agentcore-control @aws-sdk/client-cloudformation @aws-sdk/client-cloudwatch-logs @aws-sdk/client-codebuild @aws-sdk/client-ecr
    ```

    With npm, the same with `npm install`. `@aws-sdk/client-s3` and
    `@aws-sdk/client-sts` already ship with `rayito`. In Python nothing else is
    needed: `boto3`, which `rayito` already installs, has every client.

!!! note "This does not use MicroVMs"
    You do not need `rayito-base-caps`, `RayitoSecretsReader` or
    `lambda-microvms:*`: nothing here launches a MicroVM.

## Deploy { #desplegar }

Look first at what it would do. `--dry-run` makes no AWS call: it validates the
project and the options, and shows the plan, the security notes, the warnings
and the cost.

=== "CLI"

    ```bash
    rayito agent deploy ./my-agent --target agentcore --dry-run
    rayito agent deploy ./my-agent --target agentcore \
      --bedrock-model us.anthropic.claude-haiku-4-5-20251001-v1:0
    ```

=== "Python"

    ```python
    from rayito import AgentCoreOptions, AgentDeployOptions, deploy_agent

    fleet = deploy_agent(
        "./my-agent",
        options=AgentDeployOptions(bucket="amzn-s3-demo-bucket"),
        target="agentcore",
        agentcore=AgentCoreOptions(
            bedrock_models=("us.anthropic.claude-haiku-4-5-20251001-v1:0",),
        ),
    )
    reply = fleet.invoke("Summarize the README.")
    print(reply.text, reply.session_id)
    ```

=== "Python (async)"

    ```python
    from rayito import AgentCoreOptions, AgentDeployOptions, deploy_agent_async


    async def main() -> None:
        fleet = await deploy_agent_async(
            "./my-agent",
            options=AgentDeployOptions(bucket="amzn-s3-demo-bucket"),
            target="agentcore",
            agentcore=AgentCoreOptions(
                bedrock_models=("us.anthropic.claude-haiku-4-5-20251001-v1:0",),
            ),
        )
        reply = await fleet.invoke("Summarize the README.")
        print(reply.text, reply.session_id)
    ```

=== "TypeScript"

    ```ts
    import { deployAgent } from "rayito";

    const fleet = await deployAgent("./my-agent", {
      options: { bucket: "amzn-s3-demo-bucket" },
      target: "agentcore",
      agentcore: {
        bedrockModels: ["us.anthropic.claude-haiku-4-5-20251001-v1:0"],
      },
    });
    const reply = await fleet.invoke("Summarize the README.");
    console.log(reply.text, reply.sessionId);
    ```

The CLI prints the plan and asks for confirmation (`--yes` skips it; with no
terminal or with `--json`, `--yes` is required, or it exits with
`needs_confirmation`). The SDK asks nothing: calling
`deploy_agent(..., target="agentcore")` is the confirmation. To see the plan
without deploying from the SDK, `plan_deploy()` in Python and `planDeploy()` in
TypeScript take the same options and do not call AWS either.

<!-- noqa: example: salida de consola, no es código -->
```text
rayito agent deploy: support-bot en Amazon Bedrock AgentCore Runtime, región us-east-1
  pila rayito-agentcore-support-bot: crear o actualizar (se decide al leer la cuenta)
  imagen rayito/agentcore/support-bot: construir con CodeBuild si no existe (sin Docker local)
  runtime rayito_support_bot: crear o actualizar
Seguridad:
  - AgentCore no tiene la pasarela de secretos de Rayito: el agente obtiene las credenciales del rol del runtime, y cualquier código que corra en la sesión puede leerlas
  - el rol del runtime puede invocar en Bedrock: us.anthropic.claude-haiku-4-5-20251001-v1:0
  - red PUBLIC: el agente tiene salida a Internet y la política de salida de Rayito no se aplica
  - cada sesión corre en su propia microVM de AgentCore, aislada de las demás; AgentCore no asocia sesiones a usuarios: eso es cosa de tu backend
...
no se creó ni cambió nada (--dry-run)
```

The CLI output is in Spanish in both languages. The example abbreviates with
`...` the description of what the stack creates, the warnings (for example, a
`[scaling]` in the project file that AgentCore ignores) and the cost lines.

With `--json`, the CLI prints a single document with `dryRun`, `target`,
`detection` and `plan`, and on deploy one with the runtime (`runtimeId`,
`runtimeArn`, `runtimeVersion`, `status`, `imageUri`, `stackName`, `logGroup`
and the `steps` that ran). An error comes out as `{"error": {"code",
"message"}, "exitCode"}` on stdout and the line `rayito: <message>` on stderr,
like `rayito setup`.

The first time it takes a few minutes (most of it is the image build); a
redeploy with no changes, seconds.

## What it does underneath { #que-hace-por-dentro }

The plan is always three steps, and each is decided at apply time, from what it
finds in your account at that moment:

| Step | What it does | Actions |
|---|---|---|
| `stack` | deploys `rayito-agentcore-<name>` with [`infra/agentcore-runtime.yaml`](https://github.com/alejandro-cedeno-10/rayito/blob/main/infra/README.md): ECR repository, CodeBuild project and its log group, build role and runtime role | `create`, `update`, `reuse`, `wait` |
| `image` | uploads the build context (the same as [`rayito agent export --target agentcore`](despliega-tu-agente.md#4-exporta-a-un-contenedor)) to the bucket, builds it with CodeBuild on native arm64, pushes it to ECR with a tag that is the hash of the content, and deletes the zip from the bucket | `build`, `reuse` |
| `runtime` | creates or updates the runtime by name and waits for `READY`; after creating it, updates it to require MMDSv2 | `create`, `update`, `reuse`, `wait` |

- **No local Docker.** The build runs in CodeBuild (`ARM_CONTAINER`,
  `BUILD_GENERAL1_SMALL`), so it does not matter whether your machine is x86 or
  Apple silicon. AgentCore's direct code deployment is not used: it only
  supports one language runtime per agent, and TypeScript agents need the Python
  host plus Node.
- **The image is the one from `export`.** What `deploy` builds is what
  `rayito agent export --target agentcore` writes; that context has an
  `AGENT_BASE_IMAGE` build argument to pull the same base, with the same digest,
  from the ECR Public mirror instead of Docker Hub.
- **Idempotent.** The image tag is the content (`src-` and 16 characters of the
  context hash), so the same project is not rebuilt; the runtime is identified by
  its name (`rayito_<name>`) and by a marker in its description
  (`rayito.agentcore/1`). A redeploy is an update of the same runtime, never a
  duplicate, and a runtime with that name that is not Rayito's gives
  `name_conflict` and is not touched.
- **MMDSv2 always on.** Since 2026-06-30 AgentCore refuses to invoke a runtime
  without MMDSv2. `CreateAgentRuntime` cannot enable it, so Rayito updates the
  runtime right after creating it and on every update.
- **Environment variables.** The project's `env` plus `AWS_REGION` and
  `AWS_DEFAULT_REGION` set to the deploy region (they win over the project's).
  Never secrets: they can be read with `GetAgentRuntime`.

## Invoke { #invocar }

`AgentCoreFleet` has the same `invoke()` and `stream()` as `AgentFleet`, with
the same result.

=== "CLI"

    ```bash
    rayito agent invoke my-agent "Summarize the README." --target agentcore
    rayito agent invoke my-agent "And the second paragraph?" --target agentcore \
      --session-id rayito-0123456789abcdef0123456789abcdef
    rayito agent invoke my-agent "Summarize the README." --target agentcore --stream
    ```

=== "Python"

    ```python
    from rayito import AgentCoreFleet

    fleet = AgentCoreFleet.connect("my-agent", region="us-east-1")
    first = fleet.invoke("My name is Ana.")
    again = fleet.invoke("What is my name?", session_id=first.session_id)
    print(again.text)

    stream = fleet.stream("Summarize the README.", session_id=first.session_id)
    for event in stream:
        print(event)
    print(stream.result().text)
    ```

=== "TypeScript"

    ```ts
    import { AgentCoreFleet } from "rayito";

    const fleet = await AgentCoreFleet.connect("my-agent", { region: "us-east-1" });
    const first = await fleet.invoke("My name is Ana.");
    const again = await fleet.invoke("What is my name?", { sessionId: first.sessionId });
    console.log(again.text);

    const stream = await fleet.stream("Summarize the README.", { sessionId: first.sessionId });
    for await (const event of stream) {
      console.log(event);
    }
    console.log((await stream.result()).text);
    ```

In the CLI, the argument that on MicroVMs is the sandbox id is here the **agent
name**. `--token-file` does not exist on AgentCore (it gives
`option_not_supported`): the invocation is authenticated with your AWS identity
(SigV4) and needs `bedrock-agentcore:InvokeAgentRuntime`.

### Sessions { #sesiones }

Session ids work as in a MicroVM fleet: `result.session_id`
(`result.sessionId` in TypeScript) is the id you pass to continue the
conversation, and two calls with the same id reach the same microVM and the same
memory. What changes is how the id is written on AgentCore, which requires
between 33 and {{agentcoreSessionIdMaxLength}} characters:

| What you pass | What happens |
|---|---|
| nothing | Rayito creates `rayito-` plus 32 random hexadecimal characters |
| an id of 33 to {{agentcoreSessionIdMaxLength}} characters (`a-z`, `A-Z`, `0-9`, `-`, `_`; starts with a letter or digit) | used as is |
| any other non-empty id (shorter, or with other characters) | becomes `rayito-` plus 32 characters of the hash of your id: the same id always reaches the same session |
| an empty string | `InvalidArgumentException` |

The id travels only in the request header, not in the body. A session is an
AgentCore microVM of its own, isolated from the others: AgentCore **does not tie
sessions to users**, so who may continue which session is up to your backend.

## Status, list, stop and delete { #ciclo-de-vida }

=== "CLI"

    ```bash
    rayito agent status my-agent --target agentcore
    rayito agent list --target agentcore
    rayito agent stop my-agent --target agentcore --session-id rayito-0123456789abcdef0123456789abcdef
    rayito agent delete my-agent --target agentcore
    ```

=== "Python"

    ```python
    from rayito import AgentCoreFleet

    fleet = AgentCoreFleet.connect("my-agent", region="us-east-1")
    status = fleet.status()
    print(status.status, status.runtime_version, status.image_uri)

    fleet.stop_session("rayito-0123456789abcdef0123456789abcdef")

    result = fleet.delete()
    print(result.runtime_deleted, result.stack_deleted, result.log_group_deleted)
    ```

=== "TypeScript"

    ```ts
    import { AgentCoreFleet } from "rayito";

    const fleet = await AgentCoreFleet.connect("my-agent", { region: "us-east-1" });
    const status = await fleet.status();
    console.log(status.status, status.runtimeVersion, status.imageUri);

    await fleet.stopSession("rayito-0123456789abcdef0123456789abcdef");

    const result = await fleet.delete();
    console.log(result.runtimeDeleted, result.stackDeleted, result.logGroupDeleted);
    ```

- **`status`** reads the runtime: state (`READY`, `UPDATING`…), version, image
  and log group. Free.
- **`list`** shows the region's Rayito agents (the runtimes with Rayito's marker
  in their description), not the ones you created some other way. Free. You must
  pass `--target agentcore`: a MicroVM fleet is not listed here.
- **`stop`** ends the microVM of **one session** (`StopRuntimeSession`) and
  needs `--session-id` (`session_required`). With no sessions AgentCore charges
  no compute, so there is nothing to «stop» on the agent as a whole: to remove
  it, `delete`.
- **`delete`** removes only what Rayito created for that name: the runtime
  (waiting for it to disappear), the `rayito-agentcore-<name>` stack (the ECR
  repository empties itself, images included) and the runtime's log group. It
  asks for confirmation (`--yes` skips it; with no terminal it is required). If
  there is nothing, it says «nothing to delete» and exits 0. Another agent's
  images, the artifact bucket and the rest of your account are not touched.

An agent deployed with CDK (`RayitoAgent`) shows up in `list` and `status` and
is invoked the same way, but `deploy` and `delete` refuse it with
`name_conflict`: CloudFormation manages it
([Coexisting with the CLI](desplegar-con-cdk.md#convivencia)).

## Redeploy { #redesplegar }

Run the same command again. Rayito computes the content that goes into the
image and decides:

| What changed | What it does |
|---|---|
| nothing | three `reuse`: the stack does not change, there is no build and the runtime gets no new version |
| the code, the dependencies or the configuration that goes into the image | a new build and an update of the same runtime (new version) |
| the environment variables, the lifecycle or `--bedrock-model` | the runtime is updated (or the stack, if what the role can do changes); the image is only rebuilt if its content changes |

There is no way to end up with two runtimes for the same agent: the identity is
the name. An interrupted deploy resumes where it stopped (it waits for the stack
or the runtime in progress and uploads the zip again if needed). With
`--no-build` it only reuses an image already built with the same content; if
there is none, it fails with `image_missing`.

## AgentCore options { #opciones }

They only apply with `--target agentcore`; without it, they give
`option_requires_agentcore` before calling AWS.

| CLI | Python (`AgentCoreOptions`) | TypeScript (`agentcore`) | What it does |
|---|---|---|---|
| `--bedrock-model ID` (repeatable) | `bedrock_models` | `bedrockModels` | Bedrock models or inference profiles the runtime role may invoke **in addition to** the project's model, which is always included (duplicates are ignored). Default: no others. No wildcards or commas: an id, or an ARN of your account and region (`iam_resource_invalid`) |
| `--readable-secret NAME` (repeatable) | `readable_secrets` | `readableSecrets` | Secrets Manager secrets the role may read. Default: none. A name without wildcards or commas, or an ARN of your account and region (`iam_resource_invalid`) |
| `--idle-session-timeout S` | `idle_session_timeout_seconds` | `idleSessionTimeoutMs` | Seconds without use after which AgentCore ends a session's microVM: {{agentcoreLifecycleMinSeconds}} to {{agentcoreLifecycleMaxSeconds}}, default {{agentcoreIdleSessionTimeoutDefaultSeconds}} |
| `--max-session-lifetime S` | `max_session_lifetime_seconds` | `maxSessionLifetimeMs` | Maximum lifetime of a session's microVM: {{agentcoreLifecycleMinSeconds}} to {{agentcoreLifecycleMaxSeconds}}, default {{agentcoreLifecycleMaxSeconds}} (8 h) |
| `--tag K=V` (repeatable) | `tags` | `tags` | Tags for the stack and the runtime, for organizations that require them ([below](#etiquetas)) |

The idle timeout cannot exceed the maximum lifetime (`lifecycle_order`). In
TypeScript, milliseconds are converted to whole seconds: a value that is not a
multiple of 1000 gives `lifecycle_invalid`.

### What happens with models and secrets { #modelos-y-secretos }

The runtime role can invoke in Bedrock only the ARNs of the project's model and
of the ones you add with `--bedrock-model`, and each one resolves like this:

- A **model id** gives the ARN of the foundation model in the model's region
  (the project's, or the deploy region if it has none).
- An **inference profile id** (`us.`, `eu.`, `apac.`, `au.`, `global.`) gives
  the ARN of the profile in the model's region and that of the foundation model
  in any region, because the profile routes among them; a `global.` profile also
  adds the region-less foundation model ARN.
- A **full ARN** is used as is.

No other model: the role cannot invoke anything outside that list. Likewise,
the role reads with
`secretsmanager:GetSecretValue` only the secrets of `--readable-secret`. Values
are never copied into the runtime configuration: the agent reads them with the
role, at run time. A secret name becomes the ARN of that secret in the deploy
region (with Secrets Manager's random suffix as a wildcard) and a full ARN is
used as is.

```bash
rayito agent deploy ./my-agent --target agentcore \
  --bedrock-model us.anthropic.claude-haiku-4-5-20251001-v1:0 \
  --readable-secret my-app/external-key
```

### Tags for organizations that require them { #etiquetas }

An SCP that requires tags denies creating resources without them. `--tag K=V`
(`tags=` / `tags:`) passes them to the stack, and CloudFormation propagates them
to the repository, the CodeBuild project, the log group and the roles; they are
also sent when the runtime is created. They are the same as `rayito setup --tag`
and follow the same rules: `KEY=VALUE`, letters, digits, spaces and
`_ . : / = + - @`, and up to 50 counting the three Rayito adds
(`rayito:component`, `rayito:managed-by`, `rayito:sdk-version`).

```bash
rayito agent deploy ./my-agent --target agentcore --tag Owner=platform --tag Project=agents
```

The tags of a runtime that already exists are not updated on a redeploy.

### Secrets as environment variables { #secretos-como-entorno }

If your code reads from environment variables values that live in Secrets
Manager (a database URL, the key of an LLM gateway), `--secret-env` and
`--secret-env-from` (or `[secret_env]` and `[[secret_env_from]]` in the file)
set them when each session starts, read with the runtime role:

```bash
rayito agent deploy ./my-agent --target agentcore \
  --secret-env DATABASE_URL=app/db-url \
  --secret-env-from 'app/config,prefix=APP_,keys=DB_HOST|DB_USER'
```

The runtime only receives `RAYITO_SECRET_ENV`, with names and ARNs; the secrets
join those of `--readable-secret`, so the role can read exactly those.
`--check-env` warns about the `.env.example` variables left without a value and
`--create-secrets-from` creates the secret from a `.env`. All the detail is in
[Secrets as environment variables](secretos-como-entorno.md).

## Which MicroVM options do not apply { #opciones-que-no-aplican }

AgentCore is not a MicroVM fleet, and Rayito never silently approximates one
option with another. Every option of the call (an SDK argument or a CLI flag)
that makes no sense here is an `option_not_supported` error **before calling
AWS**; the same value coming from the project file (which is shared between
targets) is a warning in the plan and is ignored.

| MicroVM option | On AgentCore | Why |
|---|---|---|
| `min_vms`, `max_vms`, `warm_vms` (`--min-vms`, `--max-vms`, `--warm`) | error; in the file, warning | AgentCore creates one microVM per session and scales on its own; the cap is the account's active-session quota |
| `scale_in_after_seconds` | error; in the file, warning | use `--idle-session-timeout` |
| `memory_mib` (`--memory`) | error; in the file, warning | each session uses up to 2 vCPU and 8 GB and is billed by actual use |
| `[serve].port` (`--port`) | error; in the file, warning | the container serves on 8080 |
| `[serve].timeout` (`--timeout`) | error; in the file, warning | use `--max-session-lifetime` |
| `[serve].idle` (`--idle`) | error; in the file, warning | use `--idle-session-timeout` |
| `secret_names`, `create_secret_from_env` (`--secret`, `--create-secret`) | error | there is no secrets gateway; to let the agent read a secret, `--readable-secret` |
| `access_token` (`--token-file`) | error | AgentCore authenticates with IAM (SigV4), not an access token |
| `keep_on_failure` (`--keep-on-failure`) | error | AgentCore always keeps what was created if it fails; delete it with `rayito agent delete` |
| `base_image_version`, `ensure_base_image`, `base_publisher` (`--base-artifact`) | error; in the file, warning | AgentCore does not use the MicroVM base image |
| `session_index`, `pool_backend`, `warm_timeout_seconds` | error | AgentCore routes sessions by their id and has no warm slots |
| `egress`, `allow_out` (`--allow-out`) and the project's `network` | error `network_policy_unsupported` | in PUBLIC mode AgentCore does not filter egress: applying it halfway would be a silent risk |
| a model provider other than `bedrock` | error `provider_unsupported` | without a gateway, its key would sit in clear text in the runtime configuration |
| an app with `protocol = "raw"` | error `raw_protocol` | it does not meet the AgentCore contract (`/ping` and `/invocations`) |

These apply unchanged: `max_concurrency` (`--max-sessions`), `queue_max`
(`--queue`), `queue_timeout_seconds` (`--queue-timeout`), `bucket`,
`build_image`, `build_timeout_seconds`, `env` and `on_step`. `sessions` applies
as `dedicated`: on AgentCore each session always has its own microVM.

## Security on AgentCore { #seguridad-en-agentcore }

The difference from a Rayito MicroVM is a single one, and it is worth keeping in
mind:

- **Rayito MicroVM:** the agent calls the model through the
  [secrets gateway](../funciones-opcionales/pasarela-de-secretos.md) and can never
  read the credential.
- **AgentCore:** there is no gateway. The agent gets credentials from the
  runtime role through the microVM metadata service, and any code running in the
  session (yours, the framework's, a tool the model decides to run) can read
  them. The compensation is least privilege.

The role Rayito creates for the runtime can:

- pull **its** image from ECR and write to **its** log group
  (`/aws/bedrock-agentcore/runtimes/<runtime>-*`);
- send traces to X-Ray and metrics to the `bedrock-agentcore` namespace;
- get the runtime's workload identity tokens (`GetWorkloadAccessToken` and
  `GetWorkloadAccessTokenForJWT`), **without** `GetWorkloadAccessTokenForUserId`,
  as the AgentCore security guide recommends;
- invoke in Bedrock **only** the project's model and those of `--bedrock-model`;
- read **only** the secrets of `--readable-secret`.

It can do nothing else: no other models, no other secrets, no other
repositories, no other services. MMDSv2 is always on in the runtime. The network
is `PUBLIC` (the agent reaches the Internet unfiltered: Rayito's egress policy
does not apply), which is why a project with `egress` or `network` is not
deployed. More in [Security](../security.md#agentcore).

The Vercel AI SDK is a special case: its Bedrock client cannot read the role's
credentials by itself. For that framework, the build context includes a small
helper that the host runs before each invocation to read the role's credentials
and set them as environment variables of the Node process only for that
invocation. The plan says so under `Seguridad`.

OpenCode does not find them by itself either: it only starts its Bedrock
provider when an AWS profile or variable is present, and on AgentCore the role
is served only by the microVM metadata service. For OpenCode, the build context
includes the same helper and an AWS profile with `credential_process` that runs
it (`AWS_CONFIG_FILE` and `AWS_PROFILE` in the image environment): OpenCode gets
credentials with an expiry time and refreshes them on its own.

With `secret_env` or `secret_env_from`, the host reads those secrets when the
session starts and sets them in the agent's environment before loading its
code. The agent and everything it starts can read its environment, just as any
code in the session can read those secrets with the role: the plan says so
under `Seguridad` (`secretEnv`). More in
[Secrets as environment variables](secretos-como-entorno.md#seguridad).

## Errors { #errores }

The deploy's own failures are `AgentCoreException` in Python and
`AgentCoreError` in TypeScript, with a `code` from a closed list and an exit
code in the CLI: `2` for usage and environment (region, options, name, bucket,
confirmation…) and `1` for what fails in AWS (stack, build, runtime,
permissions). Messages are in Spanish and never carry ARNs, URLs or AWS's raw
text. The full list is in [Errors](../referencia/errores.md#agentcore).
Invocation errors are the usual ones: `429` is `RateLimitException`, an exceeded
session quota is `QuotaExceededException`, and a failure of the agent itself (the
container answered 4xx or 5xx) is `AgentException(reason="runtime_error")`.

## See also { #ver-tambien }

- [Deploy with CDK](desplegar-con-cdk.md): the same agent with the
  `RayitoAgent` construct in your CDK app.
- [Deploy your agent](despliega-tu-agente.md): detection, the project file and
  the MicroVM deploy.
- [Several sessions and several VMs](flota-de-agentes.md): MicroVM fleets.
- [CLI: `rayito agent`](../cli.md#rayito-agent)
- [Pricing: an agent on AgentCore](../cost.md#coste-de-un-agente-en-agentcore)
- [Security: AgentCore](../security.md#agentcore)
- [Optional features and their cost](../optional-features.md)
