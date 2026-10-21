## Context

This design is the ADR for `rayito setup` (to be copied as ADR-030 into
`ARCHITECTURE.md`, task D.1). It builds only on pieces that ship in 0.14:

| Piece | Python | TypeScript | What setup takes from it |
|---|---|---|---|
| Profile and region chain | `cli/_session.resolve_session` (`--profile`, `--region`, `AWS_PROFILE`, `AWS_REGION`, then botocore) | `cli/aws-services.ts` | The session; nothing stored |
| CloudFormation port | `_stacks/_port.StackProvisioner`, `_stacks/_service.OptionalStacks` | `stacks/port.ts`, `stacks/service.ts` | `status()`, `deploy()` (create or update with `UsePreviousValue`), `wait` |
| CDK bootstrap bucket | `_agent/_aws_resolve.cdk_bucket_name`, `AwsResolver.bucket_exists`, `BUCKET_MISSING_CODES` | `agent/aws-resolve.ts` | The bucket name rule and the `HeadBucket` probe |
| Image build core | `_images.py` (`image_exists`, `read_gate`, `wait_for_gate`, `find_reusable_version`, `account_image_arn`) | `images/gateway.ts` | Observing and waiting for `rayito-base` |
| Publish pipeline | `cli/_publish.build_or_reuse`, `resolve_base_image_version`, `require_matching_variant` | `images/publish-service.ts` (`buildOrReuse`, `resolveBaseImageVersion`, `requireMatchingVariant`) | Upload by content, create or update, three-state gate, reuse of an identical version |
| Doctor | `cli/doctor.run_doctor`, `render_human`, `render_json` | `cli/doctor/checks.ts` `runDoctor`, `render.ts` | The final check and its output |
| Confirmation | `typer.confirm`, `agent.base_publisher_of` | `CliConsole.confirm` | The prompt and the `--yes` rule |

Before this change no code path created an S3 bucket: `AWS_API_NOTES.md`
("Despliegue de agentes de cero configuración") states "Rayito nunca llama a
`CreateBucket`", and no `CreateBucket`, `PutBucketEncryption`,
`PutPublicAccessBlock` or `AWS::S3::Bucket` appeared in the notes or the
code. The maintainer decided on 2026-10-19 that `rayito setup` must prepare
everything in one command, like `cdk bootstrap`: when no bucket can be
reused, it **creates one through a CloudFormation stack**
(`rayito-m0-artifacts`, template `infra/artifact-bucket.yaml`, D4 and D10).
The SDK itself still never calls `CreateBucket` or any S3 bucket-configuration
API; CloudFormation does, with the caller's credentials. `rayito image publish`
also does not verify `SHA256SUMS` today: the tutorial tells the user to run
`cosign verify-blob` and `sha256sum -c` by hand.

The CLI prints Spanish only in both SDKs (every existing command); the docs
are bilingual. `rayito setup` follows that: one set of Spanish strings, in
`testdata/account-setup/contract.json`, quoted verbatim by the English docs.

## Decisions

### D1. A private application service `AccountSetup` with a pure plan, behind ports

`rayito setup` is a CLI command over a private service, like `rayito doctor`.
Nothing is exported from `rayito/__init__.py` or `src/index.ts`: a public
`AccountSetup` API would add surface (sync and async, shim, docs) that no
user asked for (`CLAUDE.md` rule 3).

Layers (Python `rayito/_account_setup/`, TypeScript `src/account-setup/`):

```text
                    +------------------------------------------------+
  CLI (adapter in)  |  cli/setup.py            | cli/commands/setup.ts |
  thin: flags,      |  cli/_setup_output.py    | cli/setup-output.ts   |
  confirm, render,  +------------------+-----------------------------+
  doctor                               | AccountSetup.plan(request)
                                       | AccountSetup.apply(plan, ...)
                    +------------------v-----------------------------+
  Application       |  AccountSetup  (_service.py | service.ts)      |
                    +---+----------+-----------+-----------+---------+
                        |          |           |           |
  Domain (pure)     _domain.py | domain.ts: classify_stack, bucket_candidates,
                    decide_bucket, creation_target, plan_bucket_creation,
                    classify_image, plan_setup, parse_checksums,
                    verify_checksum, release_tag, release_asset_url,
                    AccountSetupException/AccountSetupError, messages, constants
                        |          |           |           |
  Ports             IamStackGateway  ArtifactStackGateway  ArtifactBucketGateway
                    BaseImageGateway  BaseImagePublisher  ReleaseArtifactSource
                    (_ports.py | ports.ts)
                        |          |           |           |
  Adapters (out)    CloudFormationSetupStack (iam | artifact-bucket)
                                            -> OptionalStacks -> StackProvisioner (CFN)
                    S3ArtifactBuckets       -> HeadBucket (+ GetCallerIdentity)
                    MicrovmBaseImages       -> _images / images/gateway (GetMicrovmImage,
                                               ListMicrovmImageVersions, wait_for_gate)
                    PipelineImagePublisher  -> publish pipeline (S3 PutObject,
                                               Create/UpdateMicrovmImage, gate)
                    GithubReleaseSource     -> HTTPS GET of two release assets
```

