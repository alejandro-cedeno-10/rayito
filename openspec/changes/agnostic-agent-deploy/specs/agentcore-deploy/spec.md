## MODIFIED Requirements

### Requirement: The plan is computed without AWS and states security, warnings and cost
With `target="agentcore"`, `plan_deploy`, `planDeploy` and `rayito agent deploy --dry-run` SHALL make no AWS call and load no AWS client. They SHALL validate, in the order of `contract.json.optionsTable.description`, the region against `agentcoreSupportedRegions` (`unsupported_region`), the MicroVM options passed in the call (`option_not_supported`), the model provider (`provider_unsupported` unless `bedrock`, or unless `external_model` is set), the project network policy (`network_policy_unsupported`), the agent name (`name_invalid`), the lifecycle values (`lifecycle_invalid`, `lifecycle_order`), the tags (`tags_invalid`, counting the three fixed stack tags) the environment (`env_invalid`, counting the two region variables and the variables Rayito adds) and the IAM resources (`iam_resource_invalid`: a readable secret, a secret referenced by `secret_env` or `secret_env_from`, a Bedrock model or the project model of a `bedrock` provider that is not a plain name or id without `*`, `?` or `,`, or an ARN that does not name the deployment's partition, region and account, per `contract.json.constants.iamResourceRules`; the check repeats with the account when the role is built). Right before the IAM check, a `bedrock` provider without a project model id and without `bedrock_models` SHALL raise `model_required`. The plan SHALL list three `planned` steps (stack, image, runtime), the derived names, the bucket and its source (`argument`, `project_file`, `environment` or `discovered`), the Bedrock models, the readable secrets, the runtime environment, the effective lifecycle, the tags, the security lines, the warnings for project-file values that do not apply, and the cost lines of the contract.

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
- **THEN** its «Seguridad:» section starts with the `noGateway` line of the contract, followed by the Bedrock models line, the readable secrets line (when any), the `externalModel` line (when set), the `secretEnv` line (when used), `publicNetwork` and `sessionIsolation`

#### Scenario: Bedrock without a model
- **WHEN** a project with a `bedrock` provider and no `[model] id` is planned without `bedrock_models`
- **THEN** both SDKs raise `model_required`, as in `testdata/agent-model-optional/requirement.json`

## ADDED Requirements

### Requirement: An external model on AgentCore is an explicit opt-in
`AgentCoreOptions.external_model` (default `False`; `--external-model`; CDK `externalModel`) SHALL allow a non-`bedrock` model provider. With it, the project model SHALL NOT be added to the Bedrock models nor checked as an IAM resource, so without `bedrock_models` the role has no Bedrock statement; when the project declares `base_url_env` and the gateway has an `upstream`, the runtime environment SHALL carry `base_url_env` equal to the upstream plus `base_path`. No model key SHALL ever be written to `environmentVariables`. The plan and `plan.external_model` SHALL state it with the `externalModel` security line. Passing it with `target="microvm"` SHALL raise `option_requires_agentcore`. Without the option every existing plan SHALL be unchanged.

#### Scenario: OpenAI-compatible gateway on AgentCore
- **WHEN** a `langgraph` project with an `openai-compatible` gateway, upstream `https://llm-gateway.example.com` and `base_url_env = "LLM_BASE_URL"` is planned with `external_model=True`
- **THEN** the plan has no Bedrock models, its security lines include `noBedrockModels` and `externalModel`, its environment has `LLM_BASE_URL`, and the CDK template has no `bedrock:InvokeModel` statement

### Requirement: Secret environment variables on AgentCore reuse the readable secrets
When the project uses `secret_env` or `secret_env_from`, the plan SHALL add `RAYITO_SECRET_ENV` to the runtime environment with the compact spec of `testdata/agent-secret-env/spec.json` (names and ARNs only, never values), SHALL union every referenced secret into the readable secrets passed as `ReadableSecretArns` (so the template, its parameters and the CDK nested stack do not change), SHALL list the variable names in `plan.secret_env_names` and SHALL add the `secretEnv` security line; a spec longer than `agentcoreEnvironmentValueMaxChars` SHALL raise `env_invalid`. The exported image SHALL add the helper and the `host.json` key only in that case. Without secret env, the environment, parameters, export and image tag SHALL be unchanged.

#### Scenario: union of secrets
- **WHEN** both SDKs evaluate every case of `testdata/agent-secret-env/spec.json`
- **THEN** `RAYITO_SECRET_ENV` and the readable secret ARNs equal `expected`

#### Scenario: least privilege
- **WHEN** the rendered template is checked with the apply parameters of a secret-env deployment
- **THEN** the `ReadSecrets` statement has exactly `secretsmanager:GetSecretValue` on exactly the expected ARNs, and no `kms:*`, `ListSecrets` or `secretsmanager:*` appears
