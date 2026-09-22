## Why

M1–M5 are accepted against real AWS and the Python SDK (`rayito` 0.0.5)
already offers the whole E2B-shaped surface (`Sandbox`, `commands`, `files`,
`pty`, `run_code`, `pause`/`resume`, `get_host`, `get_metrics`). Three things
still keep it from being a drop-in for E2B users and from being released:

1. There is no `rayito.e2b` shim. `MILESTONES.md` M6 and `SPEC.md` §5 promise
   "same surface, `unimplemented` where AWS has no primitive" (the Dormice
   pattern); today an E2B cookbook snippet fails on the import line, and where
   names do exist the kwargs differ (`api_key`, `allow_internet_access`,
   `PtySize(rows, cols)`, `get_metrics()` returning a list).
2. `metadata=` — E2B's per-sandbox labels used by every multi-tenant
   cookbook (`Sandbox.list(query=SandboxQuery(metadata=...))`) — is an
   explicit `SPEC.md` §4 non-goal until M6 because MicroVMs cannot be tagged
   and `list-microvms` carries no user data (`AWS_API_NOTES.md` §1, §2). M6 is
   where it is decided with data.
3. The package has never been released: version 0.0.5, no CHANGELOG, a README
   that stops at M5, no release workflow, no docs site, and `mypy` runs only
   over `src` (34 errors in `tests` that nobody sees).

## What Changes

Track C of M6 ("Endurecimiento", `MILESTONES.md`), decided in full by
`design.md`:

- **Per-sandbox metadata, honestly**: `Sandbox.create(metadata=...)` travels
  in the only per-VM channel, `runHookPayload` (`"metadata": {...}`, additive
  to payload `v: 1`, inside the measured 4096-character budget together with
  `envs`), `rayd` keeps it in the session and echoes it in
  `HealthService.Health` (one additive proto field,
  `HealthResponse.metadata`, field 11; 9 and 10 are taken by `m6-hardening`). The SDK exposes it as
  `sbx.metadata`, `SandboxHealth.metadata`, `SandboxInfo.metadata`
  (`None` = not read from the agent) and `Sandbox.list(metadata=...)`,
  which filters **client-side** by probing `Health` on every `RUNNING`
  candidate — documented O(n), never wakes a suspended sandbox, refuses
  `states` other than `RUNNING`. Metadata is immutable for the life of the
  sandbox and is not secret (it travels like `envs`; `SECURITY.md` T4/T9).
- **`rayito.e2b` shim**: `from rayito.e2b import Sandbox, AsyncSandbox`
  replaces `from e2b_code_interpreter import ...` / `from e2b import ...`.
  E2B names re-exported (`Execution`, `Result`, `Logs`, `OutputMessage`,
  `ExecutionError`, `Context`, `CommandResult`, `CommandHandle`,
  `CommandExitException`, `ProcessInfo`, `SandboxException`,
  `TimeoutException`, `NotFoundException`, `AuthenticationException`,
  `InvalidArgumentException`, `RateLimitException`,
  `NotEnoughSpaceException`, `TemplateException`, `FilesystemEvent`,
  `FilesystemEventType`, `EntryInfo`, `WriteInfo`, `WriteEntry`, `FileType`,
  `WatchHandle`, `PtySize`, `SandboxInfo`, `ListedSandbox`, `SandboxState`,
  `SandboxQuery`, `SandboxMetrics`, `SandboxPaginator`,
  `AsyncSandboxPaginator`, `UnimplementedError`, `RayitoCompatWarning`,
  charts). E2B
  constructor/`create()` kwargs mapped: `template`, `timeout` (E2B default
  300 s → sandbox life, no idle policy), `metadata`, `envs`,
  `allow_internet_access` → `egress`, `secure`, `request_timeout`,
  `sandbox_id` (connect); `api_key`, `domain`, `debug`, `proxy` ignored with
  a `RayitoCompatWarning`. Everything AWS cannot do raises
  `rayito.e2b.UnimplementedError(NotImplementedError)` with the reason:
  `set_timeout` (ADR-007), `upload_url`/`download_url`, `get_metrics(start,
  end)` ranges, `list(next_token=...)`, `run_code(language != "python")`,
  `create_code_context(language != "python")`, `beta_create(auto_pause=...)`,
  templates/`fork`/snapshots, the class variant of `get_metrics`. Compat
  tests run E2B cookbook-style snippets against the in-process fake `rayd`;
  the real-AWS e2e runs the same snippets through the shim.
- **Release readiness**: version `0.1.0`, `CHANGELOG.md`, a README with one
  example per surface (commands, files, `run_code`, PTY, pause/resume,
  `get_host`, metadata, the E2B shim), PyPI classifiers and URLs,
  `uv build` + `twine check` + a wheel-contents check, a GitHub Actions
  `release.yml` with PyPI Trusted Publishing (tag `python-v*`; the workflow
  is written and dry-run only — nothing is published in this change and the
  PyPI publisher registration stays a manual step), and a `mkdocs-material`
  site skeleton under `docs/site` (quickstart, concepts, API reference via
  `mkdocstrings`, security, limits, E2B compatibility, cost model with the
  measured numbers) built with `--strict` in CI.
