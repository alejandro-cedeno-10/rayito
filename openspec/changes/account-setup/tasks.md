## How to use this file

Three lanes run in parallel and never edit the same file: **Track PY**
(`clients/python/`), **Track TS** (`clients/typescript/`) and **Track DOCS**
(everything else except what Track 0 already committed). Each lane works on
its own branch cut from `feat/rayito-setup` and opens a pull request back
into it; nothing is merged without the maintainer's review, and nothing goes
to `main` from a lane. The AWS acceptance (section 4) runs once, on the tip of
`feat/rayito-setup`, after the three lanes merge. Section 5 ("Bucket
creation", the maintainer's decision of 2026-10-19) is a second round with
the same three lanes and the same rules; its shared part (template,
packaging, contract, vectors) is already committed (0.6, 0.7).

Every lane reads `proposal.md`, `design.md`, `specs/account-setup/spec.md`,
the shared contract below and `testdata/account-setup/*.json` before writing
code, plus `.claude/skills/rayito-engineering/SKILL.md` and its references.
Hard rules that apply to every task: code, file and folder names in English;
docstrings, TSDoc, comments and user messages in Spanish; no magic numbers
(every value a named constant citing its source); no inline comments inside
function bodies; no AWS parameter that is not in `AWS_API_NOTES.md` or already
used in the code; nothing public added; no AI attribution in commits or PRs;
commits `git commit -s -S`, Conventional Commits.

## 0. Shared prerequisites (done in this change, do not edit in a lane)

- [x] 0.1 This OpenSpec change, validated with
      `npx -y @fission-ai/openspec@1.10.0 validate account-setup --strict --no-interactive`.
- [x] 0.2 `testdata/account-setup/contract.json` (options, help, exit codes,
      error codes, constants, every message template, JSON keys).
- [x] 0.3 `testdata/account-setup/plan-vectors.json` (16 cases),
      `checksums.json` (7 cases) and `cli-cases.json` (9 cases). Placeholders
      `{sdkVersion}` and `{cdkBucket}` are replaced by each test before
      comparing (`check_hygiene.py` forbids a literal CDK bucket name).
- [x] 0.4 `testdata/cli/surface.json`: the `setup` entry (between the last
      `sandbox` entry and `stack`). The Python surface test fails until Track
      PY adds the command; the TypeScript surface test passes before and
      after.
- [x] 0.5 `scripts/gen_stack_assets.py` renders `infra/iam.yaml`; the
      generated `clients/python/src/rayito/_stacks/_templates/iam.yaml` and
      `clients/typescript/src/stacks/templates/iam.gen.ts` are committed
      (`python3 scripts/gen_stack_assets.py --check` passes).
- [x] 0.6 Bucket creation, shared part: `infra/artifact-bucket.yaml`
      (design D10; `cfn-lint` 1.56.3 clean), rendered by
      `scripts/gen_stack_assets.py` into
      `clients/python/src/rayito/_stacks/_templates/artifact-bucket.yaml` and
      `clients/typescript/src/stacks/templates/artifact-bucket.gen.ts`
      (committed; the generator docstring names both setup templates), its
      `make infra-lint` entry (`ARTIFACT_BUCKET_TEMPLATE`), and
      `scripts/tests/test_artifact_bucket_template.py` (properties pinned,
      days/parameter/output/prefix/default-name checked against the
      contract and `limits.json`).
- [x] 0.7 Bucket creation, contract: `contract.json` (help texts, error
      codes and exit codes without `no_bucket` and with `bucket_taken`,
      `artifact_stack_conflict`; new constants, `resourceActions`,
      `bucketSources`, `bucketProbeStates`, action, cost, progress and error
      templates), `plan-vectors.json` (29 cases; every input has
      `forbiddenBuckets` and `artifactStack`, every expected has
      `bucketReads`; placeholder `{defaultBucket}`), `cli-cases.json`
      (12 cases: the old "sin bucket" error is now a dry run that creates
      one, plus its `--json` twin, a 403 `--bucket` and a taken default
      name).

## Shared contract (Python and TypeScript must match exactly)

The machine-readable source is `testdata/account-setup/contract.json`; this
section is its human summary. If they ever disagree, the JSON wins and the
lane reports the mismatch instead of guessing.

**Command**: `rayito [--profile P] [--region R] [--json] [--verbose] setup
[--bucket BUCKET] [--artifact PATH] [--yes] [--dry-run]`. Parameter order
`bucket`, `artifact`, `yes`, `dry_run`; no environment variable bound by the
parser (`RAYITO_BUCKET` is read by the command, like `rayito doctor`). Help
texts are the `help` strings of the contract, identical in both CLIs.

**Exit codes**: 0 success, dry run, or nothing to do with doctor clean;
1 failure (`stack_blocked`, `stack_incomplete`, `release_download`,
`checksum_missing`, `checksum_mismatch`, `image_build_failed`, a "no" at
the prompt, a doctor `FAIL`, an untranslated AWS error); 2 usage or
environment (`unsupported_region`, `bucket_unavailable`, `bucket_region`,
`bucket_taken`, `artifact_stack_conflict`, `artifact_missing`,
`artifact_variant`, `needs_confirmation`, and the existing no
region/profile/credentials errors). `no_bucket` was removed by section 5.

**Errors**: Python `AccountSetupException(code, message)`, TypeScript
`AccountSetupError` (`code`, `message`, `exitCode` from `errorExitCodes`).
Private (not exported). Human mode: `rayito: <message>` on stderr. JSON mode:
the JSON document with `error: {"code", "message"}` and `exitCode` on
stdout, and the same `rayito: <message>` line on stderr.

**Human output** (stdout in human mode; with `--json` only the document goes
to stdout and progress lines go to stderr). Templates in `messages`:

```text
rayito setup: cuenta {account}, región {region}
  {label} {name}: {action}            # three lines: bucket, pila de IAM, imagen
Coste aproximado:                     # only when a cost line applies
  {text}                              # pila de IAM: gratis | imagen: ... | zip ...: céntimos
La cuenta ya está preparada: no hay nada que crear.   # when ready
no se creó ni cambió nada (--dry-run)                 # with --dry-run
¿Preparar la cuenta? [y/N]:                           # when confirmation is needed
<progress lines>                                      # messages.progress + publish's own lines
<rayito doctor human output>
rayito setup: cuenta preparada en {region}            # doctor without FAIL
RAYITO_TEMPLATE={imageArn}
RAYITO_BUCKET={bucket}
rayito setup: rayito doctor encontró algún FAIL; la cuenta no está lista   # instead, on FAIL
```

Action texts: `bucketReuse` with the source label (`--bucket`,
`RAYITO_BUCKET`, `el de la pila de IAM`, `bootstrap de CDK`, `el de la pila
de artefactos`, `nombre por defecto de Rayito`), `bucketCreate`
(`{stack}` = `rayito-m0-artifacts`, `{source}` = the label), `bucketWait`
(`{stack}`, `{status}`), `stackCreate`,
`stackUpdate`, `stackReuse`, `stackWait`, `imagePublishRelease`,
`imagePublishArtifact`, `imageReuse`, `imageWait`. Cost lines, in this order:
`bucket` (with `{days}` = `artifactExpirationDays`) when the bucket action
is `create`; `iamStack` when the stack action is `create` or `update`;
`image` and `artifact` when the image action is `publish`.

**JSON document** (one object, `json.dumps(indent=2)` formatting already used
by the CLIs, keys in this order):

```json
{
  "rayito": "<sdk version>",
  "region": "us-east-1",
  "account": "123456789012 | null when the plan failed",
  "dryRun": false,
  "ready": false,
  "steps": [
    {"resource": "bucket|iamStack|baseImage", "name": "...",
     "action": "create|update|reuse|wait|publish",
     "source": "argument|environment|iamStack|cdkBootstrap|artifactStack|rayitoDefault|release|artifact|null",
     "status": "<StackStatus>|null", "imageVersion": "<x.y>|null",
     "release": "rayd-v<version>|null", "previousBucket": "<bucket>|null"}
  ],
  "cost": ["pila de IAM: gratis", "..."],
  "result": {"bucket": "...", "imageArn": "...", "imageVersion": "...",
             "artifactUploaded": true},
  "doctor": {"...": "the exact document of `rayito --json doctor`"},
  "error": {"code": "...", "message": "..."},
  "exitCode": 0
}
```

`steps` and `cost` are `[]` and `account` is `null` when the plan itself
failed. `result` is `null` unless `apply` finished; `artifactUploaded` is
`null` when no publish happened. `doctor` is `null` in `--dry-run` and when
setup stopped before the doctor. `ready` is the plan's `ready` in `--dry-run`,
`false` on any error, and "no doctor `FAIL`" after `apply`. In dry run
`exitCode` is 0.

**Constants**: stack `rayito-m0-iam`, component `iam`, capabilities
`["CAPABILITY_NAMED_IAM"]`, parameter `ArtifactBucket`, output
`BuildRoleArn`, image `rayito-base`, variant `full`, base image version
`latest`, CDK qualifier `hnb659fds`, env var `RAYITO_BUCKET`, release tag
`rayd-v<SDK version>`, URL
`https://github.com/alejandro-cedeno-10/rayito/releases/download/{tag}/{asset}`,
assets `rayito-image.zip` and `SHA256SUMS`, download deadline 600 s per asset
(`RELEASE_DOWNLOAD_TIMEOUT_SECONDS` / `RELEASE_DOWNLOAD_TIMEOUT_MS`), usable
stack states `CREATE_COMPLETE`, `UPDATE_COMPLETE`, `UPDATE_ROLLBACK_COMPLETE`,
in-progress suffix `_IN_PROGRESS`, deleting state `DELETE_IN_PROGRESS`,
building version states `PENDING`, `IN_PROGRESS`. Bucket creation (section
5): stack `rayito-m0-artifacts`, component `artifact-bucket`, template
`infra/artifact-bucket.yaml`, parameter and output `BucketName`, default
name `rayito-artifacts-{account}-{region}`, expiration 7 days, image prefix
`rayito/images/`, explicit sources `argument` and `environment`, probe
states `found`, `missing`, `forbidden`, IAM template path `infra/iam.yaml`
(for the `stack_incomplete` message, which now names `{output}` and
`{template}`).

**Hint for unprepared accounts**: `ACCOUNT_SETUP_HINT` appended as
`"{message} ({hint})"`.

**Name map** (Python → TypeScript): `AccountSetup` → `AccountSetup`;
`AccountSetupException` → `AccountSetupError`; `AccountSetupGateways`,
`SetupRequest`, `SetupStep`, `SetupPlan`, `SetupResult`, `StackObservation`,
`BucketCandidate`, `BucketProbe`, `ImageVersionItem`, `ImageObservation`,
`DownloadedRelease`, `PublishedImage`, `ImageGateResult` keep their names;
functions in camelCase (`plan_setup` → `planSetup`, `choose_bucket_candidate`
→ `chooseBucketCandidate`, `decide_bucket` → `decideBucket`,
`classify_stack` → `classifyStack`, `plan_stack` → `planStack`,
`bucket_candidates` → `bucketCandidates`, `creation_target` →
`creationTarget`, `needs_default_probe` → `needsDefaultProbe`,
`plan_bucket_creation` → `planBucketCreation`, `stack_output` →
`stackOutput`, `default_artifact_bucket_name` → `defaultArtifactBucketName`,
`ArtifactStackGateway`, `ARTIFACT_STACK_COMPONENT`, `BucketState` keep their
names,
`classify_image` → `classifyImage`, `plan_image` → `planImage`, `cost_lines`
→ `costLines`, `parse_checksums` → `parseChecksums`, `checksum_for` →
`checksumFor`, `verify_checksum` → `verifyChecksum`, `release_tag` →
`releaseTag`, `release_asset_url` → `releaseAssetUrl`,
`require_supported_region` → `requireSupportedRegion`, `with_setup_hint` →
`withSetupHint`, `cdk_bucket_name` → `cdkBucketName`); times in seconds in
Python, milliseconds in TypeScript.

## 1. Track PY (`clients/python/` only)

Gates, all green before the pull request (from `reference/gates.md`):

```bash
cd clients/python
uv run pytest tests/unit
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest ../../scripts/tests ../../infra/lambdas/events_webhooks/tests ../../infra/lambdas/dashboard/tests ../../infra/lambdas/quota_alerts/tests -p no:cacheprovider
uv run --isolated --python 3.11 pytest tests/unit
uv run --isolated --python 3.13 pytest tests/unit
uv run --group docs mkdocs build -f ../../docs/site/mkdocs.yml --strict
cd ../.. && python3 scripts/check_hygiene.py && python3 scripts/gen_stack_assets.py --check \
  && python3 scripts/gen_limits.py --check && python3 scripts/check_sdk_parity.py \
  && python3 scripts/check_sdk_parity.py --cli
```

### 1.1 Shared constants moved, not duplicated

- [x] 1.1.1 `src/rayito/_images.py`: add `DEFAULT_IAM_STACK_NAME: Final =
      "rayito-m0-iam"`, `BUILD_ROLE_OUTPUT_KEY: Final = "BuildRoleArn"` and
      `BUILDING_VERSION_STATES: Final = frozenset({"PENDING",
      "IN_PROGRESS"})` (docstrings cite `AWS_API_NOTES.md` §4
      `MicrovmImageVersionState`). `cli/_publish.py` keeps its public names
      by importing them: `DEFAULT_STACK_NAME = DEFAULT_IAM_STACK_NAME`,
      `BUILD_ROLE_OUTPUT_KEY` re-imported.
- [x] 1.1.2 `src/rayito/_account_setup/_domain.py` becomes the single home of
      `CDK_BOOTSTRAP_QUALIFIER`, `CDK_ASSETS_BUCKET_TEMPLATE` and
      `cdk_bucket_name(account: str, region: str) -> str`;
      `_agent/_aws_resolve.py` imports them from there (same names, so its
      importers and tests do not change).

### 1.2 Domain `src/rayito/_account_setup/` (pure)

- [x] 1.2.1 `__init__.py`: docstring only, no imports (so `_aws.py` can import
      `_domain` without cycles).
- [x] 1.2.2 `_domain.py` (imports only stdlib, `rayito._limits`,
      `rayito._images` constants): `Literal` types `ResourceKind`,
      `SetupAction`, `BucketSource`, `ImageSource`, `StackCondition`
      (`missing|usable|in_progress|blocked`), `ImageCondition`
      (`missing|launchable|building|unlaunchable`), `ErrorCode` (the 13 codes);
      constants of the contract (`IAM_STACK_NAME = DEFAULT_IAM_STACK_NAME`,
      `ARTIFACT_BUCKET_PARAMETER`, `BASE_IMAGE_NAME = DEFAULT_BASE_IMAGE_NAME`,
      `BASE_IMAGE_VARIANT`, `BUCKET_ENV_VAR`, `RELEASE_TAG_PREFIX`,
      `RELEASE_DOWNLOAD_URL_TEMPLATE`, `IMAGE_ASSET_NAME`,
      `CHECKSUMS_ASSET_NAME`, `RELEASE_DOWNLOAD_TIMEOUT_SECONDS: Final = 600.0`,
      `USABLE_STACK_STATES`, `IN_PROGRESS_STACK_SUFFIX`,
      `DELETING_STACK_STATE`, `CHECKSUM_LINE_PATTERN`), the error, progress,
      download-reason and hint templates (`ERROR_MESSAGES`,
      `PROGRESS_MESSAGES`, `DOWNLOAD_REASONS` as `MappingProxyType`,
      `ACCOUNT_SETUP_HINT`, `HINT_TEMPLATE`), `ERROR_EXIT_CODES`, and the cost
      texts `COST_TEXTS` (each docstring cites the doc page it quotes:
      `cost.md` row "Mantener publicada una versión de imagen",
      `configurar-aws.md` steps 2 and 3).
- [x] 1.2.3 Frozen dataclasses: `SetupRequest(region, bucket,
      environment_bucket, artifact: Path | None, sdk_version)`,
      `StackObservation(name, status: str | None, outputs: Mapping[str, str],
      parameters: Mapping[str, str])`, `BucketCandidate(name, source)`,
      `BucketProbe(exists: bool, region: str | None)`,
      `ImageVersionItem(image_version, state, status, created_at: datetime)`,
      `ImageObservation(name, image_arn, state: str | None, versions:
      tuple[ImageVersionItem, ...])`, `SetupStep(resource, name, action,
      source=None, status=None, image_version=None, release=None,
      previous_bucket=None)` with `as_json() -> dict[str, object]` (contract
      key order), `SetupPlan(region, account, steps: tuple[SetupStep, ...],
      cost: tuple[str, ...], artifact: Path | None, image_arn: str,
      stack_status: str | None)` with properties `ready`,
      `needs_confirmation`, `bucket`, `stack_step`, `image_step`,
      `SetupResult(bucket, image_arn, image_version, artifact_uploaded: bool |
      None)` with `as_json()`, `DownloadedRelease(checksums_text: str,
      artifact: Path, artifact_sha256: str)`, `PublishedImage(image_arn,
      image_version, artifact_uploaded: bool)`, `ImageGateResult(launchable:
      bool, version_state: str)`. Invariants in `__post_init__` (three steps
      in the fixed order; actions allowed per resource as in design D2).
- [x] 1.2.4 `class AccountSetupException(Exception)` with `code: ErrorCode`
      and property `exit_code` (from `ERROR_EXIT_CODES`); constructor
      `AccountSetupException(code: ErrorCode, **fields: str)` formats
      `ERROR_MESSAGES[code]`, so the text is never written twice.
- [x] 1.2.5 Pure functions: `require_supported_region(region: str) -> None`;
      `release_tag(sdk_version: str) -> str`; `release_asset_url(tag: str,
      asset: str) -> str`; `cdk_bucket_name`; `choose_bucket_candidate(request:
      SetupRequest, stack: StackObservation, account: str) ->
      BucketCandidate`; `decide_bucket(candidate: BucketCandidate, probe:
      BucketProbe, region: str) -> SetupStep`; `classify_stack(stack:
      StackObservation) -> StackCondition`; `plan_stack(stack:
      StackObservation, bucket: str) -> SetupStep`; `classify_image(image:
      ImageObservation) -> ImageCondition`; `plan_image(image:
      ImageObservation, request: SetupRequest) -> SetupStep`;
      `cost_lines(steps: Sequence[SetupStep]) -> tuple[str, ...]`;
      `plan_setup(request, account, stack, candidate, probe, image) ->
      SetupPlan`; `parse_checksums(text: str) -> dict[str, str]`;
      `checksum_for(text: str, asset: str) -> str | None`;
      `verify_checksum(release: DownloadedRelease, tag: str) -> None` (raises
      `checksum_missing`/`checksum_mismatch`); `with_setup_hint(message: str)
      -> str`. Rules exactly as design D2/D5.
- [x] 1.2.6 `_iam_stack.py`: `IAM_STACK_COMPONENT: Final = StackComponent(
      name="iam", description=..., cost=CostStatement(creates=(two roles, three
      managed policies), idle_monthly="$0 (IAM no se factura)", removal=...,
      source=...), parameters=(ArtifactBucket required; the six others
      `default=None`), capabilities=("CAPABILITY_NAMED_IAM",))`. Not added to
      `_stacks/_registry.COMPONENTS`.

### 1.3 Ports and service

- [x] 1.3.1 `_ports.py`: `Progress = Callable[[str], None]`; `Protocol`s
      `IamStackGateway` (`observe() -> StackObservation`, `create(bucket: str)
      -> StackObservation`, `update_bucket(bucket: str) -> StackObservation`,
      `wait() -> StackObservation`), `ArtifactBucketGateway` (`account_id() ->
      str`, `probe(bucket: str) -> BucketProbe`), `BaseImageGateway`
      (`observe(name: str) -> ImageObservation`, `wait(image_arn: str,
      image_version: str, on_progress: Progress) -> ImageGateResult`),
      `BaseImagePublisher` (`publish(artifact: Path, *, bucket: str,
      build_role_arn: str, on_progress: Progress) -> PublishedImage`),
      `ReleaseArtifactSource` (`fetch(tag: str, directory: Path, on_progress:
      Progress) -> DownloadedRelease`); frozen dataclass
      `AccountSetupGateways(iam_stack, buckets, images, publisher, releases)`.
- [x] 1.3.2 `_service.py`: `class AccountSetup` with `__init__(self,
      gateways: AccountSetupGateways) -> None` (no call), `plan(self, request:
      SetupRequest) -> SetupPlan` (region check, `account_id`, stack
      `observe`, one `probe`, image `observe`, `plan_setup`) and `apply(self,
      plan: SetupPlan, *, workdir: Path, on_progress: Progress) ->
      SetupResult` (order and rules of design D6; progress lines from
      `PROGRESS_MESSAGES`; `build_role_arn` from the stack observation after
      create/update/wait, `stack_incomplete` if absent; release path:
      `fetch` then `verify_checksum` then `publish`; `--artifact` path:
      `publish` only; image `wait` that ends not launchable raises
      `image_build_failed`). The class docstring carries the "Coste y
      activación" block (six entries).

### 1.4 Adapters

- [x] 1.4.1 `_account_setup/_aws.py`: `CloudFormationIamStack(provisioner:
      StackProvisioner, *, wait_timeout_seconds: float =
      DEFAULT_WAIT_TIMEOUT_SECONDS)`, which builds its own
      `OptionalStacks(provisioner=provisioner)` (`observe` via `status(
      IAM_STACK_COMPONENT, stack_name=IAM_STACK_NAME)`; `create`/
      `update_bucket` via `deploy(IAM_STACK_COMPONENT, stack_name=...,
      parameters={"ArtifactBucket": bucket}, wait_timeout=...)`; `wait` via
      `provisioner.wait(IAM_STACK_NAME, "deployed", timeout)` then `status`);
      `S3ArtifactBuckets(clients: _BucketClients)` where
      `_BucketClients` is a `Protocol` with `s3` and `account_id` (`probe`:
      one `head_bucket`, `BucketRegion` from the response, codes in
      `_agent._aws_resolve.BUCKET_MISSING_CODES` mean `exists=False`, other
      `ClientError`s propagate); `MicrovmBaseImages(clients:
      ImageBuildClients, region: str, account_id: Callable[[], str])`
      (`observe`: `account_image_arn`, `get_microvm_image` with
      `ResourceNotFoundException` as missing, `list_microvm_image_versions`
      paginator; `wait`: `_images.wait_for_gate` with the same tick line
      format as `cli/_publish.wait_for_gate`).
- [x] 1.4.2 `_account_setup/_release.py` (stdlib only): `GithubReleaseSource(*,
      opener: Callable[..., Any] = urllib.request.urlopen, timeout_seconds:
      float = RELEASE_DOWNLOAD_TIMEOUT_SECONDS, clock: Callable[[], float] =
      time.monotonic)`; `fetch` downloads `SHA256SUMS` (text) and
      `rayito-image.zip` (streamed in `DOWNLOAD_CHUNK_BYTES: Final = 1 << 20`
      chunks into `directory`, sha256 computed while writing), enforcing the
      deadline per asset; `HTTPError` → `release_download` with `HTTP
      <status>`, `TimeoutError`/deadline → `tiempo agotado`, `URLError`/
      `OSError` → `error de red`. Messages never include the URL.
- [x] 1.4.3 `cli/_setup_publisher.py` (no `typer` import):
      `PipelineImagePublisher(clients: Clients, *, sleep: Sleeper =
      time.sleep)` implementing `BaseImagePublisher` over
      `resolve_base_image_version(clients.microvms, clients.region,
      LATEST_BASE_IMAGE_VERSION)` (emits `base_image_version=<v>`),
      `PublishSettings(image_name=BASE_IMAGE_NAME, variant="full",
      build_role_arn=<given>, stack_name=DEFAULT_STACK_NAME, memory_mib=
      DEFAULT_MEMORY_MIB, force=False, timeout_seconds=
      DEFAULT_BUILD_TIMEOUT_SECONDS, ...)` and `build_or_reuse(...,
      emit=on_progress)`; a summary with `launchable: False` raises
      `AccountSetupException("image_build_failed", ...)`.
- [x] 1.4.4 `cli/_session.py`: `Clients.account_setup_override:
      AccountSetupGateways | None = None` (test seam, documented next to
      `control_plane_override`; the type imported under `TYPE_CHECKING` so
      `_session.py` gains no runtime import).

### 1.5 CLI

- [x] 1.5.1 `cli/doctor.py`: extract `doctor_document(clients, context,
      results, code) -> dict[str, Any]` from `render_json` (which now emits
      it); no output change.
- [x] 1.5.2 `cli/_setup_output.py` (no `typer` import): plan-rendering
      templates of the contract (`HEADER_TEMPLATE`, `STEP_TEMPLATE`,
      `RESOURCE_LABELS`, `ACTION_TEMPLATES`, `BUCKET_SOURCE_LABELS`,
      `COST_HEADER`, `COST_LINE_TEMPLATE`, `NOTHING_TO_DO`, `DRY_RUN_FOOTER`,
      `CONFIRM_QUESTION`, `DONE_TEMPLATE`, `TEMPLATE_LINE_TEMPLATE`,
      `BUCKET_LINE_TEMPLATE`, `DOCTOR_FAILED`); `plan_lines(plan: SetupPlan)
      -> list[str]`; `final_lines(result: SetupResult, region: str) ->
      list[str]`; `setup_document(*, sdk_version, region, account, dry_run,
      ready, plan, result, doctor, error, exit_code) -> dict[str, Any]`.
- [x] 1.5.3 `cli/setup.py`: `setup(ctx, bucket, artifact, yes, dry_run)` with
      the contract's option help; `gateways_for(clients) -> AccountSetupGateways` returns
      `clients.account_setup_override` or builds the defaults (no AWS call:
      `CloudFormationIamStack(CloudFormationProvisioner(region=clients.region,
      session=clients.session))`, `S3ArtifactBuckets(clients)`,
      `MicrovmBaseImages(clients, clients.region, lambda: clients.account_id)`,
      `PipelineImagePublisher(clients)`, `GithubReleaseSource()`); validates
      `--artifact` before any AWS call (`artifact_missing`, then
      `cli._artifact.marker_variant != "full"` or `marker_has_efs` →
      `artifact_variant`); flow of design D6 (plan, print, dry run, confirm
      with `typer.confirm(CONFIRM_QUESTION)` only when `sys.stdin.isatty()` and
      not `--json`, else `needs_confirmation`; `tempfile.TemporaryDirectory()`
      as `workdir`; `run_doctor(clients, DoctorContext(template=
      BASE_IMAGE_NAME, bucket=result.bucket, transport=clients.transport))`;
      `render_human` or `doctor_document`); errors reported as the contract
      says. The docstring states the cost (memory: every CLI command states
      its cost).
- [x] 1.5.4 `cli/app.py`: `app.command("setup")(setup)`; module docstring
      lists the command.

### 1.6 Hint for unprepared accounts

- [x] 1.6.1 `_aws.py` `BotoControlPlane.run_microvm`: on
      `SandboxNotFoundException` from `_invoke`, raise a new
      `SandboxNotFoundException(with_setup_hint(str(exc)),
      status_code=exc.status_code, aws_code=exc.aws_code)` keeping the
      sanitized cause (`raise ... from exc.__cause__`). Nothing else changes.
- [x] 1.6.2 `_agent/_aws_resolve.py`: `resolve_bucket`'s and
      `ensure_base_image`'s "missing" messages wrapped with `with_setup_hint`.
- [x] 1.6.3 Update any existing test that asserts those exact messages
      (`rg "Rayito no crea buckets|no existe en esta cuenta y región" tests`).

### 1.7 Tests (names by behaviour)

- [x] 1.7.1 `tests/unit/fake_account_setup.py`: in-memory fakes of the five
      ports that record calls (`("describe",)`, `("create", bucket)`,
      `("update", bucket)`, `("wait",)`, `("head_bucket", name)`,
      `("get_image",)`, `("list_versions",)`, `("gate", version)`,
      `("download", asset)`, `("publish", path)`), built from a vector `input`
      (`from_vector_input(input, sdk_version) -> AccountSetupGateways`).
- [x] 1.7.2 `tests/unit/test_account_setup_domain.py`: every case of
      `plan-vectors.json` and `checksums.json`; every constant and message
      against `contract.json` (domain part); `release_tag`,
      `release_asset_url`, `with_setup_hint`; `SetupStep.as_json` key order.
- [x] 1.7.3 `tests/unit/test_account_setup_service.py`: plan makes only the
      read calls (fresh account: `describe`, `head_bucket`, `get_image`, no
      `list_versions`); apply on a fresh account (`create`, `wait` inside
      create, `download` × 2, `publish`; result fields); `update` sends only
      `ArtifactBucket`; stack `wait` path; image `wait` path never publishes;
      waited build failing raises `image_build_failed`; `checksum_mismatch`
      and `checksum_missing` publish nothing; `--artifact` downloads
      nothing; prepared account writes nothing; `stack_incomplete` after a
      create without output; construction makes no call.
- [x] 1.7.4 `tests/unit/test_account_setup_adapters.py`:
      `CloudFormationIamStack` over `tests/unit/fake_stacks.py` (create with
      `CAPABILITY_NAMED_IAM` and only `ArtifactBucket`; update keeps the six
      others as `UsePreviousValue`); `IAM_STACK_COMPONENT` parameters equal the
      packaged `iam.yaml` parameters (reuse the helpers of
      `test_m15_stacks_template_parameters.py`); `iam` not in `COMPONENTS`;
      `S3ArtifactBuckets` with `botocore.stub.Stubber` (200 with
      `BucketRegion`, 404, 403, other error propagates);
      `MicrovmBaseImages` with `Stubber` (missing image, versions,
      gate wait); default gateways built with a `boto3.session.Session.client`
      spy make zero calls.
- [x] 1.7.5 `tests/unit/test_account_setup_release.py`: `GithubReleaseSource`
      with a fake opener: exact URLs, streamed file and sha256, HTTP 404,
      timeout, network error, deadline; messages contain no URL.
- [x] 1.7.6 `tests/unit/cli/test_setup.py`: every case of `cli-cases.json`
      through `CliRunner` with `obj=Clients(..., account_setup_override=...)`;
      apply flows with `rayito.cli.setup.run_doctor` monkeypatched (success
      prints the last three lines; one `FAIL` exits 1 with `DOCTOR_FAILED`);
      a terminal "n" exits 1 with no write (`sys.stdin.isatty` patched);
      `--json` apply document keys and `doctor` embedded; artifact errors exit
      2 before any fake call; plan-rendering templates against
      `contract.json`.
- [x] 1.7.7 `tests/unit/test_account_setup_hint.py`: `Sandbox.create` with a
      `Stubber` whose `run_microvm` raises `ResourceNotFoundException` →
      `SandboxNotFoundException`, `aws_code` unchanged, message contains
      `rayito setup`; `resolve_bucket` and `ensure_base_image` messages.
- [x] 1.7.8 `tests/unit/cli/test_cli_surface.py`: `assert ("setup",) in
      paths`; the golden from Track 0 must then match without regeneration.
- [x] 1.7.9 `tests/unit/cli/test_doctor.py`: `render_json` output equals
      `doctor_document`.
- [x] 1.7.10 `tests/e2e/test_account_setup.py` (AWS, header documents the
      variables generically): skips without `RAYITO_E2E=1` and
      `RAYITO_E2E_SETUP_REGION`; optional `RAYITO_E2E_SETUP_BUCKET` and
      `RAYITO_E2E_SETUP_ARTIFACT`. Pre-check read-only: if `rayito-m0-iam` or
      `rayito-base` already exist in that region, skip the create tests (never
      touch what existed). Tests: dry run plans `create`/`publish` and writes
      nothing; `--yes --json` exits 0 with `ready: true`; a second run plans
      three `reuse` and creates no new image version; resume: start `setup
      --yes` in a subprocess, kill it once a version is `IN_PROGRESS`, re-run,
      assert one version only. Plus, in the default e2e region, `setup
      --dry-run --json` on the already prepared account is three `reuse` and
      zero writes. Teardown deletes only what the run created (image, stack,
      the artifact key only when `artifactUploaded` was true), reusing
      `tests/e2e/image_cleanup.py`.

### 1.8 Changelog

- [x] 1.8.1 `clients/python/CHANGELOG.md`, `## [Unreleased]` → `### Añadido`
      (`rayito setup`) and `### Cambiado` (the hint sentence in the two
      errors).

## 2. Track TS (`clients/typescript/` only)

Gates, all green before the pull request:

```bash
cd clients/typescript
pnpm install --frozen-lockfile
pnpm lint
pnpm typecheck
pnpm build
pnpm test
pnpm pack:check
python3 ../../scripts/check_docs_examples.py --typescript
cd ../.. && python3 scripts/check_hygiene.py && python3 scripts/gen_stack_assets.py --check \
  && python3 scripts/check_sdk_parity.py && python3 scripts/check_sdk_parity.py --cli
```

### 2.1 Shared constants moved, not duplicated

- [ ] 2.1.1 `src/images/gateway.ts`: `BUILDING_VERSION_STATES:
      ReadonlySet<string>` (`PENDING`, `IN_PROGRESS`).
      `DEFAULT_STACK_NAME` and `BUILD_ROLE_OUTPUT_KEY` stay in
      `images/publish-domain.ts` and are imported, not copied.
- [ ] 2.1.2 `src/account-setup/domain.ts` becomes the home of
      `CDK_BOOTSTRAP_QUALIFIER` and `cdkBucketName`; `agent/aws-resolve.ts`
      imports and re-exports them (same exported names).

### 2.2 Domain `src/account-setup/` (pure; no `@aws-sdk/*`, no `node:` I/O)

- [ ] 2.2.1 `domain.ts`: the types, constants, messages, error class and pure
      functions of 1.2 with the name map of the contract
      (`RELEASE_DOWNLOAD_TIMEOUT_MS = 600_000`; `IAM_STACK_NAME =
      DEFAULT_STACK_NAME`; `BASE_IMAGE_NAME = DEFAULT_BASE_IMAGE_NAME`).
      Interfaces instead of dataclasses, `readonly` everywhere;
      `SetupStep`/`SetupResult` JSON via `stepJson(step)`/`resultJson(result)`
      in contract key order. `AccountSetupError extends Error` with `code`
      and `exitCode`.
- [ ] 2.2.2 `iam-stack.ts`: `IAM_STACK_COMPONENT: StackComponent` (as 1.2.6);
      not added to `stacks/registry.ts`.
- [ ] 2.2.3 `ports.ts`: `Progress`, `IamStackGateway`,
      `ArtifactBucketGateway`, `BaseImageGateway`, `BaseImagePublisher`,
      `ReleaseArtifactSource`, `AccountSetupGateways` (async methods, same
      shapes as 1.3.1).
- [ ] 2.2.4 `service.ts`: `class AccountSetup` (`constructor(gateways)`,
      `plan(request): Promise<SetupPlan>`, `apply(plan, { workdir, onProgress
      }): Promise<SetupResult>`), same rules as 1.3.2, with the "Coste y
      activación" TSDoc block.
- [ ] 2.2.5 `release.ts`: `GithubReleaseSource({ fetch = globalThis.fetch,
      timeoutMs = RELEASE_DOWNLOAD_TIMEOUT_MS })`; streams the zip to
      `directory` with `node:fs` and hashes with `node:crypto`; same error
      mapping and reasons as 1.4.2 (`AbortSignal.timeout` for the deadline).

### 2.3 Adapters and CLI

- [ ] 2.3.1 `src/stacks/packaging.ts`: `iam` in `ASSETS` (import of
      `./templates/iam.gen.js`).
- [ ] 2.3.2 `src/cli/setup-adapters.ts`: `SdkIamStack(provisioner:
      StackProvisioner, options)` building its own `OptionalStacks({
      provisioner })` (same methods as 1.4.1),
      `SdkArtifactBuckets(doctor: Pick<DoctorAws, "headBucket">, accountId: ()
      => Promise<string>)` (an `AwsOperationError` whose `code` is in the
      bucket-missing set of `agent/aws-resolve.ts` means missing),
      `SdkBaseImages(ports: PublishPorts, options: { sleep, now })` over
      `images/gateway.ts` (`getMicrovmImage`, `listMicrovmImageVersions`,
      `waitForGate`), `PipelineImagePublisher(ports: PublishPorts, options)`
      over `resolveBaseImageVersion` + `buildOrReuse` with `fileArtifacts`
      (same settings as 1.4.3), and `sdkAccountSetupGateways({ region,
      credentials, doctor, publishPorts }): AccountSetupGateways`.
- [ ] 2.3.3 `src/cli/services.ts`: `readonly accountSetup:
      AccountSetupGateways` on `CliServices`; `aws-services.ts` builds it with
      `sdkAccountSetupGateways` from the same region, credentials, `doctor`
      and `publishPorts` (no AWS call at construction). Add a fake
      `accountSetup` to the three test files that build a `CliServices`
      (`tests/unit/cli/case-world.ts`, `tests/unit/cli/image-commands.test.ts`,
      `tests/unit/dashboard-command.test.ts`).
- [ ] 2.3.4 `src/cli/doctor/render.ts`: export `doctorDocument(...)` used by
      `renderJson`; no output change.
- [ ] 2.3.5 `src/cli/console.ts`: a `canPrompt` getter (stdin is a TTY) if
      none exists, used to decide `needs_confirmation`.
- [ ] 2.3.6 `src/cli/setup-output.ts`: `planLines`, `finalLines`,
      `setupDocument` and the plan-rendering templates (as 1.5.2).
- [ ] 2.3.7 `src/cli/commands/setup.ts`: `setupCommand = defineCommand({ path:
      ["setup"], help, parameters: [option(["--bucket"]), option(["--artifact"],
      { type: "path" }), flag(["--yes"]), flag(["--dry-run"])], run })` with
      the same flow as 1.5.3 (`--artifact` checked with
      `images/artifact-node.inspectArtifact`; `mkdtemp` workdir removed in
      `finally`; `runDoctor(services.doctor, newDoctorContext({ template:
      BASE_IMAGE_NAME, bucket, launch: false }))`).
- [ ] 2.3.8 `src/cli/registry.ts`: `setupCommand` in `COMMANDS`.

### 2.4 Hint for unprepared accounts

- [ ] 2.4.1 `src/aws/control-plane.ts` `runMicrovm`: a
      `SandboxNotFoundError` from `#invoke` is rethrown as a new
      `SandboxNotFoundError(withSetupHint(error.message), { statusCode,
      awsCode, cause })` with the same fields.
- [ ] 2.4.2 `src/agent/aws-resolve.ts`: `resolveBucket` and `ensureBaseImage`
      messages wrapped with `withSetupHint`; update tests that assert them.

### 2.5 Tests (names by behaviour)

- [ ] 2.5.1 `tests/unit/account-setup-fakes.ts` (as 1.7.1) and a
      `loadAccountSetupTestdata(name)` helper next to `tests/unit/cli/paths.ts`.
- [ ] 2.5.2 `tests/unit/account-setup.test.ts`: plan vectors, checksum
      vectors, contract constants and messages, service flows of 1.7.3,
      construction makes no call.
- [ ] 2.5.3 `tests/unit/account-setup-release.test.ts`: as 1.7.5 with a fake
      `fetch`.
- [ ] 2.5.4 `tests/unit/cli/setup-adapters.test.ts`: as 1.7.4 over
      `tests/unit/m15-fake-stacks.ts`, `tests/unit/cli/doctor-fakes.ts` and
      `tests/unit/cli/image-fakes.ts`; `IAM_STACK_COMPONENT` parameters equal
      the `iam.gen.ts` template parameters; `iam` not in the stack registry.
- [ ] 2.5.5 `tests/unit/cli/setup.test.ts`: as 1.7.6 (every `cli-cases.json`
      case; apply flows with a fake doctor; prompt "n"; JSON document).
- [ ] 2.5.6 `tests/unit/account-setup-hint.test.ts`: as 1.7.7 for
      `runMicrovm`, `resolveBucket`, `ensureBaseImage`.
- [ ] 2.5.7 `tests/unit/cli/doctor.test.ts`: `renderJson` equals
      `doctorDocument`. `tests/unit/cli/surface.test.ts` passes with the
      Track 0 golden unchanged.
- [ ] 2.5.8 `tests/e2e/account-setup.e2e.test.ts`: as 1.7.10, reusing
      `tests/e2e/image-cleanup.ts`; hooks get their own timeout (stack create
      plus a build exceed the default `hookTimeout`).

### 2.6 Changelog

- [ ] 2.6.1 `clients/typescript/CHANGELOG.md`, `## [Unreleased]`, same two
      entries as 1.8.1.

## 3. Track DOCS (no file under `clients/`)

Gates:

```bash
cd clients/python && uv run --group docs mkdocs build -f ../../docs/site/mkdocs.yml --strict
cd clients/python && uv run --group dev python ../../scripts/check_docs_examples.py --ruff --mypy
# after Track PY is merged into feat/rayito-setup (the CLI must know `setup`):
cd clients/python && uv run --group dev python ../../scripts/check_docs_examples.py --cli
python3 scripts/check_docs_i18n.py
npx -y @fission-ai/openspec@1.10.0 validate --all --strict --no-interactive
python3 scripts/check_hygiene.py
gitleaks git --log-opts=HEAD --redact --no-banner .
```

- [ ] D.1 `ARCHITECTURE.md`: ADR-030 "rayito setup: preparar la cuenta en una
      orden" (Spanish, from `design.md`: context, decisions D1-D9,
      alternatives, consequences).
