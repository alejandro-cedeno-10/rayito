## Context

This design is the ADR for the CDK constructs of Rayito (copied as ADR-032
into `ARCHITECTURE.md`; it supersedes ADR-031 D12, "Sin CDK por ahora"). It
builds only on pieces that already ship in 0.15:

| Piece | Python | TypeScript | What the constructs take from it |
|---|---|---|---|
| AgentCore plan (pure) | `_agentcore/_domain.build_plan`, `bedrock_resource_arns`, `secret_resource_arns`, `image_tag_for`, `source_zip` | `agentcore/domain.ts` `buildPlan`, `arns.ts`, `identifiers.ts`, `source-zip.ts` | Names, lifecycle, environment, IAM inputs, tag limit, image tag, the deterministic source zip |
| AgentCore export | `_agent/_export.render_export(target="agentcore")` | `agent/export.ts` `renderExport` | The exact build context (Dockerfile, host, credentials helpers) |
| Per-agent template | `infra/agentcore-runtime.yaml` (packaged by `scripts/gen_stack_assets.py`) | same | Repository, build log group, build role, CodeBuild project, runtime role: identical IAM |
| Image publish (pure) | `cli/_publish.py` (`IMAGE_HOOKS`, `default_image_name`, `require_efs_combination`, `desired_configuration`, `sized_settings`) | `images/publish-domain.ts` (`desiredConfiguration`, `sizedSettings`, `validateSizes`, …) | The MicroVM image configuration, byte for byte the one `rayito image publish` sends |
| Artifact markers | `cli/_artifact.marker_variant`, `marker_has_efs` | `images/artifact-node.ts` `inspectArtifact` | The variant and EFS checks of the zip |
| Release download | `_account_setup/_release.GithubReleaseSource`, `_domain.verify_checksum` | `account-setup/release.ts`, `domain.ts` | `rayito-image.zip` verified against `SHA256SUMS` |
| Optional dependencies | `_optional.require_module`, extras | `optional.ts`, optional peers, `tsdown` entries | The lazy, cost-free loading pattern (ADR-014) |

What does not exist yet: any CDK code, a CloudFormation path for the
AgentCore runtime or the MicroVM image, and a way for the CLI to recognise a
resource that CloudFormation owns.

## Research findings (verified 2026-10-20)

Every fact below is recorded in `AWS_API_NOTES.md` («Constructos CDK»,
Q181-Q186). Account ids are masked; the schemas were read with
`aws cloudformation describe-type --type RESOURCE` in us-east-1 (read only).

1. **`AWS::BedrockAgentCore::Runtime`** (Q181). Properties:
   `AgentRuntimeArtifact`, `AgentRuntimeName`, `RoleArn` (required),
   `AuthorizerConfiguration`, `CapacityProviderConfiguration`, `Description`,
   `EnvironmentVariables`, `FilesystemConfigurations`,
   `LifecycleConfiguration`, `NetworkConfiguration`, `PlatformVersion`,
   `ProtocolConfiguration`, `RequestHeaderConfiguration`, `Tags`. Read-only:
   `AgentRuntimeArn`, `AgentRuntimeId` (primary identifier),
   `AgentRuntimeVersion`, `Status`, `CreatedAt`, `LastUpdatedAt`,
   `FailureReason`, `WorkloadIdentityDetails`. Create-only:
   `AgentRuntimeName`. **There is no metadata or MMDS property anywhere in the
   schema** (no `metadata`/`mmds` string at all). The schema is stricter than
   the API on environment values (≤ 2048 characters, not 5000) and on the
   description (≤ 1200). Tagging: `tagOnCreate`,
   `tagUpdatable`. The create handler also creates the `DEFAULT` endpoint and
   the workload identity; the update handler calls `UpdateAgentRuntime`.
