## ADDED Requirements

### Requirement: The CDK constructs ship as an optional module of each SDK
The Python package SHALL expose `rayito.cdk` (behind the extra `cdk`, requiring `aws-cdk-lib>=2.261.0,<3` and `constructs>=10.5.0,<11`) and the npm package SHALL expose the subpath `rayito/cdk` (with `aws-cdk-lib ^2.261.0` and `constructs ^10.5.0` as optional peers), both exporting `RayitoAgent`, `AgentImageBuild`, `RayitoMicrovmImage` and `MicrovmImageArtifact`, plus `RayitoCdkException` (Python) or `RayitoCdkError` (TypeScript). Importing `rayito`, the CLIs, the MCP server or the E2B entry points SHALL NOT load the Python modules `aws_cdk`, `jsii` or `constructs`, nor the npm packages `aws-cdk-lib` or `constructs`. Importing `rayito.cdk` without the extra SHALL raise the SDK's missing-extra error naming `rayito[cdk]`.

#### Scenario: no CDK import without the extra
- **WHEN** the isolation test imports `rayito` and runs the CLI entry point
- **THEN** `aws_cdk`, `jsii` and `constructs` are absent from `sys.modules`, and `pack-check.mjs` finds no import of `aws-cdk-lib` or `constructs` in any `index`, `e2b`, `cli` or `mcp` chunk

#### Scenario: same public surface in both SDKs
- **WHEN** `scripts/check_sdk_parity.py` compares `rayito.cdk.__all__` with the exports of `src/cdk/index.ts`
- **THEN** they match `testdata/agentcore-cdk/contract.json` `sdk.*.exports` up to the TypeScript-only types

### Requirement: Synthesis makes no AWS call and needs a concrete region
Constructing `RayitoAgent` or `RayitoMicrovmImage` and synthesizing the app SHALL make no AWS API call and SHALL NOT use context lookups. When the stack region is a token, the constructor SHALL raise `region_unresolved` naming the construct before adding any child. An account token SHALL be accepted, with ARNs built from the `AWS::AccountId` and `AWS::Partition` pseudo parameters.

#### Scenario: environment-agnostic stack
- **WHEN** either construct is added to a stack without `env`
- **THEN** it raises `RayitoCdkException` / `RayitoCdkError` with code `region_unresolved` and the stack has no resources from it

#### Scenario: shared golden templates
- **WHEN** each SDK synthesizes every case of `testdata/agentcore-cdk/cases/` whose `expected` is `"template"` and normalises it with the contract rules
- **THEN** the result equals `expected.template.json`, and `scripts/gen_cdk_vectors.py --check` passes

### Requirement: RayitoAgent deploys the same agent as the CLI
`RayitoAgent` SHALL accept `project`, `name`, `bedrockModels`, `readableSecrets`, `idleSessionTimeoutSeconds`, `maxSessionLifetimeSeconds`, `tags`, `build` and `exportOutputs` (Python snake_case names per `contract.sdk.propNames`), SHALL validate them with the AgentCore plan of the SDK (raising `AgentCoreException` / `AgentCoreError` with the codes of `testdata/agentcore-deploy/contract.json`), and SHALL synthesize a nested stack with `infra/agentcore-runtime.yaml` and the CLI's parameter values, an `AWS::BedrockAgentCore::Runtime` named `runtime_name_for(name)` with the plan's container URI, role, `PUBLIC` network, `HTTP` protocol, lifecycle and environment, and the custom resources of the image build and MMDSv2. The runtime role's Bedrock and Secrets Manager statements SHALL equal the resolved ARN lists and SHALL be absent when the lists are empty.

#### Scenario: identical IAM to the CLI path
- **WHEN** the synth test compares the nested template's roles with `infra/agentcore-runtime.yaml` for the same parameters
- **THEN** every statement is equal, and the only addition is the build role's `s3:GetObject` on the source asset object

#### Scenario: invalid name
- **WHEN** `RayitoAgent` gets `name: "Bad Name"`
- **THEN** it raises the AgentCore `name_invalid` error before adding any child

### Requirement: The agent image is built in AWS by default
`AgentImageBuild.codeBuild()` SHALL be the default build: a `Custom::RayitoImageBuild` resource that skips the build when the `src-<hash>` tag exists in the repository, otherwise calls `StartBuild` with `sourceLocationOverride`, `IMAGE_TAG`, `AGENT_BASE_IMAGE` and `idempotencyToken` equal to the tag, waits for `BatchGetBuilds` to report `SUCCEEDED`, and fails when the image is larger than `agentcoreMaxImageBytes`. `AgentImageBuild.localDocker()` SHALL use an arm64 `DockerImageAsset` instead and SHALL remove the repository, build role, build project and build log group from the nested stack.

#### Scenario: unchanged project
- **WHEN** a stack is deployed again with the same project
- **THEN** the image tag is the same, no CodeBuild build starts and the runtime is not updated

