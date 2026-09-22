# cli Specification

## Purpose
TBD - created by archiving change m7-cli. Update Purpose after archive.
## Requirements
### Requirement: The CLI is an optional extra with a console entry point
The Python SDK SHALL declare `[project.optional-dependencies] cli = ["typer>=0.15,<1"]` and `[project.scripts] rayito = "rayito.cli.__main__:main"`, and the `dev` dependency group SHALL contain the identical `typer` pin (asserted by `tests/unit/test_packaging.py`). `rayito.cli.__main__.main()` SHALL import the typer application lazily and, when `typer` is not installed, print a Spanish line telling the user to install `rayito[cli]` to stderr and return 2. `rayito/cli/__init__.py` SHALL contain no imports, and only `rayito/cli/app.py`, `image.py`, `sandbox.py` and `doctor.py` SHALL import `typer`, so `rayito.cli._artifact`, `_publish`, `_prune`, `_logs`, `_checks`, `_compat`, `_session` and `_console` import without the extra. `rayito.cli._artifact` SHALL import only standard-library modules (asserted by an `ast` test against `sys.stdlib_module_names`). The wheel SHALL contain `rayito/cli/__init__.py` and its `METADATA` SHALL declare `Provides-Extra: cli` and `Requires-Dist: typer>=0.15,<1; extra == "cli"`, both asserted by `scripts/check_wheel.py`.

#### Scenario: entry point without the extra
- **WHEN** `rayito` runs in an environment where `rayito` is installed without the `cli` extra
- **THEN** stderr contains `rayito[cli]` and the process exits 2

#### Scenario: library modules need no typer
- **WHEN** a test removes `typer` from `sys.modules` and blocks its import, then imports `rayito.cli._publish`, `rayito.cli._prune`, `rayito.cli._artifact`, `rayito.cli._logs`, `rayito.cli._checks` and `rayito.cli._compat`
- **THEN** every import succeeds

#### Scenario: wheel metadata
- **WHEN** `cd clients/python && uv build && python ../../scripts/check_wheel.py dist/*.whl` runs
- **THEN** it prints `OK`, the wheel lists `rayito/cli/__init__.py`, and `METADATA` contains `Provides-Extra: cli` and `Requires-Dist: typer>=0.15,<1; extra == "cli"`

### Requirement: The four scripts are thin shims over rayito.cli with unchanged argv
`scripts/publish_image.py` and `scripts/image_prune.py` SHALL delegate to the typer application (`rayito.cli.app.run_shim(["image", "publish", *argv])` and `["image", "prune", *argv]`), so their options are exactly those of `rayito image publish` and `rayito image prune`; when `typer` or `rayito` cannot be imported they SHALL print the `uv run --project clients/python python scripts/<name>.py …` hint and exit 2. `scripts/image_zip.py` and `scripts/copy_sidecar.py` SHALL load `clients/python/src/rayito/cli/_artifact.py` by file path (`importlib.util.spec_from_file_location`, path derived from `__file__`) and run `zip_main(argv)` / `copy_main(argv)`, keeping today's argv (`IMAGE_DIR DESTINATION [--variant full|slim]` and `SOURCE DESTINATION`) and working on a bare `python3` with no third-party packages; `image_zip.py` SHALL re-export `marker_variant`, `VARIANTS`, `write_zip` and `is_excluded`. The `Makefile` SHALL run the two AWS shims through `uv run --project clients/python` (variable `PY`), pass `--bucket $(BUCKET)` on every `image-publish*` target with `BUCKET ?= $(RAYITO_BUCKET)` and no other default (an `image-publish*` target with neither set SHALL stop with a `make` error naming `BUCKET` and `RAYITO_BUCKET` before building anything), remove `BOTO3_SPEC`, and leave `image-zip`/`image-zip-slim` on plain `python`. `.github/workflows/ci.yml` `build` job and `release.yml` SHALL be unchanged.

#### Scenario: stdlib shims run isolated
- **WHEN** `scripts/tests/test_shims.py` runs `python -I scripts/copy_sidecar.py <src> <dst>` and `python -I scripts/image_zip.py <dir> <zip> --variant slim` as subprocesses on a temporary image tree with a `Dockerfile` and a `requirements.txt`
- **THEN** both exit 0, the zip carries `kernel-sidecar/ipython/startup/warmup_variant` with `slim`, excludes `__pycache__`, `tests`, `.venv` and `uv.lock`, and `python -I -c "import sys; sys.path.insert(0, 'scripts'); from image_zip import marker_variant; print(marker_variant('<zip>'))"` prints `slim`