2. **`AWS::Lambda::MicrovmImage`** (Q182). Required: `Name`, `BaseImageArn`,
   `BaseImageVersion`, `BuildRoleArn` (pattern with a 12-digit account),
   `Description`, `CodeArtifact{Uri}`, `Logging`, `EgressNetworkConnectors`
   (≤ 10), `CpuConfigurations` (`ARM_64`), `Resources` (≤ 1,
   `MinimumMemoryInMiB`), `AdditionalOsCapabilities` (`ALL`), `Hooks`,
   `EnvironmentVariables` (≤ 50, list of `Key`/`Value`). Optional: `Tags`.
   Create-only: `Name` (a change replaces the image). Every other input is
   write-only. Read-only: `ImageArn` (primary identifier), `State`,
   `LatestActiveImageVersion`, `LatestFailedImageVersion`, `CreatedAt`,
   `UpdatedAt`. Provisioning type `FULLY_MUTABLE`. Handlers: create
   (`CreateMicrovmImage`, `GetMicrovmImage`, `PassNetworkConnector`,
   `iam:PassRole`), update (`UpdateMicrovmImage`, `GetMicrovmImageVersion`,
   **`DeleteMicrovmImageVersion`**, …), delete (`DeleteMicrovmImage`). The
   resource documentation says the build is asynchronous (`CREATING` →
   `CREATED` | `CREATE_FAILED`).
3. **`aws-cdk-lib` boundaries** (unpkg, Q184): `CfnRuntime` first in 2.217.0;
   the AgentCore L2 (`Runtime`, `AgentRuntimeArtifact`, …) in 2.255.0;
   `CfnMicrovmImage` in **2.261.0** (absent in 2.260.0). 2.261.0 requires
   `constructs ^10.5.0` and Node ≥ 20; on PyPI it requires Python ≥ 3.10 and
   `jsii>=1.133`. Both L1 classes implement `ITaggableV2`.
4. **botocore** 1.43.103 (`bedrock-agentcore-control/2023-06-05`):
   `UpdateAgentRuntime` has `metadataConfiguration.requireMMDSV2`,
   `CreateAgentRuntime` does not, `GetAgentRuntime` returns
   `metadataConfiguration` (Q183). The Lambda Python runtime ships its own,
   possibly older, boto3, so the handler bundles that model (D13).
5. **`GetMicrovmImage`** returns `tags` (`docs/aws-api/service-2.json`,
   `GetMicrovmImageResponse`), which is how the CLI recognises a CDK image
   without a description marker (D6).

Measured by the AWS acceptance (task 3), recorded as Q185 and Q186: runtime
versions that CloudFormation creates or updates already have `requireMMDSV2`
on, so the custom resource finds nothing to do; CloudFormation finishes the
image create only once the version is usable (a sandbox started from it right
after). Still open: whether an image update deletes earlier versions.

## Decisions

### D1. Two native constructs inside the existing packages (no jsii)

`rayito/cdk` (npm subpath export) and `rayito.cdk` (PyPI, extra `cdk`). Same
version, tags and publish jobs as the SDKs; no new trusted publisher, no
`release.yml` change. Python/TS parity comes from the shared contract
`testdata/agentcore-cdk/` (normalised golden templates) and
`scripts/check_sdk_parity.py`, as for the rest of the SDK. jsii is rejected:
it pins TypeScript ~6 (the repo uses 7), needs a third release stream and
would make Python users run the TypeScript SDK under Node to reuse the
planning code. Construct Hub listing is the only loss.

### D2. Stable `aws-cdk-lib` ≥ 2.261.0, L1 resources

Peers: `aws-cdk-lib >=2.261.0 <3`, `constructs >=10.5.0 <11`
(`limits.json` `cdkAwsCdkLibMinVersion`, `cdkConstructsMinVersion`). The floor
is `CfnMicrovmImage`'s; the AgentCore L2 alone would allow 2.255.0, but one
floor for one module is simpler and both constructs ship together. Both
constructs use the L1 classes (`CfnRuntime`, `CfnMicrovmImage`) for full
control of every property; `RayitoAgent.runtime` returns an `IRuntime` from
`Runtime.fromAgentRuntimeAttributes` for interoperability with the L2. Never
the alpha package.

