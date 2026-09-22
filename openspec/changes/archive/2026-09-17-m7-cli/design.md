## Context

State on 2026-09-16: M0–M6 accepted on real AWS and archived; `m7-oss-hygiene`
implemented, `m7-supply-chain` implemented (GitHub-side acceptance pending);
`rayito` 0.1.0 (Python, `uv_build`, `>=3.11`, deps `grpcio`, `protobuf`,
`boto3>=1.43.82,<2`), `rayd` 0.1.0 (`Health.agent_version` = `0.1.0`), image
`rayito-base` 17.0 ACTIVE. No git repository on the box; files are written,
not committed. Inputs: the M7 research report §3e, §4 and §5 (change 6) and
`MILESTONES.md` "M7" row 6.

Facts verified for this design (box: Windows, `uv` 0.7.21, Python 3.12.10,
botocore 1.43.94):

- `scripts/publish_image.py` (534 lines), `image_prune.py` (430),
  `image_zip.py` (107), `copy_sidecar.py` (47). `copy_sidecar.py` imports
  `is_excluded` from `image_zip.py`; `publish_image.py` imports `VARIANTS`
  and `marker_variant` from it. `bench_cold_start.py`, `hooks-sim.py` and the
  `check_*.py` scripts import none of the four. Tests:
  `scripts/tests/test_publish_image.py` (8 tests), `test_image_prune.py`
  (7, `Stubber`), `test_image_variant.py` (5), run by
  `cd clients/python && uv run pytest ../../scripts/tests -p no:cacheprovider`.
- Callers of the four scripts: `Makefile` targets `image-zip`,
  `image-zip-slim`, `image-publish`, `image-publish-slim`,
  `image-publish-caps`, `image-prune`; `.github/workflows/ci.yml` `build`
  job (`python3 scripts/copy_sidecar.py`, `python3 scripts/image_zip.py` twice,
  and a one-liner importing `marker_variant` from `image_zip`) and
  `release.yml` (`python3 scripts/copy_sidecar.py`, `python3
  scripts/image_zip.py`). Both workflow jobs are Rust jobs with **no `uv`**:
  the zip logic must keep running on a bare `python3`.
- `publish_image.py` hard-codes `DEFAULT_BUCKET` to the maintainer's artifact bucket (the
  org SCP denies `s3:CreateBucket` in that account) and `DEFAULT_STACK_NAME =
  "rayito-m0-iam"` with output `BuildRoleArn`.
- `rayito/__init__.py` imports the sync and async sandbox trees, so any
  `import rayito.cli.<x>` needs `grpcio` and `protobuf` installed.
- `typer` 0.27.2 resolves today (deps `click` 8.5.0, `rich` 15.0.0,
  `shellingham`; ships `py.typed`). `typer.testing.CliRunner().invoke(app,
  args, obj=X)` delivers `X` as `ctx.obj` to the callback and commands; an
  unknown subcommand exits 2; `result.stdout` and `result.stderr` are
  separate (click ≥ 8.2 removed `mix_stderr`) (probed in a scratch env on
  2026-09-16).
- `uv run --project clients/python python …` from the repository root keeps
  the cwd (`os.getcwd()` = the root) and imports the editable `rayito`
  (probed 2026-09-16).
- botocore 1.43.94 shapes: `iam.simulate_principal_policy` (input
  `PolicySourceArn`, `ActionNames`, `ResourceArns`, paginated; output
  `EvaluationResults[].EvalActionName/EvalResourceName/EvalDecision`
  with `EvalDecision` in `allowed | explicitDeny | implicitDeny`, plus
  `OrganizationsDecisionDetail` and `PermissionsBoundaryDecisionDetail`);
  `service-quotas.list_service_quotas(ServiceCode=)` paginated, `Quotas[]`
  with `QuotaCode`, `QuotaName`, `Value`, `Unit`, `Adjustable`;
  `logs.describe_log_streams(logGroupName, logStreamNamePrefix, orderBy,
  descending, limit, nextToken)` and `logs.get_log_events(logGroupName,
  logStreamName, startFromHead, nextToken, limit)`;
  `s3.head_bucket(Bucket)`, `s3.get_bucket_location(Bucket)` →
  `LocationConstraint` (absent for `us-east-1`); `sts.get_caller_identity()`
  → `Account`, `Arn`, `UserId`. The `lambda-microvms` client paginates
  `list_managed_microvm_images`, `list_managed_microvm_image_versions`,
  `list_microvm_images`, `list_microvm_image_versions`, `list_microvms`.
- Service Quotas measured in M0 (`spike/m0/out/quotas.json`, `ServiceCode`
  `lambda`, `"microvm"` in `QuotaName`): 26 quotas; the adjustable ones and
  their default values per `AWS_API_NOTES.md` §11 are `L-CD1C0CC4` memory
  (ARM_64, GB; 400 default, 1024 in the four large regions),
  `L-535CA9B6`/`L-91B95582` RunMicrovm rate/burst 5, `L-118C44B3`/`L-25EEC0A4`
  ResumeMicrovm 5, `L-90045317`/`L-139F9A48` SuspendMicrovm 2,
  `L-74787B8A`/`L-2CCA0501` TerminateMicrovm 10, `L-CE98C9E3`/`L-C9C2110E`
  GetMicrovm 100, `L-7712260B`/`L-D65D9F16` CreateMicrovmAuthToken 50,
  `L-B78D2ECC`/`L-9A5E43A5` CreateMicrovmShellAuthToken 5, `L-72E0D058`
  concurrent builds 5 (10 in the large regions), `L-942E56BE` images 100,
  `L-F8BECE9C` versions per image 50.
- CloudWatch: default group `/rayito/<image-name>` (the SDK's
  `logging="cloudwatch"` and `publish_image.py` both use `LOG_GROUP_PREFIX =
  "/rayito"`); stream name `YYYY/MM/DD[<imageVersion>]<microvmId>` (measured
  2026-09-15, `2026/09/15[1.0]microvm-<id>` for a VM whose `startedAt`
  was `2026-09-15 09:39 -05:00`); runtime logs exist only with an
  `executionRoleArn` (`AWS_API_NOTES.md` §14).
