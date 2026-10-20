## Context

This design is the ADR for the `agentcore` deploy target (to be copied as
ADR-031 into `ARCHITECTURE.md`, task D.1). It builds only on pieces that ship
in 0.14 or in `rayito setup` (ADR-030, the base branch of this change):

| Piece | Python | TypeScript | What this change takes from it |
|---|---|---|---|
| Zero-config detection and project | `_agent/_detect_*`, `_project_domain.AgentProject` | `agent/detect-*.ts`, `agent/project.ts` | The project, untouched; name, model, env, file values |
| Deploy facade and plan | `_agent/_deploy_facade.py`, `_deploy_plan.py` | `agent/deploy.ts`, `deploy-plan.ts` | `deploy_agent` / `deployAgent` entry points, `AgentDeployOptions`, `project_for`, `resolved_region`, the step callback |
| Export context | `_agent/_export.render_export(target="agentcore")` | `agent/export.ts` | The exact build context of the image (arm64, port 8080, host with `/ping`, `/invocations` and the session header) |
| Agent host | `_agent/_runner/agent_host/serve.py` | generated `agent/assets/host-assets.gen.ts` | Already serves the AgentCore HTTP contract; reads the session id from `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` when the body has none |
| Response decoding | `_agent/_deployment_base.{invocation_body, result_from_body, EventDecoder, ResultBuilder}`, `_sse.py` | `agent/deployment.ts`, `agent/sse.ts` | The body the host returns is the same on MicroVMs and AgentCore |
| Bucket resolution | `_agent/_aws_resolve.resolve_bucket`, `discover_bucket` (CDK bootstrap bucket, then `rayito-m0-artifacts` of `rayito setup`) | `agent/aws-resolve.ts` | The artifact bucket, never created by deploy |
| CloudFormation port | `_stacks/_port.StackProvisioner`, `_stacks/_service.OptionalStacks`, `stack_tags` | `stacks/port.ts`, `stacks/service.ts` | Create or update the per-agent stack with tags, wait, delete |
| Stack classification | `_account_setup/_domain.classify_stack`, `StackObservation` | `account-setup/domain.ts` | Moved to `_stacks/_condition.py` / `stacks/condition.ts` and reused (D5) |
| `--tag` parsing | `cli/stack.parse_pairs` (used by `rayito setup --tag`) | `cli/tokens.ts` `parsePairs` | The same parser and error |
| Error sanitising | `_aws_sanitize.sanitize_aws_error` | `aws/sanitize.ts` | Every AWS cause |

What does not exist yet: any call to `bedrock-agentcore-control`,
`bedrock-agentcore`, `codebuild` or the ECR API, an image build that does not
go through Lambda MicroVMs, and a public object that invokes an agent through
AWS instead of through the MicroVM endpoint.

## Research findings (official sources, consulted 2026-10-19)

Every parameter below is recorded in `AWS_API_NOTES.md`, «Despliegue en
AgentCore Runtime».

1. **Container contract.** HTTP protocol: host `0.0.0.0`, port 8080,
   `linux/arm64`, `POST /invocations` (JSON or SSE) and `GET /ping` returning
   `{"status": "Healthy" | "HealthyBusy"}`; the session id arrives in
   `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id`
   ([HTTP protocol contract](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-http-protocol-contract.html)).
   The 0.14 host and export already satisfy it.
2. **Control plane** (botocore model `bedrock-agentcore-control/2023-06-05`):
   `CreateAgentRuntime` takes `agentRuntimeName` (`[a-zA-Z][a-zA-Z0-9_]{0,47}`),
   `agentRuntimeArtifact` (`containerConfiguration.containerUri` or
   `codeConfiguration`), `roleArn`, `networkConfiguration`,
   `protocolConfiguration`, `lifecycleConfiguration`, `environmentVariables`
   (≤ 50, values ≤ 5000), `description`, `tags` (≤ 50), `clientToken`
   (idempotency, 33-256). `UpdateAgentRuntime` requires the artifact, role and
   network again and is the only operation with `metadataConfiguration`.
   `ListAgentRuntimes` returns name, id, version, status and description;
   `GetAgentRuntime` returns the full configuration.
