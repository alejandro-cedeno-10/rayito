## ADDED Requirements

### Requirement: The agentcore target is opt-in and the default stays MicroVM
`deploy_agent`, `deploy_agent_async` and `plan_deploy` (Python) SHALL accept a keyword-only `target` with the values `"microvm"` (default) and `"agentcore"`, and a keyword-only `agentcore: AgentCoreOptions | None`; `deployAgent` and `planDeploy` (TypeScript) SHALL accept `target` and `agentcore` in their options object. `rayito agent deploy`, `invoke`, `status` and `stop` SHALL accept `--target microvm|agentcore` with default `microvm`. Without `target="agentcore"` the behaviour, return types, output and AWS calls SHALL be those of 0.14, and no AgentCore, CodeBuild or ECR client SHALL be created or loaded. AgentCore-only options given with the MicroVM target SHALL raise `option_requires_agentcore` before any AWS call.

#### Scenario: default deploy unchanged
- **WHEN** `deploy_agent("./agent")` or `rayito agent deploy ./agent` runs without `target`
- **THEN** it returns an `AgentFleet` and makes exactly the AWS calls of 0.14, and a test in each SDK proves that no `bedrock-agentcore-control`, `bedrock-agentcore`, `codebuild` or `ecr` client was constructed or imported

#### Scenario: AgentCore options without the target
- **WHEN** `deploy_agent("./agent", agentcore=AgentCoreOptions(idle_session_timeout_seconds=600))` runs with the default target
- **THEN** it raises `AgentCoreException` with code `option_requires_agentcore` and makes no AWS call

#### Scenario: CLI surface
- **WHEN** the Python CLI surface test compares against `testdata/cli/surface.json`
- **THEN** `agent deploy` ends with `target`, `tag`, `bedrock_model`, `readable_secret`, `idle_session_timeout`, `max_session_lifetime`; `agent invoke` and `agent status` end with `target`; `agent stop` ends with `target`, `session_id`; and `agent delete` (`name`, `target`, `yes`) and `agent list` (`target`) exist