- The SDK already exposes everything the `sandbox` commands need:
  `Sandbox.list(template=, states=)`, `Sandbox.kill(sandbox_id)`,
  `Sandbox.get_info(sandbox_id, read_metadata=)`, `Sandbox.create(…,
  timeout=, idle=None, logging="disabled")`, `get_health()`,
  `LambdaMicrovmsControlPlane.create_auth_token(sandbox_id, ports)`,
  `shared_control_plane(session, region=)`, and the sync/async
  `probe_metadata` helpers (JWE for port 8080 + one `Health` on a dedicated
  channel, `x-access-token` not needed).
- `rayd` compatibility as recorded in `crates/rayd/CHANGELOG.md`
  "Compatibilidad": SDK 0.1.0 requires `rayito-base` ≥ 10.0 and
  `agent_version` 0.1.0; the M6 surface (`imds_blocked`, `hook_anomalies`,
  `metadata`) needs the image built from that tag (16.0+). The report §1
  fixes the future policy: lockstep `MAJOR.MINOR` between SDKs and `rayd`,
  patch may diverge, image versions stay opaque build numbers.
- `docs/site/mkdocs.yml` nav today: `index`, `quickstart`, `concepts`, `api`,
  `security`, `limits`, `e2b-compat`, `cost`, `verify` (nine pages; the
  `python-release` spec still says eight).
- `openspec/specs/image-lifecycle/spec.md` requires
  `scripts/image_prune.py --image-name <name> [--keep N] [--dry-run]` with the
  keep set, the serialised deletes and the backoff; `m7-supply-chain`'s
  pending delta requires `scripts/publish_image.py` to exit 2 without
  `--base-image-version`. Both stay true through the shims.

## Goals / Non-Goals

Goals:

- One command, `rayito`, for the operations a maintainer or an adopter does
  outside Python code: build and publish the image, list and prune versions,
  list/inspect/kill sandboxes, read a sandbox's CloudWatch stream, and
  diagnose an account before the first `Sandbox.create()`.
- The scripts' logic lives once, inside the SDK package, unit-tested with
  `Stubber`; `scripts/` keeps its four file names and argv so the `Makefile`,
  CI and the docs written for them keep working.
- `rayito doctor` answers, on a fresh account, every question the report §4
  lists, with one status per check and a machine-readable `--json`.
- Nothing printed is a secret; the CLI is `mypy --strict`, `ruff` and
  sync-only (it is a process, not a library surface).

Non-goals (report §3e, §5 and the brief):

- Declarative templates (`rayito.toml`, `rayito template build`), image
  *building* beyond the zip (the Dockerfile is still built by AWS), `--follow`
  on `logs`, shell completion packaging, a TypeScript CLI, an `rayito sandbox
  create/exec/pty` interactive surface (the SDKs and, in `m7-mcp-server`, the
  MCP server cover interactive use).
- Wrapping `bench_cold_start.py`, `hooks-sim.py`, `gen_limits.py`,
  `check_*.py`, `gen_python.py`: development tools, not operator commands.
- Changing what `publish` sends to AWS (hooks, memory, logging, no
  `egressNetworkConnectors`: the existing configuration unchanged), the S3
  key scheme or the version reuse rule.
- Async parity for the CLI modules (the constitution's parity rule covers the
  `Sandbox`/`AsyncSandbox` surface; `rayito.cli.*` is private and sync).
- Publishing to PyPI (the extra is declared; `docs/RELEASING.md` already
  owns the release steps).

## Decisions

### D1. Package layout: `rayito.cli`, typer only at the edges

```
clients/python/src/rayito/cli/
  __init__.py     docstring only; no imports (importing a submodule never pulls typer)
  __main__.py     main(): guarded import of .app; `python -m rayito.cli`
  app.py          the typer application: root callback, `image`, `sandbox`, `doctor`
  image.py        typer commands `image publish|list|prune|zip`
  sandbox.py      typer commands `sandbox list|kill|info|logs`
  doctor.py       typer command `doctor`
  _session.py     Clients (boto3 session/clients + SDK control plane), global options
  _console.py     tables, JSON mode, status glyphs, console encoding
  _artifact.py    zip + sidecar copy (stdlib only; ex image_zip.py + copy_sidecar.py)
  _publish.py     ex publish_image.py (boto3)
  _prune.py       ex image_prune.py (boto3)
  _logs.py        CloudWatch stream resolution and event paging
  _checks.py      doctor checks and their result model
  _compat.py      SDK / agent_version / image compatibility table
```

Entry point: `[project.scripts] rayito = "rayito.cli.__main__:main"`.
`main()` does `from rayito.cli.app import app` inside a `try`; on
`ModuleNotFoundError` for `typer` it prints `rayito: instala el extra:
uv pip install "rayito[cli]"` (or `pip install`) to stderr and returns 2,
otherwise returns `app(standalone_mode=…)`'s exit code. Command modules
(`image.py`, `sandbox.py`, `doctor.py`, `app.py`) are the only files that
import `typer`; `_artifact.py`, `_publish.py`, `_prune.py`, `_logs.py`,
`_checks.py`, `_compat.py`, `_session.py`, `_console.py` never do, so the
shims and the unit tests of the logic import them without the extra.
`_console.py` imports `rich` (a dependency of `typer`; the `cli` extra pins
`typer`, which brings it) lazily inside the table-rendering function so the
JSON path and the library modules do not need it.

Identifiers English; user-facing strings Spanish (help texts, statuses'
summaries, errors), like the SDK.

### D2. Dependency pins and the dev group

`[project.optional-dependencies] cli = ["typer>=0.15,<1"]` (0.15 is the
first release with the current `typer.testing` and `rich` integration the
code uses; 0.27.2 is what resolves today). The `dev` dependency group gains
the identical string so `uv run pytest`, `mypy` and `ruff` see the CLI
without a second sync mode. `tests/unit/test_packaging.py` asserts the two
pins are equal (`project.optional-dependencies.cli == ["typer>=0.15,<1"]` and
that string is in `dependency-groups.dev`). `uv lock` is regenerated once;
`uv lock --check` clean afterwards. No `rich` pin of our own: it is `typer`'s
dependency and the CLI uses only `rich.table.Table` and `rich.console.Console`.

`mypy` strict already covers `src` and `tests`; `typer` and `rich` are typed.
`ruff` config unchanged (`src/rayito/v1` stays excluded).

### D3. The scripts become shims; two kinds of shim

