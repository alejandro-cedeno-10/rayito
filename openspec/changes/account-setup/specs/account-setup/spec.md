## ADDED Requirements

### Requirement: rayito setup exists in both CLIs with one contract
The Python CLI (`rayito.cli.app`) and the TypeScript CLI (`src/cli/registry.ts`) SHALL both define the leaf command `setup` with exactly the options `--bucket` (string, default none), `--artifact` (path, default none), `--yes` (flag, default false) and `--dry-run` (flag, default false), and SHALL honour the global `--profile`, `--region` and `--json`. The command SHALL resolve the AWS session with the same chain as every other command (`--profile`, `--region`, `AWS_PROFILE`, `AWS_REGION`, then the SDK default chain) and SHALL NOT write credentials anywhere. Its parameters SHALL match the `setup` entry of `testdata/cli/surface.json`, and every option name, constant, human message, JSON key and exit code SHALL match `testdata/account-setup/contract.json` in both SDKs.

#### Scenario: surface parity
- **WHEN** the Python surface test and the TypeScript surface test run
- **THEN** both match the `setup` entry of `testdata/cli/surface.json`, with parameters `bucket`, `artifact`, `yes`, `dry_run` in that order

#### Scenario: contract parity
- **WHEN** each SDK's unit tests load `testdata/account-setup/contract.json`
- **THEN** the SDK's message templates, labels, cost texts, error codes, error exit codes and constants (stack name, output key, image name, release tag prefix, asset names, CDK qualifier, timeout, stack and version states) equal the contract's values

### Requirement: The plan is computed from read-only observations
`AccountSetup.plan` SHALL first reject a region outside `SUPPORTED_REGIONS` with code `unsupported_region` before any AWS call, and SHALL then observe the account only through read operations already documented in `AWS_API_NOTES.md` or used by the SDK: `GetCallerIdentity`, `DescribeStacks` on `rayito-m0-iam`, at most one `HeadBucket`, `GetMicrovmImage` on the account ARN of `rayito-base` and, when the image exists, `ListMicrovmImageVersions`. The plan SHALL be a list of exactly three steps in the order `bucket`, `iamStack`, `baseImage`, each with one action from `create`, `update`, `reuse`, `wait`, `publish`, decided by the pure rules of `design.md` D2. `ready` SHALL be true only when all three actions are `reuse`, and confirmation SHALL be required when any action is `create`, `update` or `publish`.

#### Scenario: shared plan vectors
- **WHEN** every case of `testdata/account-setup/plan-vectors.json` is evaluated by the pure domain functions of each SDK, after replacing `{sdkVersion}` and `{cdkBucket}`
- **THEN** the computed steps, `ready`, `needsConfirmation` and cost lines equal the case's `expected`, or the raised error's `code` equals `expected.error`

#### Scenario: an account prepared by hand is complete
- **WHEN** `rayito-m0-iam` is `CREATE_COMPLETE` with a `BuildRoleArn` output and an `ArtifactBucket` that exists in the region, and `rayito-base` is `UPDATED` with a `SUCCESSFUL`/`ACTIVE` version
- **THEN** the plan is three `reuse` steps, `ready` is true, no confirmation is asked and `apply` makes no write call

#### Scenario: an unsupported region stops before AWS
- **WHEN** `rayito --region sa-east-1 setup --dry-run` runs against fakes that record every call
- **THEN** the exit code is 2, stderr carries the `unsupported_region` message and no fake records a call

### Requirement: The artifact bucket is reused, never created
The bucket candidate SHALL be the first present of `--bucket`, `RAYITO_BUCKET`, the `ArtifactBucket` parameter of an existing `rayito-m0-iam`, and the CDK bootstrap bucket `cdk-<qualifier>-assets-<account>-<region>` (default qualifier `hnb659fds`), and SHALL be probed with exactly one `HeadBucket`. A missing or inaccessible CDK bootstrap bucket SHALL raise `no_bucket`; a missing or inaccessible bucket from any other source SHALL raise `bucket_unavailable`; a bucket whose `BucketRegion` differs from the region SHALL raise `bucket_region`. No code path of `rayito setup` SHALL call `CreateBucket` or any other S3 bucket-configuration API.

#### Scenario: no bucket anywhere
- **WHEN** no `--bucket`, no `RAYITO_BUCKET`, no IAM stack and no CDK bootstrap bucket exist
- **THEN** `rayito setup --dry-run` exits 2 with the `no_bucket` message, which names `--bucket`, `RAYITO_BUCKET` and `cdk bootstrap`