### Requirement: The plan is computed without AWS and states security, warnings and cost
With `target="agentcore"`, `plan_deploy`, `planDeploy` and `rayito agent deploy --dry-run` SHALL make no AWS call and load no AWS client. They SHALL validate, in the order of `contract.json.optionsTable.description`, the region against `agentcoreSupportedRegions` (`unsupported_region`), the MicroVM options passed in the call (`option_not_supported`), the model provider (`provider_unsupported` unless `bedrock`), the project network policy (`network_policy_unsupported`), the agent name (`name_invalid`), the lifecycle values (`lifecycle_invalid`, `lifecycle_order`), the tags (`tags_invalid`, counting the three fixed stack tags) the environment (`env_invalid`, counting the two region variables) and the IAM resources (`iam_resource_invalid`: a readable secret, a Bedrock model or the project model that is not a plain name or id without `*`, `?` or `,`, or an ARN that does not name the deployment's partition, region and account, per `contract.json.constants.iamResourceRules`; the check repeats with the account when the role is built). The plan SHALL list three `planned` steps (stack, image, runtime), the derived names, the bucket and its source (`argument`, `project_file`, `environment` or `discovered`), the Bedrock models, the readable secrets, the runtime environment, the effective lifecycle, the tags, the security lines, the warnings for project-file values that do not apply, and the cost lines of the contract.

#### Scenario: shared plan vectors
- **WHEN** each SDK evaluates every case of `testdata/agentcore-deploy/plan-vectors.json`
- **THEN** the plan equals `expected` on every key except `imageTag` and `cost`, or the raised error's `code` and interpolated fields equal `expected.error`, and no AWS client is created

#### Scenario: project-file values are warnings, call values are errors
- **WHEN** the project file has `[scaling] min_vms = 2` and the call passes nothing
- **THEN** the plan carries the `fileScaling` warning and no error
- **WHEN** the call passes `ScalingOptions(min_vms=2)` (or `--min-vms 2`)
- **THEN** the call fails with `option_not_supported` naming `scaling.min_vms`

#### Scenario: wildcard or foreign IAM inputs are rejected
- **WHEN** a call passes `readable_secrets=["*"]`, `bedrock_models=["arn:aws:bedrock:*::foundation-model/*"]` or a secret ARN of another account
- **THEN** both SDKs fail with `iam_resource_invalid` naming the field, before any AWS call, and no role is built

#### Scenario: security difference in the plan
- **WHEN** the human plan is printed
- **THEN** its «Seguridad:» section starts with the `noGateway` line of the contract, followed by the Bedrock models line, the readable secrets line (when any), `publicNetwork` and `sessionIsolation`

### Requirement: Deploy builds the image in AWS without local Docker
After confirmation (`--yes`, or a terminal; otherwise `needs_confirmation`), deploy SHALL deploy `infra/agentcore-runtime.yaml` (packaged by `scripts/gen_stack_assets.py`) as `rayito-agentcore-<name>` through `OptionalStacks` with `CAPABILITY_IAM` and the merged tags, upload the `render_export(target="agentcore")` tree as a zip to `rayito/images/agentcore/<name>/<tag>.zip` in the artifact bucket of the same region (`bucket_region` otherwise), build it with `StartBuild` (`idempotencyToken` = the tag, `AGENT_BASE_IMAGE` = the ECR Public mirror with the pinned digest), poll `BatchGetBuilds`, and delete the zip when the build ends. The image tag SHALL be `src-` plus 16 hex characters of the framed sha256 of the export tree. No code path SHALL run Docker on the caller's machine.

#### Scenario: image tag vectors
- **WHEN** each SDK computes the tag of every tree in `testdata/agentcore-deploy/image-tags.json`
- **THEN** it equals `expected`

#### Scenario: unchanged project reuses the image
- **WHEN** `DescribeImages` finds the content tag in the repository
- **THEN** no `PutObject` or `StartBuild` is made and the image step is `reuse`

#### Scenario: failed build
- **WHEN** `BatchGetBuilds` reports `FAILED`, `FAULT`, `TIMED_OUT` or `STOPPED`
- **THEN** deploy raises `image_build_failed` with the build id and the build log group, the source zip is deleted and no runtime call is made

#### Scenario: too large for AgentCore
- **WHEN** the built or reused image reports more than 2 GB in `imageSizeInBytes`
- **THEN** deploy raises `image_too_large` before any runtime call

### Requirement: The runtime is created or updated idempotently and always enforces MMDSv2
Deploy SHALL find the runtime by name (`rayito_<name>`) with `ListAgentRuntimes` and treat it as Rayito's only when its description carries `rayito.agentcore/1` (`name_conflict` otherwise). It SHALL create a missing runtime (`PUBLIC` network, `HTTP` protocol, the image URI, the runtime role from the stack output, the lifecycle, the environment, the description, the tags, a deterministic `clientToken`), wait for `READY`, and then send `UpdateAgentRuntime` with the full configuration and `metadataConfiguration.requireMMDSV2 = true`; it SHALL update an existing runtime whose compared fields differ (always with `requireMMDSV2 = true`), reuse one whose fields are equal, wait for one in progress, repair one in `UPDATE_FAILED`, and refuse one in `CREATE_FAILED`, `DELETING` or `DELETE_FAILED` (`runtime_blocked`). It SHALL never create a second runtime for the same name.

#### Scenario: shared apply vectors
- **WHEN** each SDK runs the deployer over fakes built from every case of `testdata/agentcore-deploy/apply-vectors.json`
- **THEN** the stack, image and runtime actions equal `expected`, or the error code and fields equal `expected.error`

#### Scenario: redeploy without changes
- **WHEN** a deployed project is deployed again unchanged
- **THEN** the stack update is a no-op, no build runs, no `CreateAgentRuntime` or `UpdateAgentRuntime` is sent and the three steps report `reuse`

#### Scenario: concurrent create
- **WHEN** `CreateAgentRuntime` fails with `ConflictException`
- **THEN** the deployer lists again, finds the runtime and updates it instead of failing or creating a duplicate

### Requirement: Invoke uses the AgentFleet API and maps sessions deterministically
`AgentCoreFleet.invoke`, `stream` (and `AsyncAgentCoreFleet` in Python) SHALL have the signature and result types of `AgentFleet.invoke` and `stream`, SHALL call `InvokeAgentRuntime` with SigV4 credentials of the caller, the runtime ARN, a `runtimeSessionId` derived by `contract.json.constants.sessionIdRules`, `contentType` `application/json`, `accept` `application/json` or `text/event-stream`, and a body without `session_id`, and SHALL decode the response with the existing MicroVM decoders. The returned `session_id` SHALL be the id used, so passing it back continues the same session.

#### Scenario: session id vectors
- **WHEN** each SDK maps every input of `testdata/agentcore-deploy/session-ids.json`
- **THEN** the result equals `expected`, and a call without a session id uses an id matching `newPattern`

#### Scenario: errors of the data plane
- **WHEN** `InvokeAgentRuntime` fails with `ThrottlingException`, `ServiceQuotaExceededException` or `RuntimeClientError`
- **THEN** the SDK raises `RateLimitException`/`RateLimitError`, `QuotaExceededException`/`QuotaExceededError` or `AgentException`/`AgentError` with reason `runtime_error`, with a Spanish message free of ARNs and AWS text

### Requirement: Status, list, stop and delete touch only what Rayito created
`rayito agent status|invoke|stop <name> --target agentcore` SHALL resolve the agent by name in the region; `stop` SHALL require `--session-id` and call `StopRuntimeSession`. `rayito agent list --target agentcore` SHALL list only runtimes whose name starts with `rayito_` and whose description has the marker. `rayito agent delete <name> --target agentcore` (and `AgentCoreFleet.delete()`) SHALL ask for confirmation (`--yes`), delete the owned runtime and wait until it is gone, delete the stack `rayito-agentcore-<name>` and wait, and delete the runtime log group `/aws/bedrock-agentcore/runtimes/<id>-DEFAULT`; a runtime without the marker SHALL raise `name_conflict` and nothing SHALL be deleted; nothing to delete SHALL exit 0 with the `deleteNothing` message.

#### Scenario: delete removes only owned resources
- **WHEN** `rayito agent delete support-bot --target agentcore --yes` runs against fakes holding the owned runtime, its stack and its log group, plus another runtime and another stack
- **THEN** exactly one `DeleteAgentRuntime`, one `DeleteStack` and one `DeleteLogGroup` are recorded, all for `support-bot`

#### Scenario: delete is confirmed
- **WHEN** `rayito agent delete support-bot --target agentcore --json` runs without `--yes`
- **THEN** it exits 2 with `delete_needs_confirmation` and makes no write call

### Requirement: The runtime role is least privilege and the security difference is documented
`infra/agentcore-runtime.yaml` SHALL give the runtime role only: ECR pull on its own repository plus `ecr:GetAuthorizationToken`; log permissions on `/aws/bedrock-agentcore/runtimes/<RuntimeName>-*`; X-Ray; `cloudwatch:PutMetricData` conditioned on the `bedrock-agentcore` namespace; `GetWorkloadAccessToken` and `GetWorkloadAccessTokenForJWT` on its workload identity; Bedrock invoke only on `BedrockModelArns` and Secrets Manager read only on `ReadableSecretArns`, both empty by default. Its trust policy SHALL require `aws:SourceAccount` and `aws:SourceArn`. The plan, the exported README, the deploy guide, `security.md`, `SECURITY.md` and ADR-031 SHALL state that AgentCore has no Rayito secrets gateway and that the agent reads credentials from the runtime role.

#### Scenario: model ARN vectors
- **WHEN** each SDK derives the role resources of every case of `testdata/agentcore-deploy/model-arns.json`
- **THEN** the Bedrock and secret ARNs equal `expected`, in order and without duplicates

#### Scenario: template lint and packaging
- **WHEN** `cfn-lint` and `python3 scripts/gen_stack_assets.py --check` run
- **THEN** both pass and the packaged copies in both SDKs equal `infra/agentcore-runtime.yaml`

### Requirement: Every 0.14 framework deploys to AgentCore
The `agentcore` export SHALL keep the 0.14 tree and add only the `AGENT_BASE_IMAGE` build argument before `FROM` and, for frameworks in `agentcoreEnvCredentialAdapters`, the hash-pinned credential helper (`requirements-aws-credentials.txt`, `aws_env_credentials.py`, `host.json.aws_env_credentials`), so that the host hands the role credentials to the Node bridge per invocation. The `Dockerfile` and `README.md` of the `docker` export SHALL be byte-for-byte unchanged; its `agent_host/` files and, for Node frameworks, `node_bridge.mjs` carry the AgentCore support, which changes the content hash of MicroVM agent images once (the first redeploy after upgrading builds one new image version) and which both CHANGELOGs state.

#### Scenario: export goldens
- **WHEN** both SDKs render the cases of `testdata/agent/export/` and `testdata/agent-samples/expected/export/`
- **THEN** every snapshot matches, including the `ARG AGENT_BASE_IMAGE` line and the README of `deepagents-agentcore`, and the docker-target goldens are unchanged

#### Scenario: AWS acceptance per framework
- **WHEN** the acceptance deploys each of the fifteen projects of `testdata/agent-samples/` with `--target agentcore` in us-east-1
- **THEN** each reaches `READY`, answers the manifest prompt with `42` through `invoke` and through `stream`, continues the same session with the returned id, redeploys without changes as three `reuse` steps, and is deleted leaving no runtime, stack, repository, project, role or log group of the run