| Script | Shim body | Why this kind |
|---|---|---|
| `scripts/publish_image.py` | `from rayito.cli.app import run_shim; sys.exit(run_shim(["image", "publish", *sys.argv[1:]]))` | same parser as the CLI, so `make image-publish` and `rayito image publish` cannot drift; needs the client env |
| `scripts/image_prune.py` | `run_shim(["image", "prune", *sys.argv[1:]])` | idem |
| `scripts/image_zip.py` | loads `rayito/cli/_artifact.py` **by file path** (`importlib.util.spec_from_file_location`, path computed from `__file__`: `../clients/python/src/rayito/cli/_artifact.py`), re-exports `marker_variant`, `VARIANTS`, `write_zip`, `is_excluded`, and runs `zip_main(sys.argv[1:])` | must run on a bare `python3` in the CI `build` job and in `release.yml`, where installing the SDK (grpcio) only to zip a directory would enlarge the trusted base of the signed release artifact |
| `scripts/copy_sidecar.py` | same loader, runs `copy_main(sys.argv[1:])` | idem |

`run_shim(argv)` in `app.py` invokes the typer app with `standalone_mode=True`
semantics (usage errors exit 2, `typer.Exit` codes propagate) and, when
`typer` is missing, prints `ejecuta con: uv run --project clients/python
python scripts/<name>.py …` and returns 2. The two boto3 shims therefore
require the client environment; the `Makefile` provides it (D4). The two
stdlib shims keep a 30-line `argparse` surface each (`zip_main`,
`copy_main`, living in `_artifact.py` next to the logic) identical to
today's: `image_zip.py IMAGE_DIR DESTINATION [--variant full|slim]`,
`copy_sidecar.py SOURCE DESTINATION`.

`_artifact.py` is **stdlib-only by contract**: a unit test parses it with
`ast` and asserts every imported top-level module is in
`sys.stdlib_module_names`, and `scripts/tests/test_shims.py` runs
`python scripts/image_zip.py` and `python scripts/copy_sidecar.py` as
subprocesses on a temporary image tree with `-I` (isolated mode: no
site-packages, no `PYTHONPATH`) so the by-path loader is exercised exactly as
CI runs it. The CI one-liner `from image_zip import marker_variant` keeps
working through the re-export.

The three migrated test files keep their test names (prefixed by module) in
`clients/python/tests/unit/cli/test_artifact.py`, `test_publish.py`,
`test_prune.py`; `scripts/tests/` keeps `test_bench_*`, `test_check_*` and
gains `test_shims.py` (the two stdlib subprocess runs above, plus
`publish_image.py` and `image_prune.py` invoked with no arguments through
`sys.executable` inside the client env asserting exit 2 and the typer usage
line naming `--artifact` / listing options).

### D4. Makefile and CI

- New variable `PY := uv run --project $(PYTHON_CLIENT)`; `BOTO3_SPEC`
  removed. `image-publish`, `image-publish-slim`, `image-publish-caps` become
  `$(PY) python scripts/publish_image.py --artifact … --bucket $(BUCKET)
  --base-image-version $(BASE_IMAGE_VERSION) $(PUBLISH_ARGS)`; `image-prune`
  becomes `$(PY) python scripts/image_prune.py --image-name rayito-base
  $(PRUNE_ARGS)`.
- `BUCKET ?= <the maintainer's artifact bucket>` with a comment:
  the maintainer's artifact bucket (SCP denies `CreateBucket` there);
  adopters set `BUCKET=` or `RAYITO_BUCKET`. The library has **no** bucket
  default (D6).
- `image-zip`, `image-zip-slim` unchanged (`python scripts/copy_sidecar.py`,
  `python scripts/image_zip.py`): stdlib shims.
- `test-scripts` comment updated (bench, check_auditable, check_license,
  shims); the CI `check` job step is renamed "scripts/ unit tests (bench,
  check_auditable, check_license, shims)"; the `build` job and `release.yml`
  are not edited (they call the stdlib shims with `python3`, unchanged
  behaviour, verified by `test_shims.py`).

### D5. Session, clients and global options

Root callback of the app (`app.py`):

```
rayito [--profile P] [--region R] [--json] [--verbose] <group> <command> …
```

- `--profile` → `boto3.session.Session(profile_name=P)`; `--region` →
  `region_name=R`; otherwise boto3's chain (`AWS_PROFILE`, `AWS_REGION`,
  config). No region resolved → `rayito: sin región: pasa --region o exporta
  AWS_REGION` and exit 2 before any call.
- `--json`: every command prints exactly one JSON document to stdout
  (`indent=2`, `default=str` for timestamps) and nothing else on stdout;
  progress lines go to stderr. Without it, human tables/lines via
  `_console.py`.
- `--verbose`: `logging.basicConfig(level=INFO)` for the `rayito` loggers
  (the SDK already logs reconnects and probes there). Default `WARNING`.
- `ctx.obj` holds a `Clients` dataclass (`_session.py`): `session`,
  `region`, lazy properties `microvms`, `s3`, `sts`, `iam`, `quotas`
  (`service-quotas`), `logs`, `cloudformation`, all built with the SDK's
  `client_config()` (standard retries ×5, `user_agent_extra=rayito/<ver>`,
  which the CLI extends to `rayito/<ver> cli`), and `control_plane` =
  `shared_control_plane(session, region=)` so `sandbox` commands share the
  SDK's per-operation token buckets. The callback only sets `ctx.obj` when it
  is `None`; tests pass a `Clients` built from stubbed clients through
  `CliRunner.invoke(app, args, obj=clients)`.
- Errors: `botocore.exceptions.NoCredentialsError` / `ProfileNotFound` →
  one Spanish line + exit 2; `ClientError` not handled by a command →
  `AWS error <Code>: <Message>` + exit 1 (the `log` helper of the script,
  moved to `_console.py`, keeps the `errors="replace"` console encoding for
  Windows); `rayito.exceptions.SandboxException` family → its message + exit 1.
- Exit codes: 0 success; 1 operation failed (build not launchable, prune
  left candidates, kill of an unknown id, doctor `FAIL`); 2 usage or
  environment (typer's default for bad arguments, missing region, missing
  extra).

### D6. `rayito image`