#### Scenario: AWS shims share the CLI parser
- **WHEN** `python scripts/publish_image.py` runs with no arguments inside the client environment
- **THEN** it exits 2 with typer's usage error naming `--artifact` (the missing required option) and makes no AWS call; the same for `--artifact x.zip` without `--base-image-version`

#### Scenario: make targets still work
- **WHEN** `make image-zip` and `make image-publish PUBLISH_ARGS=--force` are run on a box with the client environment and AWS credentials
- **THEN** the zip is produced by the stdlib shim and the publish runs through `uv run --project clients/python python scripts/publish_image.py --artifact image/rayito-image.zip --bucket <BUCKET> --base-image-version 1 --force`

#### Scenario: publishing without a bucket stops before the build
- **WHEN** `make image-publish` runs with neither `BUCKET` nor `RAYITO_BUCKET` set
- **THEN** make stops with an error naming `BUCKET=<tu-bucket>` and `RAYITO_BUCKET` before `cargo zigbuild` runs, and `make image-publish BUCKET=<tu-bucket>` (or with `RAYITO_BUCKET` exported) passes `--bucket <tu-bucket>` to the shim

### Requirement: Global options, session and exit codes
The `rayito` root callback SHALL accept `--profile`, `--region`, `--json` and `--verbose`, build a boto3 session from them (falling back to boto3's default chain), refuse to continue without a resolved region (`sin región` message, exit 2), and store a `Clients` object on `ctx.obj` only when it is `None`, so tests inject stubbed clients through `CliRunner.invoke(app, args, obj=…)`. `Clients` SHALL expose lazily-built `microvms`, `s3`, `sts`, `iam`, `quotas`, `logs`, `cloudformation` clients configured with the SDK's `client_config()` (user agent `rayito/<version> cli`) and the SDK's `shared_control_plane(session, region=)`. With `--json` every command SHALL write exactly one JSON document to stdout and any progress to stderr. Exit codes SHALL be 0 on success, 1 when the operation failed (build not launchable, prune candidate left, unknown sandbox id on `kill`, doctor `FAIL`, unhandled `ClientError` printed as `AWS error <Code>: <Message>`), and 2 for usage or environment errors (bad arguments, no region, missing credentials or profile, missing extra).

#### Scenario: injected clients
- **WHEN** a unit test invokes `app` with `obj=Clients(...)` whose boto3 clients are wrapped in `botocore.stub.Stubber`
- **THEN** the command uses those clients and no real boto3 client is created

#### Scenario: no region
- **WHEN** `rayito image list` runs with no `--region`, no `AWS_REGION`/`AWS_DEFAULT_REGION` and no region in the profile
- **THEN** it prints `sin región` with the two ways to set it and exits 2 before any AWS call

#### Scenario: JSON is a single document
- **WHEN** any command runs with `--json` under `CliRunner`
- **THEN** `json.loads(result.stdout)` succeeds and stdout contains nothing else

### Requirement: rayito image publish reproduces the publish pipeline
`rayito image publish --artifact PATH --base-image-version V --bucket B [--image-name N] [--variant full|slim] [--os-capabilities ALL] [--build-role-arn ARN | --stack-name S] [--memory-mib M] [--timeout-seconds T] [--force]` SHALL run `rayito.cli._publish.publish()` with today's behaviour: refuse before any AWS call when the artifact is missing or its `warmup_variant` marker does not match `--variant`; upload the zip to `s3://B/rayito/images/rayd-<first 12 hex of sha256>.zip` unless the key exists; take the build role from `--build-role-arn` or the `BuildRoleArn` output of the CloudFormation stack `--stack-name` (default `rayito-m0-iam`); send exactly the configuration of `desired_configuration` (`baseImageArn` `al2023-1` of the region, `baseImageVersion`, `buildRoleArn`, `codeArtifact.uri`, `resources[{minimumMemoryInMiB}]`, `cpuConfigurations[{architecture: ARM_64}]`, the six-hook `hooks` table on port 9000, `logging.cloudWatch.logGroup=/rayito/<name>`, and `additionalOsCapabilities: ["ALL"]` only with `--os-capabilities ALL`); reuse the newest `SUCCESSFUL`/`ACTIVE` version whose configuration matches (`baseImageVersion` compared numerically, every other key literally) unless `--force`; otherwise `create-microvm-image` or `update-microvm-image`, poll every 10 s until the three-state gate settles, print the `snapshotBuild` summary and `RAYITO_TEMPLATE=<arn>` on success (exit 0) or the `stateReason`s plus the newest build-log events on failure (exit 1). `--bucket` SHALL resolve from the flag, then `RAYITO_BUCKET`, and otherwise be a usage error (exit 2, no AWS call); the library SHALL have no default bucket. With `--json` only the summary dict is printed.

#### Scenario: reuse without a build
- **WHEN** the Stubber test lists a `SUCCESSFUL`/`ACTIVE` version whose `codeArtifact.uri` equals the computed key and whose `baseImageVersion` echoes `1.0` for a `--base-image-version 1`
- **THEN** no `create_microvm_image`/`update_microvm_image` call is made, the output says `already built from this artifact and config; reusing` and the exit code is 0

#### Scenario: failed build prints the log tail
- **WHEN** `update_microvm_image` is accepted and the gate settles with version `FAILED`/`INACTIVE` and `stateReason` `Validate hook invocation timed out after PT10M`
- **THEN** the command prints that reason, calls `describe_log_streams` and `get_log_events` on `/rayito/<name>` and exits 1

#### Scenario: bucket resolution
- **WHEN** `rayito image publish --artifact image/rayito-image.zip --base-image-version 1` runs with no `--bucket` and no `RAYITO_BUCKET`
- **THEN** it exits 2 naming `--bucket` and `RAYITO_BUCKET` and makes no AWS call

#### Scenario: real publish reuses the current version
- **WHEN** the acceptance runs `rayito image publish --artifact image/rayito-image.zip --base-image-version 1 --bucket <B>` on the zip that produced the current ACTIVE `rayito-base` version
- **THEN** it reports the reuse, the number of versions of `rayito-base` is unchanged, and the last line is `RAYITO_TEMPLATE=<arn>`

### Requirement: rayito image list, prune and zip
`rayito image list [NAME] [--name-filter F]` SHALL, without NAME, page `list_microvm_images` (with `nameFilter` when given) and print `name`, `state`, `latestActiveImageVersion`, `latestFailedImageVersion`, `createdAt`; with NAME it SHALL resolve the ARN as the SDK does and page `list_microvm_image_versions` printing `imageVersion`, `state`, `status`, `baseImageVersion`, the artifact key basename and `createdAt`, newest first. `rayito image prune [--image-name N=rayito-base] [--keep 5] [--dry-run] [--wait-timeout 600]` SHALL run `rayito.cli._prune.run()` with the keep set, serialised deletes and backoff of the `image-lifecycle` spec, printing the plan table and the JSON summary, exit 1 if any candidate survives. `rayito image zip IMAGE_DIR DESTINATION [--variant full|slim] [--sidecar SRC]` SHALL, with `--sidecar`, replace `IMAGE_DIR/kernel-sidecar` with the filtered copy of `SRC` (same exclusions as the zip, `requirements.txt` required) and then write the deterministic zip (fixed dates and modes, `Dockerfile` at the root required, `slim` marker entry only for `--variant slim`), printing the file count, size, variant and sha256 of the zip.

#### Scenario: versions of one image
- **WHEN** `rayito --json image list rayito-base` runs against a Stubber that returns versions `3.0` (`SUCCESSFUL`/`ACTIVE`) and `2.0` (`FAILED`/`INACTIVE`)
- **THEN** the JSON list has two items in that order with `imageVersion`, `state`, `status`, `baseImageVersion`, `artifact` and `createdAt` keys

#### Scenario: prune dry run
- **WHEN** `rayito image prune --keep 3 --dry-run` runs against the Stubber fixture of the `image-lifecycle` spec (versions `1.0`–`8.0`, a `RUNNING` MicroVM on `2.0`)
- **THEN** the plan keeps `8.0`, `7.0`, `6.0`, `2.0`, lists `1.0`, `3.0`, `4.0`, `5.0` as `delete`, and no `delete_microvm_image_version` call is made

#### Scenario: zip with sidecar copy
- **WHEN** `rayito image zip <dir> <zip> --variant slim --sidecar <sidecar>` runs on a temporary tree
- **THEN** `<dir>/kernel-sidecar` contains no `tests`, `.venv`, `__pycache__` or `uv.lock`, the zip has the marker entry with `slim`, and running the same command again produces a byte-identical zip

### Requirement: rayito sandbox list, info, kill and logs
`rayito sandbox list [--template T] [--template-version V] [--all-states]` SHALL call `Sandbox.list()` through the shared control plane, omitting `TERMINATING`/`TERMINATED` unless `--all-states`, and print `sandbox_id`, `state`, template name, `template_version`, `started_at` (ISO UTC) and age, without probing any endpoint. `rayito sandbox info ID [--no-metadata]` SHALL call `Sandbox.get_info(ID, read_metadata=…)` and print every `SandboxInfo` field including `metadata`. `rayito sandbox kill ID…` SHALL call `Sandbox.kill(ID)` per id, print `terminated` or `not found`, and exit 1 if any id was not found; `rayito sandbox kill --all [--template T] [--yes]` SHALL list the non-terminal sandboxes, ask for confirmation unless `--yes`, kill each, and be mutually exclusive with explicit ids. `rayito sandbox logs ID [--log-group G] [--limit N=1000] [--since ISO|30m|2h]` SHALL resolve the group as `/rayito/<template name>` unless `--log-group`, look for the stream `<startedAt UTC date YYYY/MM/DD>[<template_version>]<ID>` via `describe_log_streams(logStreamNamePrefix=)`, fall back to scanning at most 10 pages of `describe_log_streams(orderBy="LastEventTime", descending=True)` for names ending in `]<ID>`, page `get_log_events(startFromHead=True)` until `nextForwardToken` repeats or `--limit` is reached, print `<ISO timestamp> <message>` per event (`--json`: `[{timestamp, message}]`), and when no stream or group exists print `sin logs` with the three causes (logging disabled, no execution role, other group) and exit 1. No command SHALL print a JWE or access token.

#### Scenario: list hides terminal states
- **WHEN** the fake control plane lists one `RUNNING`, one `SUSPENDED` and one `TERMINATED` MicroVM
- **THEN** `rayito sandbox list` prints two rows and `rayito sandbox list --all-states` prints three

#### Scenario: kill reports unknown ids
- **WHEN** `rayito sandbox kill microvm-a microvm-b` runs and the control plane raises `SandboxNotFoundException` for `microvm-b`
- **THEN** the output shows `microvm-a terminated` and `microvm-b not found` and the exit code is 1

#### Scenario: logs by exact stream name
- **WHEN** `rayito sandbox logs microvm-x` runs for a sandbox started `2026-09-15T14:39:02Z` on version `1.0`, and `describe_log_streams(logGroupName="/rayito/rayito-base", logStreamNamePrefix="2026/09/15[1.0]microvm-x")` returns that stream
- **THEN** `get_log_events` is called with that stream name and `startFromHead=True`, and each event is printed as `<ISO timestamp> <message>`

#### Scenario: logs fallback and absence
- **WHEN** the exact stream is absent and the ordered scan returns a stream `2026/09/16[1.0]microvm-x`
- **THEN** its events are printed; and when the scan returns nothing (or the group raises `ResourceNotFoundException`) the command prints `sin logs` with the three causes and exits 1

### Requirement: rayito doctor runs ten checks and never aborts mid-way
`rayito doctor [--template T=rayito-base] [--template-version V] [--bucket B] [--launch]` SHALL run, in order, the checks `credentials`, `managed-images`, `quotas`, `iam-simulation`, `bucket`, `image-gate`, `sandboxes`, `token`, `agent`, `compatibility`, each yielding `CheckResult(name, status ∈ {OK, WARN, FAIL, SKIP}, summary, details)`; an exception inside a check SHALL become that check's `FAIL` (or `SKIP` where the design table says so), naming the operation AWS rejected (`ClientError.operation_name`, reported as `details.operation`) next to the IAM action the check needs (`details.action`), and the remaining checks SHALL still run. The statuses SHALL follow `design.md` D8: `credentials` uses `sts.get_caller_identity()` and the session region (WARN outside `SUPPORTED_REGIONS`); `managed-images` pages `list_managed_microvm_images` expecting `al2023-1` and reports the newest managed version from `list_managed_microvm_image_versions`; `quotas` pages `service-quotas.list_service_quotas(ServiceCode="lambda")` filtered by `microvm` in the name and WARNs on any adjustable quota below the D10 default (SKIP on `AccessDeniedException`); `iam-simulation` follows D9 (advisory: `implicitDeny` WARN, `explicitDeny` or an Organizations deny FAIL, SKIP for root or when `iam:GetRole`/`iam:SimulatePrincipalPolicy` are denied); `bucket` makes a single `head_bucket` call (its `s3:ListBucket` is the same permission `publish` needs for a 404 from `head_object` on a new artifact) and takes the region from its `BucketRegion` (`x-amz-bucket-region`), never calling `get_bucket_location` (SKIP without a bucket, WARN when the bucket is in another region or the header is absent, FAIL naming the rejected operation and the S3 permissions `publish` needs on 403/404); `image-gate` reuses `_publish.read_gate`/`latest_build` on `--template-version` or `latestActiveImageVersion` (FAIL when the image is missing or not launchable); `sandboxes` counts non-terminal MicroVMs of the template (WARN 1–10 `RUNNING`, FAIL above 10); `token` mints a JWE for port 8080 on the launched or newest `RUNNING` sandbox (SKIP without one); `agent` reads `Health` (`get_health()` with `--launch`, otherwise the JWE probe on a dedicated channel with a 5 s timeout) and reports `agent_version`, `kernel_ready`, `imds_blocked`, `hook_anomalies`; `compatibility` applies `rayito.cli._compat.assess`. With `--launch` the doctor SHALL create `Sandbox.create(T, template_version=V, timeout=300, idle=None, logging="disabled", metadata={"rayito": "doctor"})` before `token`, kill it in a `finally`, and report its id as `launched_sandbox_id`; without `--launch` it SHALL create no MicroVM. Human output SHALL be one line per check (`OK|WARN|FAIL|SKIP`, name, summary, details indented for WARN/FAIL) followed by the compatibility table and a totals line; `--json` SHALL print `{"rayito", "region", "account", "principal_kind", "checks", "compatibility", "launched_sandbox_id", "exit_code"}`. The exit code SHALL be 1 if any check is `FAIL`, else 0. No JWE, access token or `runHookPayload` SHALL appear in any output.

#### Scenario: fresh account without an image
- **WHEN** the Stubber answers `get_caller_identity`, lists `al2023-1`, returns the default quotas, simulates every action `allowed`, `head_bucket` succeeds, and `get_microvm_image` raises `ResourceNotFoundException`
- **THEN** `credentials`, `managed-images`, `quotas`, `iam-simulation`, `bucket` are `OK`, `image-gate` is `FAIL` with a summary mentioning `rayito image publish`, `sandboxes` is `OK` (zero), `token`, `agent`, `compatibility` are `SKIP`, the compatibility table is still printed, and the exit code is 1

#### Scenario: reduced quotas and an implicit deny are warnings
- **WHEN** `list_service_quotas` returns `L-535CA9B6` (RunMicrovm rate) with value `1` and the simulation returns `implicitDeny` for `lambda:SuspendMicrovm`
- **THEN** `quotas` is `WARN` naming the quota and `iam-simulation` is `WARN` naming the action, and neither alone makes the exit code 1

#### Scenario: doctor never leaks the token
- **WHEN** the stubbed `create_microvm_auth_token` returns `{"authToken": {"X-aws-proxy-auth": "CANARY-JWE"}}` and the doctor runs with and without `--json`
- **THEN** `CANARY-JWE` appears in neither stdout nor stderr while `token` is `OK`

#### Scenario: launch is killed whatever happens
- **WHEN** `rayito doctor --launch` runs with a fake sandbox factory whose `get_health()` raises
- **THEN** `agent` is `FAIL`, `kill()` was called on the fake sandbox, `launched_sandbox_id` is its id, and the exit code is 1

#### Scenario: real account with --launch
- **WHEN** the acceptance runs `rayito --json doctor --template $RAYITO_TEMPLATE --bucket $RAYITO_BUCKET --launch` on the account
- **THEN** the exit code is 0, no check is `FAIL`, `agent.details.agent_version` is non-empty, `compatibility` is `OK`, and `Sandbox.get_info(launched_sandbox_id).state` is `TERMINATING` or `TERMINATED` within 30 s

### Requirement: Compatibility table shared by the doctor and the docs
`rayito.cli._compat.COMPATIBILITY` SHALL be a tuple of `CompatibilityRow(sdk_series, min_agent_version, note)` starting with `("0.1", "0.1.0", …)`, and `assess(sdk_version, agent_version)` SHALL return `FAIL` when the agent is below the row's minimum, `WARN` when the agent's `MAJOR.MINOR` is newer than the SDK's, and `OK` otherwise, parsing versions as integer `MAJOR.MINOR.PATCH` (pre-release suffix ignored). The image version (`imageVersion`) SHALL NOT be a compatibility criterion: it is the per-image, per-account build counter (`rayito-base` 17.0, `rayito-base-poly` 3.0 and a fresh account's 1.0 carry the same `rayd`), while `agent_version` comes from the binary inside the image; the doctor's `compatibility` check SHALL report it only informationally (in `details.image_version` and the summary). `docs/site/docs/limits.md` SHALL contain a section `## Compatibilidad SDK ↔ rayd ↔ imagen` whose table rows equal `COMPATIBILITY`, and `tests/unit/cli/test_compat.py` SHALL parse that table and assert equality (skipped when the docs file is absent).