#### Scenario: a bucket in another region
- **WHEN** `--bucket amzn-s3-demo-bucket` names a bucket whose `HeadBucket` reports `eu-west-1` while the region is `us-east-1`
- **THEN** `rayito setup` exits 2 with the `bucket_region` message and makes no write call

### Requirement: The IAM stack is created, updated only for its bucket, or waited for
When `rayito-m0-iam` does not exist, `apply` SHALL create it through `OptionalStacks.deploy` with the packaged `infra/iam.yaml`, `Capabilities=["CAPABILITY_NAMED_IAM"]`, `ArtifactBucket=<bucket>` and no other parameter, and SHALL wait for it. When it exists in a usable state with a different `ArtifactBucket`, `apply` SHALL update it sending only `ArtifactBucket` and keeping every other parameter with `UsePreviousValue`. When it is in progress (other than `DELETE_IN_PROGRESS`), `apply` SHALL wait for it and SHALL NOT call `CreateStack` or `UpdateStack`. A stack in `ROLLBACK_COMPLETE`, `DELETE_IN_PROGRESS` or a `*_FAILED` state SHALL raise `stack_blocked`, and a usable stack without `BuildRoleArn` SHALL raise `stack_incomplete`. The `iam` component SHALL NOT appear in the `rayito stack` catalog.

#### Scenario: create on a fresh account
- **WHEN** `rayito setup --yes` runs on a fake account with a CDK bootstrap bucket and no stack
- **THEN** the fake provisioner records exactly one `create` of `rayito-m0-iam`, with `CAPABILITY_NAMED_IAM` and the single parameter `ArtifactBucket`, followed by a `wait` for `deployed`, and no `update`

#### Scenario: a changed bucket updates only that parameter
- **WHEN** `rayito setup --yes --bucket amzn-s3-demo-bucket1` runs and the stack's `ArtifactBucket` is `amzn-s3-demo-bucket`
- **THEN** the fake provisioner records one `update` whose values are `{"ArtifactBucket": "amzn-s3-demo-bucket1"}` and whose kept-previous list holds the six other template parameters present on the stack

#### Scenario: the catalog is unchanged
- **WHEN** `rayito stack list` runs
- **THEN** no component named `iam` is listed

### Requirement: rayito-base is published from a checked release artifact, and an in-progress build is waited for
When the plan's image action is `publish` without `--artifact`, `apply` SHALL download `SHA256SUMS` and `rayito-image.zip` of the release `rayd-v<SDK version>` into a temporary directory, SHALL compute the zip's sha256 while streaming it, and SHALL publish only when it equals the `rayito-image.zip` entry of `SHA256SUMS`, raising `checksum_missing` or `checksum_mismatch` otherwise. With `--artifact`, it SHALL publish that zip after checking, before any AWS call, that the file exists (`artifact_missing`) and is a `full` artifact without the EFS marker (`artifact_variant`). Publishing SHALL reuse the existing pipeline with image name `rayito-base`, base image version `latest` (printed as `base_image_version=<v>`), the stack's `BuildRoleArn` and the chosen bucket, and SHALL raise `image_build_failed` when the version does not become launchable. When a version of `rayito-base` is `PENDING` or `IN_PROGRESS`, the plan's image action SHALL be `wait`, and `apply` SHALL wait for the newest such version through the three-state gate and SHALL NOT call `CreateMicrovmImage` or `UpdateMicrovmImage`.

#### Scenario: shared checksum vectors
- **WHEN** every case of `testdata/account-setup/checksums.json` is parsed by each SDK
- **THEN** the hash found for `asset` equals `expected` (or none when `expected` is null)

#### Scenario: a tampered zip is not published
- **WHEN** the fake release source serves a `rayito-image.zip` whose sha256 differs from its `SHA256SUMS` entry
- **THEN** `rayito setup --yes` exits 1 with the `checksum_mismatch` message and the fake publisher records no call

#### Scenario: an interrupted build is resumed, not duplicated
- **WHEN** `rayito setup --yes` runs while `rayito-base` has version `2.0` in `IN_PROGRESS`
- **THEN** the plan's image step is `wait` with `imageVersion` `2.0`, no confirmation is asked for it, and the fakes record gate reads for `2.0` but no `create`/`update-microvm-image`