| Command | Options | Behaviour (library function) |
|---|---|---|
| `image publish` | `--artifact PATH` (required), `--base-image-version V` (required), `--bucket B` (required unless `RAYITO_BUCKET`), `--image-name`, `--variant full\|slim` (default `full`), `--os-capabilities ALL`, `--build-role-arn` \| `--stack-name` (default `rayito-m0-iam`), `--memory-mib 2048`, `--timeout-seconds 1800`, `--force` | `_publish.publish(clients, PublishSettings)`: exactly today's pipeline (variant marker check before any call, sha256 key `rayito/images/rayd-<12 hex>.zip`, skip upload if present, build role from flag or stack output, `desired_configuration` unchanged including `IMAGE_HOOKS` and no `egressNetworkConnectors`, reuse via `configuration_matches` with numeric `baseImageVersion`, `create` vs `update` by `get-microvm-image`, poll every 10 s until `VersionGate.settled`, `snapshotBuild` summary, `RAYITO_TEMPLATE=<arn>` last line, build-log tail on failure, exit 1). `--json` prints the summary dict only |
| `image list [NAME]` | `--name-filter F` (only without NAME) | without NAME: `list_microvm_images(nameFilter=)` paginated → `name`, `state`, `latestActiveImageVersion`, `latestFailedImageVersion`, `createdAt`; with NAME: `list_microvm_image_versions(imageIdentifier=<arn>)` → `imageVersion`, `state`, `status`, `baseImageVersion`, `codeArtifact.uri` key basename, `createdAt`, newest first. NAME → ARN via `control_plane.resolve_template_arn` (STS only for bare names, as the SDK does) |
| `image prune` | `--image-name` (default `rayito-base`), `--keep 5`, `--dry-run`, `--wait-timeout 600` | `_prune.run(clients, PruneSettings)`: today's plan/keep/delete/backoff logic verbatim (`Pruner` with injectable `sleep`/`clock` kept for tests); table + JSON summary; exit 1 when a candidate survives |
| `image zip IMAGE_DIR DESTINATION` | `--variant full\|slim`, `--sidecar SRC` | with `--sidecar`: `_artifact.copy_tree(SRC, IMAGE_DIR/kernel-sidecar)` (replaces the target wholesale, same exclusions) then `_artifact.write_zip(IMAGE_DIR, DESTINATION, variant)`; prints file count, byte size, variant (and sha256 of the zip, new, so the S3 key can be predicted) |

Settings dataclasses (`PublishSettings`, `PruneSettings`) keep today's field
names; the CLI and the shims build them from the same typer parameters, so
there is one parser. `--bucket` resolution: flag → `RAYITO_BUCKET` env →
usage error `pasa --bucket o exporta RAYITO_BUCKET` (exit 2, before any AWS
call). `DEFAULT_BUCKET` is deleted from the library.

`publish` never prints the artifact bytes or the build role's policy; the
build-log tail on failure prints CloudWatch events as today (build logs are
Dockerfile output, by construction not sandbox content).

### D7. `rayito sandbox`

| Command | Options | Behaviour |
|---|---|---|
| `sandbox list` | `--template T`, `--template-version V`, `--all-states` | `Sandbox.list(template=, template_version=, states=)`; default omits `TERMINATING|TERMINATED` (AWS keeps listing them ~20 min); `--all-states` passes the six `MICROVM_STATES`. Columns `sandbox_id`, `state`, template name (the part after `microvm-image:` of `imageArn`), `template_version`, `started_at` (ISO, UTC), `age` (`1h23m`). No metadata probe (that touches endpoints and is O(n)); `sandbox info` does that |
| `sandbox info ID` | `--no-metadata` | `Sandbox.get_info(ID, read_metadata=not no_metadata)`; prints every `SandboxInfo` field (id, state, endpoint hostname, template ARN and version, `started_at`, `terminated_at`, `state_reason`, `maximum_duration_seconds`, idle policy, execution role, connectors, metadata). Metadata is not secret (`sandbox-metadata` spec); the endpoint is a hostname; no token exists here to print |
| `sandbox kill ID…` / `sandbox kill --all [--template T] [--yes]` | | per id `Sandbox.kill(ID)` through the SDK's 10 TPS bucket; prints `terminated` / `not found`; exit 1 if any id was not found. `--all`: `Sandbox.list(template=)` (non-terminal states), prints the ids, asks `typer.confirm` unless `--yes`, then kills each; exit 0 even if the list was empty. `--all` and explicit ids are mutually exclusive (usage error) |
| `sandbox logs ID` | `--log-group G`, `--limit N` (default 1000 events), `--since ISO\|30m\|2h` | resolves the stream (below), pages `get_log_events(startFromHead=True)` until `nextForwardToken` repeats or `--limit` is reached, prints `<ISO timestamp> <message>` per event (`--json`: list of `{timestamp, message}`). `--since` maps to `startTime` (epoch ms) |

Stream resolution (`_logs.py`): `info = Sandbox.get_info(ID, read_metadata=False)`
gives `template` ARN → name → default group `/rayito/<name>` (the SDK's
`LOG_GROUP_PREFIX`) unless `--log-group`; the candidate stream name is
`f"{info.started_at.astimezone(UTC):%Y/%m/%d}[{info.template_version}]{ID}"`
(the measured format); `describe_log_streams(logGroupName=G,
logStreamNamePrefix=<candidate>)` confirms it. If absent, fallback scan:
`describe_log_streams(orderBy="LastEventTime", descending=True)` paginated
up to 10 pages of 50, keeping every stream whose name ends with `]{ID}`
(covers a stream created on a later UTC day, whether or not AWS rolls
streams daily: Q53 below records what the acceptance observes); events of
every matching stream are printed in stream order. No stream → `sin logs:
el sandbox se lanzó con logging disabled, sin executionRoleArn, o en otro
grupo (--log-group)`, exit 1. `ResourceNotFoundException` on the group →
same message. The command never filters or redacts messages: runtime logs
are what the app wrote to stdout/stderr, and `rayd` already never logs
commands, file contents, PTY bytes, tokens or hook bodies.

### D8. `rayito doctor`

```
rayito doctor [--template T=rayito-base] [--template-version V] [--bucket B] [--launch] [--json]
```

Ten checks, run in order, each producing `CheckResult(name, status,
summary, details: dict)` with `status ∈ {OK, WARN, FAIL, SKIP}`. A check
that raises `ClientError` `AccessDeniedException` is `FAIL` with `details.
action` naming the denied operation, except where the table says `SKIP`
(checks that are diagnostics of other checks, not prerequisites). Any other
exception in a check is `FAIL` with the exception class and message; the
doctor never aborts mid-way. Exit code 1 if any `FAIL`, 0 otherwise.

