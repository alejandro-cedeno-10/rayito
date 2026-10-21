## Why

Preparing an AWS account and region for Rayito takes about 15 minutes and six
manual steps today (`primeros-pasos/configurar-aws.md`): pick credentials,
create or find a bucket, deploy `infra/iam.yaml` as the `rayito-m0-iam` stack
with the AWS CLI, download `rayito-image.zip` and its `SHA256SUMS` from the
`rayd-v<version>` release, publish `rayito-base` with
`rayito image publish`, and run `rayito doctor`. Each step is a chance to get a
parameter wrong (the bucket in another region, a stack without
`BuildRoleArn`, an image built from another `rayd` version), and a new user
who calls `Sandbox.create()` before finishing them gets a bare
`ResourceNotFoundException` from AWS with no hint of what is missing.

Version 0.14 already has most of the pieces: `rayito agent deploy` reuses the
`cdk bootstrap` assets bucket (`_agent/_aws_resolve.py`, `agent/aws-resolve.ts`)
and can publish a missing base image after a confirmation, `rayito image
publish` builds or reuses an image version idempotently, `OptionalStacks`
creates or updates a CloudFormation stack, and `rayito doctor` checks the
result. This change puts them behind one idempotent command, the way
`cdk bootstrap` prepares an environment for CDK.

## What Changes

- **New command `rayito setup`** in the Python and TypeScript CLIs, with the
  same options, output text, JSON document and exit codes in both
  (`testdata/account-setup/`): `--bucket`, `--artifact`, `--yes`,
  `--dry-run`, plus the global `--profile`, `--region` and `--json`.
- It **reads** the account first (`DescribeStacks` on `rayito-m0-iam`,
  `HeadBucket` on the bucket candidates until one is reusable, and only when
  none is, `DescribeStacks` on `rayito-m0-artifacts` and one `HeadBucket` on
  the default name; then `GetMicrovmImage` and `ListMicrovmImageVersions` on
  `rayito-base`) and builds a plan: for each of the three resources,
  `create`, `update`, `reuse`, `wait` or `publish`.
- It **shows the plan and the approximate cost** (the figures already in
  `docs/site/docs/cost.md` and `configurar-aws.md`, never new ones), asks for
  confirmation when something costs or changes (`--yes` skips it), and with
  `--dry-run` stops there without any write.
- It **applies** the plan in order: the bucket is reused when one exists
  (the one given, `RAYITO_BUCKET`, the IAM stack's `ArtifactBucket` or the
  CDK bootstrap bucket) and otherwise **created through CloudFormation**:
  the new template `infra/artifact-bucket.yaml`, deployed as the stack
  `rayito-m0-artifacts` (SSE-S3, public access blocked, ACLs disabled, TLS
  only, the image zips under `rayito/images/` expiring after 7 days, the
  bucket retained when the stack is deleted), named after `--bucket` /
  `RAYITO_BUCKET` when they name a bucket that does not exist, or
  `rayito-artifacts-<account>-<region>`; the SDK itself still never calls
  `CreateBucket`. Then the IAM stack is created (with `ArtifactBucket` = that
  bucket), updated only when the artifact bucket changes, or waited for;
  `rayito-base` is published from the release `rayito-image.zip` of the SDK's
  own version after checking its sha256 against the release `SHA256SUMS`
  (or from `--artifact`), or an in-progress build is waited for instead of
  starting another.
- It **ends with `rayito doctor`** (without `--launch`) and prints its result;
  exit 1 if any check is `FAIL`.
- **An account prepared by hand is detected as complete**: a healthy
  `rayito-m0-iam` with `BuildRoleArn` and a launchable `rayito-base` produce a
  plan of three `reuse` steps and no write at all. Any account with a
  reusable bucket plans exactly what it planned before the bucket creation
  was added: no extra read, no new stack.
- **Errors of an unprepared account point to `rayito setup`**: the
  `ResourceNotFoundException` of `RunMicrovm` in `Sandbox.create()` (both SDKs
  and the E2B shims, which share the control plane) and the "no bucket" and
  "no base image" errors of `deploy_agent`/`deployAgent` append one hint
  sentence. Exception classes, codes and every other behaviour stay the same.
- `infra/iam.yaml` is now packaged in both SDKs by `scripts/gen_stack_assets.py`
  (it was excluded as hand-deployed) and deployed through the existing
  `OptionalStacks`/`StackProvisioner` port with `CAPABILITY_NAMED_IAM`. The
  new `infra/artifact-bucket.yaml` is packaged the same way and deployed
  through the same port without capabilities. Neither is added to the
  `rayito stack` catalog.
- `rayito setup` needs bootstrap-level credentials once (CloudFormation, IAM
  roles and policies and, when it creates the bucket, the S3 actions the
  CloudFormation handlers call), like `cdk bootstrap`; the docs say so.
- Docs in Spanish and English: the "Prepara tu cuenta, una vez" block of the
  landing page, the "Configurar AWS" tutorial, the CLI reference with a
  "Coste y activación" box, cost, verify, troubleshooting, READMEs, ADR-030
  and an `AWS_API_NOTES.md` note on `CAPABILITY_NAMED_IAM` and on the
  resource types and properties of `infra/artifact-bucket.yaml`.

Nothing is deprecated or removed. Every existing command, default and
exception keeps its behaviour; `rayito setup` only runs when called.

## Impact

- Capability: new `account-setup`.
- Python: new private package `rayito/_account_setup/`, `cli/setup.py` and
  two CLI helper modules; small edits to `_images.py`, `_aws.py`,
  `_agent/_aws_resolve.py`, `cli/_publish.py`, `cli/doctor.py`,
  `cli/_session.py`, `cli/app.py`. No public export.
- TypeScript: new `src/account-setup/`, `src/cli/commands/setup.ts` and
  `src/cli/setup-output.ts`; small edits to `images/gateway.ts`,
  `aws/control-plane.ts`, `agent/aws-resolve.ts`, `stacks/packaging.ts`,
  `cli/doctor/render.ts`, `cli/services.ts`, `cli/aws-services.ts`,
  `cli/registry.ts`. No public export.
- Shared: `testdata/account-setup/` (contract, plan vectors, checksum
  vectors, CLI cases), the `setup` entry of `testdata/cli/surface.json`,
  `scripts/gen_stack_assets.py` with the packaged `iam` and
  `artifact-bucket` templates, the new `infra/artifact-bucket.yaml`, its
  `make infra-lint` entry and `scripts/tests/test_artifact_bucket_template.py`.
  These shared files are committed with this change so the SDK and docs
  lanes never edit the same file.
- No change to `rayd`, the `.proto`, the image, the existing `infra/*.yaml`
  or any `OptionalStack` component; one new template. Cost-incurring
  behaviour is only reachable through an explicit `rayito setup` with
  confirmation.