#### Scenario: assessments
- **WHEN** `assess("0.1.0", "0.1.0")`, `assess("0.1.0", "0.0.9")` and `assess("0.1.0", "0.2.0")` are evaluated
- **THEN** the statuses are `OK`, `FAIL`, `WARN` respectively, each with a reason naming the offending value

#### Scenario: image build counter never fails the doctor
- **WHEN** `rayito doctor --template rayito-base-poly` (or the doctor on a fresh account whose first `rayito-base` is `1.0`) reads `agent_version` `0.1.0` from a sandbox whose `template_version` is `1.0`
- **THEN** `compatibility` is `OK`, its summary names `imagen 1.0` as informational, `details.image_version` is `"1.0"` and the exit code is 0

#### Scenario: docs drift
- **WHEN** a row of the `limits.md` compatibility table is edited without changing `COMPATIBILITY`
- **THEN** `uv run pytest tests/unit/cli/test_compat.py` fails naming the differing row

### Requirement: Health probe helper shared by the SDK and the doctor
`rayito.sandbox_sync.main` SHALL expose `probe_health(control_plane, info, transport, request_timeout)` and `rayito.sandbox_async.main` SHALL expose `probe_health_async` with the same contract: return `None` without touching the endpoint when `info.state` is not `RUNNING`; otherwise mint a JWE for port 8080, open a dedicated channel, send one `Health` (with the existing 403 remint retry), close the channel and return the raw `HealthResponse`; RPC failures raise the existing `SandboxException` naming the sandbox. `probe_metadata` / `probe_metadata_async` SHALL be implemented on top of them with unchanged signatures and behaviour.