| # | name | What it does | OK | WARN | FAIL | SKIP |
|---|---|---|---|---|---|---|
| 1 | `credentials` | `sts.get_caller_identity()`; region from the session; principal kind parsed from `Arn` (`user`, `assumed-role`, `root`, `federated-user`, other) | identity resolved and region in `SUPPORTED_REGIONS` | region not in `SUPPORTED_REGIONS` (ten regions of `_limits.py`) | `NoCredentialsError`, expired/invalid token (`ClientError` from STS), no region | — |
| 2 | `managed-images` | `list_managed_microvm_images()` paginated; expects `arn:aws:lambda:<region>:aws:microvm-image:al2023-1`; then `list_managed_microvm_image_versions(imageIdentifier=<that arn>)` → newest `imageVersion` by `createdAt` | `al2023-1` present; details carry every managed ARN and the newest version | the call works but `al2023-1` is absent | `AccessDeniedException` (names `lambda:ListManagedMicrovmImages`), endpoint/region errors (`EndpointConnectionError`, `UnknownServiceError`: botocore too old) | — |
| 3 | `quotas` | `service-quotas.list_service_quotas(ServiceCode="lambda")` paginated, keep `"microvm"` in `QuotaName.lower()`; compare each `QuotaCode` in the defaults table (D10) to its `Value` | every listed quota ≥ its default | any adjustable quota below its default ("cuenta con cuotas reducidas: pide aumento") or a default-table code missing from the response | — | `AccessDeniedException` (`servicequotas:ListServiceQuotas`) |
| 4 | `iam-simulation` | D9 | every action `allowed` | any `implicitDeny` (details list them; summary says the live checks are authoritative) | any `explicitDeny`, or `OrganizationsDecisionDetail.AllowedByOrganizations == false` on any action | caller is `root` (nothing to simulate), `iam:GetRole` or `iam:SimulatePrincipalPolicy` denied, principal kind unsupported |
| 5 | `bucket` | `s3.head_bucket(Bucket=B)` then `s3.get_bucket_location(Bucket=B)`; `LocationConstraint` absent means `us-east-1` | bucket reachable and in the session's region | bucket in another region (`S3_CROSS_REGION_ACCESS_DENIED` at build time, `AWS_API_NOTES.md` §4) | `403`/`404`/`NoSuchBucket` from `head_bucket` | no `--bucket` and no `RAYITO_BUCKET` |
| 6 | `image-gate` | `resolve_template_arn(T)`; `get_microvm_image` → `state`, `latestActiveImageVersion`; version = `--template-version` or `latestActiveImageVersion`; `get_microvm_image_version` → `state`, `status`; `_publish.read_gate` decides `launchable`; `latest_build` adds `snapshotBuild` sizes and `chipsetGeneration` | `VersionGate.launchable` | image launchable but `latestFailedImageVersion` set (a failed build lingers) | `ResourceNotFoundException` ("publica con `rayito image publish`"), image not `CREATED|UPDATED`, version not `SUCCESSFUL`/`ACTIVE` | — |
| 7 | `sandboxes` | `control_plane.list_microvms(image_arn=<T arn>)` (non-terminal states) and, when it differs, without filter; details: counts per state, oldest `RUNNING` `started_at`, ids of the `RUNNING` ones (max 20) | zero `RUNNING` | 1–10 `RUNNING` ("MicroVMs vivos facturando: `rayito sandbox list`, `rayito sandbox kill --all`") | more than 10 `RUNNING` of the template (the e2e pre-flight threshold) | — |
| 8 | `token` | target sandbox = the one `--launch` created, else the newest `RUNNING` of check 7; `control_plane.create_auth_token(id, (PortSpec.single(8080),))` | token minted (details: `sandbox_id`, `port`, `ttl_minutes` 60; **never** the JWE) | — | `AccessDeniedException` (`lambda:CreateMicrovmAuthToken`), `ValidationException` | no target sandbox (`--launch` hint) |
| 9 | `agent` | with `--launch`: `sandbox.get_health()`; otherwise `probe_health` (D12) with the JWE of check 8 on a dedicated channel, timeout 5 s | `agent_ready` (details: `agent_version`, `kernel_ready`, `imds_blocked`, `hook_anomalies`, `resume_generation`, `uptime_ms`) | `agent_ready` but not `kernel_ready` after the probe, or `hook_anomalies > 0`, or `imds_blocked` false on an image named `*-caps` | `Health` fails (`UNAVAILABLE`, `UNAUTHENTICATED`, proxy 403 after remint) | check 8 skipped or failed |
| 10 | `compatibility` | D11 with `__version__`, check 9's `agent_version`, the sandbox's `template_version` (or check 6's version) | row found and both minimums satisfied | agent newer in `MINOR` than the SDK (features the SDK does not know) | agent below the SDK's minimum, or image version below the row's minimum | no `agent_version` (checks 8–9 skipped): the table is still printed with the SDK row marked `no comprobado` |

`--launch`: before check 8, `Sandbox.create(T, template_version=V,
timeout=300, idle=None, logging="disabled", metadata={"rayito": "doctor"},
control_plane=clients.control_plane)`; the sandbox is killed in a `finally`
whatever happens later (also on `KeyboardInterrupt`), and its id is reported
as `launched_sandbox_id`. `create()` already mints the token and waits for
`agent_ready`, so check 8 records `minted by create()`; check 9 uses
`get_health()`. Cost about $0.002 (snapshot read of ~0.9 GB plus a few
seconds of compute). Without `--launch` no MicroVM is created, ever.

Output: human mode prints one line per check `OK    credentials  cuenta
123456789012, us-east-1, assumed-role` (glyph, name, summary; `WARN`/`FAIL`
lines add their `details` indented) and, at the end, the compatibility table
and `rayito doctor: N OK, N WARN, N FAIL, N SKIP`. `--json` prints
`{"rayito": "<sdk version>", "region", "account", "principal_kind",
"checks": [CheckResult…], "compatibility": [rows], "launched_sandbox_id":
str|null, "exit_code": 0|1}`. Timestamps ISO-8601 UTC. The JWE, the sandbox
access token and any `runHookPayload` are never part of any `details`.

Total wall time budget: each AWS call inherits the SDK's `client_config()`
(5 s connect, 60 s read, 5 attempts); the `Health` probe uses 5 s; with
`--launch` the create waits up to the SDK's default `ready_timeout`. No
global timeout flag.

### D9. IAM simulation (check 4)

- `PolicySourceArn`: from `sts.get_caller_identity().Arn`:
  `arn:aws:iam::A:user/…` → as is; `arn:aws:sts::A:assumed-role/NAME/SESSION`
  → `iam.get_role(RoleName=NAME).Role.Arn` (the assumed-role ARN lacks the
  role path, e.g. `/aws-reserved/sso.amazonaws.com/` for SSO roles, and the
  simulator needs the IAM ARN); `arn:aws:iam::A:root` → `SKIP`; anything
  else → `SKIP` with the ARN kind.
- Actions and resources (from `AWS_API_NOTES.md` §10 and the publish
  pipeline), evaluated in one paginated call:

  | Action | `ResourceArns` |
  |---|---|
  | `lambda:RunMicrovm`, `GetMicrovm`, `SuspendMicrovm`, `ResumeMicrovm`, `TerminateMicrovm`, `CreateMicrovmAuthToken`, `GetMicrovmImage`, `UpdateMicrovmImage`, `DeleteMicrovmImageVersion`, `GetMicrovmImageVersion`, `ListMicrovmImageVersions`, `GetMicrovmImageBuild`, `ListMicrovmImageBuilds` | the template's image ARN |
  | `lambda:ListMicrovms`, `ListMicrovmImages`, `ListManagedMicrovmImages`, `ListManagedMicrovmImageVersions`, `CreateMicrovmImage` | `*` |
  | `lambda:PassNetworkConnector` | `arn:aws:lambda:<region>:aws:network-connector:aws-network-connector:*` |
  | `s3:PutObject`, `s3:GetObject` | `arn:aws:s3:::<bucket>/rayito/images/*` (only when a bucket is known) |
  | `s3:ListBucket` | `arn:aws:s3:::<bucket>` (idem) |
  | `logs:DescribeLogStreams`, `logs:GetLogEvents` | `*` |
  | `servicequotas:ListServiceQuotas` | `*` |

  `iam:PassRole` is not simulated: the execution and build roles are not
  known to the doctor (documented in `cli.md`).
- Decision mapping per action: `allowed` → OK; `implicitDeny` → WARN;
  `explicitDeny` → FAIL; `OrganizationsDecisionDetail.AllowedByOrganizations
  == false` → FAIL naming the SCP. The summary always says `simulación
  orientativa: las comprobaciones 2, 5, 6 y 8 son las que cuentan`, because
  the simulator does not see session policies or resource policies the way a
  real call does.

### D10. Service Quotas defaults table (check 3)

`_checks.QUOTA_DEFAULTS: dict[str, tuple[str, float]]` = `QuotaCode` →
(`short name`, default) with the codes of the Context section: memory
`L-CD1C0CC4` 400 (the doctor uses 1024 when the region is one of
`us-east-1`, `us-east-2`, `us-west-2`, `ap-northeast-1`), RunMicrovm rate
`L-535CA9B6` 5 and burst `L-91B95582` 5, ResumeMicrovm `L-118C44B3`/`L-25EEC0A4`
5, SuspendMicrovm `L-90045317`/`L-139F9A48` 2, TerminateMicrovm
`L-74787B8A`/`L-2CCA0501` 10, GetMicrovm `L-CE98C9E3`/`L-C9C2110E` 100,
CreateMicrovmAuthToken `L-7712260B`/`L-D65D9F16` 50,
CreateMicrovmShellAuthToken `L-B78D2ECC`/`L-9A5E43A5` 5, concurrent builds
`L-72E0D058` 5 (10 in the four regions above), images `L-942E56BE` 100,
versions per image `L-F8BECE9C` 50. The non-adjustable quotas (duration,
connections, RPS) are listed in `details` but never compared. Values come
from `AWS_API_NOTES.md` §11 and `spike/m0/out/quotas.json`; a unit test
asserts every code in the table appears in that JSON with the same name.

### D11. Compatibility table (`_compat.py`, check 10, docs)

```python
@dataclass(frozen=True)
class CompatibilityRow:
    sdk_series: str          # "0.1"
    min_agent_version: str   # "0.1.0"
    min_image_version: str   # "10.0"  (rayito-base build number)
    note: str                # Spanish, one line

COMPATIBILITY: tuple[CompatibilityRow, ...] = (
    CompatibilityRow("0.1", "0.1.0", "10.0", "M6: imds_blocked, hook_anomalies y metadata exigen la imagen del tag rayd-v0.1.0 (16.0+)"),
)
```

`assess(sdk_version, agent_version, image_version) -> Assessment` with
`status` (`OK|WARN|FAIL`) and `reason`: the row is the one whose
`sdk_series` equals the SDK's `MAJOR.MINOR`; versions are parsed as
`MAJOR.MINOR.PATCH` integers (pre-release suffix ignored, `image_version`
`N.0` as a `Decimal` like the publish reuse check); `agent < min_agent` →
FAIL; agent `MAJOR.MINOR` > SDK → WARN (report §1 lockstep rule); image
below `min_image_version` → FAIL. The table is rendered by `doctor` and
documented in `docs/site/docs/limits.md` under a new `## Compatibilidad
SDK ↔ rayd ↔ imagen` section; `tests/unit/cli/test_compat.py` parses that
markdown table (skipped when the file is absent, like the `limits.json`
drift test) and asserts its rows equal `COMPATIBILITY`, so the docs and the
code cannot drift. Future rows are appended by the release that changes a
minimum (`docs/RELEASING.md` gets one line saying so).

### D12. `Health` probe refactor in the SDK (internal)

`sandbox_sync/main.py::probe_metadata` and
`sandbox_async/main.py::probe_metadata_async` are split, keeping their
signatures and behaviour, into `probe_health` / `probe_health_async`
(`RUNNING` guard, JWE for `DEFAULT_PORT`, dedicated channel closed on exit,
one `Health` with the 403 remint retry, returns the raw
`health_pb2.HealthResponse` or `None` when the state is not `RUNNING`;
raises the same `SandboxException` on RPC failure) plus the existing
`metadata_from_health` projection. `probe_metadata*` become three-line
wrappers. No public name changes; `test_metadata_sync.py`/`_async.py` keep
passing. `_checks.py` calls `probe_health` and `health_from_proto` for check
9 when there is no `Sandbox` object. Sync and async keep parity.

### D13. Output, encoding and secrets

- Human output via `_console.py`: `echo(line)` writes with
  `errors="replace"` for the console encoding (moved from the script's `log`,
  the reason being Windows cp1252 consoles and UTF-8 build logs);
  `table(columns, rows)` renders a `rich` table (auto width, no colour when
  stdout is not a TTY: `rich` honours `NO_COLOR` and TTY detection);
  statuses use fixed 4-char labels `OK`, `WARN`, `FAIL`, `SKIP`, never
  emoji.
- `--json` documents are built from dataclasses via `dataclasses.asdict`
  and `json.dumps(default=str)`; a unit test round-trips every command's
  JSON through `json.loads`.
- Secrets: the CLI has no code path that prints a JWE, an access token, a
  `runHookPayload`, an `envs` map or a file body. `tests/unit/cli/
  test_secrets.py` runs `doctor --json`, `sandbox info`, `image publish`
  (stubbed) with a canary JWE value in the stubbed `create_microvm_auth_token`
  response and asserts the canary never appears in stdout or stderr. `logs`
  prints CloudWatch events as-is (D7).

### D14. Tests

- Unit (`clients/python/tests/unit/cli/`, `pytest`, `mypy` strict, `ruff`):
  `conftest.py` builds a `Clients` from `boto3` clients created with dummy
  credentials and wrapped in `Stubber` (one per service, activated per
  test), plus a `FakeControlPlane` for `sandbox` commands (the SDK's tests
  already define one under `tests/unit`; reused, not copied). Files:
  `test_app.py` (entry point, missing-typer message, global options,
  `--json` exclusivity, exit codes), `test_artifact.py` (migrated 5 tests +
  stdlib-only `ast` guard + `--sidecar`), `test_publish.py` (migrated 8 tests
  against `_publish` + `Stubber` runs of the reuse path and of a failed
  build printing the log tail + `--bucket` resolution), `test_prune.py`
  (migrated 7 tests), `test_image_list.py`, `test_sandbox.py` (list/info/kill/
  `--all` confirm), `test_logs.py` (exact stream, fallback scan, no stream,
  `--since`), `test_doctor.py` (every check in every status with `Stubber`
  responses; `--launch` with a fake `Sandbox` factory injected through
  `Clients`), `test_compat.py` (assess cases + docs drift), `test_secrets.py`.
- `scripts/tests/test_shims.py` (D3).
- e2e `clients/python/tests/e2e/test_m7_cli.py` (marker `e2e`, the M1
  guardrails, `CliRunner` in-process so it reuses the session's credentials,
  `RAYITO_TEMPLATE`, optional `RAYITO_BUCKET`, `RAYITO_EXECUTION_ROLE_ARN`):
  1. `rayito --json image list` contains the template image with a
     `latestActiveImageVersion`; `rayito --json image list <name>` lists that
     version as `SUCCESSFUL`/`ACTIVE`.
  2. On the `sandbox` fixture: `sandbox list --json` contains its id with
     state `RUNNING`; `sandbox info <id> --json` returns `state RUNNING`,
     the fixture's template ARN and its metadata; `sandbox logs <id>` runs
     only with `RAYITO_EXECUTION_ROLE_ARN` (otherwise the test asserts exit 1
     and the "sin logs" message); when it runs it polls up to 60 s until at
     least one event exists (runtime logs carry `rayd`'s own stdout, e.g.
     its boot lines, never sandboxed process output) and asserts the
     resolved stream name matches the D7 format (Q53).
  3. `rayito --json doctor --template <T> --bucket <B> --launch` → exit 0,
     ten checks, none `FAIL`, check 9 `agent_version` non-empty, check 10
     `OK`, `launched_sandbox_id` set and `Sandbox.get_info(id).state` in
     `TERMINATING|TERMINATED` within 30 s. Then `doctor` without `--launch`
     while the `sandbox` fixture is `RUNNING` → checks 8–10 `OK` against
     it (no MicroVM created: `list` count unchanged).
  4. `rayito image publish --artifact image/rayito-image.zip
     --base-image-version 1 --bucket <B>` on the zip that produced the
     current ACTIVE version → output contains `already built from this
     artifact and config; reusing`, no `update-microvm-image` call (the
     version count before and after is equal), exit 0. Skipped when
     `image/rayito-image.zip` is absent or `RAYITO_BUCKET` unset.
  5. `sandbox kill <fixture id>` → `terminated`; the fixture's teardown
     `terminate` then returns idempotently.
- Existing suites unchanged: `tests/unit` (SDK), `scripts/tests` (bench,
  checks), TypeScript, Rust.

### D15. `SPEC.md` §4 clarification (constitution)

`SPEC.md` §4 lists "Templates declarativos con CLI propia (`rayito template
build`)" as a non-goal. This change does not build templates from a
configuration file: `rayito image publish` is the existing
`scripts/publish_image.py` Dockerfile flow with a stable name, and `doctor`,
`sandbox` and `image list|prune|zip` are operations, not templates. The
bullet gains one sentence: "M7 (`m7-cli`) entrega una CLI **operativa**
(`rayito image|sandbox|doctor`) sobre el flujo Dockerfile existente; los
templates declarativos (`rayito.toml`) siguen fuera." `ARCHITECTURE.md`
needs no ADR: no architectural boundary moves (the control plane stays a
library in the client; the CLI is a consumer of it), and its four script
references become `rayito image …` with the script name in parentheses.

### D16. Docs

- `docs/site/docs/cli.md` (Spanish), nav entry "CLI" after "Quickstart":
  install (`uv pip install "rayito[cli]"` / `pip install "rayito[cli]"`,
  `uv run --project clients/python rayito …` from a checkout), global
  options and credentials, `image` (publish flow with the three-state gate
  and the reuse rule, `list`, `prune` with `--dry-run` first, `zip`),
  `sandbox` (list/info/kill/logs; when logs exist), `doctor` (the ten checks
  table with statuses and what to do for each `FAIL`/`WARN`, `--launch` and
  its cost, `--json` schema, exit codes), "Equivalencias con `make`" (the
  four shims), and "Lo que la CLI no hace" (no templates declarativos, no
  `--follow`, no `exec`).
- `limits.md`: the compatibility section (D11). `quickstart.md`
  "Credenciales" gains `rayito doctor` as the first thing to run;
  "La imagen" mentions `rayito image publish`. `README.md` and
  `clients/python/README.md`: one paragraph "CLI" with three commands.
  `clients/python/CHANGELOG.md` `[Unreleased]` → `### Added` (CLI, extra,
  entry point, doctor) and `### Changed` (scripts are shims; `--bucket`
  required). `MILESTONES.md` row 6 marked in progress / accepted with the
  evidence. `AWS_API_NOTES.md` §16 gains Q53 (below) filled by the
  acceptance.

