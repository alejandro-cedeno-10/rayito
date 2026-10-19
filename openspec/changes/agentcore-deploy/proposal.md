## Why

Rayito 0.14 deploys an existing agent project to a fleet of Lambda MicroVMs
with one call (`deploy_agent`, `rayito agent deploy`, ADR-029) and can already
export the same project as an AgentCore-ready container context
(`AgentProject.export(target="agentcore")`: arm64 Dockerfile, port 8080,
`/ping` + `/invocations`, the AgentCore session header). Going from that
context to a running Amazon Bedrock AgentCore Runtime is still manual: build
an arm64 image (Docker with buildx, or CodeBuild by hand), create an ECR
repository, write an execution role, create the runtime, remember to enable
MMDSv2 (mandatory since 2026-06-30, or every invocation fails), and write
SigV4 invocation code with session ids of at least 33 characters. Teams that
standardise on AgentCore want the zero-config experience of 0.14 with
AgentCore as the destination, from the same project, with the same `invoke`
API, and with no local Docker.

## What Changes

- **New deploy target `agentcore`**, opt-in: `rayito agent deploy ./my-agent
  --target agentcore`, `deploy_agent("./my-agent", target="agentcore")`,
  `deployAgent(path, { target: "agentcore" })`. The default target stays
  `microvm`; nothing changes for existing users, flags or return types.
- **Plan, cost and confirmation** before anything is created (`--yes`,
  `--dry-run`, `--json`), with the security difference printed in the plan:
  AgentCore has no Rayito secrets gateway; the agent reads credentials from
  the runtime role, so the role is least privilege.
- **No local Docker.** Rayito uploads the export context to the artifact
  bucket and builds the `linux/arm64` image with AWS CodeBuild into an ECR
  repository. The repository, the CodeBuild project and the two roles come
  from one CloudFormation stack per agent (`rayito-agentcore-<name>`,
  `infra/agentcore-runtime.yaml`), deployed through the existing
  `OptionalStacks` port like `rayito setup` does.
- **Idempotent runtime**: create or update by name, wait for `READY`, enforce
  MMDSv2, content-addressed image tags (a redeploy without changes builds
  nothing and creates no runtime version; a changed project updates the same
  runtime, never a duplicate).
- **Same invoke API**: `AgentCoreFleet.invoke()` / `stream()` have the
  signature and session semantics of `AgentFleet`; underneath,
  `InvokeAgentRuntime` with SigV4 and a deterministic session-id mapping.
- **Lifecycle commands**: `rayito agent invoke|status|stop --target agentcore`
  and the new `rayito agent list --target agentcore` and `rayito agent delete
  <name> --target agentcore` (removes only what Rayito created for that name).
- **Explicit option table**: every MicroVM fleet option either applies, is an
  error when passed in the call (`option_not_supported`), or is a warning when
  it comes from the project file; nothing is approximated silently.
- **Options of the new target**: `--bedrock-model`, `--readable-secret`,
  `--idle-session-timeout`, `--max-session-lifetime`, `--tag K=V` (for
  tag-enforcing SCPs), and `AgentCoreOptions` in both SDKs.
- **All 0.14 frameworks** (LangGraph Python and JS, LangChain/Deep Agents
  Python and JS, Strands Python and TypeScript, the Strands harness, Hermes,
  OpenCode, Vercel AI SDK, custom Flask, custom Express): the image is the 0.14
  export with two additions for AgentCore: a build argument for the base image
  (same digest, pulled from ECR Public in CodeBuild) and, for the Vercel AI SDK
  adapter whose Bedrock client only reads environment credentials, a small
  botocore helper that hands the runtime role credentials to the Node bridge
  per invocation.
- **Docs** in Spanish and English (Diátaxis: guide, CLI and API reference,
  cost box, optional-features row, security page), ADR-031, `SECURITY.md`, and
  the exported README states the security difference.
- **CDK construct**: deferred to a follow-up change (design D12); it does not
  block the CLI or the SDKs.

## Capabilities

### New Capabilities

- `agentcore-deploy`: deploy, invoke, inspect and delete an agent on Amazon
  Bedrock AgentCore Runtime from the Python and TypeScript SDKs and CLIs.

### Modified Capabilities

None. The `ai-agent-service` and `cli` behaviours for the default target are
unchanged; the new target is specified entirely in `agentcore-deploy`.

## Impact

- New private packages: Python `rayito/_agentcore/`, TypeScript
  `src/agentcore/`; public additions `AgentCoreFleet`, `AsyncAgentCoreFleet`
  (Python), `AgentCoreFleetStream`, `AgentCoreOptions`, `AgentCoreStatus`,
  `AgentCoreDeleteResult`, `AgentCoreDeployPlan`, `AgentCoreException` /
  `AgentCoreError`; a `target` (and `agentcore`) argument on `deploy_agent`,
  `deploy_agent_async`, `plan_deploy`, `deployAgent`, `planDeploy`.
- New optional AWS clients, loaded only with `target="agentcore"`:
  `bedrock-agentcore-control`, `bedrock-agentcore`, `codebuild`, `ecr`
  (botocore already ships them; TypeScript optional peers
  `@aws-sdk/client-bedrock-agentcore-control`, `@aws-sdk/client-bedrock-agentcore`,
  `@aws-sdk/client-codebuild`, `@aws-sdk/client-ecr`, `^3.1138.0`).
- New template `infra/agentcore-runtime.yaml`, packaged in both SDKs by
  `scripts/gen_stack_assets.py`; new limits in `limits.json`; new agent assets
  (`requirements-aws-credentials.txt`, `aws_env_credentials.py`) rendered into
  TypeScript by `scripts/gen_agent_template_assets.py`.
- The `agentcore` export target gains the `AGENT_BASE_IMAGE` build argument
  and a README section; the `docker` target is byte-for-byte unchanged.
- Cost only on explicit action: CodeBuild minutes, ECR storage, AgentCore
  session time and logs. No AWS call happens unless `target="agentcore"` is
  passed; the plan itself makes none.
- No Rust, proto or `rayd` change.