3. **MMDSv2 is mandatory since 2026-06-30**: invocations of a runtime without
   `metadataConfiguration.requireMMDSV2=true` fail with `ValidationException`
   ([troubleshooting](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-troubleshooting.html)).
   Because `CreateAgentRuntime` cannot set it, a create must be followed by an
   update.
4. **Lifecycle**: microVM runtimes accept 60-28800 s for
   `idleRuntimeSessionTimeout` and `maxLifetime` (the model says 1209600, which
   applies only to capacity-provider runtimes), idle ≤ max, defaults 900 and
   28800 ([lifecycle settings](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-lifecycle-settings.html)).
5. **Data plane** (`bedrock-agentcore/2024-02-28`): `InvokeAgentRuntime`
   (`agentRuntimeArn`, `runtimeSessionId` 33-256 in the request but 1-100 with
   `[a-zA-Z0-9][a-zA-Z0-9-_]*` in the response, `contentType`, `accept`,
   `payload` ≤ 100 MB) returns a streaming `response`; errors include
   `RuntimeClientError` (424, the container answered 4xx/5xx) and
   `RetryableConflictException` (409, retried by the AWS SDKs).
   `StopRuntimeSession` ends one session's microVM.
6. **Quotas**: 2 GB image, 2 vCPU / 8 GB per session, 15 min per synchronous
   request, 60 min per stream, 25 new sessions/s, 5000 (us-east-1, us-west-2)
   or 2500 active sessions, 1000 agents, 50 TPS control-plane mutations
   ([quotas](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/bedrock-agentcore-limits.html)).
7. **Direct code deployment** exists (`codeConfiguration`: an S3 zip,
   `runtime` one of `PYTHON_3_10`…`PYTHON_3_14` or `NODE_22`, `entryPoint` of
   one or two items, 250 MB zipped / 750 MB unzipped). It runs one managed
   language runtime per agent.
8. **Execution role**: trust `bedrock-agentcore.amazonaws.com` with
   `aws:SourceAccount` and `aws:SourceArn`; ECR pull, the runtime's log group,
   X-Ray, `PutMetricData` in namespace `bedrock-agentcore`, workload access
   tokens, and Bedrock invoke
   ([runtime permissions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-permissions.html));
   «any code running inside the microVM can access execution role credentials
   by calling the metadata endpoint (MMDS)… scope your execution role
   permissions carefully», and deny `GetWorkloadAccessTokenForUserId` when not
   needed ([security best practices](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-security-best-practices.html)).