### D17. Gates

Rust unchanged (no source touched; run anyway). Python client:
`cd clients/python && uv lock --check && uv run pytest tests/unit && uv run
ruff check . && uv run ruff format --check . && uv run mypy src tests`;
scripts: `cd clients/python && uv run pytest ../../scripts/tests -p
no:cacheprovider` and `uvx ruff check scripts` from the root; wheel:
`uv build && python ../../scripts/check_wheel.py dist/*.whl` (the wheel now
contains `rayito/cli/__init__.py` and the `METADATA` has `Provides-Extra:
cli` and `Requires-Dist: typer>=0.15,<1; extra == "cli"`; `check_wheel.py`
gains both assertions); docs `mkdocs build --strict`; TypeScript unchanged
(run); `python scripts/check_license.py`, `gen_limits.py --check` clean;
`openspec validate m7-cli --strict --no-interactive`. Acceptance: the e2e of
D14 green on the account, zero live MicroVMs afterwards.

## Risks / Trade-offs

- **By-path loader in the two stdlib shims** is unusual. Mitigated by the
  `ast` stdlib-only guard, the `-I` subprocess tests, and the comment in each
  shim explaining that the release job must not install the SDK to zip a
  directory. The alternative (adding `uv` to the Rust jobs and `uv run
  --project` everywhere) would touch the SHA-pinned workflows accepted by
  `m7-supply-chain` and grow the release job's trusted base for no benefit.
