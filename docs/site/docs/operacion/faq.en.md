# FAQ

## Do I need a Rayito API key? { #necesito-una-api-key-de-rayito }

No. Rayito has no server or accounts: the SDK uses your AWS credentials
and each sandbox has its own *access token*, which the SDK generates when it
creates it.

## How much does it cost? { #cuanto-cuesta }

Whatever it consumes on your AWS bill: ≈ $0.126 per hour per 2 GB sandbox
while it runs, a fraction of a cent per launch and ≈ $0.04 per week
per published image version. An AI agent adds the model's tokens, which
usually weigh more than the VM. Examples and each component in
[Pricing](../cost.md#cuanto-cuesta-con-ejemplos).

## How do I choose CPU and memory? { #como-elijo-cpu-y-memoria }

With the image. Size is a property of the Lambda MicroVMs image:
publish the images of each size with `rayito image publish --sizes` and
choose at creation time with `Sandbox.create(size=...)`, or publish your own
image with `--memory-mib` and create it by name
([Sizes](../funciones-opcionales/tamanos.md),
[Limits: size](../limits.md#tamano-cpuram)).

## Can a sandbox live longer than 8 hours? { #puede-un-sandbox-vivir-mas-de-8-horas }

No: 8 h is the platform maximum and it also counts paused time.
To carry on with the same files, `sbx.reincarnate()` saves `HOME` to S3 and
restores it in a new sandbox with a fresh 8 h; memory (kernel variables,
processes) does not survive ([Persistence](../persistence.md)).

## Which regions? { #que-regiones }

The ten with Lambda MicroVMs: `us-east-1`, `us-east-2`, `us-west-2`,
`eu-west-1`, `eu-central-1`, `eu-north-1`, `ap-northeast-1`, `ap-south-1`,
`ap-southeast-1` and `ap-southeast-2`.

Deploying agents to AgentCore (`--target agentcore`, since 0.15.0) has its own
list: the 21 regions with AgentCore Runtime and CodeBuild arm64 builds
([Limits](../limits.md),
[Deploy to AgentCore](../guias/desplegar-en-agentcore.md#antes-de-empezar)).

## Does it work from macOS or Windows? { #funciona-desde-macos-o-windows }

Yes: the SDK and the CLI run on any system with Python ≥ 3.11 or
Node ≥ 20. The only thing that needs Linux is **building** `rayd` from
source; publishing the image from the release's `rayito-image.zip`
does not need it.

## Which languages can I run inside? { #que-lenguajes-puedo-ejecutar-dentro }

Anything with `commands.run`. With `run_code`: Python always; bash,
JavaScript and TypeScript (with Deno) in `rayito-base-poly`. Not R or Java: R
takes 1.4 GB installed and the Java kernel is unmaintained
([Languages and kernels](../kernels.md#r-y-java)).

## Which languages can I use Rayito from? { #desde-que-lenguajes-puedo-usar-rayito }

Python and TypeScript have an official SDK. Any other language with a gRPC
client and an AWS SDK can use it by generating the client from the `.proto`
([Other languages](../referencia/otros-lenguajes.md)).

## Can the code in the sandbox reach the internet? { #el-codigo-del-sandbox-puede-salir-a-internet }

By default, yes. To cut it off or limit it to certain domains, use the
`rayito-base-caps` image with `allow_internet_access=False` or `network=`, or
your own VPC network connector ([Outbound network](../network.md)).

## Can the code in the sandbox read my AWS credentials? { #puede-el-codigo-del-sandbox-leer-mis-credenciales-de-aws }

No, unless you give it an *execution role* (`execution_role_arn=`), which by
default does not exist. S3 transfers are signed by your process, not by the
sandbox ([Security](../security.md)).

## Can I run an AI agent inside the sandbox? { #puedo-correr-un-agente-de-ia-dentro-del-sandbox }

Yes, since 0.8.0: `sbx.agent.run(...)` runs a coding agent (OpenCode or
deepagents) inside the sandbox, on an image built with
`AgentTemplate`. The usual way is to create the sandbox with
`Sandbox.create(...)` and call `sbx.agent.run(...)`; the pool, warm-up and
resident server are optional ways to start faster. The model credential
only goes through the secret gateway, and the agent's permissions are not a
security boundary: the boundary is the MicroVM with egress closed
([Agent in the sandbox](../guias/agente-en-el-sandbox.md),
[Security](../security.md#agente-de-codigo-dentro-del-sandbox)).

## Can I deploy the agent I already have? { #puedo-desplegar-el-agente-que-ya-tengo }

Yes. `rayito agent deploy ./my-agent` reads the project (LangGraph, LangChain,
deepagents, Strands, Hermes, OpenCode, Vercel's AI SDK or your own HTTP app),
without running your code, and serves it on a fleet of MicroVMs
([Deploy your agent](../guias/despliega-tu-agente.md#sin-escribir-nada),
[Several sessions and several VMs](../guias/flota-de-agentes.md)). Since
0.15.0, with `--target agentcore` the same project goes to Amazon Bedrock
AgentCore Runtime, without local Docker
([Deploy to AgentCore](../guias/desplegar-en-agentcore.md)). Note: AgentCore
has no secret gateway and the agent can read the runtime role's credentials.

## Can I declare it as infrastructure as code? { #puedo-declararlo-como-infraestructura-como-codigo }

Yes, with AWS CDK, since 0.15.0: `RayitoAgent` deploys the agent to AgentCore
with the same resources and IAM as `--target agentcore`, and
`RayitoMicrovmImage` publishes a MicroVMs image in your stack. They live in
`rayito.cdk` (the `cdk` extra) and `rayito/cdk`, and are deployed with
`cdk deploy` ([Deploy with CDK](../guias/desplegar-con-cdk.md)).

## What happens if my process dies with live sandboxes? { #que-pasa-si-mi-proceso-muere-con-sandboxes-vivos }

They stay alive (and billing) until their `timeout`. With the
[server-side deadline](../lifecycle.md) (`max_lifetime` or `on_timeout`), the
agent itself kills or pauses them when it expires, even if your process no
longer exists. To clean up by hand: `rayito sandbox kill --all`.

## Rayito versus E2B, Modal or Daytona? { #rayito-frente-a-e2b-modal-o-daytona }

| | Rayito | E2B | Modal | Daytona |
|---|---|---|---|---|
| Where it runs | your AWS account | E2B's cloud (or self-hosted) | Modal's cloud | Daytona's cloud (or self-hosted) |
| Agent in the VM | `rayd` (Rust) | `envd` (Go) | proprietary | Go daemon |
| Official SDKs | Python, TypeScript | Python, JS/TS | Python (JS and Go via libmodal) | Python, TypeScript, Ruby, Go, Java |
| API | E2B 2.x's (shim) + native | E2B | Modal | Daytona |
| Server to operate | none | none (SaaS) | none (SaaS) | none (SaaS) |

Choose Rayito if you want code and data to stay in your AWS account and you
don't want to operate a cluster. Sources for the table:
[Other languages](../referencia/otros-lenguajes.md#comparado-con-otros-sandboxes).

## Is it stable? { #es-estable }

Each version is accepted against real AWS, not simulators, and the
releases are signed ([Verify a release](../verify.md)). Two optional
features are **experimental**: the
[custom domain](../funciones-opcionales/dominio-propio.md), not yet verified
end to end on real AWS, and the
[EFS volumes](../funciones-opcionales/volumenes-efs.md). The
[metadata index](../funciones-opcionales/indice-de-metadatos.md) is still
pending acceptance on real AWS. All of them are off by default.
Changes by version: [Changelog](../referencia/changelog.md).