9. **Regions**: AgentCore Runtime microVMs exist in 21 commercial regions
   ([regions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-regions.html));
   CodeBuild `ARM_CONTAINER` exists in all of them
   ([compute types](https://docs.aws.amazon.com/codebuild/latest/userguide/build-env-ref-compute-types.html)).
10. **Platform version**: omitting `platformVersion` at creation gives V1;
    the [pricing page](https://aws.amazon.com/bedrock/agentcore/pricing/) lists
    V1 at $0.0895 per vCPU-hour and $0.00945 per GB-hour, per second of actual
    use, no CPU charge during I/O wait, memory billed while the session lives.
11. **CodeBuild**: `ARM_CONTAINER` + `BUILD_GENERAL1_SMALL` is 2 vCPU / 4 GiB
    / 64 GB, image `aws/codebuild/amazonlinux-aarch64-standard:3.0`
    ([images](https://docs.aws.amazon.com/codebuild/latest/userguide/ec2-compute-images.html));
    `StartBuild.idempotencyToken` is valid for 5 minutes
    ([StartBuild](https://docs.aws.amazon.com/codebuild/latest/APIReference/API_StartBuild.html));
    $0.0034 per minute rounded up, 100 free minutes per month (Price List API,
    publication 2026-09-11, and the [CodeBuild pricing page](https://aws.amazon.com/codebuild/pricing/)).
12. **The Vercel AI SDK's Bedrock provider cannot use the role by itself**:
    the published `@ai-sdk/amazon-bedrock` has no AWS credential provider in its
    dependency tree (`testdata/agent-samples/typescript/vercel-ai-sdk/package-lock.json`
    lists none, while every other TypeScript sample pulls
    `@aws-sdk/credential-provider-node` through its Bedrock client); it reads
    `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/`AWS_SESSION_TOKEN` or a bearer
    token. On MicroVMs the secrets gateway hands it a bearer token; on AgentCore
    nothing does.

## Decisions

### D1. A new target on the existing entry points, private service behind ports

`deploy_agent`, `deploy_agent_async`, `plan_deploy` (Python, keyword-only
`target: Literal["microvm", "agentcore"] = "microvm"` and
`agentcore: AgentCoreOptions | None = None` after `session`) and `deployAgent`,
`planDeploy` (TypeScript, `DeployAgentOptions.target` and
`DeployAgentOptions.agentcore`) dispatch on the target. `microvm` runs the 0.14
code path unchanged. `agentcore` runs a new private application service
(`_agentcore/_service.py` + `_service_async.py`; `agentcore/service.ts`) over
ports, with a pure domain:

| Layer | Python `rayito/_agentcore/` | TypeScript `src/agentcore/` |
|---|---|---|
| Domain (pure: no boto3, no `@aws-sdk/*`, no I/O) | `_domain.py`: names, validation, option table, session ids, image tag, Bedrock and secret ARNs, plan, apply decisions, errors | `domain.ts` |
| Models | `_models.py`: `AgentCoreOptions`, `AgentCoreDeployPlan`, `AgentCoreStatus`, `AgentCoreDeleteResult`, step and observation records | `models.ts` |
| Ports | `_ports.py`: `RuntimeControl`, `RuntimeInvoker`, `ImageBuilder`, `ImageRegistry`, `SourceStore`, `RuntimeLogs`, `AccountIdentity`, plus the existing `StackProvisioner` | `ports.ts` |
| Application | `_service.py` / `_service_async.py` (`AgentCoreDeployer`, `AgentCoreDeleter`, `list_agents`) | `service.ts` |
| Adapters | `_aws.py` (boto3 clients: `bedrock-agentcore-control`, `bedrock-agentcore`, `codebuild`, `ecr`, `logs`, `s3`, `sts`), `_stack.py` (the `agentcore-runtime` `StackComponent`) | `aws.ts`, `stack.ts` |
| Public surface | `_fleet.py` / `_fleet_async.py` (`AgentCoreFleet`, `AsyncAgentCoreFleet`, the stream classes) | `fleet.ts` |
| CLI | `cli/_agentcore.py` (plan, output, `delete`, `list`), wired from `cli/agent.py` | `cli/agentcore.ts`, wired from `cli/registry.ts` |

Adapters translate every AWS error by code (`aws_code` / `awsCode`) through
`contract.json.awsErrorMap` into `AgentCoreException` / `AgentCoreError` (a
closed `code`, `exit_code` / `exitCode`), `RateLimitException`,
`QuotaExceededException` or `AgentException`; the cause goes through
`sanitize_aws_error`. Messages are the Spanish templates of the contract and
never carry an ARN, a URL, a token or AWS's raw message.

`AgentCoreException` is public (the SDK raises it from public calls) and does
not inherit from `SandboxException`: nothing here is attributable to a sandbox
(same reasoning as `AccountSetupException`, ADR-030 D7).

### D2. Build strategy: container image built by CodeBuild, not direct code deployment

Decision: **container deployment** with the image built by **AWS CodeBuild**
(`ARM_CONTAINER`, `BUILD_GENERAL1_SMALL`) from the 0.14 export context, pushed
to a per-agent ECR repository.

Why not direct code deployment (finding 7): one managed runtime per agent
cannot run what the zero-config host needs. Every TypeScript framework runs as
the Python host plus a Node bridge (ADR-029 D4) — two runtimes in one
microVM; OpenCode and Hermes install pinned binaries and interpreters; the 0.14
export installs hash-pinned Linux arm64 wheels and a pinned Node tarball. A
code zip would have to be assembled for arm64 on the developer's machine
(native wheels for macOS or x86 would be wrong) and would bypass the export
that 0.14 already tests byte for byte in both SDKs. The image path reuses that
export unchanged (D9), so the image that `rayito agent export --target
agentcore` writes and the image `deploy` runs are the same.

Why CodeBuild and not local Docker: the requirement is no local Docker, and
cross-building arm64 on x86 needs buildx/QEMU. CodeBuild builds natively on
Graviton in every AgentCore region (finding 9), the AgentCore troubleshooting
guide itself recommends it for arm64 builds, and it costs about $0.02-0.03 per
build after the 100 free minutes.

The base image is the export's pinned `python:3.12-slim-bookworm@sha256:…`.
CodeBuild pulls it from the ECR Public mirror of the Docker official images
(`public.ecr.aws/docker/library/python`, same digest) through the new
`AGENT_BASE_IMAGE` build argument, because anonymous Docker Hub pulls from
CodeBuild's shared addresses are rate-limited; the digest makes the two
sources byte-identical.

### D3. Per-agent infrastructure in one CloudFormation stack; the runtime through the API

`infra/agentcore-runtime.yaml` (cfn-lint clean) is deployed as
`rayito-agentcore-<name>` through `OptionalStacks.deploy` with a
`StackComponent` that is **not** in the `rayito stack` catalog (like the IAM
and bucket stacks of `rayito setup`, ADR-030 D3): ECR repository (immutable
tags, scan on push, keeps the last 5 images, `EmptyOnDelete`), CodeBuild
project, its log group (14 days), the build role and the runtime role.
`CAPABILITY_IAM` (unnamed roles); `Tags` = the three fixed tags plus `--tag`,
so tag-enforcing SCPs accept every resource.

The runtime itself is created through `CreateAgentRuntime`/`UpdateAgentRuntime`
and not as an `AWS::BedrockAgentCore::Runtime` resource, because (a) the image
must exist before the runtime and the repository must exist before the image,
so a stack-managed runtime needs two stack updates per deploy; (b) the deploy
must wait for `READY`, report the runtime version and enforce MMDSv2, which
the API gives directly; (c) idempotent update by name with a `clientToken` is
straightforward through the API.

### D4. Names

The agent name is `template_name(project)` (`[image].template` or the project
name), validated against `^[a-z0-9]+(?:[_-][a-z0-9]+)*$` and ≤ 41 characters
(`name_invalid`) so every derived name fits: runtime `rayito_<name>` with `-`
as `_` (≤ 48), stack `rayito-agentcore-<name>`, repository
`rayito/agentcore/<name>`, CodeBuild project `rayito-agentcore-<name>`, source
key `rayito/images/agentcore/<name>/<tag>.zip` (inside the `rayito/images/`
prefix that the setup bucket expires after 7 days), description
`Rayito agent <name> (rayito.agentcore/1)`. Ownership of a runtime is the
name **and** the marker `rayito.agentcore/1` in its description: a runtime
with the name and without the marker is `name_conflict`, never touched.

### D5. Plan without AWS; decisions at apply time from fresh observations

`plan_deploy(target="agentcore")`, `planDeploy` and `--dry-run` make **no AWS
call and load no AWS client** (the 0.14 rule for the plan, ADR-029 D9). The
plan validates everything that can be validated offline (D6), resolves the
bucket only from explicit sources (argument, `[deploy].bucket`,
`RAYITO_BUCKET`; otherwise `discovered`), and shows three `planned` steps
(stack, image, runtime), the security section, the warnings and the cost.
Vectors: `testdata/agentcore-deploy/plan-vectors.json`.

Apply (after confirmation) observes and decides, in order
(`apply-vectors.json`):

1. `GetCallerIdentity`; `HeadBucket` on the resolved bucket (or discovery);
   a bucket in another region is `bucket_region` (CodeBuild reads S3 sources
   only in its region).
2. **Stack** (`DescribeStacks`, classified with the shared
   `classify_stack`): missing → `create`; usable → `update` (CloudFormation
   answers «No updates are to be performed» when nothing changed, reported as
   `reuse`); in progress → `wait`, then observe again; blocked →
   `stack_blocked`; usable without the six outputs → `stack_failed`.
3. **Image** (`DescribeImages` with the content tag): exists → `reuse`;
   missing → `build` (upload the zip, `StartBuild` with
   `idempotencyToken=<tag>`, poll `BatchGetBuilds` every 10 s up to the build
   timeout, delete the zip). `build_image=False` with no image →
   `image_missing`; `True` behaves like the default (the tag is the content).
   After build or reuse, an image larger than 2 GB → `image_too_large`.
4. **Runtime** (`ListAgentRuntimes`, then `GetAgentRuntime`): none →
   `create` (+ wait `READY` + `UpdateAgentRuntime` with `requireMMDSV2` + wait);
   not owned → `name_conflict`; `CREATING`/`UPDATING` → `wait`, then decide
   again; `CREATE_FAILED`, `DELETING`, `DELETE_FAILED` → `runtime_blocked`;
   `UPDATE_FAILED` → `update`; `READY` → `reuse` when every field of
   `runtimeComparedFields` equals the desired configuration, else `update`.
   A `ConflictException` on create (a concurrent deploy won the race) lists
   again and updates.

### D6. Idempotency

- The image tag is the content: `src-` + the first 16 hex characters of the
  sha256 of the export tree, framed as `contract.json.constants.imageTagDigest`
  (vectors in `image-tags.json`). The same project gives the same tag in both
  SDKs, because both render the same tree (export goldens).
- `clientToken` of `CreateAgentRuntime`/`UpdateAgentRuntime` is `rayito-` + 32
  hex characters of the sha256 of the canonical request: a retried request
  never creates a duplicate.
- `StartBuild.idempotencyToken` is the tag: a second deploy within 5 minutes
  attaches to the same build.
- A redeploy of an unchanged project performs a no-op `UpdateStack`, no build,
  no runtime write, and reports `reuse` three times.
- An interrupted deploy resumes: the stack in progress is waited for, the zip
  is uploaded again, the runtime in `CREATING`/`UPDATING` is waited for.

### D7. Invocation and session mapping (same API as `AgentFleet`)

`AgentCoreFleet.invoke(prompt, session_id=None, model=None, reasoning=False)`
and `stream(...)` (TypeScript `invoke(prompt, { sessionId, model, reasoning })`)
return the same `AgentResult` / event stream as `AgentFleet`. The body is
`invocation_body(prompt, session_id=None, model, reasoning)` (the session goes
only in the header; the host reads the header when the body has none) and the
response is decoded with the existing `result_from_body` / `EventDecoder`.
`accept` is `application/json` for `invoke` and `text/event-stream` for
`stream`. Read timeouts: 900 s for `invoke`, 3600 s for `stream` (quotas).

Session ids (`session-ids.json`): no id → `rayito-` + 32 hex of a uuid4; an id
that already matches `[a-zA-Z0-9][a-zA-Z0-9_-]*` with 33-100 characters is
kept; any other non-empty id becomes `rayito-` + 32 hex of its sha256, so the
same caller id always reaches the same session. `result.session_id` is the id
actually used; passing it back continues the conversation, exactly like the
routable handle of a MicroVM fleet.

### D8. Error mapping

`contract.json.awsErrorMap` is normative. Summary: control-plane
`AccessDeniedException` → `access_denied`, `ResourceNotFoundException` →
`runtime_not_found`, others → `aws_error` with the operation and the code;
CodeBuild `AccountLimitExceededException` → `build_quota`; build terminal state
other than `SUCCEEDED` → `image_build_failed` (with the build id and log
group), poll timeout → `image_build_timeout`; ECR `ImageNotFoundException` →
image absent; invoke `ThrottlingException` → `RateLimitException`,
`ServiceQuotaExceededException` → `QuotaExceededException`,
`RuntimeClientError` → `AgentException(reason="runtime_error")`,
`ResourceNotFoundException`/`AccessDeniedException`/`ValidationException` →
`AgentCoreException`. Exit codes: `errorExitCodes` (2 for usage and
environment, 1 for AWS-side failures).

### D9. The image: the 0.14 export plus two AgentCore additions

`render_export(project, target="agentcore")` keeps everything it writes
today and adds, for this target only (the `docker` target is unchanged):

1. `ARG AGENT_BASE_IMAGE=<pinned reference>` before `FROM`, and
   `FROM --platform=linux/arm64 ${AGENT_BASE_IMAGE}` (D2).
2. For a framework in `agentcoreEnvCredentialAdapters` (today `vercel-ai`,
   finding 12): a hash-pinned botocore venv at
   `/opt/agents/rayito/aws-credentials` (`requirements-aws-credentials.txt`),
   the helper `/opt/agents/rayito/aws_env_credentials.py`, and
   `host.json.aws_env_credentials = {python, script}`. Before each invocation
   the host runs the helper (botocore's default chain reads the role from the
   microVM metadata service; the helper knows no endpoint), and the Node
   bridge puts the three variables in `process.env` for that invocation. A
   failure ends the invocation with `agent_failed` and
   `detail_code="aws_credentials_unavailable"`.
3. The README's AgentCore section explains `rayito agent deploy --target
   agentcore`, MMDSv2 and the security difference (golden:
   `testdata/agent/export/deepagents-agentcore/expected/README.md`).

Runtime environment variables: the project's `env` plus `AWS_REGION` and
`AWS_DEFAULT_REGION` set to the deploy region (they win). Never secrets.

### D10. Fleet options on AgentCore: explicit table, never a silent approximation

`contract.json.optionsTable` is normative. A MicroVM option passed **in the
call** (SDK argument or CLI flag) that has no AgentCore meaning raises
`option_not_supported` before any AWS call: `min_vms`, `max_vms`, `warm_vms`,
`scale_in_after_seconds`, `--memory`, `--port`, `--timeout`, `--idle`,
`secret_names`/`--secret`, `create_secret_from_env`, `access_token`/
`--token-file`, `keep_on_failure`, base-image options, `session_index`,
`pool_backend`, `warm_timeout_seconds`. The same value coming from the
**project file** (shared between targets) is a warning in the plan and is
ignored: `[scaling]`, `memory_mib`, `[serve].port|timeout|idle`,
`[deploy].base_image_version`. `max_concurrency`, `queue_max`,
`queue_timeout_seconds`, `bucket`, `build_image`, `build_timeout_seconds`,
`on_step`, `on_build_logs` and `env` apply. `sessions` applies as
"dedicated" (stronger isolation, stated in Security). A project egress or
network policy is an error (`network_policy_unsupported`): PUBLIC mode does
not filter egress, and applying half of a policy would be a silent risk. A
model provider other than `bedrock` is `provider_unsupported` (without a
gateway its key would sit in the runtime configuration in clear).

### D11. Security difference, stated everywhere

On MicroVMs the agent never sees the model credential (ADR-023, ADR-025). On
AgentCore there is no Rayito secrets gateway: the agent obtains credentials
from the runtime role through the microVM metadata service, and any code in
the session can read them (finding 8). Compensation: least privilege. The
runtime role gets Bedrock invoke only on the resolved model ARNs
(`model-arns.json`: an inference-profile id gets the profile ARN in the model
region plus the foundation model in any region, and a `global.` profile also
the region-less foundation-model ARN) and Secrets Manager read only on
`--readable-secret` ARNs; nothing else beyond the documented runtime basics
(ECR pull of its repository, its own log group, X-Ray, metrics in
`bedrock-agentcore`, workload access tokens without `ForUserId`). MMDSv2 is
always on. The plan's «Seguridad» section, the exported README, the guide,
`security.md`, `SECURITY.md` and ADR-031 say so in the same words.

### D12. CDK construct: deferred

A Rayito CDK construct would be a third package (jsii, its own release
pipeline and versioning, published to npm and PyPI) wrapping the AWS CDK L2
`aws_bedrockagentcore.Runtime`, which already exists in `aws-cdk-lib` and
accepts an ECR image or a local asset. Large and independent of the CLI and
SDKs: it goes to a follow-up change. Nothing in this change blocks it: the
template, the export context and the session rules are reusable.

### D13. Cost and activation

Only `target="agentcore"` (or `--target agentcore`) creates anything; without
it no AgentCore, CodeBuild or ECR client is created or loaded, with a test in
each SDK. The plan shows the cost lines of the contract (V1 runtime prices,
CodeBuild per minute with the free tier, ECR storage, logs ingestion, the
model billed by its provider). The `deploy_agent` / `deployAgent` docstrings,
`AgentCoreFleet` and `AgentCoreOptions` carry the «Coste y activación» block;
TypeScript registers `scripts/cost-declarations/agentcore-deploy.json`. Docs:
the box on the guide and a row in `optional-features.md`.

## Apply sequence

```
validate (D6/D10, no AWS) -> plan -> print plan + security + warnings + cost
  -> --dry-run stops | confirmation (--yes or terminal, else needs_confirmation)
  -> GetCallerIdentity -> bucket (HeadBucket / discovery)
  -> stack: DescribeStacks -> CreateStack|UpdateStack -> wait (DescribeStacks)
  -> image: DescribeImages -> [PutObject zip -> StartBuild -> BatchGetBuilds* -> DeleteObject] -> DescribeImages (size)
  -> runtime: ListAgentRuntimes -> GetAgentRuntime -> Create|Update -> GetAgentRuntime* (READY)
             -> (after create) UpdateAgentRuntime(requireMMDSV2) -> GetAgentRuntime* (READY)
  -> AgentCoreFleet (runtime ARN, id, version, image URI, log group)
```

Delete: `ListAgentRuntimes` (owned check) -> `DeleteAgentRuntime` ->
`GetAgentRuntime*` until `ResourceNotFoundException` (600 s) -> `DeleteStack`
-> wait deleted (the repository empties itself) -> `DeleteLogGroup` of
`/aws/bedrock-agentcore/runtimes/<id>-DEFAULT`. Nothing found → «nada que
borrar», exit 0. Source zips never outlive their build.

## Alternatives considered

- **Direct code deployment**: rejected (D2): one language runtime per agent,
  arm64 dependencies assembled on the developer's machine, and a second
  packaging path diverging from the tested export.
- **Local Docker / buildx**: rejected by the requirement; also slow and
  fragile under QEMU on x86.
- **The AgentCore starter toolkit or AgentCore CLI as a dependency**: a third
  tool with its own config files and IAM policies «designed for development»
  (runtime permissions page); Rayito would lose the shared contract and the
  least-privilege role.
- **Runtime as a CloudFormation resource**: rejected (D3).
- **Fixed source key `source.zip`**: concurrent deploys of two versions
  would overwrite each other; content-addressed keys plus deletion after the
  build cost nothing.
- **Mapping MicroVM options approximately** (`max_vms` → session quota,
  `--idle` → idle timeout): rejected; different semantics silently. The
  AgentCore equivalents are explicit options.
- **Passing a non-Bedrock model key as a runtime environment variable**:
  rejected; it would be readable in `GetAgentRuntime` and by the agent.
  AgentCore Identity could hold API keys, but that is a separate feature.
- **Requiring MMDSv2 only when invocations fail**: rejected; since 2026-06-30
  a runtime without it cannot be invoked at all.

## Consequences

- One command takes a 0.14 project to AgentCore; the same project still
  deploys to MicroVMs with no change. The exported AgentCore context and the
  deployed image are the same artifact.
- Rayito now calls four more AWS services (AgentCore control and data,
  CodeBuild, ECR) and CloudWatch Logs `DeleteLogGroup`, all behind ports and
  documented in `AWS_API_NOTES.md`.
- The `agentcore` export Dockerfile changes (base-image argument, README);
  the `docker` export is unchanged.
- Users of AgentCore lose the gateway guarantee; the plan, README and docs
  say so, and the role is least privilege.
- `rayito agent delete` exists only for AgentCore; MicroVM fleets keep
  `rayito agent stop`.

## Risks

- ECR reports the compressed image size; if AgentCore enforces 2 GB on the
  uncompressed image, a large agent fails at runtime creation, not at the
  pre-check. The acceptance records the measurement (new Q row).
- OpenCode's Bedrock provider credentials path was not verifiable offline. The
  acceptance showed it only starts the provider with `AWS_PROFILE` or an
  access-key, bearer-token, web-identity or container-credentials variable, none
  of which AgentCore sets (the role comes from the microVM metadata service), so
  `opencode` is listed in `agentcoreCredentialProcessRuntimes`: the export adds
  the botocore helper plus an AWS config file with a `credential_process`
  profile and sets `AWS_CONFIG_FILE`/`AWS_PROFILE` in the image (Q180).
- New accounts may have a CodeBuild concurrency quota of one ARM build:
  `build_quota` says so; the acceptance runs builds three at a time and checks
  the quota first.
- A tag-enforcing SCP that requires tags on `bedrock-agentcore:CreateAgentRuntime`
  is covered (`tags` on create); tags on an existing runtime are not updated
  (no `TagResource` in this change).
- The ECR Public mirror could lag a digest; the digest pin makes the build
  fail loudly rather than drift.
