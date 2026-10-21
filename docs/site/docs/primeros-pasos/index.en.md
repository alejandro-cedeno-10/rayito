---
description: "Launch your first Rayito sandbox in your AWS account in about 15 minutes: install the SDK, prepare the account and run code."
---

# Getting started

In about 15 minutes you will have a sandbox running in your AWS account (most
of the time goes into waiting for AWS to build the image). This section
follows a linear path: each page ends where the next one begins.

## What you need { #que-necesitas }

| Requirement | Detail |
|---|---|
| An AWS account | with administrator or *bootstrap* permissions once, so `rayito setup` can deploy CloudFormation stacks (IAM and, if you have no bucket, an S3 one) |
| A region with Lambda MicroVMs | `us-east-1`, `us-east-2`, `us-west-2`, `eu-west-1`, `eu-central-1`, `eu-north-1`, `ap-northeast-1`, `ap-south-1`, `ap-southeast-1` or `ap-southeast-2` |
| The AWS CLI v2 (optional) | only for the [manual path, without `rayito setup`](configurar-aws.md#a-mano-sin-rayito-setup) (`aws s3 mb`, `aws cloudformation deploy`), the `aws sts` checks and signing in with SSO. [Install it](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) |
| `curl` and [`cosign`](https://docs.sigstore.dev/cosign/system_config/installation/) ≥ 2.x (optional) | only on the manual path: download the IAM template and the release image, and verify its signature before publishing it ([Verify a release](../verify.md)) |
| AWS credentials on your machine | a profile (`aws configure` or `aws configure sso`), environment variables or the machine's role |
| Python ≥ 3.11 **or** Node ≥ 20 | the Python SDK (`rayito` on PyPI) or the TypeScript SDK (`rayito` on npm). The `rayito` CLI comes with either one: `pip install "rayito[cli]"` or `npx rayito` |

You do not need Docker on your machine: AWS builds the sandbox image from a
zip. There is no Rayito API key and no account to create anywhere else.

## The recommended path { #la-ruta-recomendada }

<div class="rx-steps" markdown>

1. [**Installation**](instalacion.md): the SDK, the CLI and the optional
   extras.
2. [**Set up AWS**](configurar-aws.md): IAM, the bucket (reused or created) and
   the `rayito-base` image in your account. You do it once per account and region.
3. [**First sandbox**](../quickstart.md): commands, files, code and
   reconnecting from another process.
4. [**Concepts**](../concepts.md): what runs where, the three clocks of a
   sandbox, the two tokens and reconnection.

Step 2 is done once per account and region (about 15 min). What every
application repeats is installing the SDK (step 1) and writing its code
(step 3).

</div>

When you are done, the [Guides](../guias/index.md) explain each feature
(among them, [a coding agent inside the sandbox](../guias/agente-en-el-sandbox.md))
and the [Reference](../referencia/index.md) lists every parameter. If what you
want is to deploy your own agent, start with
[Your agent in 5 minutes](agente-en-cinco-minutos.md); to take it to
Amazon Bedrock AgentCore Runtime, read [Deploy to AgentCore](../guias/desplegar-en-agentcore.md),
and to declare it in your CDK app, [Deploy with CDK](../guias/desplegar-con-cdk.md).

!!! tip "Coming from E2B?"
    Still follow [Set up AWS](configurar-aws.md): the SDK needs an image in
    your account. After that, [Migrate from E2B](../migrar-desde-e2b/index.md)
    tells you which import to change and what behaves differently.

!!! info "How much it costs to try"
    A 2 GB sandbox costs ≈ $0.126 per hour while it runs (us-east-1,
    2026-09) and each published image version ≈ $0.04 per week of storage.
    Going through this whole section costs a few cents. Details and examples
    in [Pricing](../cost.md#cuanto-cuesta-con-ejemplos).