- [ ] D.2 `docs/site/docs/index.md` / `index.en.md`: the "Prepara tu cuenta,
      una vez" / "Prepare your account, once" block becomes `rayito setup`
      (keep the anchor `prepara-tu-cuenta-una-vez`), with `rayito doctor`
      still shown as the check; the "15 minutos" sentence says the build
      wait is still about 10 minutes.
- [ ] D.3 `docs/site/docs/primeros-pasos/configurar-aws.md` / `.en.md`
      (tutorial): step 1 credentials and region, step 2 `rayito setup
      --dry-run` then `rayito setup`, with the plan and final output as a
      `text` block (copied from `cli-cases.json`, `{cdkBucket}` written as
      `amzn-s3-demo-bucket` with source `--bucket` so no CDK name appears),
      the "Coste y activación" box (six entries), what it reuses and what it
      never creates (no bucket: `cdk bootstrap` or `--bucket`); the policy
      attachment step stays; `RAYITO_TEMPLATE`; the doctor section stays.
      The current manual steps (bucket, `aws cloudformation deploy`, release
      download with cosign, `rayito image publish`) move into a collapsible
      "A mano, sin `rayito setup`" / "By hand, without `rayito setup`", also
      used for cosign-verified artifacts with `--artifact`.
- [ ] D.4 `AWS_API_NOTES.md`: in §21 add `Capabilities=["CAPABILITY_NAMED_IAM"]`
      for `infra/iam.yaml` (named roles `rayito-m0-*`), verified offline
      against the `Capability` enum of botocore 1.43.103 (2026-10-19); a new
      short section "Preparación de la cuenta (`rayito setup`)" listing the
      calls it makes, each pointing to where it is already documented, and
      the HTTPS download of the two release assets (not an AWS API).