The domain imports no `boto3`, `botocore`, `@aws-sdk/*`, `typer`, HTTP or
file-system module. The service only calls ports. The adapters translate
technical failures into `AccountSetupException`/`AccountSetupError` (with a
closed `code`) or let the existing SDK exceptions (`StackException`,
`StackError`, the CLI's `ClientError` translation) through unchanged.

Where the adapters live follows where 0.14 already put the code they wrap:

- Python: `_account_setup/_aws.py` (stack, bucket, image observation, over
  `OptionalStacks`, `HeadBucket` and `_images`), `_account_setup/_release.py`
  (download, stdlib only) and `cli/_setup_publisher.py` (the publisher,
  because the publish pipeline lives in `cli/_publish.py`).
- TypeScript: `src/cli/setup-adapters.ts` (stack, bucket, image observation
  and publisher, composed from the session's existing `stacks`,
  `doctor.headBucket` and `publishPorts`, like `ops-adapters.ts`) and
  `src/account-setup/release.ts` (download over `fetch`).

Same ports, same behaviour. The session object of each CLI hands out the
gateways (Python `cli/setup.gateways_for(clients)`, which returns
`Clients.account_setup_override` when a test sets it; TypeScript
`CliServices.accountSetup`, built in `aws-services.ts`), so the shared CLI
cases run on fakes through the same kind of seam `control_plane_override`
gives `rayito doctor`.

### D2. Three resources, five actions, one plan

The plan is a list of exactly three steps, always in this order:

| Resource | Possible actions | Write? | Needs confirmation? |
|---|---|---|---|
| `bucket` | `create`, `reuse`, `wait` | `create` (the `rayito-m0-artifacts` stack) | `create` |
| `iamStack` | `create`, `update`, `reuse`, `wait` | `create`, `update` | `create`, `update` |
| `baseImage` | `publish`, `reuse`, `wait` | `publish` | `publish` |

`ready` is true when every step is `reuse`. Confirmation is needed when at
least one step is `create`, `update` or `publish`. `wait` costs nothing new:
it waits for something already started.

**Bucket** (`bucket_candidates`, `decide_bucket`, then, only when no
candidate is reusable, `creation_target` and `plan_bucket_creation`). A
`HeadBucket` probe has three outcomes (`BucketProbe.state`): `found` (200,
with `BucketRegion`), `missing` (404, `NoSuchBucket`, `NotFound`) and
`forbidden` (403: it exists and these credentials cannot list it, usually
because another account owns it; `AWS_API_NOTES.md`, `HeadBucket`).

1. **Reuse phase.** The candidates, in order: `--bucket` (`argument`) or,
   without it, `RAYITO_BUCKET` (`environment`); when neither is given, the
   `ArtifactBucket` parameter of an existing IAM stack (`iamStack`) and then
   the CDK bootstrap bucket `cdk-<qualifier>-assets-<account>-<region>`
   (default qualifier `hnb659fds`, `cdkBootstrap`). An explicit name
   (`argument`/`environment`) is the only candidate. Each is probed once, in
   order, until one decides:
   - `found` in the region: `reuse` (this is the whole 0.14 behaviour, and
     the only path an account with a bucket ever takes: no new read, no new
     stack);
   - `found` in another region: `bucket_region` (any source);
   - `forbidden`: `bucket_unavailable` for `argument`, `environment` and
     `iamStack`; for `cdkBootstrap` the next phase (Rayito does not need
     that bucket);
   - `missing`: for `argument`/`environment`, the creation phase with that
     name; for `iamStack` and `cdkBootstrap`, the next candidate (a deleted
     stack bucket is replaced, and the IAM stack is then updated to the new
     one by the `update` rule below).
2. **Creation phase** (reached only when nothing was reused). The target is
   the explicit name, or the default `rayito-artifacts-<account>-<region>`
   (`rayitoDefault`; `default_artifact_bucket_name`). `DescribeStacks` on
   `rayito-m0-artifacts`, classified with the same `classify_stack` as the
   IAM stack:
   - `usable` with a `BucketName` output: `reuse` that bucket (`artifactStack`,
     with the stack `status`), unless the target is an explicit name that
     differs from it: `artifact_stack_conflict` (the stack holds one bucket
     and setup never renames it, which would replace it); without the
     output: `stack_incomplete`;
   - `in_progress`: `wait` (`artifactStack`, the name is the stack's
     `BucketName` parameter), with the same conflict rule;
   - `blocked`: `stack_blocked` naming `rayito-m0-artifacts`;
   - `missing` and an explicit target: `create` with that name (its probe
     already said `missing`);
   - `missing` and the default target: one more `HeadBucket` on the default
     name. `missing`: `create`. `found` or `forbidden`: `bucket_taken`. An
     unmanaged bucket with the default name is never adopted silently: it is
     either one this account kept after deleting the stack (the user passes
     it with `--bucket`, which reuses it) or a bucket another account created
     with a name derived from ours, which must never receive our artifacts.

At most three `HeadBucket` and one extra `DescribeStacks`, and only on an
account that has no usable bucket. `plan_setup` receives the decided bucket
step; the read order is part of `testdata/account-setup/plan-vectors.json`
(`bucketReads`).

**IAM stack** (`classify_stack`, then the plan). `DescribeStacks` on
`rayito-m0-iam`:

- absent: `create` with `ArtifactBucket=<bucket>` (the reused bucket, or the
  one the `bucket` step creates or waits for); every other template
  parameter is left to the template default;
- `CREATE_COMPLETE`, `UPDATE_COMPLETE` or `UPDATE_ROLLBACK_COMPLETE` (rolled
  back to the last working template, still usable) with a `BuildRoleArn`
  output: `reuse`, or `update` when its `ArtifactBucket` differs from the
  chosen bucket (only `ArtifactBucket` is sent; every other parameter keeps
  its value through `UsePreviousValue`);
- same states without `BuildRoleArn`: `stack_incomplete`;
- `DELETE_IN_PROGRESS`: `stack_blocked` (waiting for "deployed" would never
  end);
- any other state ending in `_IN_PROGRESS`: `wait`;
- anything else (`ROLLBACK_COMPLETE`, `*_FAILED`): `stack_blocked`.

**Base image** (`classify_image`, then the plan). `GetMicrovmImage` on the
account ARN of `rayito-base` and, if it exists, `ListMicrovmImageVersions`:

- absent: `publish`;
- any version in `PENDING` or `IN_PROGRESS`: `wait` on the newest such
  version (by `createdAt`);
- image `CREATED` or `UPDATED` and a version `SUCCESSFUL` and `ACTIVE`:
  `reuse` the newest such version;
- otherwise (only failed or inactive versions): `publish` (an
  `update-microvm-image`, exactly what `rayito image publish` does today).

`publish` uses `--artifact` when given (`source: artifact`), otherwise the
release `rayd-v<SDK version>` (`source: release`).

A launchable `rayito-base` is `reuse` whatever `rayd` it was built from.
Comparing it with the release would need the zip downloaded during a
read-only detection; `rayito doctor --launch` and its `compatibility` check
already report an old agent, and `rayito image publish` updates the image.

### D3. The IAM stack goes through `OptionalStacks`, not through the catalog

`infra/iam.yaml` is rendered into both SDKs by `scripts/gen_stack_assets.py`
(`iam` removed from `EXCLUDED_TEMPLATES`; files
`_stacks/_templates/iam.yaml` and `stacks/templates/iam.gen.ts`, already in
this change). A `StackComponent` named `iam`
(`IAM_STACK_COMPONENT`, in `_account_setup/_iam_stack.py` /
`account-setup/iam-stack.ts`) declares the seven template parameters
(`ArtifactBucket` required; `LogGroupPrefix`, `NetworkConnectorArns`,
`PersistenceBucket`, `PersistencePrefix`, `TransferBucket`, `TransferPrefix`
with `default=None` so the template keeps its own defaults),
`capabilities=("CAPABILITY_NAMED_IAM",)` and a cost statement. It is passed to
`OptionalStacks.status/deploy` as an object with
`stack_name="rayito-m0-iam"`. It is **not** added to `_registry.COMPONENTS`:
`rayito stack list` shows optional, cost-bearing features, and the IAM stack
is a prerequisite with its own historical name.

`deploy` tags the stack with the three fixed `rayito:*` tags, as every stack
the SDK creates. A stack created by hand is only updated when the user changes
its bucket, and then it also gets the SDK's packaged template and the tags;
the plan line says so (`actualizar ArtifactBucket a -> b`).

`CAPABILITY_NAMED_IAM` is a value of the `Capability` enum of the
`cloudformation` 2010-05-15 model (verified offline in botocore 1.43.103 for
this change: `CAPABILITY_IAM`, `CAPABILITY_NAMED_IAM`,
`CAPABILITY_AUTO_EXPAND`); task D.4 records it in `AWS_API_NOTES.md` §21,
next to `CAPABILITY_IAM`.

### D4. Create the bucket only when none can be reused, through CloudFormation

`rayito setup` keeps reusing a bucket whenever one is available (D2, reuse
phase), so every account that worked with 0.14 plans exactly what it did
before and no new stack appears. Only when nothing is reusable does the plan
gain a `bucket` `create` step, and the bucket is created by deploying
`infra/artifact-bucket.yaml` as the stack `rayito-m0-artifacts` through the
same `OptionalStacks.deploy` that creates the IAM stack (D3), with the single
parameter `BucketName`. The SDK never calls `CreateBucket` or any
`PutBucket*` API itself: CloudFormation does, with the caller's credentials,
and the SDK only uses the CloudFormation calls already in `AWS_API_NOTES.md`
§21 (no `Capabilities`: the template creates no IAM resource).

- **Name.** `--bucket NAME` or `RAYITO_BUCKET=NAME` that does not exist
  (404) is created with that name: the user asked for it explicitly, and the
  plan shows `crear con la pila rayito-m0-artifacts (--bucket)` before the
  confirmation. A name that exists but is not accessible (403) stays an
  error (`bucket_unavailable`), and one in another region stays
  `bucket_region`. Without an explicit name the bucket is
  `rayito-artifacts-<account>-<region>` (`DEFAULT_ARTIFACT_BUCKET_TEMPLATE`):
  deterministic, so a re-run finds it, and unique per account and region
  (bucket names are global; at most 44 characters in every supported
  region, under the 63 limit).
- **Stack.** `rayito-m0-artifacts`, next to `rayito-m0-iam`; component
  `artifact-bucket` (`ARTIFACT_BUCKET_COMPONENT`), passed to `OptionalStacks`
  as an object like the IAM component and **not** in the `rayito stack`
  catalog, for the same reasons (D3).
- **Order.** Bucket stack, then the IAM stack with `ArtifactBucket` = the
  stack's `BucketName` output, then the image (D6).
- **Idempotency and resume.** A re-run after success finds the bucket
  through the IAM stack's `ArtifactBucket` (reuse phase) and never reads the
  bucket stack again. A run interrupted after the bucket stack and before
  the IAM stack finds it in the creation phase: `reuse` if complete, `wait`
  if in progress.
- **Hand-prepared accounts** never reach the creation phase: they have a
  reusable bucket by definition.
- **`rayito agent deploy`** keeps resolving its bucket as in 0.14
  (`RAYITO_BUCKET`, then the CDK bootstrap bucket); the final output of setup
  still prints `RAYITO_BUCKET=<bucket>` for it.

### D5. The release artifact, checked against `SHA256SUMS`

Without `--artifact`, `GithubReleaseSource` downloads two assets of the tag
`rayd-v<SDK version>` (`__version__` / `VERSION`; the SDKs and `rayd` share
`MAJOR.MINOR.PATCH`, release-please linked versions) from
`https://github.com/alejandro-cedeno-10/rayito/releases/download/<tag>/<asset>`:
`SHA256SUMS`, then `rayito-image.zip`, streamed to a temporary directory that
the CLI owns and deletes. `parse_checksums` reads the `sha256sum` format
(`<64 lowercase hex><space><space or *><name>`, malformed lines ignored), and
`verify_checksum` compares the streamed sha256 of the zip with the entry for
`rayito-image.zip`: missing entry `checksum_missing`, different value
`checksum_mismatch`, nothing published in either case. HTTP status other
than 2xx, timeouts (`RELEASE_DOWNLOAD_TIMEOUT_SECONDS = 600` /
`RELEASE_DOWNLOAD_TIMEOUT_MS = 600_000` per asset, total) and network errors
are `release_download` with one of three reasons (`HTTP <status>`,
`tiempo agotado`, `error de red`). Python uses `urllib.request` (stdlib, no
new dependency); TypeScript the global `fetch` (Node ≥ 18), injectable.

What this proves and what it does not: the hash check catches a truncated,
corrupted or wrong asset. It does **not** prove authenticity: `SHA256SUMS`
comes from the same release as the zip, and the tutorial already warns that a
token with write access to the repository could replace both. The signature
is checked by `cosign verify-blob` (`verify.md`); a user who wants it runs
cosign and passes the verified zip with `--artifact`. The docs say this
plainly (task D.6), and SECURITY.md T10 gains the same sentence.

### D6. Apply order, idempotency and resume

```text
CLI                      AccountSetup                     ports
 |  region in SUPPORTED_REGIONS? (pure, before any call)
 |--plan(request)-------->| buckets.account_id()         -> GetCallerIdentity
 |                        | iam_stack.observe()          -> DescribeStacks rayito-m0-iam
 |                        | for candidate in bucket_candidates(...):
 |                        |   buckets.probe(candidate)   -> HeadBucket   (stop at the first decision)
 |                        | only if nothing was reused:
 |                        |   artifact_stack.observe()   -> DescribeStacks rayito-m0-artifacts
 |                        |   buckets.probe(default)     -> HeadBucket   (default name, no stack)
 |                        | images.observe(name)         -> GetMicrovmImage, ListMicrovmImageVersions
 |<-----SetupPlan---------| plan_setup(...) (pure)
 | print plan + cost; --dry-run: stop (exit 0)
 | confirmation if needed (--yes / terminal; else needs_confirmation, exit 2)
 |--apply(plan, workdir, emit)->|
 |                        | 1. bucket: create -> CreateStack rayito-m0-artifacts (BucketName) + wait
 |                        |            wait   -> wait("deployed"), re-observe
 |                        |            -> BucketName output (stack_incomplete if absent)
 |                        |            reuse  -> nothing to write
 |                        | 2. iam stack: create | update -> CreateStack/UpdateStack + wait
 |                        |               wait            -> wait("deployed"), re-observe
 |                        |               -> BuildRoleArn output (stack_incomplete if absent)
 |                        | 3. image: wait  -> wait_for_gate(newest building version)
 |                        |    publish: release -> SHA256SUMS + zip, verify  (or --artifact)
 |                        |             publisher.publish(zip, bucket, build role)
 |                        |               -> resolve `latest` base version, PutObject if absent,
 |                        |                  reuse identical version or Create/UpdateMicrovmImage,
 |                        |                  wait_for_gate
 |<-----SetupResult-------| (bucket, imageArn, imageVersion, artifactUploaded)
 | run_doctor(template=rayito-base, bucket, launch=False) -> render
 | exit 0, or 1 if any FAIL
```

Rules:

- **Read before write.** Every write is decided from a fresh observation in
  the same invocation. Re-running after success observes three `reuse` steps
  and writes nothing.
- **Stacks.** `create` and `update` use `OptionalStacks.deploy`, which
  already treats "No updates are to be performed" as success and keeps
  unlisted parameters. An interrupted run leaves a stack `*_IN_PROGRESS`; the
  next run plans `wait`. The bucket stack is only ever created or waited
  for, never updated: its `BucketName` is fixed for its life.
- **Bucket.** The IAM stack and the publisher use the bucket the `bucket`
  step returns (the reused name, or the `BucketName` output of
  `rayito-m0-artifacts` after `create`/`wait`).
- **Artifact.** The S3 key is content-addressed
  (`rayito/images/rayd-<sha12>.zip`); an interrupted upload is redone, a
  finished one is skipped (`upload_if_absent`).
- **Image.** An interrupted run leaves a version `PENDING` or `IN_PROGRESS`;
  the next run plans `wait` on it and never submits a second build while one
  is running (which AWS would reject with `ConflictException` anyway). If the
  waited build ends launchable, setup goes on; if it fails, setup stops with
  `image_build_failed` and the next run plans `publish` again, with a new
  confirmation. A `publish` that finds an identical launchable version reuses
  it (`build_or_reuse`), so a run that died after the build finished costs
  nothing more.
- **Base image version.** Always `latest`, resolved through
  `list-managed-microvm-image-versions` and printed as
  `base_image_version=<v>`, like `rayito agent deploy --base-artifact`.
- **Confirmation scope.** The confirmation covers the plan printed just
  before it. A `wait` that turns into a failure never escalates to a new
  `publish` within the same run.

### D7. Exit codes and errors

| Condition | Python | TypeScript | Exit |
|---|---|---|---|
| Success, dry run, nothing to do, doctor without `FAIL` | — | — | 0 |
| `unsupported_region`, `bucket_unavailable`, `bucket_region`, `bucket_taken`, `artifact_stack_conflict`, `artifact_missing`, `artifact_variant`, `needs_confirmation` | `AccountSetupException(code=…)` | `AccountSetupError` | 2 |
| `stack_blocked`, `stack_incomplete`, `release_download`, `checksum_missing`, `checksum_mismatch`, `image_build_failed` | `AccountSetupException(code=…)` | `AccountSetupError` | 1 |
| User answers "no" at the prompt | `typer.Exit(1)` | `return EXIT_FAILURE` | 1 |
| Doctor reports a `FAIL` | — | — | 1 |
| No region, unknown profile, no credentials | existing `UsageError` | existing `UsageError` | 2 |
| AWS error not handled above (`ClientError`, `StackException`, `AuthenticationException`) | existing `translated_failures` | existing failure translation | 1 |

`AccountSetupException` extends `Exception` (not `SandboxException`: nothing
here is attributable to a sandbox) and is private (`_account_setup._domain`),
like the CLI's own `UsageError`. Its `code` is one of the closed list above
and its message never carries a URL, a presigned link, a token or the raw
AWS message. `stack_blocked` and `stack_incomplete` name the stack (either
`rayito-m0-iam` or `rayito-m0-artifacts`; `stack_incomplete` also names the
missing output and the template); the bucket name appears in bucket errors
because the user typed or owns it, or because it is the deterministic
default (the same as `rayito doctor`'s `bucket` check).

`no_bucket` no longer exists: without a reusable bucket setup creates one.
`bucket_unavailable` now only means "exists but these credentials cannot
reach it" (403) for an explicit or IAM-stack bucket; a missing explicit
bucket is created. `bucket_taken` covers the default name already used
outside `rayito-m0-artifacts` (200 or 403), and `artifact_stack_conflict` an
explicit new name while `rayito-m0-artifacts` already holds another bucket.

In `--json` mode an `AccountSetupException` still produces the single JSON
document, with `error: {code, message}` and `exitCode`, plus the usual
`rayito: <message>` line on stderr.

### D8. Errors of an unprepared account point to `rayito setup`

One hint sentence, `ACCOUNT_SETUP_HINT` ("si la cuenta o la región aún no
están preparadas, ejecuta `rayito setup`"), appended as
`"<message> (<hint>)"`:

- `RunMicrovm` raising `ResourceNotFoundException` inside the control plane's
  `run_microvm`/`runMicrovm` (`_aws.py`, `aws/control-plane.ts`). Same class
  (`SandboxNotFoundException`/`SandboxNotFoundError`), same `status_code`,
  `aws_code` and sanitized cause; only the message grows. This covers
  `Sandbox.create()`, `AsyncSandbox.create()`, the pool and the E2B shims,
  which all launch through it.
- `resolve_bucket`/`resolveBucket` "no hay bucket" and
  `ensure_base_image`/`ensureBaseImage` "la imagen base no existe" in
  `_agent/_aws_resolve.py`/`agent/aws-resolve.ts`. Same
  `InvalidArgumentException`/`InvalidArgumentError`, same condition.

No other error changes, and no call is added to find out whether the account
is prepared.

### D9. Cost wording reuses the published figures

The plan prints only figures already in the docs, as named constants that cite
them:

- image: "≈ $0,04 por semana por versión publicada (mínimo una semana)"
  (`cost.md`, row "Mantener publicada una versión de imagen");
- IAM stack: "gratis" (`configurar-aws.md`, step 3);
- artifact zip in the bucket: "céntimos" (`configurar-aws.md`, step 2);
- a created bucket: no figure, only what the template does ("los zips de la
  imagen caducan a los 7 días (regla de ciclo de vida)", with the days from
  `ARTIFACT_EXPIRATION_DAYS`, which equals the template default); the zip
  itself is already the "céntimos" line;
- build time: "≈ 10 min" (`configurar-aws.md`, step 4).

`rayito setup` is not an optional feature (it creates nothing that a sandbox
needs at runtime beyond what the tutorial already makes the user create), so
it gets no row in `optional-features.md`; its CLI reference and the tutorial
carry the "Coste y activación" box.

### D10. `infra/artifact-bucket.yaml`

One `AWS::S3::Bucket` and its `AWS::S3::BucketPolicy`, nothing else. Every
property name and value below was checked offline against the
CloudFormation resource schemas bundled with cfn-lint 1.56.3 (the version
pinned in `.github/release/requirements-cfn-lint.txt`), and the template
passes `cfn-lint`; `scripts/tests/test_artifact_bucket_template.py` pins the
policy-relevant values, which cfn-lint does not judge.

| What | Property | Why |
|---|---|---|
| Name | `BucketName: !Ref BucketName` (parameter, `MinLength` 3, `MaxLength` 63, lowercase pattern) | The SDK must know the name before the stack exists (it probes it first), so it always passes it |
| Encryption | `BucketEncryption.ServerSideEncryptionConfiguration[].ServerSideEncryptionByDefault.SSEAlgorithm: AES256` | SSE-S3: no key to pay for, no `kms:*` for the build role |
| No public access | `PublicAccessBlockConfiguration` with `BlockPublicAcls`, `BlockPublicPolicy`, `IgnorePublicAcls`, `RestrictPublicBuckets` all `true` | The zips are private build inputs |
| No ACLs | `OwnershipControls.Rules[].ObjectOwnership: BucketOwnerEnforced` | Every object belongs to the account; the SDK never sends an ACL |
| Expiry | `LifecycleConfiguration.Rules[]`: `Id: expire-image-artifacts`, `Status: Enabled`, `Prefix: rayito/images/`, `ExpirationInDays: !Ref ArtifactExpirationDays` | A zip is read only while its image version builds (§4 "Cómo construye AWS la imagen"); `upload_if_absent` uploads it again when it is missing. Only the image prefix: `rayito/stacks/` and anything else are never expired |
| TLS only | bucket policy `Deny` `s3:*` for `Principal: "*"` on the bucket and `/*` when `aws:SecureTransport` is `"false"` | Same pattern as the `DenyNonTls` statement of `infra/efs-volumes.yaml` |
| Lifecycle of the stack | `DeletionPolicy: Retain`, `UpdateReplacePolicy: Retain` on the bucket | See the `DeletionPolicy: Delete` alternative |
| Output | `BucketName` (and `BucketArn`) | Read by setup after `create`/`wait` |

`ArtifactExpirationDays` is a `Number` parameter, `Default: 7`, `MinValue: 1`,
mirrored as `ARTIFACT_EXPIRATION_DAYS` in both SDKs and as
`constants.artifactExpirationDays` in the contract (the scripts test checks
they are equal). Seven days: a build takes about 10 minutes, and a week
leaves room to retry a failed build, or to publish the same zip again, without
uploading it; after that a re-upload costs the same "céntimos". Setup never
passes it, so the template default applies; a user who wants another value
deploys the template by hand. No versioning, no access logging (another
bucket to create and pay for), no `AbortIncompleteMultipartUpload` (the
image upload is a single `PutObject`).

### D11. Stack tags for tag-enforcing SCPs

`rayito setup` accepts `--tag K=V`, repeatable. Every tag is passed as a
CloudFormation stack `Tags` entry in the `CreateStack` and `UpdateStack` calls
of the two stacks setup owns (`rayito-m0-artifacts` and `rayito-m0-iam`);
CloudFormation propagates stack tags to the resources it creates, so the
bucket, the roles and the policies carry them without any per-resource call.
A reused stack is not retagged (setup makes no write on it), and the tags are
not applied to the MicroVM image (`CreateMicrovmImage` is unchanged). Without
`--tag` the calls are byte-for-byte the ones of D1-D10. The option and its
`K=V` parsing are the same as in `rayito stack deploy` and
`rayito events deploy`; a malformed value is a usage error (exit 2) before any
AWS call. The help text lives in `testdata/account-setup/contract.json`.

Why: an organization SCP may require tags on resource creation and deny
`s3:CreateBucket` (or other creating actions) when the request carries none,
so the bucket stack would fail with an explicit deny. Stack tags are the smallest fix: no new IAM or S3
call, and CloudFormation is already the record of what setup created (D4).

Alternatives: tagging the bucket with `PutBucketTagging` after creation (too
late for a deny on `CreateBucket`, and a new write API); a template parameter
per tag (the tag keys are not known in advance); tagging the image (not
needed by tag-enforcing SCPs, and `CreateMicrovmImage` stays out of scope).

## Alternatives considered

- **Never create a bucket** (the first version of this design): stop with
  `no_bucket` and ask for `--bucket`, `RAYITO_BUCKET` or `cdk bootstrap`.
  Superseded by the maintainer's decision of 2026-10-19: one command must
  prepare everything, as `cdk bootstrap` does.
- **Create the bucket with raw S3 calls** (`CreateBucket`,
  `PutBucketEncryption`, `PutPublicAccessBlock`, `PutBucketOwnershipControls`,
  `PutBucketPolicy`, `PutBucketLifecycleConfiguration`). Rejected: six new
  write APIs with their parameters in both SDKs (and in `AWS_API_NOTES.md`),
  a partial failure leaves a half-configured bucket with no record of what
  was applied, and Rayito provisions every other resource through
  CloudFormation (`OptionalStacks`, ADR-016). A template is one reviewed,
  lintable file, rendered into both SDKs by `gen_stack_assets.py`, and the
  stack is the record (`DescribeStacks`, drift detection, `delete-stack`).
- **Add the bucket to `infra/iam.yaml`** (one stack). Rejected: the IAM
  stack takes `ArtifactBucket` as a parameter precisely so that any bucket
  works (CDK's, one of the user's); a conditional bucket resource would turn
  "change the bucket" into a replacement of a retained resource inside the
  IAM stack, would make every hand-deployed `rayito-m0-iam` drift from the
  template, and would mix a data resource (retained) with IAM resources
  (deleted with the stack). Two small stacks keep each lifecycle simple.
- **Adopt an existing bucket with the default name** without a stack.
  Rejected: the name is derivable from the account ID, so another account
  can create it first and grant us access; the image `PutObject` does not
  send `ExpectedBucketOwner`, so our artifacts would land in their bucket
  and the build role would read what they put there. `bucket_taken` asks
  the user to pass it with `--bucket` if it is really theirs.
- **SSE-KMS with a customer key.** Rejected: a key costs per month and per
  request, and the build role and every publisher would need `kms:Decrypt`
  and `kms:GenerateDataKey`. SSE-S3 (`AES256`) encrypts at rest at no cost,
  and is what S3 applies by default to new objects anyway; the template
  states it explicitly so a later bucket policy cannot change it silently.
- **Versioning.** Rejected: the keys are content-addressed, so a version
  history adds nothing, and expired objects would linger as noncurrent
  versions that cost storage.
- **`DeletionPolicy: Delete`.** Rejected: CloudFormation cannot delete a
  bucket that still holds objects (the stack would end `DELETE_FAILED`), and
  the bucket may also hold `rayito/stacks/` artifacts of optional components
  or data of `persist=`/`transfer=` if the user points them at it.
  `Retain` (and `UpdateReplacePolicy: Retain`) as literals, as
  `infra/efs-volumes.yaml` does; deleting the bucket is an explicit step.
- **`BucketNamespace: account-regional`.** The CloudFormation schema bundled
  with cfn-lint 1.56.3 lists the property with values `global` and
  `account-regional` but no description; left out until it is documented.
- **Make the IAM stack an `OptionalStack` catalog component**
  (`rayito stack deploy iam`). Rejected: the catalog is for optional,
  cost-bearing features, its default stack name would be `rayito-iam`
  instead of the historical `rayito-m0-iam`, and a second way to deploy the
  same stack would confuse users.
- **Always `UpdateStack` the IAM stack** to bring old setups to the packaged
  template. Rejected: it turns a read-only run on a prepared account into a
  write that needs IAM permissions the user of a manually prepared account
  may not have, and it would change policies without the user asking.
- **Verify the cosign bundle inside the SDK** (sigstore-python,
  sigstore-js). Rejected: two new heavy dependencies and a trust-root update
  path for one command; `--artifact` keeps the cosign path open.
- **Embed the expected zip sha256 in the SDK release.** Rejected for now: the
  wheel and npm package are built in jobs that do not depend on `rayd-build`,
  so the hash is not known when they are built; it would need a release
  pipeline change.
- **Run `rayito doctor --launch`** at the end. Rejected: it costs a sandbox
  (≈ $0,002) that the user did not ask for; the final message suggests it.
- **A public `AccountSetup` API.** Rejected as speculative (D1).

## Consequences

- One command prepares any supported account and region: a bucket is
  reused when one exists, and created (encrypted, private, TLS only, image
  zips expiring after 7 days) through `rayito-m0-artifacts` when none does.
- Accounts that worked with 0.14 plan exactly what they planned before; the
  bucket stack only appears on accounts without a usable bucket.
- Deleting `rayito-m0-artifacts` keeps the bucket (`Retain`); the next
  `rayito setup` then reports `bucket_taken` for the default name until the
  user passes it with `--bucket` or deletes it.
- `rayito setup` never changes a working, manually prepared account, and
  never upgrades `rayito-base` to a newer `rayd`; both stay explicit
  (`rayito image publish`).
- Users who prefer cosign-verified artifacts keep that path through
  `--artifact`.
- The SDK now ships `infra/iam.yaml` and `infra/artifact-bucket.yaml`; a
  template change in `infra/` reaches users through
  `gen_stack_assets.py --check`, already a gate.
- Python and TypeScript keep one shared contract (`testdata/account-setup/`),
  so output, JSON and exit codes cannot drift silently.

## Risks

- **Organization SCP.** An SCP in the organization may deny
  `cloudfront:CreateDistribution`, `iam:CreateRole` with fixed names, `cloudformation:CreateStack` with
  `CAPABILITY_NAMED_IAM`, or `s3:CreateBucket`, in some regions. The AWS acceptance (task 4.x)
  checks first, read-only, with `rayito doctor`'s IAM simulation, and records
  the result as a new `AWS_API_NOTES.md` Q row.
- **Network.** Corporate proxies: `urllib` honours `HTTPS_PROXY`; Node's
  `fetch` does not by default. `--artifact` is the documented way around it.
- **Release availability.** During a release PR acceptance the
  `rayd-v<new version>` release does not exist yet; the e2e passes
  `--artifact` with the zip built from the tree (`RAYITO_E2E_SETUP_ARTIFACT`).
- **Region-wide IAM names.** `infra/iam.yaml` names roles
  `rayito-m0-*-<region>`, so stacks in two regions never collide, but a
  half-deleted stack in one region can leave a role name taken; the
  `stack_blocked` message tells the user to delete the stack first.
- **Permissions.** `rayito setup` runs with the caller's credentials and
  needs bootstrap-level rights, as `cdk bootstrap` does: CloudFormation on
  `rayito-m0-iam` and `rayito-m0-artifacts`, IAM role and policy creation,
  and, only when it creates the bucket, what the CloudFormation S3 handlers
  call. The schemas bundled with cfn-lint 1.56.3 list, among others,
  `s3:CreateBucket`, `s3:PutEncryptionConfiguration`,
  `s3:PutBucketPublicAccessBlock`, `s3:PutBucketOwnershipControls`,
  `s3:PutLifecycleConfiguration`, `s3:PutBucketTagging` (the stack tags
  propagate) for `AWS::S3::Bucket`, and `s3:GetBucketPolicy`,
  `s3:PutBucketPolicy` for `AWS::S3::BucketPolicy`. `CallerPolicy` and
  `ImagePublisherPolicy` of `infra/iam.yaml` are runtime policies and grant
  none of this; the docs say to run setup with administrator or
  bootstrap credentials once, then use the narrow policies.
- **Retained bucket after an AWS acceptance.** The run's cleanup deletes
  `rayito-m0-artifacts` and then, explicitly, the bucket it created (empty
  it first); the inventory includes S3 buckets.
