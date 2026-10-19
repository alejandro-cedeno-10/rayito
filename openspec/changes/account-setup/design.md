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

What does **not** exist and is not added here: any code path that creates an
S3 bucket. `AWS_API_NOTES.md` ("Despliegue de agentes de cero
configuración") states "Rayito nunca llama a `CreateBucket`", and no
`CreateBucket`, `PutBucketEncryption`, `PutPublicAccessBlock` or
`AWS::S3::Bucket` appears in the notes or the code. `rayito image publish`
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
  Domain (pure)     _domain.py | domain.ts: classify_stack, choose_bucket_candidate,
                    decide_bucket, classify_image, plan_setup, parse_checksums,
                    verify_checksum, release_tag, release_asset_url,
                    AccountSetupException/AccountSetupError, messages, constants
                        |          |           |           |
  Ports             IamStackGateway  ArtifactBucketGateway  BaseImageGateway
                    BaseImagePublisher  ReleaseArtifactSource   (_ports.py | ports.ts)
                        |          |           |           |
  Adapters (out)    CloudFormationIamStack  -> OptionalStacks -> StackProvisioner (CFN)
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
| `bucket` | `reuse` | never | no |
| `iamStack` | `create`, `update`, `reuse`, `wait` | `create`, `update` | `create`, `update` |
| `baseImage` | `publish`, `reuse`, `wait` | `publish` | `publish` |

`ready` is true when every step is `reuse`. Confirmation is needed when at
least one step is `create`, `update` or `publish`. `wait` costs nothing new:
it waits for something already started.

**Bucket** (`choose_bucket_candidate`, then `decide_bucket`). Exactly one
candidate, the first present of: `--bucket` (`argument`), `RAYITO_BUCKET`
(`environment`), the `ArtifactBucket` parameter of an existing IAM stack
(`iamStack`), the CDK bootstrap bucket `cdk-<qualifier>-assets-<account>-<region>` (default qualifier `hnb659fds`)
(`cdkBootstrap`). It is probed once with `HeadBucket`:

- not found or 403 (`BUCKET_MISSING_CODES`): `no_bucket` for `cdkBootstrap`,
  `bucket_unavailable` for any other source;
- found with `BucketRegion` different from the region: `bucket_region`;
- otherwise `reuse`.

**IAM stack** (`classify_stack`, then the plan). `DescribeStacks` on
`rayito-m0-iam`:

- absent: `create` with `ArtifactBucket=<bucket>`; every other template
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

### D4. Never create a bucket

`rayito setup` reuses a bucket and never creates one, so it keeps the 0.14
rule and calls no S3 write API that is not already in `AWS_API_NOTES.md`.
Without any candidate it stops with `no_bucket`, which names the three ways
out (`--bucket`, `RAYITO_BUCKET`, `cdk bootstrap`). Because the chosen bucket
is written into the IAM stack, the next run finds it there even without
`RAYITO_BUCKET`; the final output prints `RAYITO_BUCKET=<bucket>` for
`rayito agent deploy`, which still resolves its own bucket as in 0.14.

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
 |--plan(request)-------->| iam_stack.observe()          -> DescribeStacks
 |                        | buckets.account_id()         -> GetCallerIdentity
 |                        | buckets.probe(candidate)     -> HeadBucket
 |                        | images.observe(name)         -> GetMicrovmImage, ListMicrovmImageVersions
 |<-----SetupPlan---------| plan_setup(...) (pure)
 | print plan + cost; --dry-run: stop (exit 0)
 | confirmation if needed (--yes / terminal; else needs_confirmation, exit 2)
 |--apply(plan, workdir, emit)->|
 |                        | 1. bucket: nothing to write
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
- **Stack.** `create` and `update` use `OptionalStacks.deploy`, which already
  treats "No updates are to be performed" as success and keeps unlisted
  parameters. An interrupted run leaves the stack `*_IN_PROGRESS`; the next
  run plans `wait`.
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
| `unsupported_region`, `no_bucket`, `bucket_unavailable`, `bucket_region`, `artifact_missing`, `artifact_variant`, `needs_confirmation` | `AccountSetupException(code=…)` | `AccountSetupError` | 2 |
| `stack_blocked`, `stack_incomplete`, `release_download`, `checksum_missing`, `checksum_mismatch`, `image_build_failed` | `AccountSetupException(code=…)` | `AccountSetupError` | 1 |
| User answers "no" at the prompt | `typer.Exit(1)` | `return EXIT_FAILURE` | 1 |
| Doctor reports a `FAIL` | — | — | 1 |
| No region, unknown profile, no credentials | existing `UsageError` | existing `UsageError` | 2 |
| AWS error not handled above (`ClientError`, `StackException`, `AuthenticationException`) | existing `translated_failures` | existing failure translation | 1 |

`AccountSetupException` extends `Exception` (not `SandboxException`: nothing
here is attributable to a sandbox) and is private (`_account_setup._domain`),
like the CLI's own `UsageError`. Its `code` is one of the closed list above
and its message never carries a URL, a presigned link, a token or the raw
AWS message. `stack_blocked` names the stack and its status; the bucket name
appears in bucket errors because the user typed or owns it (the same as
`rayito doctor`'s `bucket` check).

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
- build time: "≈ 10 min" (`configurar-aws.md`, step 4).

`rayito setup` is not an optional feature (it creates nothing that a sandbox
needs at runtime beyond what the tutorial already makes the user create), so
it gets no row in `optional-features.md`; its CLI reference and the tutorial
carry the "Coste y activación" box.

## Alternatives considered

- **Create the bucket when none exists** (encryption, public access block,
  lifecycle), as `cdk bootstrap` does. Rejected for this change: it needs S3
  write APIs or an `AWS::S3::Bucket` template that `AWS_API_NOTES.md` does not
  document, and 0.14 deliberately states that Rayito never creates buckets.
  It can be a follow-up (an `artifact-bucket` template rendered like `iam`,
  with its properties verified offline and recorded in the notes) if the
  maintainer approves it (open question 1 in `tasks.md`).
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

- One command prepares an account where a bucket can be found; otherwise the
  user still runs one `cdk bootstrap` or passes `--bucket`.
- `rayito setup` never changes a working, manually prepared account, and
  never upgrades `rayito-base` to a newer `rayd`; both stay explicit
  (`rayito image publish`).
- Users who prefer cosign-verified artifacts keep that path through
  `--artifact`.
- The SDK now ships `infra/iam.yaml`; a template change in `infra/` reaches
  users through `gen_stack_assets.py --check`, already a gate.
- Python and TypeScript keep one shared contract (`testdata/account-setup/`),
  so output, JSON and exit codes cannot drift silently.

## Risks

- **Test account SCP.** The organisation SCP of the test account denied
  `cloudfront:CreateDistribution` before (Q141). It may also deny
  `iam:CreateRole` with fixed names, or `cloudformation:CreateStack` with
  `CAPABILITY_NAMED_IAM`, in some regions. The AWS acceptance (task 4.x)
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