#### Scenario: metadata probe unchanged
- **WHEN** the existing `tests/unit/test_metadata_sync.py` and `test_metadata_async.py` run after the refactor
- **THEN** every test passes without modification

#### Scenario: doctor uses the probe
- **WHEN** `rayito doctor` runs without `--launch` against a fake control plane with one `RUNNING` sandbox and a fake `HealthService` answering `agent_version="0.1.0"`
- **THEN** `agent.details.agent_version == "0.1.0"` and exactly one `create_auth_token` call was made for port 8080

### Requirement: CLI documentation and milestone bookkeeping
`docs/site/docs/cli.md` SHALL exist in Spanish with the sections install, global options, `image`, `sandbox`, `doctor` (the ten checks with what to do on `FAIL`/`WARN`, `--launch` and its cost, the `--json` shape, exit codes), `Equivalencias con make` and `Lo que la CLI no hace`, and SHALL be in the `mkdocs.yml` nav after "Quickstart". `quickstart.md` SHALL name `rayito doctor` under "Credenciales" and `rayito image publish` under "La imagen"; `README.md` and `clients/python/README.md` SHALL have a short "CLI" paragraph; `clients/python/CHANGELOG.md` `[Unreleased]` SHALL list the CLI under `### Added` and the shim/`--bucket` change under `### Changed`; `SPEC.md` §4 SHALL carry the one-sentence clarification of design D15; `ARCHITECTURE.md` SHALL reference `rayito image …` where it named the scripts; `MILESTONES.md` row 6 of M7 SHALL record the state and, at acceptance, the evidence.

#### Scenario: strict docs build
- **WHEN** `cd clients/python && uv run --group docs mkdocs build -f ../../docs/site/mkdocs.yml --strict --site-dir ../../docs/site/_build` runs
- **THEN** it succeeds and `docs/site/_build/cli/index.html` exists

#### Scenario: no stale script references
- **WHEN** `grep -n "BOTO3_SPEC" Makefile` and `grep -rn "publish_image.py\|image_prune.py" docs/site docs/RELEASING.md README.md clients/python/README.md ARCHITECTURE.md` run
- **THEN** the first has no matches and every match of the second sits next to its `rayito image publish` / `rayito image prune` equivalent

