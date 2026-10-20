---
title: Deploy with CDK
description: Two AWS CDK constructs, RayitoAgent and RayitoMicrovmImage, to declare in your own CDK app an agent on Amazon Bedrock AgentCore Runtime and a Lambda MicroVMs image with rayd, with the same resources and IAM as the CLI, in Python and TypeScript.
---

# Deploy with CDK

<small>Since 0.15.0 (not published yet).</small>

If your team already manages infrastructure with
[AWS CDK](https://docs.aws.amazon.com/cdk/v2/guide/home.html), Rayito ships two
constructs to declare its pieces in the same app and deploy them with
`cdk deploy`:

| Construct | What it deploys | Same as |
|---|---|---|
| `RayitoAgent` | an agent on Amazon Bedrock AgentCore Runtime: the arm64 image (built in CodeBuild, no local Docker), the ECR repository, the roles and the runtime | `rayito agent deploy --target agentcore` ([Deploy to AgentCore](desplegar-en-agentcore.md)) |
| `RayitoMicrovmImage` | a Lambda MicroVMs image with `rayd` (and one more image per size), with its build role and log groups | `rayito image publish` ([Images](../images.md)) |

Both live in the same packages as the SDK: `rayito.cdk` in Python (extra
`cdk`) and `rayito/cdk` in TypeScript. They use the same planning code as the
CLI, so a project means the same thing on both paths, and `RayitoAgent`
creates the repository, the CodeBuild project and the roles with the same
template as `rayito agent deploy`: **the IAM is identical**. `cdk synth` makes
no AWS call.

Running MicroVMs are not CloudFormation resources: with `RayitoMicrovmImage`
the image lives in your stack and sandboxes are still created with the SDK
(`Sandbox.create(template=...)`).

!!! info "Cost and activation"
    - **Turns it on:** instantiating `RayitoAgent` or `RayitoMicrovmImage` in a
      CDK app and running `cdk deploy`. Installing `rayito[cdk]` or importing
      `rayito/cdk` creates nothing, and `cdk synth` makes no AWS call. Without the
      extra (Python) or without the `aws-cdk-lib` and `constructs` peers
      (TypeScript), `import rayito` and the CLI load nothing from CDK.
    - **AWS resources and calls:** `RayitoAgent` creates a nested stack (ECR
      repository, CodeBuild project with its log group, build role and runtime
      role), an S3 asset with the code, two Lambda functions and a Step Functions
      state machine that wait for the build (the custom resources' `Provider`), the
      AgentCore runtime, and a resource that enforces MMDSv2 (`GetAgentRuntime`,
      `UpdateAgentRuntime`). For each new content, one CodeBuild build
      (`StartBuild`, `BatchGetBuilds`, `ecr:DescribeImages`); with `localDocker()`,
      no CodeBuild build: your Docker builds the image and it goes to the CDK
      bootstrap repository. `RayitoMicrovmImage` creates an S3 asset with the zip,
      a build role and, per image (the base and each size), a log group and an
      `AWS::Lambda::MicrovmImage`.
    - **Approximate cost** (us-east-1, list prices, checked 2026-10-19): the
      runtime, as with the CLI, charges $0.0895 per vCPU-hour and $0.00945 per
      GB-hour of actual use, and only while there are sessions; the build, $0.0034
      per CodeBuild `arm1.small` minute (100 free minutes a month; only with
      `codeBuild()`); the image in ECR, $0.10 per GB-month (the last 5 are kept;
      with `localDocker()` they go to the CDK bootstrap repository, which has no
      such limit and keeps them until `cdk gc`); logs, $0.50 per GB ingested and {{cdkMicrovmImageLogRetentionDays}} days of
      retention; each MicroVM image version, ≈ $0.04 per week of snapshot (one week
      minimum), one per size; the Lambdas and Step Functions waiting for the build,
      cents per deployment. Assets stay in the CDK bootstrap bucket (S3 Standard)
      until `cdk gc`. Details in [Pricing](../cost.md#coste-de-un-agente-en-agentcore).
    - **IAM:** CloudFormation deploys with the CDK bootstrap execution role. With
      the default bootstrap nothing else is needed; with a restricted one, the
      permissions in [Restricted bootstrap](#bootstrap-restringido).
    - **How to turn it off:** `cdk destroy` (or remove the construct and
      `cdk deploy`) deletes the runtime, the nested stack with its repository, the
      images, the log groups and the `Provider` functions. Bootstrap assets are
      removed with `cdk gc`; with `localDocker()` so are the agent images, which
      live in the bootstrap repository and not in the stack's.
    - **Example:** [An agent](#rayitoagent) and [an image](#rayitomicrovmimage).
    - **Tracing, VPC and log delivery** for the runtime stay off, as with the
      CLI.
    - **Measured:** pending. The constructs have not been accepted on real AWS
      yet; the figures are those of the CLI deploy, which creates the same
      resources.

!!! warning "Security: no secret gateway on AgentCore"
    An agent on AgentCore gets the runtime role's credentials, and any code
    running in the session can read them. `RayitoAgent` creates the same
    least-privilege role as the CLI: it only invokes the models in
    `bedrockModels` and only reads the secrets in `readableSecrets`. Details in
    [Security on AgentCore](desplegar-en-agentcore.md#seguridad-en-agentcore).

## Before you start { #antes-de-empezar }

1. **The CDK dependencies**, installed only if you use the constructs:

    === "Python"

        ```bash
        pip install "rayito[cdk]"
        ```

    === "TypeScript"

        ```bash
        pnpm add rayito aws-cdk-lib constructs
        ```

    You need `aws-cdk-lib` {{cdkAwsCdkLibMinVersion}} or later (the first with
    `CfnMicrovmImage`) and `constructs` {{cdkConstructsMinVersion}} or later;
    never the AgentCore alpha package. CDK in Python runs on Node (jsii), so you
    need Node 20 or later in Python too, plus the CDK CLI
    (`npm install -g aws-cdk` or `npx aws-cdk`).
2. **A concrete region in the stack.** The construct validates the region at
   synth time (that it has AgentCore Runtime, the MicroVMs base image ARN), so
   an environment-agnostic stack fails with `region_unresolved`. The account can
   stay unset: `env={"region": "us-east-1"}` is enough. The regions with
   AgentCore Runtime and arm64 builds are listed in
   [Deploy to AgentCore](desplegar-en-agentcore.md#antes-de-empezar).
3. **The CDK bootstrap** of that account and region, once
   ([below](#bootstrap)). `rayito setup` is not needed: the constructs use the
   bootstrap bucket and `RayitoMicrovmImage` creates its own build role.
4. **Model access in Bedrock** for the agent, as with the CLI
   ([Agent providers](agente-proveedores.md)).

## CDK bootstrap with tags { #bootstrap }

`cdk bootstrap` creates the asset bucket, the asset ECR repository and the
roles that `cdk deploy` uses. If your organization requires tags on create,
pass them here too: the bootstrap is deployed before your stacks, and an SCP
that requires tags would reject it just the same.

```bash
cdk bootstrap aws://123456789012/us-east-1 \
  --tags Owner=platform --tags Project=agents
```

Your stacks' tags go in `cdk deploy --tags K=V` or in code
(`Tags.of(app).add(...)`). The constructs also add their own to every resource
that supports tags, at create time (including the nested stack, the Lambda
functions, the state machine and the two AgentCore and MicroVMs resources):

| Tag | Value |
|---|---|
| `rayito:component` | `agentcore-runtime` (`RayitoAgent`) or `microvm-image` (`RayitoMicrovmImage`) |
| `rayito:managed-by` | `rayito-cdk` |
| `rayito:sdk-version` | the SDK version |
| those of the `tags` prop | yours, up to 50 counting the three above |

### Restricted bootstrap { #bootstrap-restringido }

By default the bootstrap's CloudFormation execution role has
`AdministratorAccess`. If you restrict it
(`cdk bootstrap --cloudformation-execution-policies ...`), that policy needs,
besides the usual CDK permissions (asset S3, `iam:*` on the roles the stack
creates, Lambda, Step Functions and CloudWatch Logs):

| For | Permissions |
|---|---|
| `RayitoAgent`: the nested stack | `cloudformation:CreateStack`, `UpdateStack`, `DeleteStack` and `DescribeStacks`; create, update and delete an ECR repository, a CodeBuild project and a log group |
| `RayitoAgent`: the runtime | `bedrock-agentcore:CreateAgentRuntime`, `UpdateAgentRuntime`, `GetAgentRuntime`, `DeleteAgentRuntime`, `CreateAgentRuntimeEndpoint` and `CreateWorkloadIdentity`; `iam:PassRole` on the runtime role and `iam:CreateServiceLinkedRole` |
| `RayitoMicrovmImage` | `lambda:CreateMicrovmImage`, `UpdateMicrovmImage`, `GetMicrovmImage`, `GetMicrovmImageVersion`, `DeleteMicrovmImageVersion`, `DeleteMicrovmImage`, `TagResource` and `UntagResource`; `lambda:PassNetworkConnector` on the managed `INTERNET_EGRESS` connector (AWS asks for it even when the image names none); `iam:PassRole` on the image build role |

```bash
cdk bootstrap aws://123456789012/us-east-1 \
  --cloudformation-execution-policies arn:aws:iam::123456789012:policy/RayitoCdkDeploy \
  --tags Owner=platform
```

Whoever runs `cdk deploy` only needs to assume the bootstrap roles, as in any
CDK app.

## An agent on AgentCore: `RayitoAgent` { #rayitoagent }

An app with one agent, the same project that
`rayito agent deploy ./my-agent --target agentcore` deploys:

=== "Python"

    ```python
    import aws_cdk as cdk
    from aws_cdk import aws_iam as iam
    from rayito.cdk import RayitoAgent

    app = cdk.App()
    stack = cdk.Stack(app, "Agents", env=cdk.Environment(region="us-east-1"))

    agent = RayitoAgent(
        stack,
        "SupportBot",
        project="./my-agent",
        name="support-bot",
        bedrock_models=["us.anthropic.claude-haiku-4-5-20251001-v1:0"],
        readable_secrets=["my-app/external-key"],
        tags={"Owner": "platform"},
    )

    backend = iam.Role(
        stack, "Backend", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com")
    )
    agent.grant_invoke(backend)

    app.synth()
    ```

=== "TypeScript"

    ```ts
    import { App, Stack } from "aws-cdk-lib";
    import * as iam from "aws-cdk-lib/aws-iam";
    import { RayitoAgent } from "rayito/cdk";

    const app = new App();
    const stack = new Stack(app, "Agents", { env: { region: "us-east-1" } });

    const agent = new RayitoAgent(stack, "SupportBot", {
      project: "./my-agent",
      name: "support-bot",
      bedrockModels: ["us.anthropic.claude-haiku-4-5-20251001-v1:0"],
      readableSecrets: ["my-app/external-key"],
      tags: { Owner: "platform" },
    });

    const backend = new iam.Role(stack, "Backend", {
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
    });
    agent.grantInvoke(backend);
    ```

```bash
cdk synth     # no AWS call: validates the project and options like --dry-run
cdk deploy
```

The first time takes a few minutes (mostly the image build); a `cdk deploy`
with no project changes builds nothing again.

### Props { #props-del-agente }

| TypeScript | Python | CLI equivalent | What it does |
|---|---|---|---|
| `project` | `project` | the directory | The agent directory or its project file (required). Read at synth time |
| `name` | `name` | `name` in the project file | Agent name; defaults to the project's. Sets the runtime `rayito_<name>` and the repository `rayito/agentcore/<name>` |
| `bedrockModels` | `bedrock_models` | `--bedrock-model` | Models or inference profiles the runtime role may invoke; defaults to the project's model |
| `readableSecrets` | `readable_secrets` | `--readable-secret` | Secrets the role may read with `GetSecretValue`; none by default. Never copied into the environment |
| `idleSessionTimeoutSeconds` | `idle_session_timeout_seconds` | `--idle-session-timeout` | Idle seconds after which AgentCore ends a session's microVM: {{agentcoreLifecycleMinSeconds}} to {{agentcoreLifecycleMaxSeconds}}, default {{agentcoreIdleSessionTimeoutDefaultSeconds}} |
| `maxSessionLifetimeSeconds` | `max_session_lifetime_seconds` | `--max-session-lifetime` | Maximum life of a session's microVM, default {{agentcoreLifecycleMaxSeconds}} |
| `tags` | `tags` | `--tag` | Tags for every resource of the construct |
| `build` | `build` | — | `AgentImageBuild.codeBuild()` (default) or `localDocker()` ([below](#construir-la-imagen)) |
| `exportOutputs` | `export_outputs` | — | `true` adds `CfnOutput`s with the runtime ARN and id; none by default |

The rules are the CLI's and so are the errors (`name_invalid`,
`tags_invalid`, `lifecycle_order`, `iam_resource_invalid`,
`provider_unsupported`, `network_policy_unsupported`…), raised at synth time as
`AgentCoreException` / `AgentCoreError`. Two differences:

- In TypeScript session times are in **seconds**
  (`idleSessionTimeoutSeconds`), as in the CloudFormation resource, not in
  milliseconds like `idleSessionTimeoutMs` in `deployAgent()`.
- An environment variable value cannot exceed
  {{cdkRuntimeEnvironmentValueMaxChars}} characters (the limit of the
  `AWS::BedrockAgentCore::Runtime` resource, stricter than the API): a longer
  one is `env_invalid` at synth time.

### Building the image: CodeBuild or local Docker { #construir-la-imagen }

| Mode | What it does | When |
|---|---|---|
| `AgentImageBuild.codeBuild()` / `code_build()` (default) | Uploads the build context (the same as `rayito agent export --target agentcore`) as an S3 asset and builds it in the nested stack's CodeBuild project, natively on arm64. The tag is the content hash: if it already exists in ECR, there is no build | Whenever you do not want Docker on the machine or CI that deploys |
| `AgentImageBuild.codeBuild({ timeoutMinutes: 20 })` / `code_build(timeout_minutes=20)` | The same, with another build timeout. The resource waits up to that timeout plus {{cdkBuildTotalTimeoutMarginMinutes}} minutes, polling every {{cdkBuildQueryIntervalSeconds}} s | Long or short builds |
| `AgentImageBuild.localDocker()` / `local_docker()` | Builds the image with your Docker (or Finch) for `linux/arm64` when you run `cdk deploy` and pushes it to the bootstrap repository. Does not create the nested stack's repository, project or build role | If you already have Docker with arm64 and do not want to pay for CodeBuild minutes |

### What it creates { #que-crea-el-agente }

- **Nested stack `Build`** with
  [`infra/agentcore-runtime.yaml`](https://github.com/alejandro-cedeno-10/rayito/blob/main/infra/README.md),
  the same template the CLI deploys: ECR repository (immutable tags, scan on
  push, emptied on delete), CodeBuild project and its log group, build role and
  runtime role. The artifact bucket is the bootstrap's, and the build role can
  also read the exact object of the code asset.
- **`ImageBuild`** (`Custom::RayitoImageBuild`): starts the build if the tag does
  not exist, waits for it to finish and checks the image is no bigger than 2 GB.
  With `localDocker()` it is not created: Docker pushes the image during
  `cdk deploy` and nothing checks the 2 GB (AgentCore refuses it when creating
  the runtime if it is bigger).
- **`Runtime`** (`AWS::BedrockAgentCore::Runtime`): `rayito_<name>`, the image
  just built, `PUBLIC` network, `HTTP` protocol, the plan's lifecycle and
  environment, and the description `Rayito agent <name> (rayito.agentcore/1+cdk)`.
- **`Mmds`** (`Custom::RayitoRuntimeMmds`): the CloudFormation resource cannot
  require MMDSv2, which AgentCore needs since 2026-06-30 to invoke, so this
  resource turns it on with `UpdateAgentRuntime` and checks it again on every new
  runtime version. CloudFormation drift detection may flag it as a change made
  outside the stack.
- **`Provider`** and **`Handler`**: the two Lambda functions (Python, code from
  `infra/agentcore-cdk-handler.py`) and the state machine behind the two custom
  resources, with 14-day logs.

Attributes for the rest of your app: `agentName`, `runtimeName`, `runtimeArn`,
`runtimeId`, `runtimeVersion`, `roleArn`, `repositoryUri`, `imageUri` and
`runtime` (an `IRuntime` from the `aws-cdk-lib` L2, to combine it with other
constructs), in `snake_case` in Python. `runtimeVersion` is the version that
already requires MMDSv2 (the one `Mmds` publishes), not the one CloudFormation
created, so it is the one you can pin an endpoint to. `grantInvoke(grantee)` /
`grant_invoke(grantee)` grants `bedrock-agentcore:InvokeAgentRuntime` on the
runtime.

### Invoking it { #invocar }

An agent deployed with CDK is invoked, listed and inspected exactly like one
from the CLI, by its name:

=== "CLI"

    ```bash
    rayito agent invoke support-bot "Summarize the README." --target agentcore
    rayito agent status support-bot --target agentcore
    rayito agent list --target agentcore
    ```

=== "Python"

    ```python
    from rayito import AgentCoreFleet

    fleet = AgentCoreFleet.connect("support-bot", region="us-east-1")
    reply = fleet.invoke("Summarize the README.")
    print(reply.text, reply.session_id)
    ```

=== "TypeScript"

    ```ts
    import { AgentCoreFleet } from "rayito";

    const fleet = await AgentCoreFleet.connect("support-bot", { region: "us-east-1" });
    const reply = await fleet.invoke("Summarize the README.");
    console.log(reply.text, reply.sessionId);
    ```

Sessions work the same: [Sessions](desplegar-en-agentcore.md#sesiones).

## A MicroVMs image: `RayitoMicrovmImage` { #rayitomicrovmimage }

Publishes an image with `rayd` from a release's `rayito-image.zip` (or your
own zip), with the same hooks, variables and sizes as `rayito image publish`:

=== "Python"

    ```python
    import aws_cdk as cdk
    from rayito.cdk import MicrovmImageArtifact, RayitoMicrovmImage

    artifact = MicrovmImageArtifact.download_release()

    app = cdk.App()
    stack = cdk.Stack(app, "Sandboxes", env=cdk.Environment(region="us-east-1"))

    image = RayitoMicrovmImage(
        stack,
        "TeamSandbox",
        name="team-sandbox",
        artifact=artifact,
        base_image_version="1",
        os_capabilities=True,
        sizes=["4gb"],
        allowed_mount_buckets=["amzn-s3-demo-bucket"],
        tags={"Owner": "platform"},
    )
    cdk.CfnOutput(stack, "ImageArn", value=image.image_arn)

    app.synth()
    ```

=== "TypeScript"

    ```ts
    import { App, CfnOutput, Stack } from "aws-cdk-lib";
    import { MicrovmImageArtifact, RayitoMicrovmImage } from "rayito/cdk";

    async function main(): Promise<void> {
      const artifact = await MicrovmImageArtifact.downloadRelease();

      const app = new App();
      const stack = new Stack(app, "Sandboxes", { env: { region: "us-east-1" } });

      const image = new RayitoMicrovmImage(stack, "TeamSandbox", {
        name: "team-sandbox",
        artifact,
        baseImageVersion: "1",
        osCapabilities: true,
        sizes: ["4gb"],
        allowedMountBuckets: ["amzn-s3-demo-bucket"],
        tags: { Owner: "platform" },
      });
      new CfnOutput(stack, "ImageArn", { value: image.imageArn });

      app.synth();
    }

    void main();
    ```

`downloadRelease()` is asynchronous in TypeScript because it downloads before
the stack is built; that is why the app goes inside an `async` function.

### Props { #props-de-la-imagen }

| TypeScript | Python | `rayito image publish` | What it does |
|---|---|---|---|
| `name` | `name` | `--image-name` | Image name (required): letters, digits, `-` and `_`, up to 64 characters including the size suffix. Names starting with `rayito-base` are reserved for `rayito setup` and `rayito image publish` (`image_name_reserved`) |
| `artifact` | `artifact` | `--artifact` | The image zip (required, [below](#el-artefacto)) |
| `baseImageVersion` | `base_image_version` | `--base-image-version` | Major version of the managed `al2023-1` base image (required). `latest` is refused: resolving it needs an AWS read at synth time (`option_invalid`) |
| `variant` | `variant` | `--variant` | `full` (default), `slim` or `poly`; must match the zip's marker (`artifact_variant_mismatch`) |
| `osCapabilities` | `os_capabilities` | the `-caps` variant | `additionalOsCapabilities ALL`: what the egress policy, the IMDS block and S3 mounts need |
| `withEfs` | `with_efs` | `--with-efs` | Image for EFS volumes; requires `variant` `full`, `osCapabilities` and a zip with the EFS marker |
| `memoryMib` | `memory_mib` | — | Minimum memory of the base image |
| `sizes` | `sizes` | `--sizes` | One more image per size (`<name>-4gb`), from the same zip |
| `allowedMountBuckets` | `allowed_mount_buckets` | — | Buckets sandboxes may mount (`RAYITO_ALLOWED_MOUNT_BUCKETS`) |
| `environment` | `environment` | — | Image environment variables |
| `tags` | `tags` | — | Tags for every resource of the construct |

### The artifact { #el-artefacto }

`MicrovmImageArtifact` says which zip is published, without reading AWS at
synth time:

| Factory | What it does |
|---|---|
| `MicrovmImageArtifact.downloadRelease({ version?, directory? })` / `download_release(*, version=None, directory=None)` | Downloads `rayito-image.zip` and `SHA256SUMS` from the `rayd-v<version>` release (by default your SDK's, like `rayito setup`), checks the sha256 and keeps both files in `directory` (by default `.rayito/releases/<tag>/` under the working directory). Later synths reuse them offline |
| `MicrovmImageArtifact.fromFile(path, { sha256? })` / `from_file(path, *, sha256=None)` | A zip of your own (for example, from `rayito image zip`). With `sha256`, synth fails if it does not match |

At synth time the construct checks that the zip exists (`artifact_not_found`),
its sha256 (`artifact_checksum_mismatch`), the variant
(`artifact_variant_mismatch`) and the EFS marker (`artifact_efs_mismatch`). The
zip is uploaded once to the bootstrap bucket and shared by the base image and
all its sizes.

### What it creates { #que-crea-la-imagen }

- **`Artifact`**: the zip's S3 asset.
- **`BuildRole`**: a role only Lambda in your account can assume, which reads
  **only** the asset object and writes **only** to its images' log groups. It
  does not use the `rayito-m0-iam` stack's role.
- **`LogGroup`** (and `LogGroup<SIZE>`): `/rayito/<name>`, with
  {{cdkMicrovmImageLogRetentionDays}} days of retention, deleted with the stack.
- **`Image`** (and `Image<SIZE>`): an `AWS::Lambda::MicrovmImage` with the same
  configuration `rayito image publish` sends and the description
  `Rayito image <name> (rayito.microvm-image/1+cdk)`. The description has no date
  or version on purpose: any property change creates a new image version (with
  its one-week snapshot minimum), and a redeploy with no changes does not.

Attributes: `imageName`, `imageArn`, `latestActiveImageVersion`,
`buildRoleArn`, `logGroupName` and `sizeImageArns` (size → ARN), in
`snake_case` in Python.

### Launching sandboxes from the image { #lanzar-sandboxes }

Nothing changes in the SDK: a sandbox is created by the image name, with the
execution role you already use (or none).

=== "Python"

    ```python
    from rayito import Sandbox

    with Sandbox.create(template="team-sandbox") as sbx:
        print(sbx.commands.run("uname -m").stdout)

    with Sandbox.create(template="team-sandbox-4gb") as big:
        print(big.commands.run("nproc").stdout)
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    await using sbx = await Sandbox.create({ template: "team-sandbox" });
    console.log((await sbx.commands.run("uname -m")).stdout);

    await using big = await Sandbox.create({ template: "team-sandbox-4gb" });
    console.log((await big.commands.run("nproc")).stdout);
    ```

Agents on MicroVMs (`AgentTemplate`) are out of 0.15: building that image
reads AWS (the published base image, secrets), and the fleet only exists once
the SDK starts the MicroVMs. You can publish the base with CDK and keep
deploying agents with `rayito agent deploy`.

## Coexisting with the CLI { #convivencia }

CloudFormation owns what a construct creates, and the CLI respects that: it
reads and uses it, but does not change it.

| Command | With a CDK resource |
|---|---|
| `rayito agent list`, `status` and `invoke` with `--target agentcore`, `AgentCoreFleet.connect` | work just as with a CLI one |
| `rayito agent deploy` and `rayito agent delete` with `--target agentcore`, `deploy_agent(target="agentcore")`, `deployAgent(path, { target: "agentcore" })` | `name_conflict` before writing anything (the message, in Spanish, says the agent is managed by CloudFormation and to use `cdk deploy` / `cdk destroy`) |
| `rayito image publish --image-name team-sandbox` | refuses (the image is managed by CloudFormation: change it with `cdk deploy`) |
| `rayito image prune --image-name team-sandbox` | works: it only deletes old versions, never the latest active one nor the configuration |
| `Sandbox.create(template="team-sandbox")` | works |

How it tells them apart: the runtime carries the marker
`rayito.agentcore/1+cdk` in its description (the CLI's is `rayito.agentcore/1`),
and the image, the tag `rayito:managed-by=rayito-cdk`. A runtime or image
without a Rayito marker is touched by neither the CLI nor the construct.

## Migrating an agent deployed with the CLI { #migrar }

An agent or an image can only have one owner. If one with the same name
already exists from the CLI, `cdk deploy` fails (the runtime, ECR repository or
image name already exists), CloudFormation rolls back its own changes and the
CLI's resources are not touched.

**With a short outage**, same name:

1. `rayito agent delete support-bot --target agentcore` deletes the runtime, its
   `rayito-agentcore-support-bot` stack and its logs.
2. `cdk deploy` creates the agent again under CloudFormation.

Open sessions are lost, and the runtime id and ARN change: callers using
`AgentCoreFleet.connect("support-bot")` or `rayito agent invoke support-bot`
keep working; callers that stored the ARN must update it (`agent.runtimeArn`,
or `exportOutputs: true`).

**Without an outage**, with another name:

1. Declare the agent with another `name` (`support-bot-v2`) and `cdk deploy`.
2. Move your clients to the new name.
3. `rayito agent delete support-bot --target agentcore`.

**An image** published with `rayito image publish`: publish the CDK one under
another name, change your sandboxes' `template` and delete the old one's
versions with `rayito image prune --image-name <old>` (the image itself with
the `DeleteMicrovmImage` API). The `rayito-base*` images from `rayito setup` are
not migrated: they stay with the CLI and coexist with yours.

**From CDK to the CLI**: `cdk destroy` (or remove the construct and
`cdk deploy`), then `rayito agent deploy` or `rayito image publish`.

## Deleting { #borrar }

`cdk destroy` deletes the runtime, the nested stack (the ECR repository empties
itself, with its images), the MicroVMs images, the log groups, the build role
and the `Provider` functions. The log group AgentCore creates on the first
`invoke` (`/aws/bedrock-agentcore/runtimes/...`) is not part of the stack:
delete it by hand if you do not want it. Bootstrap assets (the agent code, the
image zip, the handler code) stay in the bootstrap bucket until `cdk gc`. With
`localDocker()` the agent images live in the CDK bootstrap ECR repository, which
`cdk destroy` does not delete or limit to 5: they pile up until `cdk gc`.

## Troubleshooting { #solucion-de-problemas }

| Symptom | Cause | What to do |
|---|---|---|
| `region_unresolved` at synth | the stack has no `env.region` | set the region (`env={"region": "us-east-1"}`); the account can stay unset |
| `InvalidArgumentException` asking you to install the `cdk` extra when importing `rayito.cdk` | the extra is missing | `pip install "rayito[cdk]"` |
| Python synth fails with a jsii or Node error | CDK in Python needs Node | Node 20 or later on the `PATH` |
| `cdk deploy` fails with "already exists" on the runtime, repository or image | an agent or image with that name already exists (from the CLI or another stack) | [Migrate](#migrar) or use another `name` |
| `rayito agent deploy` gives `name_conflict` "managed by CloudFormation" | the agent belongs to CDK | change it in your app and `cdk deploy` |
| `env_invalid` at synth with a long value | a variable exceeds {{cdkRuntimeEnvironmentValueMaxChars}} characters, the CloudFormation resource's limit | shorten it, or read it at run time from a secret (`readableSecrets`) |
| `ImageBuild` fails and CloudFormation rolls back | the CodeBuild build failed or the image exceeds 2 GB | the reason (the tag and the build status) is in the stack event; the log, in the CodeBuild project's log group |
| `ImageBuild` takes long and times out | the build took longer than `timeoutMinutes` plus the margin | raise `timeoutMinutes` in `AgentImageBuild.codeBuild(...)` |
| `localDocker()` fails in `cdk deploy` | no Docker, or it cannot build `linux/arm64` | start Docker with arm64 support (buildx or QEMU), or go back to `codeBuild()` |
| Drift detection flags the runtime | `Mmds` turns on MMDSv2 outside CloudFormation | expected; the next `cdk deploy` checks it again |
| `AccessDenied` on `PassNetworkConnector`, `CreateMicrovmImage` or `CreateAgentRuntime` | restricted bootstrap | add the permissions in [Restricted bootstrap](#bootstrap-restringido) |
| `release_download_failed` | no network or no GitHub access on the first synth | download once with network (it is kept in `.rayito/releases/`) or use `fromFile` |
| `image_name_reserved` | the name starts with `rayito-base` | pick another; `rayito-base*` belongs to `rayito setup` |
| Image versions pile up | each property change creates a version with its snapshot | `rayito image prune --image-name <name>` deletes the old ones |

## Errors { #errores }

The constructs' own errors are `RayitoCdkException` in Python and
`RayitoCdkError` in TypeScript, with a `code` from a closed list; they are
raised at synth time, before any resource is added. The agent plan's errors
are still `AgentCoreException` / `AgentCoreError`
([Errors](../referencia/errores.md#agentcore)).

| `code` | When |
|---|---|
| `region_unresolved` | the stack has no concrete region |
| `account_invalid` | the stack's account is set and is not a 12-digit id |
| `image_name_invalid` | the image name, with the size suffix, does not match the pattern or exceeds 64 characters |
| `image_name_reserved` | the name starts with `rayito-base` |
| `artifact_not_found` | the zip does not exist |
| `artifact_checksum_mismatch` | the zip's sha256 is not the expected one |
| `artifact_variant_mismatch` | the zip is of another variant |
| `artifact_efs_mismatch` | the zip's EFS marker does not match `withEfs` |
| `option_invalid` | an option is not valid (`latest` as base version, `withEfs` without `full` and `osCapabilities`, sizes outside the catalog, memory that is not a positive integer or, with `sizes`, differs from the baseline) |
| `release_download_failed` | the release could not be downloaded or verified |

## See also { #ver-tambien }

- [Deploy to AgentCore](desplegar-en-agentcore.md): the same agent with the CLI
  or the SDK, its options and its security model.
- [Images](../images.md) and [Sizes](../funciones-opcionales/tamanos.md).
- [Security: CDK constructs](../security.md#cdk)
- [Optional features and their cost](../optional-features.md)