- **`--bucket` is now required** for `publish` (no maintainer-account
  default in library code). `make image-publish` keeps working through
  `BUCKET`; the docs say so. Anyone scripting `python scripts/publish_image.py`
  by hand must add `--bucket`: acceptable for an operator tool that is
  about to go public.
- **`doctor` IAM simulation is advisory.** SSO/assumed-role principals need
  `iam:GetRole` and `iam:SimulatePrincipalPolicy`; without them the check is
  `SKIP`, never a false `FAIL`; the live checks decide.
- **`--launch` costs money** and creates a MicroVM: opt-in only, 300 s cap,
  killed in `finally`, id printed. Without it the doctor is read-only.
- **CloudWatch stream naming** is measured for one case; the fallback scan
  bounds the cost (≤ 10 `describe_log_streams` pages) and Q53 records
  whether streams roll per day.
- **`typer` version drift**: pinned `<1`; the CLI uses only stable typer
  features (`Typer`, `Option`, `Argument`, `Context`, `Exit`, `confirm`,
  `testing.CliRunner`).
- The migrated tests move directories; `git`-less box means the move is a
  copy+delete; the task list makes the old files' deletion explicit so no
  duplicate test module names remain (`scripts/tests` and `tests/unit/cli`
  are separate pytest roots but `-p no:cacheprovider` runs both in one
  session in `make test-python`, and duplicate basenames without
  `__init__.py` would collide: `tests/unit/cli/` has an `__init__.py`).