- **Gates**: the 34 pre-existing `mypy` errors under `tests` are fixed
  without weakening `strict` (one narrowly scoped override for subclassing
  the untyped generated servicers); `uv run mypy src tests` and
  `uv run ruff format --check .` join `make lint` and `ci.yml`.

Full acceptance criteria: `design.md` "Acceptance test list".

## Capabilities

### New Capabilities
- `sandbox-metadata`: the `metadata` key of the run payload, its echo in
  `HealthService.Health`, and the SDK surface (`create(metadata=)`,
  `sbx.metadata`, `SandboxHealth.metadata`, `SandboxInfo.metadata`,
  `Sandbox.list(metadata=)` with its O(n) probe rules), sync and async.
- `e2b-compat`: the `rayito.e2b` module — re-exported names, kwarg mapping,
  ignored-kwarg warnings, `UnimplementedError` for every E2B feature without
  an AWS primitive, the E2B-shaped `SandboxInfo`/`SandboxMetrics`/
  `PtySize`/`SandboxPaginator`, and the cookbook compat tests.
- `python-release`: packaging metadata and version, CHANGELOG, README,
  wheel build and checks, the release workflow with Trusted Publishing, the
  docs site skeleton, and the `mypy`-over-tests gate in lint and CI.

### Modified Capabilities
- None. `suspend-resume` "Health exposes the resume state" is untouched:
  the metadata echo is an additive field specified under
  `sandbox-metadata`.

## Impact

- `proto/rayito/v1/health.proto`: `HealthResponse.metadata`
  (`map<string, string>`, field 11); `clients/python/src/rayito/v1/health_pb2*`
  regenerated (`python scripts/gen_python.py`); Rust regenerates at
  `cargo build`; `buf breaking` reports only the additive field.
- `crates/rayd-core`: `run_payload.rs` (`RunPayload.metadata`), `session.rs`
  (`metadata()`), `health.rs` (`HealthSnapshot.metadata`); `crates/rayd`:
  `grpc/health.rs` (`to_response` copies the map), `hooks` logging counts
  keys only. No new port, no adapter, no behaviour change elsewhere.
- `clients/python/src/rayito`: `_payload.py` (`metadata` in the payload,
  budget error naming both `envs` and `metadata`), `_models.py`
  (`SandboxHealth.metadata`, `SandboxInfo.metadata`,
  `SandboxListItem.metadata`), `_sandbox_base.py` (`validated_metadata`,
  `metadata_matches`, `list_states_for_metadata`, `health_from_proto`),
  `sandbox_sync/main.py` and `sandbox_async/main.py` (`create(metadata=)`,
  `metadata` property, `get_info` variants, `list(metadata=)` with the
  per-VM probe), `__init__.py`; new package `rayito/e2b/` (`__init__.py`,
  `_compat.py`, `_models.py`, `_sync.py`, `_async.py`, `exceptions.py`);
  `_version.py` 0.1.0.
- `clients/python/tests/unit`: `FakeRayd.metadata`, new
  `test_e2b_compat_base.py`, `test_e2b_compat_sync.py`,
  `test_e2b_compat_async.py`, `test_metadata_sync.py`,
  `test_metadata_async.py`, `test_packaging.py`; the 16 files with `mypy`
  errors fixed; `tests/e2e/test_m6_e2b_compat.py`.
- `clients/python/pyproject.toml` (version, classifiers, urls, keywords,
  `docs` dependency group, `mypy` override for the fakes),
  `clients/python/CHANGELOG.md`, `clients/python/README.md`,
  `scripts/check_wheel.py`, `.github/workflows/release.yml`,
  `.github/workflows/ci.yml` (mypy over tests, `ruff format --check`, docs
  build), `Makefile` (`lint`, `docs`), `docs/site/mkdocs.yml`,
  `docs/site/docs/*.md`.
- Docs: `ARCHITECTURE.md` (payload shape with `metadata`, SDK layout with
  `rayito/e2b`, Health row), `SPEC.md` §3/§4 (metadata moves from non-goal
  to shipped-with-limits; the E2B shim), `SECURITY.md` (T4/T9 rows mention
  metadata; the shim's default connectors), `AWS_API_NOTES.md` §16 new
  measurement Q42 (egress without a connector) and the `list(metadata=)`
  probe timings, `MILESTONES.md` M6 Track C state, `README.md` (root).
- Governed by `AWS_API_NOTES.md` facts: `runHookPayload` ≤ 4096 chars
  (Q11), it is the only per-VM channel (§2), MicroVMs cannot be tagged (§1)
  and `list-microvms` items carry only `microvmId`, `state`, `imageArn`,
  `imageVersion`, `startedAt` (§1, `docs/aws-api/model_summary.md`), no
  `UpdateMicrovm` (ADR-007), `CreateMicrovmAuthToken` 5 TPS (§11), a unary
  RPC is traffic for the idle policy (Q15), `Health` is the only tokenless
  RPC (ADR-004). This change adds **no** AWS parameter: `allow_internet_access`
  maps to the existing `egressNetworkConnectors` (`INTERNET_EGRESS` or
  none), and whether a MicroVM without an egress connector still reaches the
  internet is measured, not assumed (Q42).
