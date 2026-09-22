## 0. [pre-flight] Facts

- [x] 0.1 Confirm the four scripts, their callers and tests are as listed in `design.md` "Context": `grep -rn "image_zip\|publish_image\|image_prune\|copy_sidecar" Makefile .github/workflows scripts docs README.md ARCHITECTURE.md clients/python` (excluding `openspec/`, `target/`, `.venv`); if any new caller appeared, add it to D3/D4 before continuing
- [x] 0.2 `cd clients/python && uv run --with typer python -c "import typer, importlib.metadata as m; print(m.version('typer'))"` resolves a `0.x` ≥ 0.15 with `py.typed`; record the version in the "Notes" section
- [x] 0.3 `openspec validate m7-cli --strict --no-interactive` passes before touching code

## 1. [packaging] Extra, entry point, package skeleton (design D1, D2)

- [x] 1.1 `clients/python/pyproject.toml`: `[project.optional-dependencies] cli = ["typer>=0.15,<1"]`, `[project.scripts] rayito = "rayito.cli.__main__:main"`, the same `typer` pin appended to `[dependency-groups] dev`; `uv lock`; `uv lock --check` clean; `uv sync` installs `typer`, `rich`, `click`, `shellingham`
- [x] 1.2 `tests/unit/test_packaging.py`: `test_cli_extra_and_dev_group_pin_typer_identically` (extra equals `["typer>=0.15,<1"]`, the string is in `dev`, `project.scripts.rayito == "rayito.cli.__main__:main"`)
- [x] 1.3 `src/rayito/cli/__init__.py` (module docstring only, no imports) and `src/rayito/cli/__main__.py` with `main() -> int`: guarded `from rayito.cli.app import app`; on `ModuleNotFoundError` whose `name` is `typer` (or a `typer` submodule) print `rayito: la CLI necesita el extra: uv pip install "rayito[cli]" (o pip install "rayito[cli]")` to stderr and return 2; else return the app's exit code; `if __name__ == "__main__": sys.exit(main())`
- [x] 1.4 `tests/unit/cli/__init__.py`, `tests/unit/cli/conftest.py`: fixtures `stubbed_clients` (boto3 clients for `lambda-microvms`, `s3`, `sts`, `iam`, `service-quotas`, `logs`, `cloudformation` created with dummy credentials and `region_name="us-east-1"`, each wrapped in a `Stubber` the test activates) and `clients` (a `rayito.cli._session.Clients` built from them plus the SDK's `FakeControlPlane` from `tests/unit`); `runner` = `typer.testing.CliRunner()` (click ≥ 8.2 always separates `result.stdout` and `result.stderr`; `mix_stderr` no longer exists)
- [x] 1.5 `tests/unit/cli/test_app.py::test_entry_point_without_typer` (monkeypatch `builtins.__import__` to raise `ModuleNotFoundError(name="typer")` for `typer`, call `main()`, assert 2 and the message) and `test_library_modules_import_without_typer` (block `typer` in `sys.modules` via a `None` entry, import the eight library modules)
- [x] 1.6 `scripts/check_wheel.py`: assert the wheel lists `rayito/cli/__init__.py` and `METADATA` has `Provides-Extra: cli` and `Requires-Dist: typer>=0.15,<1; extra == "cli"`; `cd clients/python && uv build && python ../../scripts/check_wheel.py dist/*.whl` → `OK`

## 2. [library] Move the scripts' logic into rayito.cli (design D3, D6)

- [x] 2.1 `src/rayito/cli/_artifact.py` (stdlib only): move `EXCLUDED_*`, `VARIANTS`, `WARMUP_MARKER_ENTRY`, `SLIM_MARKER_CONTENT`, `FIXED_DATE_TIME`, `is_excluded`, `image_files`, `normalized`, `write_zip`, `marker_variant` from `image_zip.py` and `copy_tree` from `copy_sidecar.py`; add `zip_main(argv) -> int` and `copy_main(argv) -> int` (today's `argparse` surfaces and messages) and `artifact_sha256(path) -> str`; docstrings carried over; no inline comments in bodies
- [x] 2.2 `tests/unit/cli/test_artifact.py`: the five tests of `scripts/tests/test_image_variant.py` that concern the zip (full has no marker + exclusions, slim adds only the marker, `zip_main --variant`) rewritten against `_artifact`, plus `test_copy_tree_applies_the_same_exclusions`, `test_zip_is_byte_identical_on_rerun`, and `test_artifact_module_is_stdlib_only` (`ast.parse` the file; every `Import`/`ImportFrom` top-level name in `sys.stdlib_module_names`, no relative import)
- [x] 2.3 `src/rayito/cli/_publish.py`: move everything from `publish_image.py` except `parse_args`/`main`; `Settings` renamed `PublishSettings` (same fields; `bucket: str` required, `DEFAULT_BUCKET` deleted; `DEFAULT_STACK_NAME`, `DEFAULT_MEMORY_MIB`, `IMAGE_HOOKS`, `S3_KEY_PREFIX`, `LOG_GROUP_PREFIX`, `POLL_INTERVAL_SECONDS`, `DEFAULT_BUILD_TIMEOUT_SECONDS` kept); the `Aws` class replaced by the `Clients` protocol of `_session.py` (attributes `region`, `account_id`, `microvms`, `s3`, `cloudformation`, `logs`); `log` moved to `_console.echo`; `publish(clients, settings) -> int` and `publish_summary(...) -> dict` so `--json` can print the dict; `read_gate`, `latest_build`, `VersionGate`, `configuration_matches`, `base_image_version_matches`, `desired_configuration`, `print_recent_logs` kept as public module functions (the doctor reuses the first two)
- [x] 2.4 `tests/unit/cli/test_publish.py`: the eight tests of `scripts/tests/test_publish_image.py` rewritten against `_publish` (`FakeAws` → a minimal `Clients` stand-in), plus Stubber tests `test_reuse_makes_no_build_call` (list versions with the matching `codeArtifact.uri` and `baseImageVersion` `1.0` → no `update_microvm_image`, output has `reusing`, exit 0), `test_failed_build_prints_state_reason_and_log_tail` (`update` accepted, gate `FAILED`/`INACTIVE`, `describe_log_streams` + `get_log_events` stubbed, exit 1), `test_upload_skipped_when_key_exists` (`head_object` 200 → no `put_object`), `test_variant_mismatch_stops_before_any_call`
- [x] 2.5 `src/rayito/cli/_prune.py`: move everything from `image_prune.py` except `parse_args`/`main`; `Settings` renamed `PruneSettings`; `Aws` replaced by `Clients`; `run(clients, settings, *, sleep=time.sleep) -> int` and `prune_summary(...)`; `Pruner` keeps injectable `sleep`/`clock`
- [x] 2.6 `tests/unit/cli/test_prune.py`: the seven tests of `scripts/tests/test_image_prune.py` rewritten against `_prune` (same Stubber fixtures and assertions: keep set, in-flight, dry run makes no mutating call, conflict → wait → retry with `attempts: 2`, refused ACTIVE deactivated first, summary shape and exit 1, defaults and bounds)
- [x] 2.7 Delete `scripts/tests/test_image_variant.py`, `test_publish_image.py`, `test_image_prune.py` (their cases now live under `tests/unit/cli/`); `cd clients/python && uv run pytest ../../scripts/tests -p no:cacheprovider` still green with `test_bench_*`, `test_check_*`

## 3. [session, console, compat, probe] Shared pieces (design D5, D11, D12, D13)

- [x] 3.1 `src/rayito/cli/_session.py`: `@dataclass class Clients` with `session: boto3.session.Session`, `region: str`, `user_agent_suffix: str = "cli"`, lazy cached properties `microvms`, `s3`, `sts`, `iam`, `quotas` (`service-quotas`), `logs`, `cloudformation` (each `session.client(name, region_name=region, config=client_config() with user_agent_extra "rayito/<ver> cli")`), `account_id` (from `sts.get_caller_identity()`, cached), `control_plane` (`shared_control_plane(session, region=region)`), and `sandbox_factory: Callable[..., Sandbox]` defaulting to `Sandbox.create` (the doctor's `--launch` seam); `resolve_session(profile, region) -> Clients` raising `UsageError` subclasses for `ProfileNotFound`, `NoCredentialsError` and no region (`sin región: pasa --region o exporta AWS_REGION`)
- [x] 3.2 `src/rayito/cli/_console.py`: `echo(message, *, err=False)` with the `errors="replace"` encoding of the old `log`; `emit_json(document)` (`json.dumps(indent=2, default=str)`); `table(columns, rows)` rendering a `rich.table.Table` through a `rich.console.Console` (imported inside the function); `status_label(status)` returning the fixed 4-char labels `OK`, `WARN`, `FAIL`, `SKIP`; `age(started_at, now)` → `1h23m`
- [x] 3.3 `src/rayito/cli/_compat.py`: `CompatibilityRow`, `COMPATIBILITY` with the single `("0.1", "0.1.0", "10.0", "M6: imds_blocked, hook_anomalies y metadata exigen la imagen del tag rayd-v0.1.0 (16.0+)")` row, `parse_semver(text) -> tuple[int, int, int]` (pre-release suffix ignored, `ValueError` otherwise), `Assessment(status, reason, row)` and `assess(sdk_version, agent_version, image_version)` per D11; `tests/unit/cli/test_compat.py` with the four assessments of the spec scenario, a malformed version case, and `test_docs_table_matches_code` parsing `docs/site/docs/limits.md` (skip if absent)
- [x] 3.4 Refactor `sandbox_sync/main.py`: `probe_health(control_plane, info, transport, request_timeout) -> health_pb2.HealthResponse | None` (the current body of `probe_metadata` up to and including the `Health` call and channel close), `probe_metadata` = `probe_health` → `None`/`metadata_from_health`; mirror in `sandbox_async/main.py` as `probe_health_async`; `tests/unit/test_metadata_sync.py` and `test_metadata_async.py` unchanged and green; `mypy` clean
- [x] 3.5 `src/rayito/cli/_logs.py`: `default_log_group(template_arn) -> str` (`/rayito/<name>`), `expected_stream_name(info) -> str` (`f"{started_at UTC:%Y/%m/%d}[{template_version}]{sandbox_id}"`), `find_streams(logs, group, info) -> list[str]` (exact prefix lookup, then the bounded ordered scan for names ending in `]<id>`, ≤ 10 pages of 50), `iter_events(logs, group, stream, *, start_time_ms, limit)` paging `get_log_events(startFromHead=True)` until the forward token repeats; `parse_since(text, now)` for ISO-8601 and `30m`/`2h`/`1d`
- [x] 3.6 `tests/unit/cli/test_logs.py`: exact stream found; fallback scan finds `2026/09/16[1.0]microvm-x` after the exact lookup returns nothing; scan stops after 10 pages; group `ResourceNotFoundException` → `LogsNotFound`; `parse_since` cases; `iter_events` stops on a repeated token and at `limit`

## 4. [commands] typer application (design D5–D7)

- [x] 4.1 `src/rayito/cli/app.py`: `app = typer.Typer(no_args_is_help=True, help="Rayito: sandboxes sobre AWS Lambda MicroVMs")`; root callback with `--profile`, `--region`, `--json`, `--verbose` storing `Clients` on `ctx.obj` only when `ctx.obj is None` and the flags on `ctx.meta`; `add_typer` for `image`, `sandbox`; `doctor` command registered; `run_shim(argv) -> int` (invokes `app` with `standalone_mode=False`, maps `click.exceptions.UsageError` to exit 2 with the usage message on stderr, `typer.Exit`/`SystemExit` codes propagated; on `ModuleNotFoundError` for `typer` prints the `uv run --project clients/python …` hint and returns 2); unhandled `ClientError` → `AWS error <Code>: <Message>`, exit 1; `SandboxException` → message, exit 1
- [x] 4.2 `src/rayito/cli/image.py`: `publish` (options of D6, `--bucket` resolved flag → `RAYITO_BUCKET` → `typer.BadParameter` naming both), `list` (`[NAME]`, `--name-filter`), `prune` (`--image-name`, `--keep`, `--dry-run`, `--wait-timeout`), `zip` (`IMAGE_DIR`, `DESTINATION`, `--variant`, `--sidecar`); each builds the settings dataclass and calls the library; `--json` prints the summary / list only
- [x] 4.3 `src/rayito/cli/sandbox.py`: `list` (`--template`, `--template-version`, `--all-states`), `info` (`ID`, `--no-metadata`), `kill` (`ID…` or `--all --template --yes`, mutually exclusive, `typer.confirm` without `--yes`, exit 1 on any `not found`), `logs` (`ID`, `--log-group`, `--limit`, `--since`); all through `clients.control_plane` and the `Sandbox` classmethods with `control_plane=` injected
- [x] 4.4 Shims: rewrite `scripts/publish_image.py` and `scripts/image_prune.py` as `run_shim` delegates (docstring: what they are, the `uv run --project clients/python` invocation, the `make` target); rewrite `scripts/image_zip.py` and `scripts/copy_sidecar.py` with the by-path loader of `_artifact.py` (docstring explains the bare-`python3` requirement of the CI `build` and `release.yml` jobs), `image_zip.py` re-exporting `marker_variant`, `VARIANTS`, `write_zip`, `is_excluded`; `uvx ruff check scripts` clean
- [x] 4.5 `scripts/tests/test_shims.py`: subprocess runs with `[sys.executable, "-I", …]` of `copy_sidecar.py` and `image_zip.py --variant slim` on a `tmp_path` image tree (Dockerfile, `requirements.txt`, a `tests/` dir, a `.pyc`, a `uv.lock` to be excluded), asserting exit 0, the marker entry, the exclusions, and `marker_variant` through the re-export; `publish_image.py` and `image_prune.py` with no arguments via `sys.executable` (client env) asserting exit 2 and the usage text naming `--artifact` / `--image-name`
- [x] 4.6 `Makefile`: `PY := uv run --project $(PYTHON_CLIENT)`, `BUCKET ?= <the maintainer's artifact bucket>` (comment: maintainer's bucket, SCP denies CreateBucket, adopters override), `BOTO3_SPEC` removed, `image-publish`/`-slim`/`-caps` and `image-prune` rewritten per D4 with `--bucket $(BUCKET)`, `test-scripts` comment updated; `.github/workflows/ci.yml` line "scripts/ unit tests (…)" renamed to "scripts/ unit tests (bench, check_auditable, check_license, shims)"; `build` job and `release.yml` untouched (diff shows no change there)
- [x] 4.7 `tests/unit/cli/test_image_list.py` (images table and JSON; versions of one image newest first with the six keys; `--name-filter` forwarded as `nameFilter`), `test_sandbox.py` (list hides terminal states / `--all-states`; info prints metadata and `--no-metadata` skips the probe; kill per id with `not found` → exit 1; `--all` requires confirmation and `--yes` skips it; `--all` with ids is a usage error), `test_app.py` additions (`--json` single document for every command via `json.loads`; `sin región` exit 2; `AWS error` mapping exit 1; `run_shim` exit codes)

## 5. [doctor] Checks and command (design D8–D10)

- [x] 5.1 `src/rayito/cli/_checks.py`: `CheckStatus` (`Literal["OK", "WARN", "FAIL", "SKIP"]`), `CheckResult` dataclass, `QUOTA_DEFAULTS` (D10, with the large-region overrides for memory and builds), `SIMULATED_ACTIONS` (D9 table as `(action, resource_kind)` pairs), `principal_kind(arn)`, `policy_source_arn(clients, arn)` (user as is; assumed-role → `iam.get_role(RoleName=)`; root/other → `None`), and one function per check: `check_credentials`, `check_managed_images`, `check_quotas`, `check_iam_simulation`, `check_bucket`, `check_image_gate`, `check_sandboxes`, `check_token`, `check_agent`, `check_compatibility`, each `(clients, DoctorContext) -> CheckResult`, wrapped by `run_check` that turns exceptions into `FAIL` (or `SKIP` where D8 says so) and never re-raises; `DoctorContext` carries `template_arn`, `template_version`, `bucket`, `launched: Sandbox | None`, `target_sandbox: SandboxInfo | None`, `minted_token: str | None` (used only for the `Health` probe, never printed), `health: SandboxHealth | None`
- [x] 5.2 `src/rayito/cli/doctor.py`: the command (`--template`, `--template-version`, `--bucket`, `--launch`), the `--launch` `try/finally` around checks 8–10 using `clients.sandbox_factory(...)` with `timeout=300, idle=None, logging="disabled", metadata={"rayito": "doctor"}` and `kill()` in `finally`, human rendering (one line per check, details indented for WARN/FAIL, compatibility table, totals line), the `--json` document of D8, exit code 1 iff any `FAIL`
- [x] 5.3 `tests/unit/cli/test_doctor.py`: one test per check per status using Stubber responses (credentials OK/WARN-region/FAIL-no-creds; managed-images OK/WARN-missing-al2023/FAIL-denied; quotas OK/WARN-below-default/SKIP-denied with the `spike/m0/out/quotas.json` fixture and `test_quota_defaults_codes_exist_in_measured_json`; iam OK/WARN-implicit/FAIL-explicit/FAIL-organizations/SKIP-root/SKIP-get-role-denied, `test_assumed_role_uses_get_role_arn_with_path`; bucket OK/WARN-other-region/FAIL-403/SKIP-none; image-gate OK/WARN-failed-version-lingers/FAIL-not-found/FAIL-inactive; sandboxes OK/WARN/FAIL-above-10; token OK/SKIP/FAIL-denied; agent OK/WARN-kernel-not-ready/WARN-hook-anomalies/FAIL-rpc; compatibility OK/WARN/FAIL/SKIP), the spec scenarios "fresh account without an image", "reduced quotas and an implicit deny are warnings", "launch is killed whatever happens" (fake sandbox whose `get_health` raises → `kill` called, `FAIL`, exit 1), "doctor uses the probe" (fake control plane + fake `HealthService` via the SDK test fixtures, exactly one `create_auth_token` for port 8080), and `test_exception_in_one_check_does_not_stop_the_rest`
- [x] 5.4 `tests/unit/cli/test_secrets.py`: canary JWE `CANARY-JWE` in the stubbed `create_microvm_auth_token`, canary `runHookPayload` in `get_microvm`, run `doctor` (human and `--json`), `sandbox info`, `sandbox list`, `image publish` (reuse path): assert the canaries appear in neither stdout nor stderr

## 6. [docs] Site, README, changelog, constitution (design D15, D16)

- [x] 6.1 `docs/site/docs/cli.md` with the sections of D16 (install; global options and credentials; `image` with the publish pipeline, the reuse rule and the three-state gate, `list`, `prune` with `--dry-run` first, `zip`; `sandbox` list/info/kill/logs and when logs exist; `doctor` with the ten-check table, statuses, what to do on FAIL/WARN, `--launch` and its ≈ $0.002, the `--json` shape, exit codes; "Equivalencias con `make`" for the four shims; "Lo que la CLI no hace"); `mkdocs.yml` nav entry `- CLI: cli.md` after Quickstart
- [x] 6.2 `docs/site/docs/limits.md`: `## Compatibilidad SDK ↔ rayd ↔ imagen` with the table (`SDK`, `rayd mínimo`, `rayito-base mínima`, `Nota`) equal to `COMPATIBILITY` and the lockstep sentence from the report §1; `test_compat.py::test_docs_table_matches_code` green
- [x] 6.3 `docs/site/docs/quickstart.md` ("Credenciales": run `rayito doctor` first; "La imagen": `rayito image publish …` next to `make image-publish`), `README.md` and `clients/python/README.md` ("CLI" paragraph: install the extra, `rayito doctor`, `rayito sandbox list`, `rayito image publish`), `docs/RELEASING.md` (one line: a release that raises a minimum appends a `COMPATIBILITY` row and the `limits.md` row together)
- [x] 6.4 `clients/python/CHANGELOG.md` `[Unreleased]`: `### Added` (CLI `rayito` as extra `rayito[cli]`, `image publish|list|prune|zip`, `sandbox list|kill|info|logs`, `doctor`) and `### Changed` (`scripts/publish_image.py`, `image_prune.py`, `image_zip.py`, `copy_sidecar.py` are shims over `rayito.cli`; `--bucket`/`RAYITO_BUCKET` required for publish; `Makefile` `BUCKET`)
- [x] 6.5 `SPEC.md` §4: append the D15 sentence to the "Templates declarativos" bullet; `ARCHITECTURE.md`: the four script mentions become `rayito image publish|prune|zip` with the script in parentheses; `AWS_API_NOTES.md` §16: add row Q53 (CloudWatch stream per day or per VM; stream present before the first runtime line) with the "Medida" column empty
- [x] 6.6 `MILESTONES.md` M7 row 6: state "en curso" with the D17 gate set as evidence (updated at acceptance)
- [x] 6.7 `cd clients/python && uv run --group docs mkdocs build -f ../../docs/site/mkdocs.yml --strict --site-dir ../../docs/site/_build` green; `docs/site/_build/cli/index.html` exists

## 7. [gates] Everything green on the box (design D17)

- [x] 7.1 Python client: `cd clients/python && uv lock --check && uv run pytest tests/unit && uv run ruff check . && uv run ruff format --check . && uv run mypy src tests` (record the unit test count, up from 429 + M6)
- [x] 7.2 Scripts: `cd clients/python && uv run pytest ../../scripts/tests -p no:cacheprovider`; `uvx ruff check scripts` from the root; `python scripts/gen_limits.py --check`; `python scripts/check_license.py`
- [x] 7.3 Wheel: `cd clients/python && uv build && python ../../scripts/check_wheel.py dist/*.whl && uvx twine check dist/*` → `OK`, `PASSED`; paste the `Provides-Extra`/`Requires-Dist` lines in "Notes"
- [x] 7.4 Rust and TypeScript unchanged but run: `cargo fmt --check`, `cargo clippy --workspace --all-targets -- -D warnings`, `cargo test --workspace`, `cargo zigbuild --release --target aarch64-unknown-linux-musl -p rayd`; `cd clients/typescript && pnpm lint && pnpm typecheck && pnpm test && pnpm build`
- [x] 7.5 `make image-zip` equivalent by hand (`cp` the binary, `python scripts/copy_sidecar.py kernel-sidecar image/kernel-sidecar`, `python scripts/image_zip.py image image/rayito-image.zip`) produces a zip whose sha256 equals the one `rayito image zip image image/rayito-image.zip` produces on the same tree
- [x] 7.6 `openspec validate m7-cli --strict --no-interactive` passes with the ticked tasks

## 8. [acceptance] Real AWS (design D14; acceptance agent only)

- [x] 8.1 `clients/python/tests/e2e/test_m7_cli.py` (marker `e2e`, M1 guardrails, `CliRunner` in-process): the five blocks of D14 (`image list` + versions; `sandbox list|info|logs` on the fixture, `logs` only with `RAYITO_EXECUTION_ROLE_ARN`; `doctor --launch` then `doctor` against the fixture; `image publish` reuse path when `image/rayito-image.zip` and `RAYITO_BUCKET` exist; `sandbox kill` of the fixture)
- [x] 8.2 Run: `cd clients/python && RAYITO_E2E=1 RAYITO_TEMPLATE=<arn> RAYITO_BUCKET=<bucket> [RAYITO_EXECUTION_ROLE_ARN=<arn>] uv run pytest tests/e2e/test_m7_cli.py -m e2e -v -s` → green; record wall time, the doctor's ten statuses, `agent_version`, the launched sandbox id and its terminal state, the reuse line of `image publish`, and that `rayito sandbox list` shows zero `RUNNING` afterwards
- [x] 8.3 Then the full suite once: `uv run pytest tests/e2e -m e2e` green (no regression from the `probe_health` refactor: M6 metadata tests included)
- [x] 8.4 Fill Q53 in `AWS_API_NOTES.md` from the `logs` block (stream name observed, whether it existed before the first line, one stream or one per day if a fixture crossed UTC midnight; otherwise "no observado") and the Organizations detail of the IAM simulation (D "Facts to measure") in "Notes"
- [x] 8.5 `MILESTONES.md` row 6 → accepted with the evidence of 8.2–8.4; if `m7-suspended-pool` was archived first, re-read `openspec/specs/python-release/spec.md` and keep `pool.md` in this change's "Docs site skeleton" text before archiving (design D18); `openspec archive m7-cli --yes`

## Notes

Implementer (2026-09-16, box: Windows, `uv` 0.7.21, Python 3.12.10, botocore 1.43.94):

- 0.1: callers of the four scripts as listed in `design.md` "Context" plus the
  `m7-poly-kernels` additions that landed first on the scripts (`--variant
  poly`, `kernels_variant` marker, `requirements-poly.txt`, `image-zip-poly`
  / `image-publish-poly` targets, the CI one-liner asserting the three
  markers): carried over into `rayito.cli._artifact` / `_publish` (D18
  rule), their tests migrated to `tests/unit/cli/test_artifact.py` and
  `test_publish.py`.
- 0.2: `typer` **0.27.2** resolves (`py.typed` present). It vendors click
  (`typer._click`, no `click` dependency any more) and ships `rich` 15.0.0 +
  `shellingham` 1.5.4. `CliRunner.invoke(app, args, obj=...)` and separate
  `result.stdout` / `result.stderr` confirmed. Consequence for D3/4.1:
  `run_shim` does not import click's `UsageError` (private module in 0.27);
  it runs the app in typer's standalone mode and maps `SystemExit` to the
  exit code (usage errors 2 with the usage box on stderr, `typer.Exit(code)`
  propagated), which is version-agnostic across `typer>=0.15,<1`.
- 1.6 / 7.3: `uv_build` renders the extra as `Requires-Dist: typer>=0.15,<1 ;
  extra == 'cli'` (space before `;`, single quotes), the same spelling the
  `mcp` extra uses; `check_wheel.py` asserts that literal form plus
  `Provides-Extra: cli`, `rayito/cli/__init__.py` and the entry point
  `rayito = rayito.cli.__main__:main` in `entry_points.txt`. `uv build` +
  `check_wheel.py` → `OK (92 entradas)`; `twine check` → PASSED ×2.
- 3.1: `resolve_session` honours `AWS_REGION` explicitly (botocore 1.43.94
  only reads `AWS_DEFAULT_REGION`/the profile), so the documented
  `export AWS_REGION=…` works for the CLI. `Clients` also carries a
  `transport` seam (loopback `rayd` in tests). `tests/unit` had no
  `FakeControlPlane`; `tests/unit/cli/conftest.py` defines an in-memory one.
- 3.4: `FakeRayd` in `tests/unit/conftest.py` gained an `agent_version`
  field (default `"test"`, additive) for the "doctor uses the probe" scenario.
- 5.1 (D9): the simulation runs **one `simulate_principal_policy` call per
  resource group** (image ARN, `*`, connector ARN, bucket objects, bucket:
  five calls with a bucket, three without) instead of one call, because the
  simulator evaluates every action against every `ResourceArns` entry and a
  single call would report spurious `implicitDeny` for mismatched pairs.
  Check 8 mints the JWE once and check 9 feeds it to `probe_health` through
  a `PremintedControlPlane` wrapper (the 403 remint still reaches AWS), so
  the two checks make exactly one `create-microvm-auth-token` call.
- 6.5: `Q53` and `Q54` were taken by sibling tracks by the time this landed;
  the CloudWatch stream question is **Q55** in `AWS_API_NOTES.md` §16 (the
  "Medida" column is what the acceptance fills; `_logs.py` names Q55).
- 7.1: `uv lock --check` clean; `pytest tests/unit`: **927 passed, 2 failed,
  1 skipped** in 294 s — the two failures are `tests/unit/test_limits.py`
  (`S_3_BUCKET_NAME_MIN/MAX` drift between `limits.json` and `_limits.py`,
  in-progress work of `m7-s3-persistence`, not this change); the 107 tests of
  `tests/unit/cli/` plus `test_packaging.py` and the metadata suites pass.
  `ruff check .` reports only two `RUF043` in `tests/unit/test_pool_base.py`
  (`m7-suspended-pool`); `ruff format --check` clean; `mypy src tests` clean
  (130 files).
- 7.2: `scripts/tests` → 57 passed (incl. `test_shims.py`, whose isolated runs
  use `python -I -S`); `uvx ruff check scripts` clean; `gen_limits.py
  --check` OK; `check_license.py` OK.
- 7.4: Rust: `cargo fmt --check` fails on `crates/rayd/src/persistence/*`
  and `cargo clippy`/`cargo test`/`cargo zigbuild` fail to compile `rayd`
  (`S3ObjectStore::from_imds` missing) — in-progress `m7-s3-persistence`
  work; nothing in `crates/` was touched by this change. TypeScript: `pnpm
  lint`, `pnpm typecheck`, `pnpm build` green; `pnpm test` 325 passed, 6
  failed (`limits.test.ts` `S_3_BUCKET_NAME_*` drift and
  `transport-errors.test.ts` loopback plaintext: other tracks).
- 7.5: `copy_sidecar.py` + `image_zip.py` by hand and `rayito image zip
  image <dst>` on the same tree: both 33 files, 4 812 857 bytes, sha256
  `1e39221e2ec744f008f5834896c7c167d5fdf68b279a5ae412126a0e387a04fd`
  (byte-identical). The existing `image/rayito-image.zip` (sha256 `45037c…`)
  is the older artifact of the current 17.0 version and was left untouched.
- 6.7: `mkdocs build --strict` green; `docs/site/_build/cli/index.html`
  exists; nav is Inicio, Quickstart, **CLI**, Conceptos, … (the `mcp.md`
  entry of `m7-mcp-server` sits after Compatibilidad con E2B).

Acceptance (2026-09-16, 19:20-19:30 -05:00, test account, us-east-1,
`rayito-base` 17.0, execution role `rayito-m0-execution-us-east-1`,
`RAYITO_BUCKET=<bucket>`):

- 8.2: `uv run pytest tests/e2e/test_m7_cli.py -m e2e -v -s` → **6 passed in
  83.2 s** (wall 85 s). `image list`: 4 images, 3 versions of `rayito-base`,
  active 17.0 (3.8 s). `sandbox list|info|logs` on fixture
  `microvm-<id>`: stream
  `2026/09/17[17.0]microvm-<id>`, 5 events, exact-name hit (Q55).
  `doctor --launch` (16.2 s): credentials OK, managed-images OK (al2023-1,
  newest managed version 1), quotas OK (26 MicroVM quotas, none below
  default), iam-simulation **WARN** (`lambda:PassNetworkConnector`
  `implicitDeny` in the simulator even for `AdministratorAccess`; `run-microvm`
  works), bucket OK, image-gate OK (17.0 launchable), sandboxes WARN (the
  fixture is RUNNING), token OK (`create()`), agent OK (`agent_version`
  **0.1.0**, `kernel_ready=True`, `imds_blocked=False`), compatibility OK
  (SDK 0.1.0 / rayd 0.1.0 / image 17.0); `launched_sandbox_id`
  `microvm-<id>`, `TERMINATING` within the
  30 s window. `doctor` without `--launch` (10.2 s): token/agent/compatibility
  OK against the fixture (`source: create-microvm-auth-token`), no MicroVM
  created. `image publish` reuse (4.1 s): `artifact already in S3, skipping
  upload: …/rayd-45037c481630.zip`, `version 17.0 already built from this
  artifact and config; reusing`, `RAYITO_TEMPLATE=arn:…:microvm-image:rayito-base`,
  version count unchanged (3). `sandbox kill` of fixture
  `microvm-<id>` → `terminated`, `TERMINATING`,
  `rayito sandbox list --template rayito-base` shows zero `RUNNING`.
- Organizations detail (design "Facts to measure"): `s3:CreateBucket` on the
  bucket ARN → `EvalDecision: explicitDeny` **with**
  `OrganizationsDecisionDetail.AllowedByOrganizations: false` for the SSO
  role `AWSReservedSSO_AdministratorAccess_<suffix>` (resolved through
  `iam:GetRole` to the `/aws-reserved/sso.amazonaws.com/` ARN): the SCP is
  visible to the simulator, so the doctor's `FAIL` on an Organizations deny is
  reachable in practice.
- Environment finding fixed during acceptance: the `rayito-m0-iam` stack had
  been updated at 23:18Z with `LogGroupPrefix=C:/Program Files/Git/rayito`
  (MSYS path conversion of `/rayito` in a Git Bash deploy by the
  `m7-s3-persistence` track), which silently removed `logs:*` on
  `/rayito/*` from the execution and build roles: no MicroVM wrote runtime
  logs between 23:18Z and 00:20Z. Restored with a parameter-only
  `update-stack --use-previous-template` (`LogGroupPrefix=/rayito`,
  `UPDATE_COMPLETE`); nothing in `spike/m0/iam.yaml` was edited. Deploy that
  stack with `MSYS_NO_PATHCONV=1` (or from PowerShell).
- Cost of the acceptance: 3 fixtures of ≤ 900 s killed at the end of each
  test, the doctor's 300 s sandbox and two 100 s log probes: ≈ $0.02.
- 8.3: `pytest tests/e2e/test_m1_hello.py … test_m6_hardening.py test_m7_cli.py
  -m e2e` (the pre-existing suite plus this change; `test_m7_mcp.py`,
  `test_m7_pool.py` and `test_m7_poly_kernels.py` belong to sibling tracks
  still in progress) → **17 passed, 2 skipped, 2 failed in 794 s**. The M6
  metadata/hardening tests pass (no regression from `probe_health`).
  `test_m5_pty_suspend_resume.py::test_auto_resume` failed while another
  session was creating VMs on the same image (its sweeper terminates every
  live VM of the template) and **passed when re-run alone**;
  `test_m6_e2b_compat.py::test_e2b_shim_cookbook` fails with `language
  javascript is not installed in this image; use rayito-base-poly`: the
  `m7-poly-kernels` track changed that test/shim to need the `-poly` image.
  Neither is caused by this change.
- 8.5: no sibling M7 change had been archived at archive time, so the
  `python-release` requirement text is this change's delta (relative order,
  `cli.md` after `quickstart.md`); the next archive that touches it
  (`m7-suspended-pool` `pool.md`, `m7-mcp-server` `mcp.md`) must re-read the
  archived text and keep every page (design D18).