### Requirement: Plan, confirmation, dry run and JSON output
Before any write, `rayito setup` SHALL print the plan (header, one line per step, and the cost lines of the actions that cost) with the exact templates of `contract.json`. When confirmation is required it SHALL ask `¿Preparar la cuenta?` on a terminal unless `--yes` is given; without a terminal or with `--json` and without `--yes` it SHALL raise `needs_confirmation` (exit 2), and a "no" answer SHALL exit 1 without writing. `--dry-run` SHALL perform only the read operations of the plan, print the plan followed by `no se creó ni cambió nada (--dry-run)` and exit 0. With `--json`, stdout SHALL carry exactly one JSON document with the keys of `contract.json` (`rayito`, `region`, `account`, `dryRun`, `ready`, `steps`, `cost`, `result`, `doctor`, `error`, `exitCode`) and every progress line SHALL go to stderr.

#### Scenario: shared CLI cases
- **WHEN** every case of `testdata/account-setup/cli-cases.json` runs through each SDK's CLI with fakes built from its `input` and a non-terminal stdin
- **THEN** the exit code, stdout (exact text, or the parsed JSON for `json` cases) and stderr equal the case's `expected`, after replacing `{sdkVersion}` and `{cdkBucket}`

#### Scenario: dry run makes no write
- **WHEN** `rayito setup --dry-run` runs on a fresh fake account
- **THEN** the fakes record only `GetCallerIdentity`, `DescribeStacks`, `HeadBucket` and `GetMicrovmImage` calls and no download is attempted

### Requirement: setup ends with rayito doctor
After a successful `apply` (including when nothing had to be done), `rayito setup` SHALL run the doctor checks without `--launch`, with template `rayito-base` and the chosen bucket, SHALL print their human output (or embed the same document `rayito --json doctor` emits under `doctor`), and SHALL exit 1 when any check is `FAIL`. When no check fails it SHALL print `rayito setup: cuenta preparada en <region>`, `RAYITO_TEMPLATE=<image ARN>` and `RAYITO_BUCKET=<bucket>` as its last three lines.

#### Scenario: doctor failure fails the command
- **WHEN** `apply` succeeds and the fake doctor returns one `FAIL`
- **THEN** the exit code is 1, the doctor lines are printed and the last line is the `doctorFailed` message

#### Scenario: success prints the next-step variables
- **WHEN** `apply` succeeds and the fake doctor returns no `FAIL`
- **THEN** the exit code is 0 and the last two stdout lines are `RAYITO_TEMPLATE=arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base` and `RAYITO_BUCKET=<bucket>`

### Requirement: Errors of an unprepared account point to rayito setup
When `RunMicrovm` fails with `ResourceNotFoundException`, the control plane SHALL raise the same `SandboxNotFoundException`/`SandboxNotFoundError` as before, with the same `status_code`/`statusCode`, `aws_code`/`awsCode` and sanitized cause, and a message ending with ` (si la cuenta o la región aún no están preparadas, ejecuta `rayito setup`)`. The `deploy_agent`/`deployAgent` errors for a missing artifact bucket and a missing base image SHALL keep their class and condition and SHALL end with the same hint. No other error SHALL change and no AWS call SHALL be added to produce the hint.

#### Scenario: Sandbox.create on an account without the image
- **WHEN** `Sandbox.create(template="rayito-base")` runs against a control plane whose `RunMicrovm` raises `ResourceNotFoundException`
- **THEN** `SandboxNotFoundException` (Python) / `SandboxNotFoundError` (TypeScript) is raised with `aws_code`/`awsCode` `ResourceNotFoundException` and its message contains `rayito setup`

#### Scenario: deploy_agent without a bucket
- **WHEN** `deploy_agent` resolves no bucket
- **THEN** `InvalidArgumentException`/`InvalidArgumentError` is raised and its message contains both `Rayito no crea buckets` and `rayito setup`

### Requirement: Nothing is created or charged without the explicit command
Constructing `AccountSetup` or any of its adapters SHALL make no AWS call and SHALL load no optional AWS client. No SDK call other than `rayito setup` SHALL invoke `AccountSetup`. Without `--yes` or a terminal confirmation, `rayito setup` SHALL make no write call (`CreateStack`, `UpdateStack`, `PutObject`, `CreateMicrovmImage`, `UpdateMicrovmImage`) and SHALL download nothing.

#### Scenario: construction is free
- **WHEN** `AccountSetup` and its default adapters are constructed with a client factory spy
- **THEN** zero clients are built and zero calls are recorded

#### Scenario: declining writes nothing
- **WHEN** `rayito setup` runs on a fresh fake account with a terminal and the answer `n`
- **THEN** the exit code is 1 and the fakes record no write call and no download