### D18. Coordination with the sibling M7 changes (written in parallel)

- `m7-poly-kernels` modifies the `image-variant` requirements (`image_zip.py
  --variant poly`, `publish_image.py --variant poly`, `image-zip-poly` /
  `image-publish-poly` targets). Those requirements keep naming the scripts
  and stay true through the shims (same argv). Rule for whichever lands
  second: the `poly` marker, the third default image name and the
  two-marker refusal live in `rayito.cli._artifact` / `_publish` (one source
  of truth); if `m7-poly-kernels` is implemented first on the scripts, task
  2.1/2.3 carries its code over in the move and its tests migrate with the
  others; if `m7-cli` lands first, `m7-poly-kernels` implements in the
  library modules and the shims need no change. The `Makefile` `PY`
  variable (D4) applies to `image-publish-poly` too.
- `m7-suspended-pool` also modifies the `python-release` requirement "Docs
  site skeleton builds strictly" (adds `pool.md` after `cost.md`). Both
  deltas state a relative order rather than a closed page list where this
  change can; at archive time the second archive must re-read the archived
  text of the first and keep both pages (`cli.md` after `quickstart.md`,
  `pool.md` after `cost.md`), which task 8.5 checks before running
  `openspec archive`.
- `m7-mcp-server` and `m7-s3-persistence` touch neither the scripts nor the
  docs nav requirement; `rayito doctor` needs no change for them.

## Migration Plan

1. Land the package, the shims and the Makefile change together (a shim
   without its library module breaks `make image-publish`).
2. Run the unit gates, then the e2e (D14) on the account; fill Q53 and the
   task notes; update `MILESTONES.md`.
3. Archive with `openspec archive m7-cli --yes` after the e2e is green.
   Rollback = restore the four scripts from the archived change's context
   (they are the pre-change files) and drop the `cli` package; no AWS state
   is created by this change except the doctor's transient MicroVM.

## Facts to measure in acceptance (no decision depends on them)

- **Q53 (`AWS_API_NOTES.md` §16, filled by the acceptance):** does a MicroVM
  that lives across a UTC midnight write to one CloudWatch stream
  (`<start day>[v]<id>`) or to one per day, and does the stream exist before
  the first runtime line? The fallback scan of D7 handles both; the answer
  decides whether the exact-name lookup alone suffices later.
- Whether `iam:SimulatePrincipalPolicy` returns `OrganizationsDecisionDetail`
  for the maintainer's SSO role (the SCP denying `s3:CreateBucket` is a
  good probe: the acceptance adds `s3:CreateBucket` on the bucket ARN as a
  temporary extra action, expects `AllowedByOrganizations == false`, and
  records the result in the task notes; the action is not part of the
  shipped table).