### D3. `RayitoAgent`: the CLI's resources, through CloudFormation

Props mirror `AgentCoreOptions` so a project means the same thing in both
paths (contract `sdk.*.RayitoAgent`). Per instance:

1. **Nested stack `Build`** with `infra/agentcore-runtime.yaml` loaded by
   `cloudformation_include.CfnInclude` (id `Template`), every parameter
   folded in exactly like `_stack_parameters` / `#stackParameters`
   (`ArtifactBucket` = the bootstrap asset bucket). IAM is identical by
   construction; two agents in one stack never collide on logical ids.
2. **`Source`**: `s3_assets.Asset` of the deterministic `source_zip`; the
   build role gets one extra `s3:GetObject` on that object key (the template
   only allows `rayito/images/agentcore/<name>/*`).
3. **`Provider`** (`custom_resources.Provider`) with the two functions
   `OnEvent`/`IsComplete` under `Handler`, from the shared handler zip (D13);
   total timeout = build timeout + `cdkBuildTotalTimeoutMarginMinutes`, query
   interval `cdkBuildQueryIntervalSeconds`; log groups with 14-day retention.
4. **`ImageBuild`** (`Custom::RayitoImageBuild`): skip if the tag exists in
   ECR (immutable tags), otherwise `StartBuild` with
   `sourceLocationOverride`, `IMAGE_TAG`, `AGENT_BASE_IMAGE` and
   `idempotencyToken = <tag>`, poll `BatchGetBuilds`, then the 2 GB check.
   IAM: `codebuild:StartBuild`/`BatchGetBuilds` on that project,
   `ecr:DescribeImages` on that repository.