- [ ] D.5 `docs/site/docs/cli.md` / `cli.en.md`: a `rayito setup` section
      (synopsis, options, plan rules, exit codes, JSON keys, the "Coste y
      activación" box, `--dry-run` example); mention it in the command list.
- [ ] D.6 `docs/site/docs/verify.md` / `verify.en.md` and `SECURITY.md` T10:
      what `rayito setup` checks (sha256 against the release `SHA256SUMS`,
      over HTTPS) and what it does not (the cosign signature); how to publish
      a cosign-verified zip with `rayito setup --artifact`.
- [ ] D.7 `docs/site/docs/cost.md` / `cost.en.md`: one row "Preparar la cuenta
      (`rayito setup`)" in the summary table, quoting the existing figures
      (no new number).
- [ ] D.8 `docs/site/docs/operacion/solucion-de-problemas.md` / `.en.md`: a
      "rayito setup" section with each error code, its exit code and the fix.
- [ ] D.9 `docs/site/docs/migrar-desde-e2b/index.md` / `.en.md` step 1, and
      `docs/site/docs/guias/despliega-tu-agente.md` / `.en.md` prerequisites:
      point to `rayito setup`.
- [ ] D.10 `README.md` / `README.es.md`: the account preparation steps become
      `rayito setup` (manual path linked to the tutorial).
- [ ] D.11 `infra/README.md` (`iam.yaml` is deployed by `rayito setup` or by
      hand) and `CONTRIBUTING.md` (the e2e precondition may use `rayito
      setup`; the manual command stays valid).
- [ ] D.12 `rg -n "aws cloudformation deploy --stack-name rayito-m0-iam|Prepara tu cuenta" docs README* CONTRIBUTING.md`
      shows no stale first-time instructions that ignore `rayito setup`.

## 4. AWS acceptance (after the three lanes merge, on `feat/rayito-setup`)

Regression level: SDK and CLI change, no `rayd`, image, proto or `infra/*.yaml`
change (`reference/release-and-aws.md`): full CI, local e2e, and a short,
targeted AWS check per language lane, with inventory before and after.

- [ ] 4.1 Read-only pre-check in the test account: `rayito doctor` IAM
      simulation for `cloudformation:CreateStack`, `iam:CreateRole` and
      `iam:CreatePolicy` in the chosen regions (SCP risk); stop and report if
      denied.
- [ ] 4.2 Inventory of the two lane regions (stacks, images and versions, S3
      `rayito/images/` keys, roles `rayito-m0-*`).
- [ ] 4.3 Python lane e2e (`RAYITO_E2E_SETUP_REGION=<region A>`) and
      TypeScript lane e2e (`<region B>`) in parallel, never the same region.
- [ ] 4.4 Prepared-account check in the main test region (`--dry-run`, three
      `reuse`, zero writes) in both CLIs.
- [ ] 4.5 Inventory again; the difference is zero. Record the measurements
      (build time, any SCP finding) as the next free Q rows of
      `AWS_API_NOTES.md`.
- [ ] 4.6 Review sweep (`reference/review-checklist.md`), then archive this
      change (`openspec archive account-setup --yes`).

## 5. Bucket creation (maintainer decision 2026-10-19)

`rayito setup` creates the artifact bucket when none can be reused, through
`infra/artifact-bucket.yaml` deployed as `rayito-m0-artifacts` (design D2
"Bucket", D4, D10; spec "The artifact bucket is reused when possible and
otherwise created through CloudFormation"). The shared part is done (0.6,
0.7): do not edit `infra/`, `scripts/`, `Makefile` or `testdata/` in a lane;
report a contract mismatch instead. Three lanes, each on its own branch cut
from `feat/rayito-setup` after 0.6/0.7, never touching each other's files.
Same gates as sections 1, 2 and 3. Until 5.PY and 5.TS land, these tests fail
against the updated contract (expected): Python
`tests/unit/test_account_setup_domain.py` (plan vectors, contract constants
and messages), `tests/unit/cli/test_setup.py` (CLI cases);
TypeScript `tests/unit/account-setup.test.ts`, `tests/unit/cli/setup.test.ts`.

### 5.PY Track PY (`clients/python/` only)

- [ ] 5.PY.1 `src/rayito/_account_setup/_domain.py`:
      - Types: `BucketSource` adds `"artifactStack"`, `"rayitoDefault"`; new
        `BucketState = Literal["found", "missing", "forbidden"]`;
        `ErrorCode` drops `"no_bucket"`, adds `"bucket_taken"`,
        `"artifact_stack_conflict"`.
      - `ALLOWED_ACTIONS["bucket"] = frozenset({"create", "reuse", "wait"})`.
      - Constants (docstrings cite the contract and design D4/D10):
        `ARTIFACT_STACK_NAME: Final = "rayito-m0-artifacts"`,
        `ARTIFACT_COMPONENT_NAME: Final = "artifact-bucket"`,
        `BUCKET_NAME_PARAMETER: Final = "BucketName"`,
        `BUCKET_NAME_OUTPUT_KEY: Final = "BucketName"`,
        `DEFAULT_ARTIFACT_BUCKET_TEMPLATE: Final = "rayito-artifacts-{account}-{region}"`,
        `ARTIFACT_EXPIRATION_DAYS: Final = 7` (equals the template default;
        `scripts/tests/test_artifact_bucket_template.py` enforces it through
        the contract), `IAM_TEMPLATE_PATH: Final = "infra/iam.yaml"`,
        `ARTIFACT_TEMPLATE_PATH: Final = "infra/artifact-bucket.yaml"`,
        `EXPLICIT_BUCKET_SOURCES: Final = frozenset({"argument", "environment"})`.
      - `BUCKET_SOURCE_LABELS` adds the two labels; `COST_TEXTS` adds
        `"bucket"` (template with `{days}`, formatted by `cost_lines`);
        `ERROR_MESSAGES`, `ERROR_EXIT_CODES` and `PROGRESS_MESSAGES`
        (`bucketCreate`, `bucketWait`, `bucketReady`) exactly as the contract.
      - `BucketProbe(state: BucketState, region: str | None)` replaces
        `exists: bool`.
      - New pure functions: `default_artifact_bucket_name(account, region) ->
        str`; `bucket_candidates(request, stack, account) ->
        tuple[BucketCandidate, ...]` (replaces `choose_bucket_candidate`,
        which is removed: explicit name alone, else IAM stack bucket if the
        stack exists and has it, then the CDK bucket); `decide_bucket(
        candidate, probe, region) -> SetupStep | None` (`None` = next
        candidate or the creation phase; rules of design D2);
        `creation_target(request, account) -> BucketCandidate` (explicit
        name with its source, else the default with `rayitoDefault`);
        `needs_default_probe(target, artifact_stack: StackObservation) ->
        bool` (`rayitoDefault` and no stack); `plan_bucket_creation(target,
        artifact_stack, probe: BucketProbe | None, region) -> SetupStep`
        (`reuse`/`wait` from the stack with source `artifactStack` and the
        stack `status`, `create` with the target's source, or
        `stack_blocked`, `stack_incomplete`, `artifact_stack_conflict`,
        `bucket_taken`); `stack_output(observation, key, template) -> str`
        (raises `stack_incomplete` with `name`, `output`, `template`; used for
        `BuildRoleArn` and `BucketName`).
      - Changed: `plan_stack` raises `stack_incomplete` through the new
        fields; `plan_setup(request, account, stack, bucket_step, image)`
        takes the decided bucket step (no candidate/probe); `cost_lines`
        puts the `bucket` line first when the bucket action is `create`.
- [ ] 5.PY.2 New `src/rayito/_account_setup/_artifact_stack.py`:
      `ARTIFACT_STACK_COMPONENT: Final = StackComponent(name=
      ARTIFACT_COMPONENT_NAME, description=..., parameters=(StackParameter(
      BUCKET_NAME_PARAMETER, ..., required=True), StackParameter(
      "ArtifactExpirationDays", ...)), capabilities=(), cost=CostStatement(
      creates=("AWS::S3::Bucket", "AWS::S3::BucketPolicy"), idle_monthly=
      <the zips' storage, "céntimos", expiring after 7 days>, removal=<the
      stack and the policy are deleted, the bucket is kept (Retain)>,
      source="AWS_API_NOTES.md, Preparación de la cuenta"))`. Not in
      `_stacks/_registry.COMPONENTS`.
- [ ] 5.PY.3 `src/rayito/_account_setup/_ports.py`: `ArtifactStackGateway`
      (`observe() -> StackObservation`, `create(bucket: str) ->
      StackObservation`, `wait() -> StackObservation`; no update: the bucket
      name of the stack never changes); `AccountSetupGateways.artifact_stack`
      right after `iam_stack`.
- [ ] 5.PY.4 `src/rayito/_account_setup/_service.py`: `plan` = region check,
      `account_id`, IAM `observe`, `_resolve_bucket` (probe each
      `bucket_candidates` until `decide_bucket` returns a step; else
      `creation_target`, artifact stack `observe`, the default probe only if
      `needs_default_probe`, `plan_bucket_creation`), image `observe`,
      `plan_setup`. `apply` = `_apply_bucket(plan, on_progress) -> str`
      (`create` → progress `bucketCreate`, `artifact_stack.create(name)`;
      `wait` → `bucketWait`, `artifact_stack.wait()`; both then
      `stack_output(..., BUCKET_NAME_OUTPUT_KEY, ARTIFACT_TEMPLATE_PATH)` and
      `bucketReady`; `reuse` → `plan.bucket`), then `_apply_stack(plan,
      bucket, on_progress)` and the image with that bucket; `SetupResult.bucket`
      is that bucket. `_build_role_of` becomes `stack_output(...,
      BUILD_ROLE_OUTPUT_KEY, IAM_TEMPLATE_PATH)`. The "Coste y activación"
      docstring names the extra reads, the bucket stack and the
      bootstrap-level IAM requirement.
- [ ] 5.PY.5 `src/rayito/_account_setup/_aws.py`: `CloudFormationIamStack`
      becomes `CloudFormationSetupStack(provisioner, *, component,
      stack_name, bucket_parameter, wait_timeout_seconds=
      DEFAULT_WAIT_TIMEOUT_SECONDS)` (same `observe`/`create`/
      `update_bucket`/`wait`; `_observation(status, stack_name)`), with two
      factories `iam_stack_gateway(provisioner, **kw)` and
      `artifact_stack_gateway(provisioner, **kw)`. `S3ArtifactBuckets.probe`
      returns the three states: `BUCKET_NOT_FOUND_CODES: Final =
      frozenset({"404", "NoSuchBucket", "NotFound"})` → `missing`,
      `BUCKET_FORBIDDEN_CODES: Final = frozenset({"403"})` → `forbidden`
      (docstrings cite the `HeadBucket` row of `AWS_API_NOTES.md`), any other
      `ClientError` propagates. `_agent._aws_resolve.BUCKET_MISSING_CODES`
      stays as it is for `deploy_agent`.
- [ ] 5.PY.6 `src/rayito/cli/setup.py`: `gateways_for` builds one
      `CloudFormationProvisioner` and passes it to both factories; the
      command help and the `--bucket` help are the contract's strings.
      `cli/_setup_output.py`: `ACTION_TEMPLATES` adds `bucketCreate`,
      `bucketWait`; `action_text` renders the bucket step by action
      (`stack=ARTIFACT_STACK_NAME`, the source label, the status).
- [ ] 5.PY.7 Tests (`tests/unit/`):
      - `fake_account_setup.py`: `FakeArtifactStack` recording
        `("describe_artifact_stack",)`, `("create_artifact_stack", bucket)`,
        `("wait_artifact_stack",)`; `FakeBuckets.probe` answers from
        `buckets`/`forbiddenBuckets`; `from_vector_input` reads
        `artifactStack`; `fill_placeholders` also replaces
        `{defaultBucket}`; helper `bucket_reads(calls) -> list[str]`
        (`head:<name>` and `artifactStack`).
      - `test_account_setup_domain.py`: plan vectors through
        `AccountSetup(gateways).plan(request)` (not `plan_setup`), with
        `bucketReads`; every contract constant (except `imageArtifactPrefix`,
        checked by the scripts test), `resourceActions`, `bucketSources`,
        `bucketProbeStates` and every message against the domain;
        `default_artifact_bucket_name`.
      - `test_account_setup_service.py`: fresh account without bucket →
        calls in order `create_artifact_stack(<default>)`, IAM `create(<default>)`,
        downloads, `publish(bucket=<default>)`; `--bucket` 404 creates that
        name; bucket `wait` never creates; a created bucket stack without the
        `BucketName` output raises `stack_incomplete`; an account with a
        reusable bucket records no `describe_artifact_stack` (0.14
        behaviour).
      - `test_account_setup_adapters.py`: `ARTIFACT_STACK_COMPONENT`
        parameters equal the packaged `artifact-bucket.yaml` parameters;
        `artifact-bucket` not in `COMPONENTS`; the artifact gateway's
        `create` sends only `{"BucketName": ...}` and no capabilities (over
        `fake_stacks.py`); `probe` 200 → `found` with region, 404 →
        `missing`, 403 → `forbidden`, other error propagates; default
        gateways construction still makes zero calls.
      - `cli/test_setup.py`: the 12 `cli-cases.json`; an apply on an account
        without bucket prints the `bucketCreate`/`bucketReady` progress and
        ends with `RAYITO_BUCKET=<default>`.
- [ ] 5.PY.8 `tests/e2e/test_account_setup.py`: one more test behind
      `RAYITO_E2E_SETUP_CREATE_BUCKET=1`: `setup --yes --bucket
      rayito-e2e-setup-<random hex>` (a name with no account) creates
      `rayito-m0-artifacts` and the bucket; the bucket has the template's
      stack `CREATE_COMPLETE` with the `BucketName` output, and `HeadBucket`
      finds the bucket in the region (its configuration is pinned by
      `scripts/tests/test_artifact_bucket_template.py`; no new S3 read API);
      a second `--dry-run` plans `reuse`. Skip if
      `rayito-m0-artifacts` already exists in the region. Teardown deletes
      what the run created only: the image and the IAM stack as today, then
      `rayito-m0-artifacts`, then the retained bucket (emptied first).
- [ ] 5.PY.9 `clients/python/CHANGELOG.md` `## [Unreleased]`: `### Añadido`
      "rayito setup crea el bucket de artefactos con la pila
      rayito-m0-artifacts cuando no hay ninguno"; `### Cambiado` the error
      codes (`no_bucket` removed, `bucket_unavailable` only for 403, new
      `bucket_taken`, `artifact_stack_conflict`).

### 5.TS Track TS (`clients/typescript/` only)

- [ ] 5.TS.1 `src/account-setup/domain.ts`: the same types, constants,
      messages and pure functions as 5.PY.1 with the name map
      (`BucketProbe { state: BucketState; region: string | undefined }`;
      `chooseBucketCandidate` removed; `planSetup({ request, account, stack,
      bucketStep, image })`; `ARTIFACT_EXPIRATION_DAYS = 7`).
- [ ] 5.TS.2 New `src/account-setup/artifact-stack.ts`:
      `ARTIFACT_STACK_COMPONENT: StackComponent` (as 5.PY.2); not in
      `src/stacks/registry.ts`.
- [ ] 5.TS.3 `src/stacks/packaging.ts`: `"artifact-bucket"` in `ASSETS`
      (import of `./templates/artifact-bucket.gen.js`, already generated).
- [ ] 5.TS.4 `src/account-setup/ports.ts`: `ArtifactStackGateway`
      (`observe`, `create(bucket)`, `wait`, all async);
      `AccountSetupGateways.artifactStack`.
- [ ] 5.TS.5 `src/account-setup/service.ts`: `plan`/`apply` as 5.PY.4
      (`#resolveBucket`, `#applyBucket`), same progress lines and TSDoc
      "Coste y activación" update.
- [ ] 5.TS.6 `src/cli/setup-adapters.ts`: `SdkIamStack` becomes
      `SdkSetupStack(provisioner, { component, stackName, bucketParameter,
      waitTimeoutMs })` with `sdkIamStack(provisioner)` and
      `sdkArtifactStack(provisioner)`; `SdkArtifactBuckets.probe` returns
      the three states with `BUCKET_NOT_FOUND_CODES` and
      `BUCKET_FORBIDDEN_CODES` (exported `ReadonlySet<string>` in this file;
      `agent/aws-resolve.ts` `BUCKET_MISSING_CODES` unchanged);
      `sdkAccountSetupGateways` builds one `CloudFormationProvisioner` for
      both stacks.
- [ ] 5.TS.7 `src/cli/setup-output.ts`: `bucketCreate`/`bucketWait` action
      texts; `src/cli/commands/setup.ts`: command and `--bucket` help from
      the contract.
- [ ] 5.TS.8 Tests: `tests/unit/account-setup-fakes.ts` (as 5.PY.7 fakes,
      `{defaultBucket}`, `bucketReads`), `tests/unit/account-setup.test.ts`
      (plan vectors through `AccountSetup.plan`, contract, service flows of
      5.PY.7), `tests/unit/cli/setup-adapters.test.ts` (component parameters
      equal `artifact-bucket.gen.ts`, not in the registry, `create` sends
      only `BucketName`, probe states), `tests/unit/cli/setup.test.ts`
      (12 CLI cases, apply without bucket), `tests/e2e/account-setup.e2e.test.ts`
      (as 5.PY.8, `RAYITO_E2E_SETUP_CREATE_BUCKET=1`, own hook timeouts).
- [ ] 5.TS.9 `clients/typescript/CHANGELOG.md`, same entries as 5.PY.9.

### 5.DOCS Track DOCS (no file under `clients/`, `infra/`, `scripts/`, `testdata/`, `Makefile`)

- [ ] 5.D.1 `ARCHITECTURE.md` ADR-030: amend point 4 ("nunca crea un
      bucket") with the new rule and the reuse/creation phases (design D2,
      D4), add the template (D10), the new alternatives (raw S3 calls,
      bucket inside the IAM stack, adopting the default name, KMS,
      versioning, `Delete`), consequences and the permissions risk. Dated
      "Enmienda 2026-10-19".
- [ ] 5.D.2 `AWS_API_NOTES.md`: in "Despliegue de agentes de cero
      configuración", say the "nunca llama a `CreateBucket`" rule is about
      the SDK's own calls (`deploy_agent` still only reuses). In
      "Preparación de la cuenta": the bucket row lists up to three
      `HeadBucket` and the 404/403 split; a new row for
      `rayito-m0-artifacts` (`DescribeStacks`/`CreateStack` with only
      `BucketName`, no `Capabilities`, §21); a sub-table "Plantilla
      `infra/artifact-bucket.yaml`" with the resource types and exactly the
      properties of design D10, verified offline against the CloudFormation
      schemas bundled with cfn-lint 1.56.3 (2026-10-19), and the S3
      permissions the schemas' handlers list for `AWS::S3::Bucket` create
      (`s3:CreateBucket`, `s3:PutEncryptionConfiguration`,
      `s3:PutBucketPublicAccessBlock`, `s3:PutBucketOwnershipControls`,
      `s3:PutLifecycleConfiguration`, `s3:PutBucketTagging` among others)
      and `AWS::S3::BucketPolicy` (`s3:GetBucketPolicy`,
      `s3:PutBucketPolicy`). Nothing else.
- [ ] 5.D.3 `docs/site/docs/primeros-pasos/configurar-aws.md` / `.en.md`:
      the bucket is no longer a prerequisite; the plan example of an account
      without a bucket (from `cli-cases.json` "dry-run sin ningún bucket",
      the bucket name written as `rayito-artifacts-<cuenta>-<región>` /
      `rayito-artifacts-<account>-<region>`); what the created bucket is
      (encrypted, private, TLS only, 7-day expiry, kept when the stack is
      deleted); credentials: run setup once with administrator or bootstrap
      credentials (CloudFormation, IAM, and S3 when it creates the bucket),
      `CallerPolicy` is not enough; "Coste y activación" box updated.
- [ ] 5.D.4 `docs/site/docs/cli.md` / `.en.md`: `rayito setup` section:
      bucket resolution (reuse then creation, `--bucket`/`RAYITO_BUCKET`
      that do not exist are created with that name), the new actions,
      sources and error codes, exit codes.
- [ ] 5.D.5 `docs/site/docs/operacion/solucion-de-problemas.md` / `.en.md`:
      remove `no_bucket`; `bucket_unavailable` (403), `bucket_taken`,
      `artifact_stack_conflict`, `stack_blocked`/`stack_incomplete` for
      `rayito-m0-artifacts`, each with exit code and fix (delete the stack,
      the retained bucket, pass `--bucket`).
- [ ] 5.D.6 `docs/site/docs/cost.md` / `.en.md`: the `rayito setup` row
      adds the created bucket (no figure: zips expire after 7 days; the zip
      is the existing "céntimos").
- [ ] 5.D.7 `docs/site/docs/index.md` / `.en.md`,
      `docs/site/docs/migrar-desde-e2b/index.md` / `.en.md`,
      `docs/site/docs/guias/despliega-tu-agente.md` / `.en.md`: drop "you
      need a bucket first" / "Rayito no crea buckets" where it refers to
      setup (`rayito agent deploy` still does not create one; say so
      there and point to `rayito setup`).
- [ ] 5.D.8 `README.md` / `README.es.md`: `rayito setup` also creates the
      bucket when needed.
- [ ] 5.D.9 `infra/README.md`: a section for `artifact-bucket.yaml` (what
      it creates, `Retain`, deployed by `rayito setup` as
      `rayito-m0-artifacts`, or by hand with `aws cloudformation deploy
      --stack-name rayito-m0-artifacts --parameter-overrides
      BucketName=<name>`), and that it is not a `rayito stack` component.
      (Docs-only edit of `infra/README.md`; the template itself is 0.6.)
- [ ] 5.D.10 `SECURITY.md`: the created bucket's controls (SSE-S3, public
      access block, ACLs disabled, TLS-only policy) and why an unmanaged
      bucket with the default name is never adopted (`bucket_taken`).
      `CONTRIBUTING.md`: the e2e cleanup of `rayito-m0-artifacts` and its
      retained bucket.