#### Scenario: local Docker build
- **WHEN** the case `agent-local-docker` is synthesized
- **THEN** the template has a container asset for `linux/arm64`, no `AWS::CodeBuild::Project` and no `Custom::RayitoImageBuild`

### Requirement: Every CDK runtime enforces MMDSv2
`RayitoAgent` SHALL declare a `Custom::RayitoRuntimeMmds` resource whose properties are the runtime id and `AgentRuntimeVersion`; its handler SHALL send `UpdateAgentRuntime` with the configuration returned by `GetAgentRuntime` and `metadataConfiguration.requireMMDSV2=true` only when the runtime does not already require it, with a deterministic `clientToken`, and SHALL complete when the runtime is `READY` with MMDSv2 required.

#### Scenario: runtime already enforcing MMDSv2
- **WHEN** the handler runs for a runtime whose `requireMMDSV2` is true
- **THEN** it sends no `UpdateAgentRuntime`

### Requirement: CDK-managed agents are readable but not mutable from the CLI
The description of a CDK runtime SHALL be `Rayito agent <name> (rayito.agentcore/1+cdk)` and every taggable resource SHALL carry `rayito:managed-by=rayito-cdk`, `rayito:component` and `rayito:sdk-version`. Both SDKs SHALL treat such a runtime as Rayito's for `AgentCoreFleet.connect`, `rayito agent list`, `status` and `invoke`, and `rayito agent deploy`, `rayito agent delete --target agentcore`, `deploy_agent(target="agentcore")` and `deployAgent({ target: "agentcore" })` SHALL fail with `name_conflict` and the contract message pointing to `cdk deploy` and `cdk destroy`, before any write.

#### Scenario: deploy over a CDK agent
- **WHEN** `rayito agent deploy ./agent --target agentcore --yes` finds a runtime with the `+cdk` marker for that name
- **THEN** it exits 2 with code `name_conflict`, the contract message, and no AWS write

#### Scenario: list includes CDK agents
- **WHEN** `rayito agent list --target agentcore` runs in an account with one CLI agent and one CDK agent
- **THEN** both are listed

### Requirement: RayitoMicrovmImage publishes a Rayito MicroVM image through CloudFormation
`RayitoMicrovmImage` SHALL accept `name`, `artifact`, `baseImageVersion`, `variant`, `withEfs`, `osCapabilities`, `memoryMib`, `sizes`, `allowedMountBuckets`, `environment` and `tags`, and SHALL synthesize one S3 asset of the artifact, one build role trusted by `lambda.amazonaws.com` that can read only that object and write only the image log groups, and, for the base image and each extra size, a log group `/rayito/<name>` and an `AWS::Lambda::MicrovmImage` whose properties equal the configuration `rayito image publish` sends for the same inputs, mapped to CloudFormation names, with the description `Rayito image <name> (rayito.microvm-image/1+cdk)`. Names starting with `rayito-base` SHALL be refused with `image_name_reserved`; an artifact that does not match `sha256`, `variant` or `withEfs` SHALL be refused before synthesis; `baseImageVersion` SHALL be required and `latest` refused.

#### Scenario: same configuration as the CLI
- **WHEN** the synth test builds `image-caps-sizes-mounts`
- **THEN** each image's `Hooks`, `Resources`, `CpuConfigurations`, `AdditionalOsCapabilities`, `Logging` and `EnvironmentVariables` equal `desired_configuration` / `desiredConfiguration` for the same settings, and the extra sizes are named `<name>-512mb` and `<name>-4gb`

#### Scenario: reserved name
- **WHEN** `RayitoMicrovmImage` gets `name: "rayito-base"`
- **THEN** it raises `image_name_reserved`

#### Scenario: release artifact verified offline after the first download
- **WHEN** `downloadRelease` / `download_release` runs twice with the same `directory`
- **THEN** the second call makes no network request and still verifies `rayito-image.zip` against `SHA256SUMS`

### Requirement: The CLI does not publish over a CDK-managed image
`rayito image publish` SHALL read `tags` from the `GetMicrovmImage` call it already makes for an existing image and SHALL fail without any write when `rayito:managed-by` is `rayito-cdk`, with the message of `contract.cliRefusals.imagePublish`. `rayito image prune` SHALL keep working on such images.

#### Scenario: publish over a CDK image
- **WHEN** `rayito image publish --image-name team-sandbox` targets an image tagged `rayito:managed-by=rayito-cdk`
- **THEN** it fails with the contract message and makes no `UpdateMicrovmImage` call

### Requirement: Deploying the constructs is opt-in and documented with its cost
Only `cdk deploy` of a stack that contains a construct SHALL create billable resources. The docstrings and TSDoc of both constructs SHALL carry a «Coste y activación» block, `optional-features.md` SHALL have a row, and the bilingual guide SHALL list the resources and their cost lines.

#### Scenario: synthesis is free
- **WHEN** a test synthesizes every contract case with AWS credentials unset
- **THEN** it succeeds and no AWS client is constructed