5. **`Runtime`** (`CfnRuntime`): `AgentRuntimeName = runtime_name_for(name)`,
   `ContainerUri = <RepositoryUri>:<tag>`, the runtime role, `PUBLIC`,
   `HTTP`, lifecycle and environment from the plan, `Description` with the
   `+cdk` marker (D6). Depends on `ImageBuild`. The construct refuses an
   environment value longer than `cdkRuntimeEnvironmentValueMaxChars` (2048,
   the resource schema's cap) with the plan's `env_invalid`, at synth.
6. **`Mmds`** (`Custom::RayitoRuntimeMmds`, D5).

`localDocker()` replaces 2 and 4 with a `DockerImageAsset` (`Platform.LINUX_ARM64`,
id `Image`) and `grantPull` for the runtime role, and removes the
Repository, BuildRole, BuildProject and BuildLogGroup from the include.

Attributes: `agentName`, `runtimeName`, `runtimeArn`, `runtimeId`,
`runtimeVersion`, `roleArn`, `repositoryUri`, `imageUri`, `runtime`; method
`grantInvoke(grantee)` (`bedrock-agentcore:InvokeAgentRuntime` on the runtime
ARN). `CfnOutput`s only with `exportOutputs: true`.

### D4. Image build: CodeBuild by default (maintainer decision)

`AgentImageBuild.codeBuild({ timeoutMinutes? })` is the default: no local
Docker, the same image as the CLI, costs CodeBuild minutes.
`AgentImageBuild.localDocker()` needs Docker or Finch with arm64 at
`cdk deploy` time and costs nothing in CodeBuild.

### D5. MMDSv2 through a custom resource

The resource schema has no metadata property (Q181) and `CreateAgentRuntime`
cannot set it, so `Custom::RayitoRuntimeMmds` reads `GetAgentRuntime` and, if
`metadataConfiguration.requireMMDSV2` is not true, sends `UpdateAgentRuntime`
with the configuration it read plus `requireMMDSV2=true` and a deterministic
`clientToken` (sha256 of id and version); it waits for `READY`. Its
`AgentRuntimeVersion` property (`GetAtt`) makes it run again whenever
CloudFormation creates a new runtime version, so a reset by a later update
would be repaired in the same deployment (Q185: none seen, CloudFormation's
versions already require MMDSv2). IAM:
`bedrock-agentcore:GetAgentRuntime`/`UpdateAgentRuntime` on that runtime and
`iam:PassRole` on the runtime role only. Only that out-of-band update, if it
ever happens, could show as drift; the docs say so.

### D6. Ownership: `+cdk` (maintainer decision)

- Runtime description: `Rayito agent <name> (rayito.agentcore/1+cdk)`
  (`limits.json` `cdkOwnershipSuffix`). `has_marker` / `isOwned` accept both
  markers, so `AgentCoreFleet.connect`, `rayito agent list/status/invoke` work
  unchanged. A new `is_cdk_managed` / `isCdkManaged` makes `rayito agent
  deploy`, `rayito agent delete --target agentcore`, `deploy_agent` and
  `deployAgent` fail with `name_conflict` and the contract message pointing to
  `cdk deploy` / `cdk destroy` (`contract.cliRefusals.agentDeploy`).
- Tags: `rayito:component` (`agentcore-runtime` or `microvm-image`),
  `rayito:managed-by=rayito-cdk` (`cdkManagedByTagValue`) and
  `rayito:sdk-version`, plus user tags, via `Tags.of(this)` on every taggable
  resource at create time (nested stack included).
- MicroVM images have no readable description marker (`GetMicrovmImage` does
  not return it); `rayito image publish` reads `tags` from the
  `GetMicrovmImage` call it already makes and refuses an image tagged
  `rayito:managed-by=rayito-cdk`. `rayito image prune` still works on them:
  it only deletes old versions, never configuration (D9.6).
- Same name in both paths: `cdk deploy` fails on the runtime name, the ECR
  repository name or the image name; the docs give the migration (delete with
  the CLI, then `cdk deploy`).

### D7. Synthesis makes no AWS call

No `fromLookup`, no SDK client, no `GetCallerIdentity`. The region must be
concrete (`Stack.of(this).region` not a token): `RayitoCdkException` /
`RayitoCdkError` `region_unresolved` otherwise, because
`require_supported_region` and the image base ARN need it. The account may
be a token: ARNs then use `Aws.ACCOUNT_ID`/`Aws.PARTITION` and the
account-matching IAM check is skipped. The TypeScript planning path is async
(`planAgentCoreWith`, `renderExport`); the TS track extracts a `ProjectReader`
port with an async fs adapter (SDK) and a sync adapter (CDK) so
`planAgentCoreSync` shares every rule with the async path.

### D8. Zero impact for users without the extra

Python: `rayito/cdk/__init__.py` loads through
`require_module("aws_cdk", extra="cdk", feature="rayito.cdk")`; a test proves
`import rayito`, the CLI and MCP leave `aws_cdk`, `jsii` and `constructs` out
of `sys.modules`; `check_wheel.py` checks `Provides-Extra: cdk`. TypeScript:
`./cdk` export and tsdown entry, optional peers, and `pack-check.mjs` asserts
no `index`, `e2b`, `cli` or `mcp` chunk imports them. `check_sdk_parity.py`
compares `rayito.cdk.__all__` with `src/cdk/index.ts`.

### D9. `RayitoMicrovmImage` (MicroVM addendum, maintainer decision)

1. **API (parity).**
   TypeScript: `new RayitoMicrovmImage(scope, id, { name, artifact,
   baseImageVersion, variant?, withEfs?, osCapabilities?, memoryMib?, sizes?,
   allowedMountBuckets?, environment?, tags? })`. Python:
   `RayitoMicrovmImage(scope, id, *, name, artifact, base_image_version,
   variant="full", with_efs=False, os_capabilities=False, memory_mib=None,
   sizes=(), allowed_mount_buckets=(), environment=None, tags=None)`.
   `artifact` is a `MicrovmImageArtifact`:
   `fromFile(path, { sha256? })` / `from_file(path, *, sha256=None)`, or
   `await MicrovmImageArtifact.downloadRelease({ version?, directory? })` /
   `MicrovmImageArtifact.download_release(*, version=None, directory=None)`,
   which fetches `rayito-image.zip` and `SHA256SUMS` of `rayd-v<version>`
   (default: the SDK's version, as `rayito setup`) **before** the app builds
   the stack, verifies the checksum with the existing `verify_checksum`, keeps
   both files in `directory` (default `.rayito/releases/<tag>/` under the
   working directory) and reuses them offline on the next synth. Attributes:
   `imageName`, `imageArn`, `latestActiveImageVersion`, `buildRoleArn`,
   `logGroupName`, `sizeImageArns` (size name → ARN).
2. **Validation at synth (no AWS):** concrete region (`region_unresolved`);
   name pattern and length including any size suffix (`image_name_invalid`);
   names starting with `rayito-base` are reserved for `rayito setup` and
   `rayito image publish` (`image_name_reserved`); artifact exists
   (`artifact_not_found`), matches `sha256` (`artifact_checksum_mismatch`),
   carries the marker of `variant` (`artifact_variant_mismatch`) and the EFS
   marker iff `withEfs` (`artifact_efs_mismatch`); `withEfs` needs `full` and
   `osCapabilities` (the existing `require_efs_combination` message, as
   `option_invalid`); `sizes` and `memoryMib` through the existing
   `validate_sizes`/`validate_baseline_memory`.
3. **What it synthesizes:** one `Artifact` (`s3_assets.Asset` of the zip,
   shared by every size); one `BuildRole` (trust `lambda.amazonaws.com` with
   `sts:AssumeRole`/`sts:TagSession` and `aws:SourceAccount`, as
   `infra/iam.yaml` BuildRole; `s3:GetObject` on the asset object only;
   `logs:CreateLogGroup`/`CreateLogStream`/`PutLogEvents` on the image log
   groups); per image a `LogGroup` `/rayito/<name>` (14 days,
   `cdkMicrovmImageLogRetentionDays`, removed with the stack) and an `Image`
   (`CfnMicrovmImage`) whose properties are the existing
   `desired_configuration` / `desiredConfiguration` output mapped to
   CloudFormation names by one pure function per SDK
   (`cfn_image_properties` / `cfnImageProperties`): `BaseImageArn` (managed
   `al2023-1` of the region), `BaseImageVersion`, `BuildRoleArn`,
   `CodeArtifact.Uri` (`asset.s3ObjectUrl`), `Resources`,
   `CpuConfigurations`, `Hooks` (`IMAGE_HOOKS`), `Logging`,
   `AdditionalOsCapabilities` (`[]` or `["ALL"]`), `EgressNetworkConnectors`
   (`[]`), `EnvironmentVariables` (sorted by key; `RAYITO_ALLOWED_MOUNT_BUCKETS`
   from `allowedMountBuckets`, `RAYITO_BASELINE_MEMORY_MIB` for sized images,
   then `environment`), and `Description` =
   `Rayito image <name> (rayito.microvm-image/1+cdk)`. The description has no
   timestamp or version: it is write-only and any change creates a new image
   version (and a week of snapshot storage). Each `Image` depends on the
   role's policy and its log group.
4. **Reuse of the publish planning.** The Python track moves the pure part of
   `cli/_publish.py` (`IMAGE_HOOKS`, `HOOKS_PORT`, `MANAGED_BASE_IMAGE_NAME`,
   `ALL_OS_CAPABILITIES`, `default_image_name`, `require_efs_combination`,
   `sized_settings`, `BASELINE_MEMORY_ENV_VAR` and a `desired_configuration`
   that takes `region` and `build_role_arn` instead of clients) to
   `rayito/_images_publish_domain.py`; `cli/_publish.py` imports it, so
   `testdata/images/publish-vectors.json` keeps passing unchanged and
   `rayito.cdk` never imports the CLI (typer). TypeScript already has it pure
   in `images/publish-domain.ts`.
5. **Why no AWS read at synth.** `Template.build` reads AWS at build time
   (`PY/_templates/_build.py:208` fetches the base artifact; `rayito image
   publish --base-image-version latest` lists managed versions; the build role
   comes from the `rayito-m0-iam` stack outputs). The construct removes all
   three: the artifact is an explicit, checksummed file; `baseImageVersion`
   is a required prop (the major number, Q115; `latest` is refused as
   `option_invalid`); the build role is created by the construct.
6. **Coexistence.** `rayito setup` keeps owning `rayito-base` (reserved
   prefix) and the `rayito-m0-iam` stack; the construct does not need either.
   `Sandbox.create(template="<name>")` and `AsyncSandbox.create` work with a
   CDK image unchanged, with the execution role the user already passes or
   `rayito setup` created. `rayito image publish` refuses a CDK image (D6);
   `rayito image prune --image-name <name>` may delete its old versions (it
   never touches the latest active one), which is the documented answer if
   CloudFormation keeps earlier versions (Q186, still open for updates).
7. **Deploy-time permissions.** CloudFormation calls `CreateMicrovmImage`
   (authorised on `*`, Q114), `lambda:PassNetworkConnector` on the managed
   `INTERNET_EGRESS` connector even with no connector named (Q114) and
   `iam:PassRole` on the build role. The default CDK execution role has them;
   a restricted bootstrap must add them (documented).

### D10. Agent images on MicroVMs stay out

`AgentTemplate` with runtimes layers an agent on the published base image and
reads AWS at build time (base artifact, secrets, gateway preflight), and an
agent fleet only exists once the SDK starts MicroVMs. None of that is
trivially reusable at synth time, so 0.15 ships `RayitoMicrovmImage` for the
Rayito base image only. A team can publish its base with CDK and keep
deploying agents with `deploy_agent`/`rayito agent deploy`.

### D11. Tags and SCPs

`Tags.of(this)` gives the fixed and user tags to every taggable resource at
create time, including the nested stack, the Lambda functions, the state
machine and both L1 resources (`ITaggableV2`), which keeps tag-on-create SCPs
satisfied. A synth test asserts that every resource whose type supports
`Tags` carries all of them. User tags count toward the 50-tag limit with the
three fixed ones (`validate_tags`).

### D12. Tests and contract

1. Offline synth in both languages on every PR (`Template.fromStack` /
   `Template.from_stack`, fixed env): resource counts and properties, IAM
   equal to the resolved ARN lists (`testdata/agentcore-deploy/model-arns.json`)
   and absent when empty, IAM of the nested template equal to
   `infra/agentcore-runtime.yaml`, tags on every taggable resource, the
   errors of D7/D9, MMDS wiring, `localDocker` dropping the build resources,
   and the image properties equal to the publish configuration.
2. Shared contract `testdata/agentcore-cdk/` (cases, normalisation,
   `scripts/gen_cdk_vectors.py --check`); both SDKs match the same golden.
3. AWS e2e, opt-in only (`workflow_dispatch` `suite: cdk` in
   `.github/workflows/e2e.yml`, never on PRs or nightly): bootstrap once by
   hand (documented, tagged); deploy a run-named stack with an agent and an
   image; invoke once through the SDK; `connect`/`list` see the agent;
   `rayito agent deploy` and `rayito image publish` refuse; create one
   sandbox from the image and kill it; `cdk destroy --force` in `always()`;
   sweep only that run's resources by tag. It also measures Q185 and Q186.

### D13. One handler file, packaged with its botocore model

`infra/agentcore-cdk-handler.py` is the single source of the custom-resource
code (Python 3.13 Lambda runtime, `index.on_event` / `index.is_complete`).
`scripts/gen_stack_assets.py` zips it (stored, fixed dates) with the
`bedrock-agentcore-control` model files copied verbatim from botocore
1.43.103 (`docs/aws-api/bedrock-agentcore-control/2023-06-05/*.json.gz`) into
`_stacks/_artifacts/agentcore-cdk-handler.zip` and
`stacks/templates/agentcore-cdk-handler.gen.ts`; both SDKs write those bytes
to a temporary file and use `lambda.Code.fromAsset(<zip>)`, so the asset hash
is identical. The handler sets `AWS_DATA_PATH` to its `models/` directory
before creating clients.

### D14. Cost and activation (ADR-014)

Synthesis costs nothing. `cdk deploy` creates billable resources: CodeBuild
minutes (codeBuild mode), ECR storage (lifecycle keeps 5 images), MicroVM
image builds and snapshot storage per version (one per size), logs (14 days),
Lambda and Step Functions polling (cents per deployment) and AgentCore usage.
Tracing, VPC and log delivery stay off. CDK bootstrap assets persist until
`cdk gc` (unlike the CLI, which deletes its source zip). Every docstring and
TSDoc carries a «Coste y activación» block; `optional-features.md` gets a row.

## Synthesis and deployment sequence

1. Construct: validate props and the stack environment (D7, D9.2); plan
   (`build_plan` / `planAgentCoreSync`) or image settings; any error raises
   before a child construct is added.
2. Synthesis: assets (source zip, handler zip, image zip) staged in
   `cdk.out`; templates rendered; no AWS call.
3. `cdk deploy`: assets published to the bootstrap bucket; nested stack
   (repository, build project, roles); `ImageBuild` waits for CodeBuild; the
   runtime is created; `Mmds` enforces MMDSv2; images build in parallel with
   the agent path.
4. `cdk destroy`: runtime, nested stack (the repository empties itself),
   images, log groups and the handler functions are removed; bootstrap assets
   remain until `cdk gc`.

## Alternatives considered

- **jsii package (ADR-031 D12)**: rejected (D1).
- **The AgentCore L2 `Runtime` with `fromAsset`**: needs local Docker with
  arm64 by default and a role that differs from the CLI's; the L1 plus the
  shared template keeps the IAM identical.
- **`@cdklabs/deploy-time-build`**: a third-party dependency for a build the
  repository already has (the CodeBuild project of the template).
- **`AWS::BedrockAgentCore::Runtime` with a hypothetical metadata property**:
  does not exist (Q181).
- **One shared ownership marker**: would let the CLI update or delete a
  runtime CloudFormation owns (drift); rejected by the maintainer.
- **Leaving MicroVM out of 0.15**: rejected by the maintainer; D9 keeps the
  scope to the image resource.
- **Description marker on MicroVM images**: not readable back
  (`GetMicrovmImage` returns no description); the managed-by tag is.
- **Resolving `latest` base image version at synth**: needs an AWS call.

## Consequences

`cdk deploy` and `rayito agent deploy` produce the same agent from the same
project; the CLI and SDKs read and invoke CDK agents and refuse to mutate
them. A Rayito MicroVM image can live in a CDK stack next to the rest of an
application. Two more optional dependencies exist; users who do not install
them see no change in size, imports or behaviour.

## Risks

- **MMDSv2 outside CloudFormation** (Q185): checked on each new runtime
  version; CloudFormation's versions already have it, so only a version that
  came without it would be updated out of band (and could show as drift).
- **Image update semantics** (Q186): if CloudFormation keeps every version,
  snapshot storage grows; `rayito image prune` is the documented answer.
- **Build ceilings**: a stuck CodeBuild build blocks the stack up to the
  total timeout (40 min by default).
- **Heavier installs**: `aws-cdk-lib` is about 139 MB and Python synthesis
  needs Node (jsii); only for `rayito[cdk]` users and CI.
- **Nested stack plus `CfnInclude` coupling**: a template change reaches CDK
  users' stacks; the IAM-equality test and the goldens catch it.
- **Lambda runtime boto3 age**: handled by bundling the model (D13).
- **Restricted CDK bootstraps**: need the permissions of D9.7; documented.