- [ ] 5.D.11 `git grep -n -i -E "no crea buckets|nunca crea|never creates? a bucket|no_bucket"`
      outside `clients/`, `openspec/` and `testdata/` shows only statements
      about `rayito agent deploy`.

### 5.AWS Acceptance additions (with section 4)

- [ ] 5.AWS.1 4.1 also simulates `s3:CreateBucket`,
      `s3:PutEncryptionConfiguration`, `s3:PutBucketPolicy`,
      `s3:PutLifecycleConfiguration` (SCP risk) before any write.
- [ ] 5.AWS.2 4.2/4.5 inventories include S3 buckets and the
      `rayito-m0-artifacts` stack; the run deletes the stack and then the
      retained bucket it created (emptied first), and nothing else.
- [ ] 5.AWS.3 Python and TypeScript e2e of 5.PY.8/5.TS.8 in their own
      regions; the prepared-account check (4.4) still reads no
      `rayito-m0-artifacts` (zero new reads on an account with a bucket).
- [ ] 5.AWS.4 Regression level: `infra/` changed (one new template), so the
      targeted checks above plus the release's full regression
      (`reference/release-and-aws.md`); record the stack create time and
      any SCP finding as the next free Q row of `AWS_API_NOTES.md`.

## Open questions (defaults already chosen; change only if the maintainer says so)

1. **Bucket creation.** Resolved by the maintainer on 2026-10-19: setup
   creates the bucket when none can be reused, through the
   `rayito-m0-artifacts` stack (design D4, D10; section 5).
2. **Updating an old `rayito-base`.** Default: a launchable image is reused
   whatever `rayd` it holds; upgrading stays `rayito image publish`. A
   `--update-image` flag could be a follow-up.
3. **`rayito agent deploy` and the created bucket.** Default: unchanged
   (`RAYITO_BUCKET`, then the CDK bootstrap bucket); setup prints
   `RAYITO_BUCKET=<bucket>` for it. Teaching `resolve_bucket`/`resolveBucket`
   to read the `ArtifactBucket` of `rayito-m0-iam` would let it find a
   setup-created bucket without the variable; a follow-up if the maintainer
   wants it.
